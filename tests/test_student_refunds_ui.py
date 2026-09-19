from decimal import Decimal

import pytest

from apps.billing.models import PaymentRefund
from apps.billing.tests.test_manual_refunds import manual
from apps.students.tests.factories import StudentFactory

pytestmark = pytest.mark.django_db
manual = manual


def test_card_refund_form_retains_error_replays_and_keeps_payment(manual, client, owner_user):
    payment, args = manual
    client.force_login(owner_user)
    url = f"/dashboard/students/{payment.student_id}/payments/{payment.id}/refund/"
    response = client.get(url)
    assert response.status_code == 200 and "Можно вернуть".encode() in response.content
    data = dict(
        amount="200",
        accounting_date=args["accounting_date"].isoformat(),
        reason="Возврат по заявлению",
        idempotency_key="ui-refund",
        entitlement_action="revoke_remaining",
    )
    response = client.post(url, data)
    assert "Частичный возврат".encode() in response.content and data["reason"].encode() in response.content
    assert not PaymentRefund.objects.exists()
    data["entitlement_action"] = "kept_partial"
    response = client.post(url, data)
    assert response.status_code == 200 and "Возврат записан".encode() in response.content
    client.post(url, data)
    assert PaymentRefund.objects.count() == 1
    payment.refresh_from_db()
    assert payment.amount == Decimal("1000")
    foreign = StudentFactory()
    assert client.get(f"/dashboard/students/{foreign.id}/payments/{payment.id}/refund/").status_code == 404
