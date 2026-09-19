"""Trusted actor-scoped adapter for the same private import services as HTMX."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.common.exceptions import BusinessLogicError
from apps.students.imports.runner import accept_batch, run_batch_chunk
from apps.students.imports.selectors import batch_status
from apps.students.imports.services import create_template, export_result, prepare_batch, validate_batch
from apps.students.imports.storage import private_path, save_private


class Command(BaseCommand):
    help = "Подготовка, проверка и применение приватной партии начальных состояний учеников."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["template", "prepare", "validate", "apply", "status", "export-result"])
        parser.add_argument("--club-id", type=int, required=True)
        parser.add_argument("--actor-user-id", type=int, required=True)
        parser.add_argument("--file", type=Path)
        parser.add_argument("--batch-id", type=int)
        parser.add_argument("--revision", type=int)
        parser.add_argument("--selection-token")
        parser.add_argument("--item-id", type=int, action="append", dest="item_ids")
        parser.add_argument("--wait", action="store_true", help="Применить через тот же runner до результата.")

    def handle(self, *args, **options):
        club_id, actor_user_id = options["club_id"], options["actor_user_id"]
        scope = {"club_id": club_id, "actor_user_id": actor_user_id}
        action = options["action"]
        try:
            if action == "template":
                result = {"status": "prepared", "private_result": str(private_path(name=create_template(**scope)))}
            elif action == "prepare":
                if options["file"] is None:
                    raise CommandError("Укажите --file.")
                batch = prepare_batch(**scope, path=options["file"])
                result = batch_status(**scope, batch_id=batch.id)
                result["private_result"] = str(private_path(name=batch.prepared_file))
            else:
                batch_id = options["batch_id"]
                if batch_id is None:
                    raise CommandError("Укажите --batch-id.")
                if action == "validate":
                    preview = validate_batch(**scope, batch_id=batch_id, selected_item_ids=options["item_ids"])
                    name = save_private(content=json.dumps({
                        "batch_id": batch_id, "revision": preview.revision,
                        "selection_token": str(preview.selection_token), "selection": preview.selection,
                    }, ensure_ascii=False).encode(), suffix="json")
                    result = batch_status(**scope, batch_id=batch_id)
                    result["private_result"] = str(private_path(name=name))
                elif action == "apply":
                    if options["revision"] is None or options["selection_token"] is None:
                        raise CommandError("Укажите --revision и --selection-token из принятой проверки.")
                    accept_batch(**scope, batch_id=batch_id, revision=options["revision"],
                                 selection_token=options["selection_token"], channel="assistant_cli")
                    if options["wait"]:
                        while True:
                            result = run_batch_chunk(club_id=club_id, batch_id=batch_id)
                            if result["status"] != "applying":
                                break
                    else:
                        from django_q.tasks import async_task

                        async_task("apps.students.imports.runner.run_batch_worker", club_id=club_id, batch_id=batch_id)
                        result = batch_status(**scope, batch_id=batch_id)
                elif action == "export-result":
                    result = batch_status(**scope, batch_id=batch_id)
                    result["private_result"] = str(private_path(name=export_result(**scope, batch_id=batch_id)))
                else:
                    result = batch_status(**scope, batch_id=batch_id)
        except BusinessLogicError as exc:
            # No row values, identities, source filenames or stacktraces in stdout.
            raise CommandError(exc.code) from None
        except (OSError, ValueError):
            raise CommandError("import_input_unavailable") from None
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
