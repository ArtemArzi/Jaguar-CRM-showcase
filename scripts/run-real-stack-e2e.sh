#!/usr/bin/env bash
# Run the production-like browser gate against Django + django-q2 + Vite preview.
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="$PROJECT_DIR/frontend"

BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8011}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-4174}"
# A real run derives this endpoint from the validated isolated database URL.
# Keep these empty until that boundary is accepted so caller-provided PGHOST
# cannot make readiness probe a different database than Django migrates.
POSTGRES_HOST=""
POSTGRES_PORT=""
REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
REDIS_PORT="${REDIS_PORT:-6379}"
PYTHON_BIN="${PYTHON_BIN:-}"
FIXTURE_COMMAND="${REAL_STACK_E2E_FIXTURE_COMMAND:-prepare_real_stack_e2e}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-90}"
PLAYWRIGHT_CONFIG_SOURCE="${REAL_STACK_E2E_PLAYWRIGHT_CONFIG:-$FRONTEND_DIR/playwright.real-stack.config.ts}"

PLAYWRIGHT_ARGS=()
DRY_RUN=false

usage() {
  cat <<'USAGE'
Usage: bash scripts/run-real-stack-e2e.sh [--dry-run] [playwright args...]

Runs a non-destructive real-stack E2E gate:
  1. Check PostgreSQL and Redis availability.
  2. Run Django migrations.
  3. Prepare a temporary fixture through manage.py prepare_real_stack_e2e.
  4. Start Django, django-q2 qcluster, and Vite preview.
  5. Run Playwright with REAL_STACK_E2E_FIXTURE and PLAYWRIGHT_BASE_URL.

Configurable environment:
  BACKEND_HOST              default 127.0.0.1
  BACKEND_PORT              default 8011
  FRONTEND_HOST             default 127.0.0.1
  FRONTEND_PORT             default 4174
  PostgreSQL readiness host/port are derived from REAL_STACK_E2E_DATABASE_URL
  REDIS_HOST                default 127.0.0.1
  REDIS_PORT                default 6379
  PYTHON_BIN                default .venv/bin/python if present, else python3
  REAL_STACK_E2E_LOG_DIR    default mktemp under /tmp
  REAL_STACK_E2E_FIXTURE_COMMAND default prepare_real_stack_e2e
  REAL_STACK_E2E_PLAYWRIGHT_CONFIG default frontend/playwright.real-stack.config.ts
  REAL_STACK_E2E_TEST_MATCH default real-stack-kiosk.spec.ts in the Playwright config
  REAL_STACK_E2E_DATABASE_URL  required for a non-dry run; isolated local PostgreSQL database only
  PLAYWRIGHT_BASE_URL       default http://FRONTEND_HOST:FRONTEND_PORT

The fixture value passed to Playwright is a file path, not fixture contents.
Fixture JSON is kept outside the log directory and removed during cleanup.
Logs are written under REAL_STACK_E2E_LOG_DIR or a temporary /tmp directory.
--dry-run validates tooling and the fixture command without touching ports,
services, migrations, frontend build, or Playwright.
USAGE
}

