"""Scenario 1 -- the gap between "QBO created the customer" and "we wrote down the Id".

The process is killed for real (``os._exit`` in a child process, exit 137)
at the exact instruction between the remote commit and the local one. What
is on disk afterwards is all the next run gets.
"""

from __future__ import annotations

import pytest

from qbo_idempotent_sync_proof.client import UnknownOutcome
from qbo_idempotent_sync_proof.fake_qbo import ROUTE_CUSTOMER
from qbo_idempotent_sync_proof.ledger import (
    PENDING,
    REQUESTID_MAX_LEN,
    STEP_CUSTOMER,
    SYNC_SYNCED,
    display_name_for,
    requestid_for,
)
from qbo_idempotent_sync_proof.sync import SyncEngine, naive_customer_sync


def test_naive_retry_after_a_real_kill_makes_a_second_customer(
    qbo, ledger_factory, client_factory, run_child, make_webhook
):
    """The anti-pattern, demonstrated rather than asserted.

    QBO commits the customer, the response is lost, the worker dies before
    writing anything down. The retry sends the same DisplayName, gets a
    ``6240``, and handles it the instinctive way: append a number.
    """
    webhook = make_webhook()
    deal_id = ledger_factory().record_webhook(webhook)

    qbo.drop_response_after_commit(ROUTE_CUSTOMER)
    run_child(qbo, deal_id, crash_at="-", strategy="naive-customer")

    # QBO has the customer. The ledger has no idea.
    assert len(qbo.customers) == 1
    assert ledger_factory().get_deal(deal_id).qbo_customer_id is None

    # The retry, behaving exactly as written.
    naive_customer_sync(ledger_factory(), client_factory(qbo), deal_id)

    names = sorted(c["DisplayName"] for c in qbo.customers.values())
    assert names == ["Ana Perez & Tomas Ruiz", "Ana Perez & Tomas Ruiz (2)"], (
        "this is the bug this repo exists to prevent: one couple, two customers"
    )


def test_durable_sync_survives_a_real_kill_between_200_and_persist(
    qbo, ledger_factory, engine_factory, run_child, make_webhook
):
    """The same kill, against the design under test.

    What happens if the process dies after QBO creates the customer but
    before you save its Id?
    """
    deal_id = ledger_factory().record_webhook(make_webhook())

    # The remote call succeeds; the worker is killed on the next instruction.
    run_child(qbo, deal_id, crash_at="after_customer_response")

    killed = ledger_factory()
    intent = killed.get_intent(deal_id, STEP_CUSTOMER, "0")
    assert intent is not None and intent.state == PENDING, "the intent outlived the process"
    assert killed.get_deal(deal_id).qbo_customer_id is None, "the Id was never written"
    assert len(qbo.customers) == 1

    # A brand new process, with nothing but the database.
    recovered = engine_factory(qbo).recover_all()

    assert len(qbo.customers) == 1, "recovery must not create a second customer"
    assert len(recovered) == 1
    assert recovered[0].qbo_customer_id == next(iter(qbo.customers))
    assert recovered[0].sync_status == SYNC_SYNCED

    posts = [r for r in qbo.request_log if r == f"POST /customer requestid={intent.requestid}"]
    assert len(posts) == 2, "two creates were sent under the same requestid..."
    assert len(qbo.customers) == 1, "...and the second one was recognised as a replay"


def test_lost_response_is_resolved_without_duplicating(qbo, engine_factory, make_webhook):
    """Same hazard, no crash: the answer is lost in the network.

    The process is alive and still cannot tell whether the write landed.
    """
    webhook = make_webhook()
    engine = engine_factory(qbo)

    qbo.drop_response_after_commit(ROUTE_CUSTOMER)
    with pytest.raises(UnknownOutcome):
        engine.apply(webhook)

    assert len(qbo.customers) == 1, "QBO committed; we simply were not told"
    assert engine.ledger.get_deal(webhook["deal_id"]).qbo_customer_id is None

    deal = engine_factory(qbo).apply(webhook)

    assert len(qbo.customers) == 1
    assert deal.qbo_customer_id == next(iter(qbo.customers))
    assert deal.sync_status == SYNC_SYNCED


def test_a_create_that_never_landed_is_actually_sent(qbo, engine_factory, make_webhook):
    """The mirror case, and the reason "do nothing on doubt" is not the answer.

    The connection died *before* QBO committed. Recovery must create the
    customer, not adopt a customer that does not exist.
    """
    webhook = make_webhook()

    qbo.drop_response_before_commit(ROUTE_CUSTOMER)
    with pytest.raises(UnknownOutcome):
        engine_factory(qbo).apply(webhook)

    assert qbo.customers == {}, "nothing was created"

    deal = engine_factory(qbo).apply(webhook)

    assert len(qbo.customers) == 1, "recovery created the customer that was missing"
    assert deal.sync_status == SYNC_SYNCED


