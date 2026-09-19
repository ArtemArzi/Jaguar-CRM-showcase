from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from ninja.testing import TestClient

from apps.attendance.models import Checkin
from apps.billing.models import Debt, Payment
from apps.clubs.models import Club, ClubMembership
from apps.documents.models import StudentDocument
from apps.students.models import Student
from config.api import api


class Command(BaseCommand):
    help = "Assert cross-tenant API/UI oracle probes from tenant negative matrix fixture."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_tenant_negative_matrix_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll before failing.",
        )

    def handle(self, *args, **options):
        fixture = self._load_fixture(Path(options["fixture"]).expanduser())
        timeout_seconds = max(float(options["timeout_seconds"]), 0)
        deadline = time.monotonic() + timeout_seconds

        while True:
            try:
                evidence = self._collect_evidence(fixture)
            except CommandError as exc:
                if time.monotonic() >= deadline:
                    raise CommandError(f"tenant negative matrix E2E assertion failed: {exc}") from exc
                time.sleep(0.5)
                continue

            self.stdout.write(json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str))
            return

    def _load_fixture(self, path: Path) -> dict:
        if not path.exists():
            raise CommandError(f"fixture file not found: {path}")
        try:
            fixture = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"fixture file is not valid JSON: {path}") from exc

        required = {"fixture_id", "club_a", "club_b"}
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        client = TestClient(api)
        club_a = Club.objects.get(id=fixture["club_a"]["club_id"])
        owner_auth = self._auth_params(
            user_id=fixture["club_a"]["owner"]["user_id"],
            club=club_a,
        )
        parent_auth = self._auth_params(
            user_id=fixture["club_a"]["parent"]["user_id"],
            club=club_a,
            role=ClubMembership.Role.PARENT,
        )
        club_a_kiosk_auth = {"headers": {"X-Kiosk-Token": fixture["club_a"]["kiosk_token"]}}
        club_b = fixture["club_b"]
        markers = club_b["markers"]

        with patch("apps.common.auth.TenantJWTAuth.__call__", side_effect=self._mock_auth):
            api_evidence = {
                "owner_list_students": self._list_probe(
                    client.get("/students/", **owner_auth),
                    marker=markers["student_name"],
                ),
                "owner_get_foreign_student": self._status_probe(
                    client.get(f"/students/{club_b['child_id']}/", **owner_auth)
                ),
                "owner_get_foreign_schedule": self._status_probe(
                    client.get(f"/schedules/{club_b['schedule_id']}/", **owner_auth)
                ),
                "owner_batch_foreign_student": self._status_probe(
                    client.post(
                        "/checkins/batch/",
                        json={
                            "schedule_id": fixture["club_a"]["schedule_id"],
                            "date": fixture["club_a"]["checkin_date"],
                            "present_student_ids": [club_b["child_id"]],
                            "training_type_id": fixture["club_a"]["training_type_id"],
                        },
                        **owner_auth,
                    )
                ),
                "owner_get_foreign_payment": self._status_probe(
                    client.get(f"/billing/payments/{club_b['payment_id']}/", **owner_auth)
                ),
                "owner_get_foreign_debtors": self._list_probe(
                    client.get("/billing/debtors/", **owner_auth),
                    marker=markers["student_name"],
                ),
                "owner_get_foreign_freezes": self._status_probe(
                    client.get(f"/billing/subscriptions/{club_b['subscription_id']}/freezes/", **owner_auth)
                ),
                "owner_get_foreign_document_checklist": self._status_probe(
                    client.get(f"/documents/students/{club_b['child_id']}/checklist/", **owner_auth)
                ),
                "owner_get_foreign_lead": self._status_probe(
                    client.get(f"/leads/{club_b['lead_id']}", **owner_auth)
                ),
                "owner_get_foreign_retention_task": self._status_probe(
                    client.get(f"/retention/tasks/{club_b['retention_task_id']}/", **owner_auth)
                ),
                "parent_children": self._parent_children_probe(
                    client.get("/parents/children/", **parent_auth),
                ),
                "parent_get_foreign_child": self._status_probe(
                    client.get(f"/parents/children/{club_b['child_id']}/", **parent_auth)
                )
            }
        api_evidence.update(
            {
                "kiosk_lookup_foreign_phone": self._kiosk_lookup_probe(
                    client.post(
                        "/checkins/kiosk/lookup/",
                        json={"phone_suffix": markers["phone_suffix"]},
                        **club_a_kiosk_auth,
                    ),
                    marker=markers["student_name"],
                ),
                "kiosk_foreign_schedule_today": self._list_probe(
                    client.get(
                        f"/checkins/kiosk/schedules/today/?date={fixture['club_a']['checkin_date']}",
                        **club_a_kiosk_auth,
                    ),
                    marker=markers["group_name"],
                ),
            }
        )

        self._assert_expected(api_evidence)
        database_evidence = {
            "club_a_foreign_row_counts": {
                "students": Student.objects.for_club(club_a).filter(id=club_b["child_id"]).count(),
                "checkins": Checkin.objects.for_club(club_a).filter(id=club_b["checkin_id"]).count(),
                "debts": Debt.objects.for_club(club_a).filter(id=club_b["debt_id"]).count(),
                "payments": Payment.objects.for_club(club_a).filter(id=club_b["payment_id"]).count(),
                "documents": StudentDocument.objects.for_club(club_a).filter(id=club_b["document_id"]).count(),
            }
        }
        if any(database_evidence["club_a_foreign_row_counts"].values()):
            raise CommandError(f"foreign rows visible through club A managers: {database_evidence}")

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "api": api_evidence,
            "database": database_evidence,
        }

    def _auth_params(self, *, user_id: int, club: Club, role: str = ClubMembership.Role.OWNER) -> dict:
        user = get_user_model().objects.get(id=user_id)
        membership = ClubMembership.objects.filter(user=user, club=club, is_active=True).first()
        if membership is None:
            membership = ClubMembership.objects.create(user=user, club=club, role=role)
        return {
            "user": user,
            "club": club,
            "_membership": membership,
            "auth": {"user_id": user.id, "club_id": club.id, "role": membership.role},
        }

    def _mock_auth(self, request):
        return request.auth if getattr(request, "auth", None) else None

    def _status_probe(self, response) -> dict:
        return {"status_code": response.status_code}

    def _list_probe(self, response, *, marker: str) -> dict:
        payload = self._safe_json(response)
        return {
            "status_code": response.status_code,
            "foreign_marker_visible": self._contains_marker(payload, marker),
        }

    def _parent_children_probe(self, response) -> dict:
        payload = self._safe_json(response)
        if not isinstance(payload, list):
            raise CommandError(f"parent children payload is not a list: {payload}")
        return {
            "status_code": response.status_code,
            "child_ids": [item["id"] for item in payload],
        }

    def _kiosk_lookup_probe(self, response, *, marker: str) -> dict:
        payload = self._safe_json(response)
        if not isinstance(payload, list):
            raise CommandError(f"kiosk lookup payload is not a list: {payload}")
        return {
            "status_code": response.status_code,
            "match_count": len(payload),
            "foreign_marker_visible": self._contains_marker(payload, marker),
        }

    def _safe_json(self, response):
        try:
            return response.json()
        except ValueError:
            return response.content.decode("utf-8", errors="replace")

    def _contains_marker(self, payload, marker: str) -> bool:
        return marker in json.dumps(payload, ensure_ascii=False, default=str)

    def _assert_expected(self, api_evidence: dict) -> None:
        expectations = {
            "owner_get_foreign_student": {404},
            "owner_get_foreign_schedule": {404},
            "owner_batch_foreign_student": {400, 404},
            "owner_get_foreign_payment": {404},
            "owner_get_foreign_freezes": {404},
            "owner_get_foreign_document_checklist": {404},
            "owner_get_foreign_lead": {404},
            "owner_get_foreign_retention_task": {404},
            "parent_get_foreign_child": {404},
        }
        for key, allowed_statuses in expectations.items():
            status_code = api_evidence[key]["status_code"]
            if status_code not in allowed_statuses:
                raise CommandError(f"{key} expected {allowed_statuses}, got {status_code}")

        for key in ("owner_list_students", "owner_get_foreign_debtors", "kiosk_foreign_schedule_today"):
            if api_evidence[key]["status_code"] != 200:
                raise CommandError(f"{key} expected 200, got {api_evidence[key]['status_code']}")
            if api_evidence[key]["foreign_marker_visible"]:
                raise CommandError(f"{key} leaked foreign marker")

        kiosk_lookup = api_evidence["kiosk_lookup_foreign_phone"]
        if kiosk_lookup["status_code"] != 200:
            raise CommandError(f"kiosk lookup expected 200, got {kiosk_lookup['status_code']}")
        if kiosk_lookup["match_count"] != 0 or kiosk_lookup["foreign_marker_visible"]:
            raise CommandError("kiosk lookup leaked foreign student")
