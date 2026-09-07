"""Scenario 4 -- payments are where duplicates cost money, and the ledger is what the business reads.

Webhooks arrive twice, arrive late, and arrive out of order. Every payment
must land on the same customer, project and invoice exactly once, and the
operational record must state the balance rather than count it.
"""

from __future__ import annotations

import sqlite3

import pytest

from qbo_idempotent_sync_proof.fake_qbo import ROUTE_PAYMENT
from qbo_idempotent_sync_proof.ledger import PENDING, STEP_PAYMENT, SYNC_SYNCED
from qbo_idempotent_sync_proof.sync import naive_payment_sync


def test_duplicate_webhook_delivery_records_one_payment(qbo, engine_factory, make_webhook):
    """HubSpot delivers the same webhook twice. QBO must see one payment."""
    webhook = make_webhook(amount=2500.0)
    engine = engine_factory(qbo)

    first = engine.apply(webhook)
    second = engine_factory(qbo).apply(webhook)  # a different worker, same delivery

    assert len(qbo.payments) == 1
    assert first.amount_paid == second.amount_paid == 2500.0
    assert second.amount_owed == 10000.0
    assert qbo.invoices[second.qbo_invoice_id]["Balance"] == 10000.0


def test_a_second_payment_hits_the_same_customer_project_and_invoice(
    qbo, engine_factory, make_webhook
):
    """Later payments on the same deal reuse everything and add one Payment."""
    engine = engine_factory(qbo)
    first = engine.apply(make_webhook(payment_id="pay-1", amount=2500.0, paid_at="2026-09-01T10:00:00Z"))
    second = engine.apply(make_webhook(payment_id="pay-2", amount=4000.0, paid_at="2026-10-01T10:00:00Z"))

    assert (second.qbo_customer_id, second.qbo_project_id, second.qbo_invoice_id) == (
        first.qbo_customer_id,
        first.qbo_project_id,
        first.qbo_invoice_id,
    )
    assert len(qbo.customers) == 1 and len(qbo.sub_customers) == 1, "nothing else was created"
    assert len(qbo.invoices) == 1
    assert len(qbo.payments) == 2
    assert second.amount_paid == 6500.0
    assert second.amount_owed == 6000.0, "read back from the invoice, not added up locally"
    assert second.last_payment_at == "2026-10-01T10:00:00Z"
    assert second.sync_status == SYNC_SYNCED


def test_out_of_order_delivery_ends_in_the_same_state(qbo, engine_factory, make_webhook):
    """The second payment's webhook arrives first, then the first, then both again."""
    pay1 = make_webhook(payment_id="pay-1", amount=2500.0, paid_at="2026-09-01T10:00:00Z")
    pay2 = make_webhook(payment_id="pay-2", amount=4000.0, paid_at="2026-10-01T10:00:00Z")

    engine = engine_factory(qbo)
    for webhook in (pay2, pay1, pay2, pay1):
        deal = engine.apply(webhook)

    assert len(qbo.payments) == 2
    assert deal.amount_paid == 6500.0 and deal.amount_owed == 6000.0
    assert deal.last_payment_at == "2026-10-01T10:00:00Z"
    ids = {p.qbo_payment_id for p in engine.ledger.payments_for(deal.deal_id)}
    assert ids == set(qbo.payments), "each HubSpot payment maps to exactly one QBO payment"


def test_naive_payment_retry_after_a_real_kill_pays_the_invoice_twice(
    qbo, engine_factory, ledger_factory, client_factory, run_child, make_webhook
):
    """The anti-pattern where it costs money.

    Customer, project and invoice exist. The payment create commits, the
    response is lost, the worker dies. The retry has no requestid and no
    record of the attempt, and there is no name to collide on -- so the
    invoice is simply paid twice.
    """
    webhook = make_webhook(amount=2500.0)
    engine = engine_factory(qbo)
    deal_id = engine.ledger.record_webhook(webhook)
    deal = engine.ledger.get_deal(deal_id)
    engine._ensure_customer(deal)
    engine._ensure_project(engine.ledger.get_deal(deal_id))
    engine._ensure_invoice(engine.ledger.get_deal(deal_id))

    qbo.drop_response_after_commit(ROUTE_PAYMENT)
    run_child(qbo, deal_id, crash_at="-", strategy="naive-payment", payment_id="pay-1")
    assert len(qbo.payments) == 1

    naive_payment_sync(ledger_factory(), client_factory(qbo), deal_id, "pay-1")

    invoice = qbo.invoices[engine.ledger.get_deal(deal_id).qbo_invoice_id]
    assert len(qbo.payments) == 2, "one HubSpot payment, two QBO payments"
    assert invoice["Balance"] == 12500.0 - 5000.0, "the books now say the couple paid twice"


def test_a_payment_survives_a_real_kill_between_200_and_persist(
    qbo, ledger_factory, engine_factory, run_child, make_webhook
):
    """The same kill, against the design under test. One payment, one Id."""
    deal_id = ledger_factory().record_webhook(make_webhook(amount=2500.0))

    run_child(qbo, deal_id, crash_at="after_payment_response")

    killed = ledger_factory()
    intent = killed.get_intent(deal_id, STEP_PAYMENT, "pay-1")
    assert intent is not None and intent.state == PENDING
    assert killed.payments_for(deal_id)[0].qbo_payment_id is None
    assert len(qbo.payments) == 1

    deal = engine_factory(qbo).recover_all()[0]

    assert len(qbo.payments) == 1, "recovery must not book the payment twice"
    assert deal.amount_paid == 2500.0 and deal.amount_owed == 10000.0
    assert deal.sync_status == SYNC_SYNCED
    posts = [r for r in qbo.request_log if r.startswith("POST /payment ")]
    assert len(posts) == 2 and posts[0] == posts[1], "same requestid on the wire, twice"


def test_the_schema_refuses_a_second_binding(ledger_factory, make_webhook):
    """A last line of defence that does not depend on the code being right.

    One QBO customer, invoice or payment binds to at most one local thing.
    If a future change ever tries to bind a second, the database rejects it
    rather than letting the duplicate become someone's bookkeeping problem.
    """
    ledger = ledger_factory()
    a = ledger.record_webhook(make_webhook(deal_id="deal-a", payment_id="pay-a"))
    b = ledger.record_webhook(make_webhook(deal_id="deal-b", payment_id="pay-b"))

    ledger.bind_customer(a, "41", "Ana Perez & Tomas Ruiz")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.bind_customer(b, "41", "Ana Perez & Tomas Ruiz")

    ledger.bind_invoice(a, "77")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.bind_invoice(b, "77")

    ledger.bind_payment(a, "pay-a", "90")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.bind_payment(b, "pay-b", "90")


def test_apply_is_safe_to_call_repeatedly(qbo, engine_factory, make_webhook):
    """Idempotence is a property of the operation, not of a retry budget."""
    webhook = make_webhook(amount=2500.0)
    engine = engine_factory(qbo)

    first = engine.apply(webhook)
    for _ in range(5):
        again = engine.apply(webhook)
        assert again.qbo_invoice_id == first.qbo_invoice_id
        assert again.amount_paid == 2500.0
        assert again.amount_owed == 10000.0

    assert (len(qbo.customers), len(qbo.sub_customers), len(qbo.invoices), len(qbo.payments)) == (
        1,
        1,
        1,
        1,
    )
