from django.db import migrations, models
from django.db.models import Q

import apps.students.models


class Migration(migrations.Migration):
    dependencies = [
        ("students", "0008_backfill_lead_status_constraint"),
    ]

    operations = [
        migrations.AddField(
            model_name="student",
            name="guardian_phone",
            field=models.CharField(
                blank=True,
                default="",
                max_length=20,
                validators=[apps.students.models.phone_validator],
            ),
        ),
        migrations.AlterField(
            model_name="student",
            name="phone",
            field=models.CharField(
                blank=True,
                default="",
                max_length=20,
                validators=[apps.students.models.phone_validator],
            ),
        ),
        migrations.RemoveConstraint(
            model_name="student",
            name="unique_student_phone_per_club",
        ),
        migrations.AddIndex(
            model_name="student",
            index=models.Index(fields=["club", "guardian_phone"], name="students_st_club_id_c8e72e_idx"),
        ),
        migrations.AddConstraint(
            model_name="student",
            constraint=models.UniqueConstraint(
                condition=Q(deleted_at__isnull=True) & ~Q(phone=""),
                fields=("club", "phone"),
                name="unique_student_phone_per_club",
            ),
        ),
        migrations.AddConstraint(
            model_name="student",
            constraint=models.CheckConstraint(
                condition=Q(is_child=True) | ~Q(phone=""),
                name="student_adult_requires_phone",
            ),
        ),
        migrations.AddConstraint(
            model_name="student",
            constraint=models.CheckConstraint(
                condition=Q(is_child=False) | ~Q(phone="") | ~Q(guardian_phone=""),
                name="student_child_requires_contact",
            ),
        ),
    ]
