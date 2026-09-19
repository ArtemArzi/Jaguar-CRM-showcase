# Generated manually for P1 lead/student create-flow unification.

from django.db import migrations, models
from django.db.models import Q


def backfill_lead_status(apps, schema_editor):
    Student = apps.get_model("students", "Student")
    Student.objects.filter(status="lead", lead_status__isnull=True).update(lead_status="new")


class Migration(migrations.Migration):

    dependencies = [
        ("students", "0007_accountaccess"),
    ]

    operations = [
        migrations.RunPython(backfill_lead_status, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="student",
            constraint=models.CheckConstraint(
                condition=~Q(status="lead") | Q(lead_status__isnull=False),
                name="student_lead_requires_lead_status",
            ),
        ),
    ]