while (($#)); do
  case "$1" in
    --help|-h)
      usage
      exit 0
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --)
      shift
      while (($#)); do
        PLAYWRIGHT_ARGS+=("$1")
        shift
      done
      ;;
    *)
      PLAYWRIGHT_ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
  elif [[ -x "$PROJECT_DIR/.venv/bin/python3" ]]; then
    PYTHON_BIN="$PROJECT_DIR/.venv/bin/python3"
  else
    PYTHON_BIN="python3"
  fi
fi

if [[ -n "${REAL_STACK_E2E_LOG_DIR:-}" ]]; then
  LOG_DIR="$REAL_STACK_E2E_LOG_DIR"
  mkdir -p "$LOG_DIR"
else
  LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/jaguar-real-stack-e2e.XXXXXX")"
fi

BACKEND_URL="http://${BACKEND_HOST}:${BACKEND_PORT}"
FRONTEND_URL="http://${FRONTEND_HOST}:${FRONTEND_PORT}"
PLAYWRIGHT_BASE_URL="${PLAYWRIGHT_BASE_URL:-$FRONTEND_URL}"
FIXTURE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/jaguar-real-stack-e2e-fixture.XXXXXX")"
chmod 700 "$FIXTURE_DIR"
FIXTURE_FILE="$FIXTURE_DIR/fixture.json"
PLAYWRIGHT_CONFIG="$LOG_DIR/playwright.real-stack.config.cjs"
PIDS=()

log() {
  printf '[real-stack-e2e] %s\n' "$*"
}

fail() {
  log "ERROR: $*"
  log "Logs: $LOG_DIR"
  exit 1
}

validate_isolated_database_url() {
  local validation_output
  if ! validation_output="$("$PYTHON_BIN" - "${REAL_STACK_E2E_DATABASE_URL:-}" "${DATABASE_URL:-}" 2>&1 <<'PY'
from __future__ import annotations

import re
import sys
from urllib.parse import parse_qs, unquote, urlsplit


def reject(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def canonical_host(host: str) -> str:
    normalized = host.lower()
    if normalized in {"", "127.0.0.1", "localhost", "::1"}:
        return "local"
    return normalized


def parse_database_url(raw: str, *, label: str, isolated: bool):
    try:
        parsed = urlsplit(raw)
        parsed_port = parsed.port
    except ValueError:
        reject(f"{label} must be a valid PostgreSQL URL.")
    if parsed.scheme not in {"postgres", "postgresql"}:
        reject(f"{label} must be a valid PostgreSQL URL.")
    if isolated and not parsed.hostname:
        reject("REAL_STACK_E2E_DATABASE_URL must use an explicit local PostgreSQL host.")
    if isolated and parsed.query:
        reject(
            "REAL_STACK_E2E_DATABASE_URL must not contain query parameters that can "
            "override the validated database target."
        )

    options = parse_qs(parsed.query, keep_blank_values=True)
    database_name = options.get("dbname", [unquote(parsed.path).lstrip("/")])[-1]
    host = parsed.hostname or options.get("host", [""])[-1]
    raw_port = str(parsed_port) if parsed_port is not None else options.get("port", ["5432"])[-1]
    try:
        port = int(raw_port)
    except ValueError:
        reject(f"{label} must use a valid PostgreSQL port.")
    if not (1 <= port <= 65535):
        reject(f"{label} must use a valid PostgreSQL port.")
    return parsed, host, port, database_name


raw_url, ordinary_url = sys.argv[1:]
if not raw_url:
    reject("REAL_STACK_E2E_DATABASE_URL is required for a real run so migrations and fixtures cannot touch the ordinary development database.")

parsed, host, port, database_name = parse_database_url(
    raw_url,
    label="REAL_STACK_E2E_DATABASE_URL",
    isolated=True,
)
if canonical_host(host) != "local":
    reject("REAL_STACK_E2E_DATABASE_URL must use a local PostgreSQL host.")
if not database_name or "/" in database_name or not re.search(r"(?:^|[_-])(test|e2e)(?:[_-]|$)", database_name, re.IGNORECASE):
    reject("REAL_STACK_E2E_DATABASE_URL database name must identify an isolated test or e2e database.")

if ordinary_url:
    _ordinary, ordinary_host, ordinary_port, ordinary_database_name = parse_database_url(
        ordinary_url,
        label="DATABASE_URL",
        isolated=False,
    )
    identity = (
        canonical_host(host),
        port,
        database_name,
    )
    ordinary_identity = (
        canonical_host(ordinary_host),
        ordinary_port,
        ordinary_database_name,
    )
    if identity == ordinary_identity:
        reject("REAL_STACK_E2E_DATABASE_URL must differ from the ordinary DATABASE_URL.")

# This is intentionally only an endpoint, never the full URL or credentials.
print(f"{host}\t{port}")
PY
)"; then
    fail "$validation_output"
  fi
  IFS=$'\t' read -r POSTGRES_HOST POSTGRES_PORT <<<"$validation_output"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

ensure_jwt_private_key() {
  if [[ -n "${JWT_PRIVATE_KEY:-}" ]]; then
    return
  fi
  if [[ -f "$PROJECT_DIR/jwt-key.pem" ]]; then
    return
  fi

  local generated_key
  generated_key="$("$PYTHON_BIN" - <<'PY'
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pem = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)
print(pem.decode("ascii"), end="")
PY
)" || fail "Could not generate ephemeral JWT private key for real-stack run."

  if [[ -z "$generated_key" ]]; then
    fail "Could not generate ephemeral JWT private key for real-stack run."
  fi

  export JWT_PRIVATE_KEY="$generated_key"
  log "Generated ephemeral JWT private key for this real-stack run."
}

