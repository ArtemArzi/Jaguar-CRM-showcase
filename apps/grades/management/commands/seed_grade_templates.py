from django.core.management.base import BaseCommand, CommandError

from apps.grades.services import GRADE_TEMPLATES, seed_grade_templates


class Command(BaseCommand):
    help = "Seed grade templates for a club (BJJ, Каратэ, Бокс, Тайский бокс)"

    def add_arguments(self, parser):
        parser.add_argument("--club-id", type=int, required=True, help="Club ID to seed templates for")
        parser.add_argument(
            "--disciplines",
            type=str,
            default="",
            help="Comma-separated discipline names (e.g. BJJ,Каратэ,Бокс)",
        )
        parser.add_argument("--all", action="store_true", help="Seed all supported disciplines")

    def handle(self, *args, **options):
        club_id = options["club_id"]

        if options["all"]:
            disciplines = list(GRADE_TEMPLATES.keys())
        elif options["disciplines"]:
            disciplines = [d.strip() for d in options["disciplines"].split(",")]
        else:
            raise CommandError("Specify --disciplines or --all")

        created = seed_grade_templates(club_id=club_id, disciplines=disciplines)

        for gs in created:
            grade_count = gs.grades.count()
            self.stdout.write(
                self.style.SUCCESS(
                    f"Created GradeSystem '{gs.discipline}' with {grade_count} grades for club {club_id}"
                )
            )

        if not created:
            self.stdout.write("No new grade systems created (already exist).")
