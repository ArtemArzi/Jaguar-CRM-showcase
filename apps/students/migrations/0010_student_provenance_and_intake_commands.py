from django.conf import settings
from django.db import migrations, models


def backfill_became_student_at(apps, schema_editor):
    """Set the monotonic fact only when persisted operational evidence exists.

    This deliberately does not infer admission from a current Student status or
    lead stage.  Each source is immutable or historical enough to establish
    that the person was accepted at least once; no financial or attendance
    record is changed by this expansion migration.
    """

    Student = apps.get_model("students", "Student")
    StudentProvenanceBackfillReceipt = apps.get_model("students", "StudentProvenanceBackfillReceipt")
    Payment = apps.get_model("billing", "Payment")
    Subscription = apps.get_model("billing", "Subscription")
    TrainingGroupMembership = apps.get_model("attendance", "TrainingGroupMembership")
    Checkin = apps.get_model("attendance", "Checkin")
    ScheduleEnrollment = apps.get_model("attendance", "ScheduleEnrollment")
    PersonalDropInBooking = apps.get_model("attendance", "PersonalDropInBooking")

    for student in Student.objects.filter(became_student_at__isnull=True).iterator():
        evidence: list[tuple[object, str, int]] = []

        for payment_id, verified_at, created_at in (
            Payment.objects.filter(
                club_id=student.club_id,
                student_id=student.id,
                status="confirmed",
            )
            .values_list("id", "verified_at", "created_at")
        ):
            evidence.append((verified_at or created_at, "confirmed_payment", payment_id))

        for subscription_id, activated_at, created_at in (
            Subscription.objects.filter(
                club_id=student.club_id,
                student_id=student.id,
                status__in=("active", "frozen", "expired"),
                deleted_at__isnull=True,
            )
            .values_list("id", "activated_at", "created_at")
        ):
            evidence.append((activated_at or created_at, "subscription", subscription_id))

        memberships = TrainingGroupMembership.objects.filter(
            club_id=student.club_id,
            student_id=student.id,
            status__in=("active", "frozen", "transferred"),
            source__in=("manual", "paid_conversion", "import", "transfer"),
        )
        for membership_id, source, created_at in memberships.values_list(
            "id", "source", "created_at"
        ):
            # A paid-conversion membership is only evidence once its own
            # payment was confirmed.  Pending/rejected admissions create this
            # row before review and must not imply commercial conversion.
            if source == "paid_conversion" and not Payment.objects.filter(
                club_id=student.club_id,
                student_id=student.id,
                status="confirmed",
                conversion_group_membership_id=membership_id,
            ).exists():
                continue
            evidence.append((created_at, "canonical_membership", membership_id))

        checkins = Checkin.objects.filter(
            club_id=student.club_id,
            student_id=student.id,
            deleted_at__isnull=True,
            cancelled_at__isnull=True,
        ).order_by("created_at", "id")
        for checkin in checkins:
            is_exact_trial = (
                ScheduleEnrollment.objects.filter(
                    club_id=student.club_id,
                    student_id=student.id,
                    schedule_id=checkin.schedule_id,
                    status="trial",
                    starts_on__lte=checkin.date,
                )
                .filter(models.Q(ends_on__isnull=True) | models.Q(ends_on__gte=checkin.date))
                .exists()
            )
            if is_exact_trial:
                continue

            has_operational_evidence = (
                checkin.subscription_id is not None
                or checkin.is_debt
                or ScheduleEnrollment.objects.filter(
                    club_id=student.club_id,
                    student_id=student.id,
                    schedule_id=checkin.schedule_id,
                    status__in=("active", "frozen"),
                    starts_on__lte=checkin.date,
                )
                .filter(models.Q(ends_on__isnull=True) | models.Q(ends_on__gte=checkin.date))
                .exists()
                or PersonalDropInBooking.objects.filter(
                    club_id=student.club_id,
                    checkin_id=checkin.id,
                    state="attended",
                ).exists()
            )
            if has_operational_evidence:
                evidence.append((checkin.created_at, "qualifying_checkin", checkin.id))

        if evidence:
            earliest_at, evidence_type, evidence_id = min(
                evidence,
                key=lambda item: (item[0], item[1], item[2]),
            )
            Student.objects.filter(id=student.id, became_student_at__isnull=True).update(
                became_student_at=earliest_at,
            )
            StudentProvenanceBackfillReceipt.objects.get_or_create(
                club_id=student.club_id,
                student_id=student.id,
                defaults={
                    "evidence_type": evidence_type,
                    "evidence_id": evidence_id,
                    "became_student_at": earliest_at,
                },
            )