def test_two_deals_with_the_same_names_stay_two_customers(qbo, engine_factory, make_webhook):
    """Why the 6240 handler must not merge.

    Two couples can share both names. QBO refuses the second DisplayName,
    and the sync answers with a *deterministic* variant -- the wedding date
    -- rather than adopting a stranger's customer or minting "(2)".
    """
    first = make_webhook(deal_id="deal-1", wedding_date="2027-03-14", payment_id="pay-1")
    second = make_webhook(deal_id="deal-2", wedding_date="2027-11-20", payment_id="pay-2")

    engine = engine_factory(qbo)
    a = engine.apply(first)
    b = engine.apply(second)

    assert a.qbo_customer_display_name == "Ana Perez & Tomas Ruiz"
    assert b.qbo_customer_display_name == "Ana Perez & Tomas Ruiz (2027-11-20)"
    assert a.qbo_customer_id != b.qbo_customer_id
    assert len(qbo.customers) == 2, "two real couples stayed two customers"
    assert len(qbo.invoices) == 2 and len(qbo.payments) == 2

    # And the variant is stable: a fresh process re-derives the same name.
    again = engine_factory(qbo).apply(second)
    assert again.qbo_customer_id == b.qbo_customer_id
    assert len(qbo.customers) == 2


def test_a_display_name_with_an_apostrophe_is_escaped_in_the_query(
    qbo, engine_factory, make_webhook
):
    """QBO's query language escapes ``'`` as ``\\'``. Get it wrong and the
    collision lookup returns a parse error instead of the holder."""
    stranger = make_webhook(deal_id="deal-x", couple_names=("Niamh O'Brien", "Sean D'Arcy"))
    ours = make_webhook(
        deal_id="deal-y", couple_names=("Niamh O'Brien", "Sean D'Arcy"), wedding_date="2027-06-06"
    )
    engine = engine_factory(qbo)
    engine.apply(stranger)
    deal = engine.apply(ours)

    assert deal.qbo_customer_display_name == "Niamh O'Brien & Sean D'Arcy (2027-06-06)"
    queries = [r for r in qbo.request_log if r.startswith("GET /query")]
    assert any("Niamh O\\'Brien & Sean D\\'Arcy" in q for q in queries), queries


def test_replaying_a_requestid_with_a_corrected_body_returns_the_old_customer(
    qbo, client_factory
):
    """The quiet one.

    A requestid means "this is the same request", so QBO returns the
    *original* response and ignores the body. Fix a typo in the name, retry
    under the same requestid, and the fix is silently discarded -- with a
    200 and a customer Id, so nothing looks wrong.
    """
    client = client_factory(qbo)
    requestid = requestid_for("deal-typo", "customer", "0")

    created = client.create_customer({"DisplayName": "Ana Perez & Tomas Ruis"}, requestid=requestid)
    replayed = client.create_customer(
        {"DisplayName": "Ana Perez & Tomas Ruiz"}, requestid=requestid
    )

    assert replayed.entity["Id"] == created.entity["Id"]
    assert replayed.entity["DisplayName"] == "Ana Perez & Tomas Ruis", (
        "the corrected name was accepted and thrown away"
    )
    assert replayed.replayed is True
    assert len(qbo.customers) == 1


def test_a_rebuilt_ledger_re_derives_every_requestid_and_creates_nothing(
    qbo, engine_factory, make_webhook, tmp_path
):
    """Why the requestid is derived and not merely persisted.

    Lose the ledger -- a restore from an old backup, a rebuild from the
    webhook log -- and replay the webhook against an empty database. A
    process with no memory of the first run computes the same requestids,
    QBO replays the same responses, and nothing is created twice.
    """
    webhook = make_webhook()
    first = engine_factory(qbo).apply(webhook)
    assert (len(qbo.customers), len(qbo.invoices), len(qbo.payments)) == (1, 1, 1)

    rebuilt = engine_factory(qbo, path=str(tmp_path / "rebuilt.db")).apply(webhook)

    assert (len(qbo.customers), len(qbo.invoices), len(qbo.payments)) == (1, 1, 1)
    assert rebuilt.qbo_customer_id == first.qbo_customer_id
    assert rebuilt.qbo_invoice_id == first.qbo_invoice_id
    assert rebuilt.amount_paid == 2500.0 and rebuilt.amount_owed == 10000.0
    assert len(requestid_for(webhook["deal_id"], "payment", "pay-1")) <= REQUESTID_MAX_LEN


def test_when_the_server_forgot_the_requestid_the_planted_marker_still_adopts(
    qbo, engine_factory, make_webhook, tmp_path
):
    """Belt and braces for the customer step.

    How long QBO keeps a requestid is not documented. If it has forgotten it,
    a resend earns a ``6240`` -- and the holder of that name carries the
    marker we planted, so it is adopted rather than suffixed or duplicated.
    """
    webhook = make_webhook()
    first = engine_factory(qbo).apply(webhook)

    qbo.forget_requestids()
    rebuilt = engine_factory(qbo, path=str(tmp_path / "rebuilt.db"))
    deal_id = rebuilt.ledger.record_webhook(webhook)
    # Only the customer step is under test: invoices and payments have no
    # name to collide on, so with the replay window gone they have nothing
    # to be adopted by. That limit is stated in the README, not hidden here.
    rebuilt._ensure_customer(rebuilt.ledger.get_deal(deal_id))

    assert rebuilt.ledger.get_deal(deal_id).qbo_customer_id == first.qbo_customer_id
    assert len(qbo.customers) == 1, "adopted by marker, not recreated with a suffix"
    assert display_name_for(webhook["couple_names"], webhook["wedding_date"]) == "Ana Perez & Tomas Ruiz"
