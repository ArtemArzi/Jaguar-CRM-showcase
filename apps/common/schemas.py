from collections.abc import Iterable

from ninja import Schema


class ErrorResponse(Schema):
    detail: str
    code: str = "error"


def schema_sent_fields(schema: Schema, allowed_fields: Iterable[str]) -> dict[str, object]:
    fields_set = getattr(schema, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(schema, "__fields_set__", set())
    return {field: getattr(schema, field) for field in allowed_fields if field in fields_set}
