from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from ninja.testing import TestClient

from apps.billing.models import Payment, SubscriptionFreeze
from apps.clubs.models import Club, ClubMembership
from apps.feedback.models import FeedbackResponse
from apps.grades.models import StudentGrade
from apps.students.models import Student, StudentNote
from config.api import api


class Command(BaseCommand):
    help = "Assert trainer student cockpit scope E2E access boundaries."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_trainer_student_cockpit_scope_e2e.",
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
                    raise CommandError(f"trainer student cockpit scope E2E assertion failed: {exc}") from exc
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

        required = {
            "fixture_id",
            "club_id",
            "foreign_club_id",
            "trainer",
            "assigned_student",
            "unassigned_student",
            "package_owned_student",
            "other_trainer_student",
            "foreign_student",
            "ids",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        client = TestClient(api)
        club = Club.objects.get(id=int(fixture["club_id"]))
        trainer_auth = self._auth_params(
            user_id=int(fixture["trainer"]["user_id"]),
            club=club,
            role=ClubMembership.Role.TRAINER,
        )
        assigned_id = int(fixture["assigned_student"]["id"])
        unassigned_id = int(fixture["unassigned_student"]["id"])
        package_owned_id = int(fixture["package_owned_student"]["id"])
        other_trainer_student_id = int(fixture["other_trainer_student"]["id"])
        foreign_student_id = int(fixture["foreign_student"]["id"])
        ids = fixture["ids"]

        with patch("apps.common.auth.TenantJWTAuth.__call__", side_effect=self._mock_auth):
            list_response = client.get("/students/", **trainer_auth)
            list_payload = self._safe_json(list_response)
            visible_ids = [item["id"] for item in list_payload.get("items", list_payload)]

            api_evidence = {
                "students_list": {
                    "status_code": list_response.status_code,
                    "visible_ids": visible_ids,
                },
                "assigned_detail": self._status_probe(
                    client.get(f"/students/{assigned_id}/", **trainer_auth)
                ),
                "assigned_detail_payload": self._student_detail_probe(
                    client.get(f"/students/{assigned_id}/", **trainer_auth)
                ),
                "assigned_subscriptions": self._subscriptions_probe(
                    client.get(f"/billing/subscriptions/?student_id={assigned_id}", **trainer_auth)
                ),
                "assigned_feedback_responses": self._feedback_responses_probe(
                    client.get(f"/feedback/students/{assigned_id}/responses/", **trainer_auth)
                ),
                "assigned_send_survey": self._status_probe(
                    client.post(f"/feedback/send-survey/{assigned_id}/", **trainer_auth)
                ),
                "package_owned_detail": self._status_probe(
                    client.get(f"/students/{package_owned_id}/", **trainer_auth)
                ),
                "package_owned_detail_payload": self._student_detail_probe(
                    client.get(f"/students/{package_owned_id}/", **trainer_auth)
                ),
                "package_owned_subscriptions": self._subscriptions_probe(
                    client.get(f"/billing/subscriptions/?student_id={package_owned_id}", **trainer_auth)
                ),
                "package_owned_send_survey": self._status_probe(
                    client.post(f"/feedback/send-survey/{package_owned_id}/", **trainer_auth)
                ),
                "unassigned_detail": self._status_probe(
                    client.get(f"/students/{unassigned_id}/", **trainer_auth)
                ),
                "other_trainer_detail": self._status_probe(
                    client.get(f"/students/{other_trainer_student_id}/", **trainer_auth)
                ),
                "foreign_detail": self._status_probe(
                    client.get(f"/students/{foreign_student_id}/", **trainer_auth)
                ),
                "unassigned_notes": self._status_probe(
                    client.get(f"/students/{unassigned_id}/notes/", **trainer_auth)
                ),
                "unassigned_add_note": self._status_probe(
                    client.post(
                        f"/students/{unassigned_id}/notes/",
                        json={"text": "Forbidden E2E note"},
                        **trainer_auth,
                    )
                ),
                "unassigned_account_access_open": self._status_probe(
                    client.post(
                        f"/students/{unassigned_id}/account-access/open/",
                        json={},
                        **trainer_auth,
                    )
                ),
                "unassigned_account_access_reset": self._status_probe(
                    client.post(
                        f"/students/{unassigned_id}/account-access/reset/",
                        **trainer_auth,
                    )
                ),
                "unassigned_checkins": self._status_probe(
                    client.get(f"/students/{unassigned_id}/checkins/", **trainer_auth)
                ),
                "unassigned_feedback_responses": self._status_probe(
                    client.get(f"/feedback/students/{unassigned_id}/responses/", **trainer_auth)
                ),
                "unassigned_send_survey": self._status_probe(
                    client.post(f"/feedback/send-survey/{unassigned_id}/", **trainer_auth)
                ),
                "unassigned_feedback_submit": self._status_probe(
                    client.post(
                        "/feedback/submit/",
                        json={
                            "form_id": int(ids["feedback_form_id"]),
                            "student_id": unassigned_id,
                            "answers": [
                                {"question_id": int(ids["feedback_question_id"]), "rating_value": 4}
                            ],
                        },
                        **trainer_auth,
                    )
                ),
                "unassigned_subscriptions": self._status_probe(
                    client.get(f"/billing/subscriptions/?student_id={unassigned_id}", **trainer_auth)
                ),
                "unassigned_subscription_detail": self._status_probe(
                    client.get(
                        f"/billing/subscriptions/{ids['unassigned_subscription_id']}/",
                        **trainer_auth,
                    )
                ),
                "unassigned_debts": self._status_probe(
                    client.get(f"/billing/debts/?student_id={unassigned_id}", **trainer_auth)
                ),
                "unassigned_payment": self._status_probe(
                    client.post(
                        "/billing/payments/",
                        json={
                            "student_id": unassigned_id,
                            "tariff_id": int(ids["tariff_id"]),
                            "payment_method": "cash",
                            "debt_ids": [int(ids["unassigned_debt_id"])],
                        },
                        **trainer_auth,
                    )
                ),
                "unassigned_freeze": self._status_probe(
                    client.post(
                        f"/billing/subscriptions/{ids['unassigned_subscription_id']}/freeze/",
                        json={"days": 7, "reason": "vacation"},
                        **trainer_auth,
                    )
                ),
                "unassigned_freezes": self._status_probe(
                    client.get(
                        f"/billing/subscriptions/{ids['unassigned_subscription_id']}/freezes/",
                        **trainer_auth,
                    )
                ),
                "unassigned_grade_progress": self._status_probe(
                    client.get(f"/grades/students/{unassigned_id}/progress/", **trainer_auth)
                ),
                "unassigned_grade_assign": self._status_probe(
                    client.post(
                        "/grades/student-grades/",
                        json={
                            "student_id": unassigned_id,
                            "grade_system_id": int(ids["grade_system_id"]),
                        },
                        **trainer_auth,
                    )
                ),
                "unassigned_grade_promote": self._status_probe(
                    client.post(
                        f"/grades/student-grades/{ids['unassigned_student_grade_id']}/promote/",
                        json={"new_grade_id": int(ids["next_grade_id"])},
                        **trainer_auth,
                    )
                ),
                "unassigned_grade_delete": self._status_probe(
                    client.delete(
                        f"/grades/student-grades/{ids['unassigned_student_grade_id']}/",
                        **trainer_auth,
                    )
                ),
                "ready_for_promotion": self._ready_probe(
                    client.get(
                        f"/grades/systems/{ids['grade_system_id']}/ready-for-promotion/",
                        **trainer_auth,
                    )
                ),
            }

        self._assert_expected(
            api_evidence=api_evidence,
            assigned_id=assigned_id,
            unassigned_id=unassigned_id,
            package_owned_id=package_owned_id,
            other_trainer_student_id=other_trainer_student_id,
            foreign_student_id=foreign_student_id,
            expected=fixture.get("expected", {}),
        )
        self._assert_no_forbidden_mutation(
            fixture=fixture,
            club=club,
            unassigned_id=unassigned_id,
        )

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "api": api_evidence,
        }

    def _auth_params(self, *, user_id: int, club: Club, role: str) -> dict:
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

    def _student_detail_probe(self, response) -> dict:
        payload = self._safe_json(response)
        if not isinstance(payload, dict):
            return {"status_code": response.status_code, "notes": [], "can_manage_account_access": None}
        return {
            "status_code": response.status_code,
            "id": payload.get("id"),
            "status": payload.get("status"),
            "note_texts": [note.get("text") for note in payload.get("notes", [])],
            "can_manage_account_access": payload.get("can_manage_account_access"),
            "account_access_present": payload.get("account_access") is not None,
        }

    def _subscriptions_probe(self, response) -> dict:
        payload = self._safe_json(response)
        items = payload.get("items", payload) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            items = []
        return {
            "status_code": response.status_code,
            "items": [
                {
                    "id": item.get("id"),
                    "tariff_name": item.get("tariff", {}).get("name"),
                    "status": item.get("status"),
                    "trainings_left": item.get("trainings_left"),
                    "trainings_used": item.get("trainings_used"),
                }
                for item in items
                if isinstance(item, dict)
            ],
        }

    def _ready_probe(self, response) -> dict:
        payload = self._safe_json(response)
        return {
            "status_code": response.status_code,
            "student_ids": [item["student_id"] for item in payload] if isinstance(payload, list) else [],
        }

    def _feedback_responses_probe(self, response) -> dict:
        payload = self._safe_json(response)
        answers = []
        if isinstance(payload, list):
            for item in payload:
                answers.extend(item.get("answers", []))
        return {
            "status_code": response.status_code,
            "count": len(payload) if isinstance(payload, list) else 0,
            "answers": [
                {
                    "question_text": answer.get("question_text"),
                    "rating_value": answer.get("rating_value"),
                }
                for answer in answers
            ],
        }

    def _safe_json(self, response):
        try:
            return response.json()
        except ValueError:
            return response.content.decode("utf-8", errors="replace")

    def _assert_expected(
        self,
        *,
        api_evidence: dict,
        assigned_id: int,
        unassigned_id: int,
        package_owned_id: int,
        other_trainer_student_id: int,
        foreign_student_id: int,
        expected: dict,
    ) -> None:
        students_list = api_evidence["students_list"]
        if students_list["status_code"] != 200:
            raise CommandError(f"student list expected 200, got {students_list['status_code']}")
        visible_ids = set(students_list["visible_ids"])
        if assigned_id not in visible_ids:
            raise CommandError("assigned student is missing from trainer list")
        if package_owned_id not in visible_ids:
            raise CommandError("package-owned student is missing from trainer list")
        for hidden_id, label in (
            (unassigned_id, "unassigned"),
            (other_trainer_student_id, "other trainer"),
            (foreign_student_id, "foreign"),
        ):
            if hidden_id in visible_ids:
                raise CommandError(f"{label} student leaked into trainer list")

        if api_evidence["assigned_detail"]["status_code"] != 200:
            raise CommandError(
                f"assigned detail expected 200, got {api_evidence['assigned_detail']['status_code']}"
            )
        assigned_detail = api_evidence["assigned_detail_payload"]
        if assigned_detail["status_code"] != 200:
            raise CommandError(
                f"assigned detail payload expected 200, got {assigned_detail['status_code']}"
            )
        if assigned_detail["id"] != assigned_id:
            raise CommandError(f"assigned detail id mismatch: expected {assigned_id}, got {assigned_detail['id']}")
        if assigned_detail["can_manage_account_access"] is not True:
            raise CommandError("assigned detail should allow trainer account-access management")
        expected_note = expected.get("assigned_note_text")
        if expected_note and expected_note not in assigned_detail["note_texts"]:
            raise CommandError("assigned detail note is missing")

        assigned_subscriptions = api_evidence["assigned_subscriptions"]
        if assigned_subscriptions["status_code"] != 200:
            raise CommandError(
                f"assigned subscriptions expected 200, got {assigned_subscriptions['status_code']}"
            )
        expected_tariff = expected.get("assigned_tariff_name")
        if expected_tariff and not any(
            item["tariff_name"] == expected_tariff and item["status"] == "active"
            for item in assigned_subscriptions["items"]
        ):
            raise CommandError("assigned active subscription is missing expected tariff")

        assigned_feedback = api_evidence["assigned_feedback_responses"]
        if assigned_feedback["status_code"] != 200:
            raise CommandError(
                f"assigned feedback responses expected 200, got {assigned_feedback['status_code']}"
            )
        if assigned_feedback["count"] < 1:
            raise CommandError("assigned feedback response is missing")
        expected_question = expected.get("assigned_feedback_question")
        if expected_question and not any(
            answer["question_text"] == expected_question and answer["rating_value"] == 4
            for answer in assigned_feedback["answers"]
        ):
            raise CommandError("assigned feedback answer is missing expected rating")
        if api_evidence["assigned_send_survey"]["status_code"] != 200:
            raise CommandError(
                f"assigned send survey expected 200, got {api_evidence['assigned_send_survey']['status_code']}"
            )

        if api_evidence["package_owned_detail"]["status_code"] != 200:
            raise CommandError(
                "package-owned detail expected 200, "
                f"got {api_evidence['package_owned_detail']['status_code']}"
            )
        package_detail = api_evidence["package_owned_detail_payload"]
        if package_detail["status_code"] != 200:
            raise CommandError(
                "package-owned detail payload expected 200, "
                f"got {package_detail['status_code']}"
            )
        if package_detail["id"] != package_owned_id:
            raise CommandError(
                f"package-owned detail id mismatch: expected {package_owned_id}, got {package_detail['id']}"
            )
        if package_detail["can_manage_account_access"] is not True:
            raise CommandError("package-owned detail should allow trainer account-access management")
        package_subscriptions = api_evidence["package_owned_subscriptions"]
        if package_subscriptions["status_code"] != 200:
            raise CommandError(
                "package-owned subscriptions expected 200, "
                f"got {package_subscriptions['status_code']}"
            )
        expected_package_tariff = expected.get("package_owned_tariff_name")
        if expected_package_tariff and not any(
            item["tariff_name"] == expected_package_tariff and item["status"] == "active"
            for item in package_subscriptions["items"]
        ):
            raise CommandError("package-owned active subscription is missing expected tariff")
        if api_evidence["package_owned_send_survey"]["status_code"] != 200:
            raise CommandError(
                "package-owned send survey expected 200, "
                f"got {api_evidence['package_owned_send_survey']['status_code']}"
            )

        denied_keys = [
            "unassigned_detail",
            "other_trainer_detail",
            "unassigned_notes",
            "unassigned_add_note",
            "unassigned_account_access_open",
            "unassigned_account_access_reset",
            "unassigned_checkins",
            "unassigned_feedback_responses",
            "unassigned_send_survey",
            "unassigned_feedback_submit",
            "unassigned_subscriptions",
            "unassigned_subscription_detail",
            "unassigned_debts",
            "unassigned_payment",
            "unassigned_freeze",
            "unassigned_freezes",
            "unassigned_grade_progress",
            "unassigned_grade_assign",
            "unassigned_grade_promote",
            "unassigned_grade_delete",
        ]
        for key in denied_keys:
            status_code = api_evidence[key]["status_code"]
            if status_code != 403:
                raise CommandError(f"{key} expected 403, got {status_code}")

        foreign_status = api_evidence["foreign_detail"]["status_code"]
        if foreign_status not in {403, 404}:
            raise CommandError(f"foreign detail expected 403/404, got {foreign_status}")

        ready = api_evidence["ready_for_promotion"]
        if ready["status_code"] != 200:
            raise CommandError(f"ready-for-promotion expected 200, got {ready['status_code']}")
        if assigned_id not in ready["student_ids"]:
            raise CommandError("assigned ready student missing from ready-for-promotion")
        if unassigned_id in ready["student_ids"]:
            raise CommandError("unassigned ready student leaked into ready-for-promotion")

    def _assert_no_forbidden_mutation(self, *, fixture: dict, club: Club, unassigned_id: int) -> None:
        if StudentNote.objects.for_club(club).filter(
            student_id=unassigned_id,
            text="Forbidden E2E note",
        ).exists():
            raise CommandError("forbidden note was created")
        if SubscriptionFreeze.objects.for_club(club).filter(
            subscription_id=int(fixture["ids"]["unassigned_subscription_id"]),
        ).exists():
            raise CommandError("forbidden freeze was created")
        if Payment.objects.for_club(club).filter(student_id=unassigned_id).exists():
            raise CommandError("forbidden payment was created")
        if FeedbackResponse.objects.for_club(club).filter(
            student_id=unassigned_id,
            form_id=int(fixture["ids"]["feedback_form_id"]),
        ).count() != 1:
            raise CommandError("feedback response count changed for unassigned student")
        student_grade = StudentGrade.objects.for_club(club).filter(
            id=int(fixture["ids"]["unassigned_student_grade_id"]),
            student_id=unassigned_id,
        ).first()
        if student_grade is None:
            raise CommandError("unassigned student grade was deleted")
        if student_grade.current_grade_id != int(fixture["ids"]["current_grade_id"]):
            raise CommandError("unassigned student grade was promoted")
        if not Student.objects.for_club(club).filter(id=unassigned_id, deleted_at__isnull=True).exists():
            raise CommandError("unassigned student disappeared")
