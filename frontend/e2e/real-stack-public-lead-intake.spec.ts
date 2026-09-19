import { execFileSync, execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type APIRequestContext, type APIResponse } from "@playwright/test";
import { backendUrl } from "./support/real-stack-urls";

interface LeadPayload {
  name: string;
  phone: string;
  goal: string;
  preferred_format: string;
  is_child: boolean;
  consent: {
    personal_data: boolean;
    privacy_policy_version: string;
    consent_text_hash: string;
  };
  source: {
    page: string;
    utm_source: string;
    utm_medium: string;
    utm_campaign: string;
    utm_content: string;
    utm_term: string;
  };
  idempotency_key: string;
  hp_field: string;
}

interface PublicLeadIntakeFixture {
  payloads: {
    first: LeadPayload;
    repeat_same_phone: LeadPayload;
    existing_active: LeadPayload;
    existing_lost: LeadPayload;
    invalid_consent: LeadPayload;
  };
  expected: {
    invalid_consent_code: string;
  };
}

interface BackendAssertResponse {
  ok?: boolean;
}

const specDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(specDir, "..", "..");
const PUBLIC_INTAKE_PATH = "/api/public/lead-intakes/";

function requireFixturePath(): string {
  const rawPath = process.env.REAL_STACK_E2E_FIXTURE;
  if (!rawPath) {
    throw new Error("REAL_STACK_E2E_FIXTURE must point to a public lead intake fixture JSON file.");
  }

  const fixturePath = isAbsolute(rawPath) ? rawPath : resolve(repoRoot, rawPath);
  if (!existsSync(fixturePath)) {
    throw new Error("REAL_STACK_E2E_FIXTURE does not point to an existing file.");
  }
  return fixturePath;
}

function readFixture(fixturePath: string): PublicLeadIntakeFixture {
  const data = JSON.parse(readFileSync(fixturePath, "utf8")) as Partial<PublicLeadIntakeFixture>;
  if (
    !data.payloads?.first ||
    !data.payloads.repeat_same_phone ||
    !data.payloads.existing_active ||
    !data.payloads.existing_lost ||
    !data.payloads.invalid_consent
  ) {
    throw new Error("Fixture public lead intake payloads are required.");
  }
  if (!data.expected?.invalid_consent_code) {
    throw new Error("Fixture expected invalid consent code is required.");
  }
  return data as PublicLeadIntakeFixture;
}

async function postLead(
  request: APIRequestContext,
  payload: LeadPayload,
  requestId: string,
  xForwardedFor?: string,
): Promise<APIResponse> {
  return await request.post(backendUrl(PUBLIC_INTAKE_PATH), {
    data: payload,
    headers: {
      "X-Request-ID": requestId,
      ...(xForwardedFor ? { "X-Forwarded-For": xForwardedFor } : {}),
      "User-Agent": "Jaguar public lead intake E2E",
    },
  });
}

async function assertCorsPreflight(request: APIRequestContext): Promise<void> {
  const response = await request.fetch(backendUrl(PUBLIC_INTAKE_PATH), {
    method: "OPTIONS",
    headers: {
      Origin: "https://jaguar-fight-club.ru",
      "Access-Control-Request-Method": "POST",
      "Access-Control-Request-Headers": "content-type,x-request-id",
    },
  });
  expect(response.status()).toBe(200);
  expect(response.headers()["access-control-allow-origin"]).toBe("https://jaguar-fight-club.ru");
  expect(response.headers()["access-control-allow-headers"].toLowerCase()).toContain("x-request-id");
}

async function acceptedId(response: APIResponse): Promise<number> {
  expect(response.status()).toBe(201);
  const body = await response.json();
  expect(body.data.status).toBe("accepted");
  expect(typeof body.data.id).toBe("number");
  return body.data.id;
}

function runBackendAssert(fixturePath: string): void {
  const command = process.env.REAL_STACK_E2E_ASSERT_COMMAND;
  const pythonBin = process.env.PYTHON_BIN ?? ".venv/bin/python";
  const output = command
    ? execSync(command, {
        cwd: repoRoot,
        encoding: "utf8",
        env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath },
        stdio: ["ignore", "pipe", "pipe"],
      })
    : execFileSync(
        pythonBin,
        ["manage.py", "assert_public_lead_intake_e2e", "--fixture", fixturePath],
        {
          cwd: repoRoot,
          encoding: "utf8",
          env: { ...process.env, REAL_STACK_E2E_FIXTURE: fixturePath },
          stdio: ["ignore", "pipe", "pipe"],
        },
      );

  const result = JSON.parse(output) as BackendAssertResponse;
  expect(result.ok).toBe(true);
}

test("real-stack public lead intake accepts, reuses, and rejects safely", async ({ request }) => {
  const fixturePath = requireFixturePath();
  const fixture = readFixture(fixturePath);

  await assertCorsPreflight(request);

  const firstId = await acceptedId(
    await postLead(request, fixture.payloads.first, "public-lead-intake-e2e-first"),
  );
  const idempotentId = await acceptedId(
    await postLead(request, fixture.payloads.first, "public-lead-intake-e2e-idempotent"),
  );
  expect(idempotentId).toBe(firstId);

  const repeatId = await acceptedId(
    await postLead(request, fixture.payloads.repeat_same_phone, "public-lead-intake-e2e-repeat"),
  );
  expect(repeatId).not.toBe(firstId);

  const existingActiveId = await acceptedId(
    await postLead(
      request,
      fixture.payloads.existing_active,
      "public-lead-intake-e2e-existing-active",
    ),
  );
  expect(existingActiveId).not.toBe(firstId);
  expect(existingActiveId).not.toBe(repeatId);

  const existingLostId = await acceptedId(
    await postLead(request, fixture.payloads.existing_lost, "public-lead-intake-e2e-existing-lost"),
  );
  expect(existingLostId).not.toBe(firstId);
  expect(existingLostId).not.toBe(repeatId);
  expect(existingLostId).not.toBe(existingActiveId);

  const invalidConsentResponse = await postLead(
    request,
    fixture.payloads.invalid_consent,
    "public-lead-intake-e2e-invalid-consent",
    "203.0.113.241",
  );
  expect(invalidConsentResponse.status()).toBe(400);
  expect((await invalidConsentResponse.json()).code).toBe(fixture.expected.invalid_consent_code);

  runBackendAssert(fixturePath);
});