class Migration(migrations.Migration):
    dependencies = [
        ("students", "0009_student_guardian_phone"),
        ("attendance", "0022_alter_scheduleenrollment_created_from_traininggroup_and_more"),
        ("billing", "0033_paymentreturnstate"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="student",
            name="last_name",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="student",
            name="became_student_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="student",
            name="crm_entered_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.SET_NULL,
                related_name="crm_entered_students",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="student",
            name="crm_entry_kind",
            field=models.CharField(
                choices=[
                    ("legacy_unknown", "Legacy unknown"),
                    ("lead_intake", "Lead intake"),
                    ("existing_student", "Existing student"),
                ],
                db_index=True,
                default="legacy_unknown",
                max_length=32,
            ),
        ),
        migrations.CreateModel(
            name="StudentIntakeCommand",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("idempotency_key", models.UUIDField()),
                ("request_fingerprint", models.CharField(max_length=64)),
                (
                    "intake_kind",
                    models.CharField(
                        choices=[
                            ("new_contact", "New contact"),
                            ("existing_student", "Existing student"),
                        ],
                        max_length=32,
                    ),
                ),
                ("result_kind", models.CharField(max_length=64)),
                ("result_receipt", models.JSONField(default=dict)),
                (
                    "club",
                    models.ForeignKey(
                        on_delete=models.PROTECT,
                        related_name="%(class)ss",
                        to="clubs.club",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=models.PROTECT,
                        related_name="student_intake_commands",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "student",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.PROTECT,
                        related_name="intake_commands",
                        to="students.student",
                    ),
                ),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.CreateModel(
            name="StudentProvenanceBackfillReceipt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "evidence_type",
                    models.CharField(
                        choices=[
                            ("confirmed_payment", "Confirmed payment"),
                            ("subscription", "Subscription"),
                            ("canonical_membership", "Canonical membership"),
                            ("qualifying_checkin", "Qualifying check-in"),
                        ],
                        max_length=32,
                    ),
                ),
                ("evidence_id", models.PositiveBigIntegerField()),
                ("became_student_at", models.DateTimeField()),
                (
                    "club",
                    models.ForeignKey(
                        on_delete=models.PROTECT,
                        related_name="%(class)ss",
                        to="clubs.club",
                    ),
                ),
                (
                    "student",
                    models.OneToOneField(
                        on_delete=models.PROTECT,
                        related_name="provenance_backfill_receipt",
                        to="students.student",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="studentintakecommand",
            index=models.Index(fields=["club", "created_at"], name="students_si_club_id_3e302f_idx"),
        ),
        migrations.AddIndex(
            model_name="studentintakecommand",
            index=models.Index(fields=["club", "student", "created_at"], name="students_si_club_id_6969d5_idx"),
        ),
        migrations.AddConstraint(
            model_name="studentintakecommand",
            constraint=models.UniqueConstraint(
                fields=("club", "idempotency_key"),
                name="unique_student_intake_command_key_per_club",
            ),
        ),
        migrations.AddIndex(
            model_name="studentprovenancebackfillreceipt",
            index=models.Index(fields=["club", "evidence_type"], name="students_sp_club_id_a3c3c8_idx"),
        ),
        migrations.AddIndex(
            model_name="studentprovenancebackfillreceipt",
            index=models.Index(fields=["club", "created_at"], name="students_sp_club_id_35c97f_idx"),
        ),
        migrations.RunPython(backfill_became_student_at, migrations.RunPython.noop),
    ]
