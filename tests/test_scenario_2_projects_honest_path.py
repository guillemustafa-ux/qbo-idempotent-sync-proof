"""Scenario 2 -- QBO Projects are tier-gated, and a sub-customer is not a Project.

The Projects API is GraphQL and available only to Silver-or-above partners
of the Intuit App Partner Program. A private internal app is a Builder-tier
app, and gets a 403. What the sync does at that moment is the difference
between an honest integration and one that quietly books every wedding as a
sub-customer while the dashboard says "Project".
"""

from __future__ import annotations

import pytest

from qbo_idempotent_sync_proof.client import UnknownOutcome
from qbo_idempotent_sync_proof.fake_qbo import ROUTE_CUSTOMER
from qbo_idempotent_sync_proof.ledger import (
    PENDING,
    PROJECT_MODE_FALLBACK,
    PROJECT_MODE_GRAPHQL,
    STEP_PROJECT,
    SYNC_ERROR,
    SYNC_SYNCED,
)
from qbo_idempotent_sync_proof.sync import UnresolvableStep, project_name_for


def test_builder_tier_falls_back_to_a_sub_customer_and_says_so(
    qbo, engine_factory, client_factory, make_webhook
):
    """The honest path on a 403.

    The wedding still gets a container for its invoice and payments -- a
    REST sub-customer -- and the ledger row says exactly what it is. Nobody
    reads ``IsProject: false`` off a report six months later.
    """
    engine = engine_factory(qbo)
    deal = engine.apply(make_webhook())

    assert deal.project_mode == PROJECT_MODE_FALLBACK
    assert deal.qbo_project_id is not None
    assert deal.sync_status == SYNC_SYNCED

    sub = client_factory(qbo).get_customer(deal.qbo_project_id)
    assert sub["Job"] is True
    assert sub["ParentRef"] == {"value": deal.qbo_customer_id}
    assert sub["IsProject"] is False, "sent as True on the create; read-only, so ignored"
    assert qbo.projects == {}, "nothing appears in the Projects tab"

    assert len(engine.notices) == 1 and "sub-customer" in engine.notices[0]
    assert any("-> 403" in r for r in qbo.request_log), "GraphQL was tried first"


def test_silver_tier_creates_a_real_project_over_graphql(silver_qbo, engine_factory, make_webhook):
    """The same code, an eligible app: a real Project, and the ledger says so."""
    engine = engine_factory(silver_qbo)
    deal = engine.apply(make_webhook())

    assert deal.project_mode == PROJECT_MODE_GRAPHQL
    assert deal.qbo_project_id in silver_qbo.projects
    assert silver_qbo.projects[deal.qbo_project_id]["IsProject"] is True
    assert silver_qbo.projects[deal.qbo_project_id]["DisplayName"] == project_name_for(deal)
    assert engine.notices == []
    assert len(silver_qbo.customers) == 1, "no sub-customer was created alongside"


def test_crash_between_create_project_and_persist_adopts_by_planted_name(
    silver_qbo, ledger_factory, engine_factory, run_child, make_webhook
):
    """GraphQL has no requestid, so the question has to be asked another way.

    The child is killed after the mutation returns and before the id is
    written. Recovery looks for the name it planted under the customer,
    finds exactly one, and adopts it. No second project.
    """
    deal_id = ledger_factory().record_webhook(make_webhook())

    run_child(silver_qbo, deal_id, crash_at="after_project_response")

    killed = ledger_factory()
    intent = killed.get_intent(deal_id, STEP_PROJECT)
    assert intent is not None and intent.state == PENDING
    assert intent.mode == PROJECT_MODE_GRAPHQL
    assert killed.get_deal(deal_id).qbo_project_id is None
    assert len(silver_qbo.projects) == 1

    deal = engine_factory(silver_qbo).recover_all()[0]

    assert len(silver_qbo.projects) == 1, "adopted, not recreated"
    assert deal.qbo_project_id == next(iter(silver_qbo.projects))
    assert deal.project_mode == PROJECT_MODE_GRAPHQL
    assert deal.sync_status == SYNC_SYNCED
    creates = [r for r in silver_qbo.request_log if r.endswith("projectManagementCreateProject")]
    assert len(creates) == 1, "the lookup settled it; no second mutation was sent"


def test_two_projects_with_the_same_planted_name_escalate_instead_of_guessing(
    silver_qbo, ledger_factory, engine_factory, run_child, make_webhook
):
    """The honest failure.

    A second project with our name already exists under the customer --
    someone created it by hand, or an earlier bug did. Picking either one is
    a guess with an invoice attached to it. The sync stops, records why, and
    leaves both projects exactly as they were.
    """
    deal_id = ledger_factory().record_webhook(make_webhook())
    run_child(silver_qbo, deal_id, crash_at="after_project_response")

    deal = ledger_factory().get_deal(deal_id)
    silver_qbo.plant_project(deal.qbo_customer_id, project_name_for(deal))
    assert len(silver_qbo.projects) == 2

    with pytest.raises(UnresolvableStep) as exc:
        engine_factory(silver_qbo).recover_all()

    assert exc.value.step == STEP_PROJECT
    assert "2 projects" in exc.value.detail
    after = ledger_factory().get_deal(deal_id)
    assert after.sync_status == SYNC_ERROR and "refusing to pick one" in after.last_error
    assert after.qbo_project_id is None
    assert len(silver_qbo.projects) == 2, "still two -- we refused to make it three"
    assert silver_qbo.invoices == {}, "and nothing downstream was booked against a guess"


def test_the_fallback_sub_customer_replays_its_requestid_after_a_lost_response(
    qbo, engine_factory, make_webhook
):
    """The fallback is REST, so it gets the REST guarantee.

    The response to the sub-customer create is lost after QBO commits.
    Recovery resends the same requestid and gets the original back.
    """
    webhook = make_webhook()
    engine = engine_factory(qbo)
    # The first customer create must succeed; the fault is for the second
    # customer-shaped create, which is the sub-customer.
    engine._ensure_customer(engine.ledger.get_deal(engine.ledger.record_webhook(webhook)))
    qbo.drop_response_after_commit(ROUTE_CUSTOMER)

    with pytest.raises(UnknownOutcome):
        engine.sync(webhook["deal_id"])
    assert len(qbo.sub_customers) == 1, "committed; the caller was never told"

    deal = engine_factory(qbo).sync(webhook["deal_id"])

    assert len(qbo.sub_customers) == 1, "the replay was recognised"
    assert deal.qbo_project_id == next(iter(qbo.sub_customers))
    assert deal.project_mode == PROJECT_MODE_FALLBACK
    assert deal.sync_status == SYNC_SYNCED