probe_port() {
  local port="$2"
  local port_hex
  port_hex="$(printf '%04X' "$port")"

  local checked_proc=false
  local table
  for table in /proc/net/tcp /proc/net/tcp6; do
    if [[ -r "$table" ]]; then
      checked_proc=true
      if awk -v port="$port_hex" '
        NR > 1 {
          split($2, address, ":")
          if (toupper(address[2]) == port && $4 == "0A") {
            found = 1
          }
        }
        END { exit found ? 0 : 1 }
      ' "$table"; then
        return 0
      fi
    fi
  done
  if [[ "$checked_proc" == "true" ]]; then
    return 1
  fi

  if command -v ss >/dev/null 2>&1; then
    local sockets
    if sockets="$(ss -H -tln 2>/dev/null)"; then
      awk -v port=":$port" '$4 ~ port "$" { found = 1 } END { exit found ? 0 : 1 }' <<<"$sockets"
      return $?
    fi
  fi

  "$PYTHON_BIN" - "$1" "$2" <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
try:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        sys.exit(0 if sock.connect_ex((host, port)) == 0 else 1)
except PermissionError:
    sys.exit(2)
PY
}

ensure_port_free() {
  local name="$1"
  local host="$2"
  local port="$3"
  local status=0

  probe_port "$host" "$port" || status=$?
  if ((status == 0)); then
    fail "$name port ${host}:${port} is already in use; set ${name^^}_PORT/${name^^}_HOST to override."
  fi
  if ((status != 1)); then
    fail "Cannot inspect ${name} port ${host}:${port}; check local socket permissions or run outside the sandbox."
  fi
}

wait_for_port() {
  local name="$1"
  local host="$2"
  local port="$3"
  local pid="$4"
  local deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))

  until probe_port "$host" "$port"; do
    local probe_status=$?
    if ((probe_status != 1)); then
      fail "Cannot inspect $name port ${host}:${port}; check local socket permissions or run outside the sandbox."
    fi
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      fail "$name exited before port ${host}:${port} became available"
    fi
    if ((SECONDS >= deadline)); then
      fail "$name did not become ready on ${host}:${port} within ${HEALTH_TIMEOUT_SECONDS}s"
    fi
    sleep 1
  done
}

wait_for_port_release() {
  local name="$1"
  local host="$2"
  local port="$3"
  local deadline=$((SECONDS + 30))

  while probe_port "$host" "$port"; do
    if ((SECONDS >= deadline)); then
      log "WARNING: $name port ${host}:${port} did not become free within 30s"
      return
    fi
    sleep 1
  done
}

start_background() {
  local name="$1"
  local logfile="$2"
  shift 2
  setsid "$@" >"$logfile" 2>&1 &
  local pid=$!
  PIDS+=("$pid")
  log "$name started with PID $pid; log: $logfile"
}

