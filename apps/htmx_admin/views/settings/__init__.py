from .billing import (
    discount_form,
    discount_toggle,
    settings_billing_view,
    tariff_archive_view,
    tariff_form,
    tariff_price_revision_form,
    tariff_toggle,
    training_type_form,
    training_type_toggle,
)
from .catalog import (
    grade_delete,
    grade_form,
    grade_system_delete,
    grade_system_form,
    location_delete,
    location_form,
    settings_catalog_view,
)
from .documents import document_type_form, document_type_toggle, settings_documents_view
from .general import settings_general_view, settings_redirect
from .kiosk import kiosk_deactivate, kiosk_generate_pin, settings_kiosk_view
from .notifications import (
    notification_template_form,
    notification_template_toggle,
    settings_notifications_view,
)

__all__ = [
    "settings_redirect",
    "settings_general_view",
    "settings_billing_view",
    "tariff_archive_view",
    "settings_catalog_view",
    "settings_documents_view",
    "settings_notifications_view",
    "settings_kiosk_view",
    "kiosk_generate_pin",
    "kiosk_deactivate",
    "notification_template_form",
    "notification_template_toggle",
    "location_form",
    "location_delete",
    "grade_system_form",
    "grade_system_delete",
    "grade_form",
    "grade_delete",
    "document_type_form",
    "document_type_toggle",
    "training_type_form",
    "training_type_toggle",
    "tariff_form",
    "tariff_price_revision_form",
    "tariff_toggle",
    "discount_form",
    "discount_toggle",
]
