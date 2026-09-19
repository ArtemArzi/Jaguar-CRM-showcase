from __future__ import annotations

import json
import time
from datetime import date, timedelta
from datetime import time as time_type
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.models import (
    Checkin,
    KioskDevice,
    Schedule,
    ScheduleEnrollment,
    ScheduleException,
    TrainingGroup,
    TrainingGroupMappingEvent,
    TrainingGroupMembership,
    TrainingGroupMembershipEvent,
    TrainingGroupRolloutState,
)
from apps.attendance.selectors import get_schedule_occurrences_for_date, get_students_for_schedule
from apps.attendance.services.training_group_reconciliation import audit_training_groups
from apps.attendance.training_group_roster import resolve_expected_roster_by_schedule_date
from apps.billing.models import Debt, Expense, Payment, Subscription
from apps.clubs.models import Club, ClubMembership
from apps.dashboard.selectors import get_dashboard_metrics
from apps.dashboard.services import get_pnl_report, get_salary_total
from apps.retention.models import RetentionTask, TaskComment


class Command(BaseCommand):
    help = "Assert owner/admin dashboard business picture E2E side effects."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            required=True,
            help="Path to fixture JSON from prepare_owner_dashboard_business_e2e.",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=float,
            default=30,
            help="Seconds to poll for dashboard state before failing.",
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
                    raise CommandError(f"owner dashboard business E2E assertion failed: {exc}") from exc
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
            "owner",
            "student",
            "payment_ids",
            "debt_id",
            "expense_id",
            "retention_admin",
            "report_range",
            "browser_expense",
            "enrollment_admin",
            "enrollment_consistency",
            "schedule_admin",
            "training_group_reconciliation",
            "expected",
        }
        missing = sorted(required - set(fixture))
        if missing:
            raise CommandError(f"fixture is missing required fields: {', '.join(missing)}")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        club = Club.objects.get(id=int(fixture["club_id"]))
        owner_user_id = int(fixture["owner"]["user_id"])
        membership = ClubMembership.objects.filter(
            user_id=owner_user_id,
            club=club,
            is_active=True,
            role=ClubMembership.Role.OWNER,
        ).first()
        if membership is None:
            raise CommandError("owner membership not found")

        today = date.today()
        report_range = fixture["report_range"]
        report_from = date.fromisoformat(report_range["date_from"])
        report_to = date.fromisoformat(report_range["date_to"])
        browser_expense = fixture["browser_expense"]
        enrollment_admin = fixture["enrollment_admin"]
        enrollment_consistency = fixture["enrollment_consistency"]
        schedule_admin = fixture["schedule_admin"]
        reconciliation = fixture["training_group_reconciliation"]
        retention_admin = fixture["retention_admin"]
        enrollment_student_id = int(enrollment_admin["student_id"])
        source_schedule_id = int(enrollment_admin["source_schedule_id"])
        target_schedule_id = int(enrollment_admin["target_schedule_id"])
        transfer_date = date.fromisoformat(enrollment_admin["transfer_date"])
        cancel_date = date.fromisoformat(enrollment_admin["cancel_date"])
        dashboard = get_dashboard_metrics(club=club, date_from=today, date_to=today)
        salary = get_salary_total(club=club, date_from=today, date_to=today)
        pnl = get_pnl_report(club=club, date_from=report_from, date_to=report_to)
        payments = Payment.objects.for_club(club)
        open_debts = Debt.objects.for_club(club).filter(resolved_at__isnull=True)
        active_subscriptions = Subscription.objects.for_club(club).filter(
            status=Subscription.Status.ACTIVE,
            deleted_at__isnull=True,
        )
        checkins = Checkin.objects.for_club(club).filter(deleted_at__isnull=True)
        expenses = Expense.objects.for_club(club).filter(deleted_at__isnull=True)
        kiosk_devices = KioskDevice.objects.filter(club=club)
        active_kiosk_devices = KioskDevice.objects.filter(club=club, is_active=True)

        expected = fixture["expected"]
        self._assert_decimal("dashboard revenue", dashboard["revenue"], expected["dashboard_revenue"])
        self._assert_decimal("dashboard salary", salary, expected["dashboard_salary"])
        self._assert_decimal("pnl income", pnl["income"], expected["pnl_income"])
        self._assert_decimal("pnl salary", pnl["salary_expenses"], expected["pnl_salary"])
        self._assert_decimal("pnl manual expenses", pnl["manual_expenses"], expected["manual_expense"])
        self._assert_decimal("pnl margin", pnl["margin"], expected["pnl_margin"])

        if dashboard["active_subscriptions"] != 1:
            raise CommandError("dashboard active subscription count mismatch")
        if dashboard["debtors"] != 1:
            raise CommandError("dashboard debtors count mismatch")
        if active_subscriptions.count() != 1:
            raise CommandError("active subscription count mismatch")
        if payments.filter(status=Payment.Status.PENDING).count() != 1:
            raise CommandError("pending payment count mismatch")
        if payments.filter(status=Payment.Status.CONFIRMED).count() != 1:
            raise CommandError("confirmed payment count mismatch")
        if open_debts.count() != 1:
            raise CommandError("open debt count mismatch")
        if checkins.count() != 2:
            raise CommandError("check-in count mismatch")
        if expenses.count() != 1:
            raise CommandError("expense count mismatch")
        if not expenses.filter(id=int(fixture["expense_id"])).exists():
            raise CommandError("seed expense disappeared")
        if not kiosk_devices.exists():
            raise CommandError("kiosk device not created")
        expected_kiosk_active_count = int(expected.get("kiosk_active_count", 0))
        expected_kiosk_device_count = int(expected.get("kiosk_device_count", 1))
        if active_kiosk_devices.count() != expected_kiosk_active_count:
            raise CommandError(
                f"kiosk active count mismatch: expected {expected_kiosk_active_count}, "
                f"got {active_kiosk_devices.count()}"
            )
        if kiosk_devices.count() != expected_kiosk_device_count:
            raise CommandError(
                f"kiosk device count mismatch: expected {expected_kiosk_device_count}, got {kiosk_devices.count()}"
            )
        stored_pin_count = kiosk_devices.exclude(pin_code="").count()
        if stored_pin_count:
            raise CommandError(f"kiosk stored PIN count mismatch: expected 0, got {stored_pin_count}")
        if expenses.filter(name=browser_expense["name"]).exists():
            raise CommandError("browser-created expense is still active")
        deleted_browser_expenses = Expense.objects.for_club(club).filter(
            name=browser_expense["name"],
            amount=Decimal(browser_expense["amount"]),
            date=date.fromisoformat(browser_expense["date"]),
            category=browser_expense["category"],
            deleted_at__isnull=False,
        )
        if deleted_browser_expenses.count() != 1:
            raise CommandError("browser-created expense soft-delete evidence missing")

        admin_enrollments = ScheduleEnrollment.objects.for_club(club).filter(student_id=enrollment_student_id)
        source_transfer = admin_enrollments.filter(
            schedule_id=source_schedule_id,
            status=ScheduleEnrollment.Status.TRANSFERRED,
            starts_on=date.fromisoformat(enrollment_admin["session_date"]),
            ends_on=transfer_date - timedelta(days=1),
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        ).first()
        if source_transfer is None:
            raise CommandError("enrollment admin source transfer evidence missing")
        target_cancelled = admin_enrollments.filter(
            schedule_id=target_schedule_id,
            status=ScheduleEnrollment.Status.CANCELLED,
            starts_on=transfer_date,
            ends_on=cancel_date - timedelta(days=1),
            created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
        ).first()
        if target_cancelled is None:
            raise CommandError("enrollment admin target cancel evidence missing")
        open_admin_count = admin_enrollments.filter(
            schedule_id__in=[source_schedule_id, target_schedule_id],
            status__in=[
                ScheduleEnrollment.Status.ACTIVE,
                ScheduleEnrollment.Status.TRIAL,
                ScheduleEnrollment.Status.FROZEN,
            ],
        ).count()
        if open_admin_count:
            raise CommandError(f"enrollment admin open enrollment count mismatch: got {open_admin_count}")

        consistency_student_id = int(enrollment_consistency["student_id"])
        consistency_schedule_id = int(enrollment_consistency["schedule_id"])
        consistency_date = date.fromisoformat(enrollment_consistency["session_date"])
        transferred_enrollment = ScheduleEnrollment.objects.for_club(club).filter(
            id=int(enrollment_consistency["transferred_enrollment_id"]),
            student_id=consistency_student_id,
            schedule_id=consistency_schedule_id,
            status=ScheduleEnrollment.Status.TRANSFERRED,
        ).first()
        active_enrollment = ScheduleEnrollment.objects.for_club(club).filter(
            id=int(enrollment_consistency["active_enrollment_id"]),
            student_id=consistency_student_id,
            schedule_id=consistency_schedule_id,
            status=ScheduleEnrollment.Status.ACTIVE,
        ).first()
        if transferred_enrollment is None or active_enrollment is None:
            raise CommandError("enrollment consistency rows are missing")
        roster_row = next(
            (
                row
                for row in get_students_for_schedule(
                    club=club,
                    schedule_id=consistency_schedule_id,
                    reference_date=consistency_date,
                )
                if row["id"] == consistency_student_id
            ),
            None,
        )
        if roster_row is None:
            raise CommandError("enrollment consistency roster row is missing")
        if roster_row["enrollment_id"] != active_enrollment.id:
            raise CommandError("enrollment consistency roster did not select the active enrollment")
        if roster_row["enrollment_status"] != ScheduleEnrollment.Status.ACTIVE:
            raise CommandError("enrollment consistency roster status is not active")

        schedule_admin_evidence = self._collect_schedule_admin_evidence(
            club=club,
            schedule_admin=schedule_admin,
        )
        retention_admin_evidence = self._collect_retention_admin_evidence(
            club=club,
            retention_admin=retention_admin,
        )
        reconciliation_evidence = self._collect_training_group_reconciliation_evidence(
            club=club,
            reconciliation=reconciliation,
            runtime=fixture.get("runtime", {}).get("training_group_reconciliation"),
            owner_user_id=owner_user_id,
        )

        return {
            "ok": True,
            "fixture_id": fixture["fixture_id"],
            "checked_at": timezone.now().isoformat(),
            "owner": {
                "user_id": owner_user_id,
                "membership_role": membership.role,
            },
            "dashboard": {
                "revenue": self._money(dashboard["revenue"]),
                "active_subscriptions": dashboard["active_subscriptions"],
                "debtors": dashboard["debtors"],
                "trainer_salaries": self._money(salary),
            },
            "payments": {
                "pending_count": payments.filter(status=Payment.Status.PENDING).count(),
                "confirmed_count": payments.filter(status=Payment.Status.CONFIRMED).count(),
            },
            "debtors": {
                "open_count": open_debts.count(),
                "amount": self._money(open_debts.get(id=int(fixture["debt_id"])).tariff_price),
            },
            "pnl": {
                "income": self._money(pnl["income"]),
                "salary_expenses": self._money(pnl["salary_expenses"]),
                "manual_expenses": self._money(pnl["manual_expenses"]),
                "margin": self._money(pnl["margin"]),
                "report_from": report_from.isoformat(),
                "report_to": report_to.isoformat(),
                "active_expense_count": expenses.count(),
                "browser_expense_deleted_count": deleted_browser_expenses.count(),
            },
            "kiosk": {
                "active_count": active_kiosk_devices.count(),
                "device_count": kiosk_devices.count(),
                "stored_pin_count": stored_pin_count,
            },
            "enrollment_admin": {
                "student_id": enrollment_student_id,
                "source_status": source_transfer.status,
                "source_ends_on": source_transfer.ends_on.isoformat() if source_transfer.ends_on else None,
                "target_status": target_cancelled.status,
                "target_starts_on": target_cancelled.starts_on.isoformat() if target_cancelled.starts_on else None,
                "target_ends_on": target_cancelled.ends_on.isoformat() if target_cancelled.ends_on else None,
                "open_count": open_admin_count,
            },
            "enrollment_consistency": {
                "student_id": consistency_student_id,
                "schedule_id": consistency_schedule_id,
                "transferred_enrollment_id": transferred_enrollment.id,
                "active_enrollment_id": active_enrollment.id,
                "roster_enrollment_id": roster_row["enrollment_id"],
                "roster_status": roster_row["enrollment_status"],
            },
            "schedule_admin": schedule_admin_evidence,
            "retention_admin": retention_admin_evidence,
            "training_group_reconciliation": reconciliation_evidence,
        }

    def _collect_training_group_reconciliation_evidence(
        self,
        *,
        club: Club,
        reconciliation: dict,
        runtime: dict | None,
        owner_user_id: int,
    ) -> dict:
        if not settings.TRAINING_GROUP_NEW_WRITES_ENABLED:
            raise CommandError("owner reconciliation fixture requires training-group new writes enabled")
        if not settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED:
            raise CommandError("owner reconciliation fixture requires manual operational admission enabled")
        if runtime is None:
            raise CommandError("owner reconciliation runtime evidence is missing")

        applied = runtime.get("apply") or {}
        preview_digest = str(runtime.get("ui_preview_digest") or "")
        if not preview_digest or preview_digest != applied.get("preview_digest"):
            raise CommandError("owner reconciliation preview/apply digest evidence mismatch")
        selected_schedule_ids = sorted(int(value) for value in reconciliation["schedule_ids"])
        if sorted(int(value) for value in applied.get("selected_schedule_ids", [])) != selected_schedule_ids:
            raise CommandError("owner reconciliation apply selected unexpected schedule IDs")
        if applied.get("membership_count") != 1 or applied.get("projection_count") != 1:
            raise CommandError("owner reconciliation apply backfill counts mismatch")

        state = TrainingGroupRolloutState.objects.for_club(club).get()
        if state.mode != TrainingGroupRolloutState.Mode.ACTIVE:
            raise CommandError("owner reconciliation rollout did not reach active")
        group = TrainingGroup.objects.for_club(club).get(
            id=int(applied["training_group_id"]),
            name=reconciliation["canonical_name"],
            responsible_trainer_id=int(reconciliation["responsible_trainer_id"]),
            status=TrainingGroup.Status.ACTIVE,
        )
        mapped_schedule_ids = list(
            Schedule.objects.for_club(club)
            .filter(id__in=selected_schedule_ids, training_group_id=group.id)
            .order_by("id")
            .values_list("id", flat=True)
        )
        if mapped_schedule_ids != selected_schedule_ids:
            raise CommandError("owner reconciliation did not map the exact selected schedule IDs")

        membership = TrainingGroupMembership.objects.for_club(club).get(
            training_group=group,
            student_id=int(reconciliation["source_student_id"]),
        )
        if (
            membership.status != TrainingGroupMembership.Status.ACTIVE
            or membership.authority != TrainingGroupMembership.Authority.INDEPENDENT
            or membership.source != TrainingGroupMembership.Source.MIGRATION
        ):
            raise CommandError("owner reconciliation backfill membership is not active independent migration authority")
        source = ScheduleEnrollment.objects.for_club(club).get(id=int(reconciliation["source_enrollment_id"]))
        if (
            source.schedule_id != int(reconciliation["source_schedule_id"])
            or source.status != ScheduleEnrollment.Status.ACTIVE
            or source.created_from != ScheduleEnrollment.CreatedFrom.MANUAL
            or source.training_group_membership_id != membership.id
        ):
            raise CommandError("owner reconciliation rewrote legacy manual source provenance")
        source_events = list(TrainingGroupMembershipEvent.objects.for_club(club).filter(
            membership=membership,
            source_enrollment_id=source.id,
            action__in=["backfilled", "source_linked"],
        ))
        if len(source_events) != 1 or source_events[0].actor_id != owner_user_id:
            raise CommandError("owner reconciliation source-link audit event is missing")
        projections = list(
            ScheduleEnrollment.objects.for_club(club)
            .filter(
                training_group_membership=membership,
                created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
            )
            .order_by("schedule_id", "id")
            .values_list("schedule_id", "status", "created_from")
        )
        expected_projection = [int(reconciliation["projection_schedule_id"])]
        if projections != [
            (
                expected_projection[0],
                ScheduleEnrollment.Status.ACTIVE,
                ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
            )
        ]:
            raise CommandError("owner reconciliation missing exact compatibility projection")
        roster = resolve_expected_roster_by_schedule_date(
            club=club,
            schedule_ids=selected_schedule_ids,
            target_date=membership.starts_on,
        )
        roster_schedule_ids = []
        for schedule_id in selected_schedule_ids:
            entry = roster.get(schedule_id, {}).get(membership.student_id)
            if (
                entry is None
                or entry.source != "training_group_membership"
                or entry.membership_id != membership.id
                or entry.enrollment_status != TrainingGroupMembership.Status.ACTIVE
            ):
                raise CommandError("owner reconciliation membership does not cover every mapped slot")
            roster_schedule_ids.append(schedule_id)

        mapping_events = TrainingGroupMappingEvent.objects.for_club(club).filter(training_group=group)
        schedule_event_ids = list(
            mapping_events.filter(action="schedule_mapped")
            .order_by("schedule_id")
            .values_list("schedule_id", flat=True)
        )
        apply_events = list(mapping_events.filter(action="apply_completed", schedule__isnull=True))
        if schedule_event_ids != selected_schedule_ids or len(apply_events) != 1:
            raise CommandError("owner reconciliation retry created duplicate or dirty mapping audit events")
        if str(apply_events[0].batch_id) != str(applied.get("batch_id", "")):
            raise CommandError("owner reconciliation apply batch evidence mismatch")
        audit = audit_training_groups(club=club)
        if not audit["valid"]:
            raise CommandError("owner reconciliation audit is not clean")
        return {
            "training_group_id": group.id,
            "rollout_mode": state.mode,
            "new_writes_enabled": settings.TRAINING_GROUP_NEW_WRITES_ENABLED,
            "manual_operational_admission_enabled": settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED,
            "selected_schedule_ids": mapped_schedule_ids,
            "membership_id": membership.id,
            "membership_authority": membership.authority,
            "source_enrollment_id": source.id,
            "projection_schedule_ids": [schedule_id for schedule_id, _, _ in projections],
            "roster_membership_schedule_ids": roster_schedule_ids,
            "mapping_event_count": mapping_events.count(),
            "apply_event_count": len(apply_events),
            "audit_valid": audit["valid"],
        }

    def _collect_retention_admin_evidence(self, *, club: Club, retention_admin: dict) -> dict:
        task = (
            RetentionTask.objects.for_club(club)
            .select_related("student", "trainer")
            .filter(id=int(retention_admin["task_id"]))
            .first()
        )
        if task is None:
            raise CommandError("retention dashboard task evidence missing")
        if task.status != retention_admin["status"]:
            raise CommandError("retention dashboard task status mismatch")
        if task.level != retention_admin["level"]:
            raise CommandError("retention dashboard task level mismatch")
        if task.trainer_id != int(retention_admin["trainer_id"]):
            raise CommandError("retention dashboard trainer scope mismatch")

        comment = (
            TaskComment.objects.for_club(club)
            .filter(id=int(retention_admin["comment_id"]), task=task, text=retention_admin["comment_text"])
            .first()
        )
        if comment is None:
            raise CommandError("retention dashboard comment evidence missing")

        return {
            "task_id": task.id,
            "student_id": task.student_id,
            "trainer_id": task.trainer_id,
            "status": task.status,
            "level": task.level,
            "comment_count": task.comments.count(),
            "comment_id": comment.id,
        }

    def _collect_schedule_admin_evidence(self, *, club: Club, schedule_admin: dict) -> dict:
        create = schedule_admin["create"]
        create_date = date.fromisoformat(create["date"])
        edited_start_time = time_type.fromisoformat(create["edited_start_time"])
        edited_end_time = time_type.fromisoformat(create["edited_end_time"])
        created_name = create["created_group_name"]
        edited_name = create["edited_group_name"]

        edited_schedule = (
            Schedule.objects.for_club(club)
            .filter(
                group_name=edited_name,
                one_time_date=create_date,
                start_time=edited_start_time,
                end_time=edited_end_time,
                trainer_id=int(create["trainer_id"]),
                location_id=int(create["location_id"]),
                training_type_id=int(create["edited_training_type_id"]),
                is_active=True,
            )
            .first()
        )
        if edited_schedule is None:
            raise CommandError("schedule admin created schedule evidence missing")
        if Schedule.objects.for_club(club).filter(group_name=created_name, is_active=True).exists():
            raise CommandError("schedule admin stale created schedule name is still active")

        cancel = schedule_admin["cancel"]
        cancel_date = date.fromisoformat(cancel["date"])
        cancel_exception = (
            ScheduleException.objects.for_club(club)
            .filter(
                schedule_id=int(cancel["schedule_id"]),
                date=cancel_date,
                exception_type=ScheduleException.ExceptionType.CANCELLED,
                reason=cancel["reason"],
            )
            .first()
        )
        if cancel_exception is None:
            raise CommandError("schedule admin cancel exception evidence missing")
        cancel_visible = any(
            occurrence.schedule_id == int(cancel["schedule_id"])
            for occurrence in get_schedule_occurrences_for_date(club=club, target_date=cancel_date)
        )
        if cancel_visible:
            raise CommandError("schedule admin cancelled occurrence is still visible")

        reschedule = schedule_admin["reschedule"]
        reschedule_old_date = date.fromisoformat(reschedule["old_date"])
        reschedule_new_date = date.fromisoformat(reschedule["new_date"])
        reschedule_new_start = time_type.fromisoformat(reschedule["new_start_time"])
        reschedule_new_end = time_type.fromisoformat(reschedule["new_end_time"])
        reschedule_exception = (
            ScheduleException.objects.for_club(club)
            .filter(
                schedule_id=int(reschedule["schedule_id"]),
                date=reschedule_old_date,
                exception_type=ScheduleException.ExceptionType.RESCHEDULED,
                new_date=reschedule_new_date,
                new_start_time=reschedule_new_start,
                new_end_time=reschedule_new_end,
            )
            .first()
        )
        if reschedule_exception is None:
            raise CommandError("schedule admin reschedule exception evidence missing")
        reschedule_old_visible = any(
            occurrence.schedule_id == int(reschedule["schedule_id"])
            for occurrence in get_schedule_occurrences_for_date(club=club, target_date=reschedule_old_date)
        )
        if reschedule_old_visible:
            raise CommandError("schedule admin rescheduled old occurrence is still visible")
        reschedule_new_visible = any(
            occurrence.schedule_id == int(reschedule["schedule_id"])
            and occurrence.is_rescheduled
            and occurrence.effective_start_time == reschedule_new_start
            and occurrence.effective_end_time == reschedule_new_end
            for occurrence in get_schedule_occurrences_for_date(club=club, target_date=reschedule_new_date)
        )
        if not reschedule_new_visible:
            raise CommandError("schedule admin rescheduled new occurrence is not visible")

        substitute = schedule_admin["substitute"]
        substitute_date = date.fromisoformat(substitute["date"])
        substitute_trainer_id = int(substitute["substitute_trainer_id"])
        substitute_exception = (
            ScheduleException.objects.for_club(club)
            .filter(
                schedule_id=int(substitute["schedule_id"]),
                date=substitute_date,
                exception_type=ScheduleException.ExceptionType.SUBSTITUTE,
                substitute_trainer_id=substitute_trainer_id,
            )
            .first()
        )
        if substitute_exception is None:
            raise CommandError("schedule admin substitute exception evidence missing")
        substitute_visible = any(
            occurrence.schedule_id == int(substitute["schedule_id"])
            and occurrence.is_substitute
            and occurrence.trainer_id == substitute_trainer_id
            for occurrence in get_schedule_occurrences_for_date(club=club, target_date=substitute_date)
        )
        if not substitute_visible:
            raise CommandError("schedule admin substitute occurrence is not visible")

        revert = schedule_admin["revert"]
        revert_date = date.fromisoformat(revert["date"])
        revert_exception_exists = ScheduleException.objects.for_club(club).filter(
            schedule_id=int(revert["schedule_id"]),
            date=revert_date,
        ).exists()
        if revert_exception_exists:
            raise CommandError("schedule admin revert exception still exists")
        reverted_visible = any(
            occurrence.schedule_id == int(revert["schedule_id"])
            and not occurrence.is_substitute
            and occurrence.trainer_id == int(revert["original_trainer_id"])
            for occurrence in get_schedule_occurrences_for_date(club=club, target_date=revert_date)
        )
        if not reverted_visible:
            raise CommandError("schedule admin reverted occurrence is not visible")

        return {
            "created_schedule_id": edited_schedule.id,
            "created_group_name_inactive": created_name,
            "edited_group_name": edited_schedule.group_name,
            "edited_training_type_id": edited_schedule.training_type_id,
            "cancel_exception_id": cancel_exception.id,
            "reschedule_exception_id": reschedule_exception.id,
            "reschedule_new_visible": reschedule_new_visible,
            "substitute_exception_id": substitute_exception.id,
            "substitute_visible": substitute_visible,
            "revert_exception_exists": revert_exception_exists,
            "reverted_visible": reverted_visible,
        }

    def _assert_decimal(self, label: str, actual, expected: str) -> None:
        if Decimal(str(actual)).quantize(Decimal("0.01")) != Decimal(expected).quantize(Decimal("0.01")):
            raise CommandError(f"{label} mismatch: expected {expected}, got {actual}")

    def _money(self, value) -> str:
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