cleanup() {
  local status=$?
  if [[ -e "${FIXTURE_FILE:-}" ]]; then
    rm -f "$FIXTURE_FILE" >/dev/null 2>&1 || true
  fi
  if [[ -d "${FIXTURE_DIR:-}" ]]; then
    rmdir "$FIXTURE_DIR" >/dev/null 2>&1 || true
  fi
  if ((${#PIDS[@]})); then
    log "Stopping own background jobs..."
    for pid in "${PIDS[@]}"; do
      if kill -0 "$pid" >/dev/null 2>&1; then
        kill -- "-$pid" >/dev/null 2>&1 || kill "$pid" >/dev/null 2>&1 || true
      fi
    done
    for pid in "${PIDS[@]}"; do
      wait "$pid" >/dev/null 2>&1 || true
    done
    wait_for_port_release "Django" "$BACKEND_HOST" "$BACKEND_PORT"
    wait_for_port_release "Vite preview" "$FRONTEND_HOST" "$FRONTEND_PORT"
  fi
  if ((status == 0)); then
    log "Done. Logs: $LOG_DIR"
  else
    log "Failed. Logs: $LOG_DIR"
  fi
}
trap cleanup EXIT

cd "$PROJECT_DIR"

log "Logs: $LOG_DIR"
log "Backend: $BACKEND_URL"
log "Frontend: $FRONTEND_URL"

require_command "$PYTHON_BIN"
require_command pg_isready
require_command redis-cli
require_command npm
require_command setsid

if [[ "$DRY_RUN" != "true" ]]; then
  validate_isolated_database_url
  export DATABASE_URL="$REAL_STACK_E2E_DATABASE_URL"
  # The production defaults are fail-closed. Disposable real-stack fixtures
  # always force the local mock provider and keep Tochka reconciliation off.
  # Only payment-focused packs explicitly opt into mock financial writes and
  # unsigned mock callbacks through the pack wrapper.
  export DEBUG=true
  export PAYMENT_PROVIDER=mock
  export TOCHKA_PAYMENT_RECONCILIATION_ENABLED=false
  export ONLINE_PAYMENT_ORDER_CREATION_ENABLED=true
  export MOCK_PAYMENT_WEBHOOKS_ENABLED="${MOCK_PAYMENT_WEBHOOKS_ENABLED:-false}"
  export MOCK_PAYMENT_ORDER_CREATION_ENABLED="${MOCK_PAYMENT_ORDER_CREATION_ENABLED:-false}"
  export MOCK_PAYMENT_BASE_URL="$BACKEND_URL"
  export JAGUAR_PAYMENT_RETURN_ORIGIN="$FRONTEND_URL"
fi

log "Checking fixture management command..."
"$PYTHON_BIN" manage.py help "$FIXTURE_COMMAND" >"$LOG_DIR/fixture-help.log" 2>&1 \
  || fail "Missing management command: manage.py ${FIXTURE_COMMAND}."

if [[ "$DRY_RUN" == "true" ]]; then
  log "Dry run passed. No port checks, service checks, migrations, fixture creation, servers, build, or Playwright run executed."
  exit 0
fi

ensure_jwt_private_key

ensure_port_free "backend" "$BACKEND_HOST" "$BACKEND_PORT"
ensure_port_free "frontend" "$FRONTEND_HOST" "$FRONTEND_PORT"

log "Checking PostgreSQL..."
pg_isready -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" >"$LOG_DIR/postgres.log" 2>&1 \
  || fail "PostgreSQL is not available at ${POSTGRES_HOST}:${POSTGRES_PORT}."

log "Checking Redis..."
redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" ping >"$LOG_DIR/redis.log" 2>&1 \
  || fail "Redis is not available at ${REDIS_HOST}:${REDIS_PORT}."

log "Running migrations..."
"$PYTHON_BIN" manage.py migrate --noinput >"$LOG_DIR/migrate.log" 2>&1

log "Preparing real-stack fixture..."
"$PYTHON_BIN" manage.py "$FIXTURE_COMMAND" --output "$FIXTURE_FILE" >"$LOG_DIR/fixture.stdout.log" 2>"$LOG_DIR/fixture.stderr.log"
if [[ ! -s "$FIXTURE_FILE" ]]; then
  fail "Fixture command did not create a non-empty fixture file at $FIXTURE_FILE."
fi

log "Ensuring isolated-database rollout state..."
"$PYTHON_BIN" manage.py ensure_real_stack_rollout_state_e2e --fixture "$FIXTURE_FILE" --all-clubs \
  >"$LOG_DIR/fixture-rollout-state.log" 2>&1

fixture_landing_default_club_id="$("$PYTHON_BIN" - "$FIXTURE_FILE" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        value = json.load(fh).get("landing_default_club_id", "")
except Exception:
    value = ""

if isinstance(value, int) and value > 0:
    print(value)
PY
)"
if [[ -n "$fixture_landing_default_club_id" && -z "${LANDING_DEFAULT_CLUB_ID:-}" ]]; then
  export LANDING_DEFAULT_CLUB_ID="$fixture_landing_default_club_id"
  log "Configured landing default club from fixture."
fi

log "Starting Django..."
start_background "Django" "$LOG_DIR/django.log" "$PYTHON_BIN" manage.py runserver "${BACKEND_HOST}:${BACKEND_PORT}"
DJANGO_PID="${PIDS[-1]}"
wait_for_port "Django" "$BACKEND_HOST" "$BACKEND_PORT" "$DJANGO_PID"

log "Starting django-q2..."
start_background "django-q2" "$LOG_DIR/qcluster.log" "$PYTHON_BIN" manage.py qcluster

log "Building frontend..."
VITE_API_URL="$BACKEND_URL" npm --prefix "$FRONTEND_DIR" run build >"$LOG_DIR/frontend-build.log" 2>&1

log "Starting Vite preview..."
start_background "Vite preview" "$LOG_DIR/vite-preview.log" \
  env VITE_API_URL="$BACKEND_URL" npm --prefix "$FRONTEND_DIR" run preview -- --host "$FRONTEND_HOST" --port "$FRONTEND_PORT"
VITE_PID="${PIDS[-1]}"
wait_for_port "Vite preview" "$FRONTEND_HOST" "$FRONTEND_PORT" "$VITE_PID"

if [[ -f "$PLAYWRIGHT_CONFIG_SOURCE" ]]; then
  PLAYWRIGHT_CONFIG="$PLAYWRIGHT_CONFIG_SOURCE"
  log "Using Playwright config: $PLAYWRIGHT_CONFIG"
else
  log "No source real-stack Playwright config found; generating temporary config."
  cat >"$PLAYWRIGHT_CONFIG" <<EOF
const { defineConfig, devices } = require("$FRONTEND_DIR/node_modules/@playwright/test");

module.exports = defineConfig({
  testDir: "$FRONTEND_DIR/e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? "$FRONTEND_URL",
    trace: "off",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
EOF
fi

log "Running Playwright real-stack gate..."
(
  cd "$FRONTEND_DIR"
  REAL_STACK_E2E_FIXTURE="$FIXTURE_FILE" \
    REAL_STACK_E2E_TEST_MATCH="${REAL_STACK_E2E_TEST_MATCH:-}" \
    REAL_STACK_E2E_BACKEND_URL="$BACKEND_URL" \
    PLAYWRIGHT_BASE_URL="$PLAYWRIGHT_BASE_URL" \
    VITE_API_URL="$BACKEND_URL" \
    PYTHON_BIN="$PYTHON_BIN" \
    npx playwright test --config "$PLAYWRIGHT_CONFIG" "${PLAYWRIGHT_ARGS[@]}"
) >"$LOG_DIR/playwright.log" 2>&1

log "Playwright passed."
