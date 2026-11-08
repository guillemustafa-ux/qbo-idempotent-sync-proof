"""The sync under test: one "deal paid" webhook, four idempotent writes.

One entry point, ``apply(webhook)``, safe to call any number of times, from
any process, in any order of delivery. It decides what is left to do from
durable state, never from memory, and it refuses to guess when QBO gives it
no way to know.

The recovery strategy per step is dictated by what QBO offers for it:

==============================  ============================================
The step                        How an in-flight write is settled
==============================  ============================================
Customer, Invoice, Payment      REST creates take a ``requestid``. Resend
(REST)                          the same one: QBO returns the original
                                response, or performs the create if it
                                truly never landed. One call settles both.
Project (GraphQL)               No requestid. Look the project up by the
                                name we planted under the customer: exactly
                                one -> adopt, zero -> create, more -> stop.
Project, when the app tier      A REST sub-customer (``Job=true``,
cannot use the Projects API     ``ParentRef``) -- with ``requestid`` -- and
                                the ledger says so, in a field the business
                                reads. Never silently.
==============================  ============================================

A sub-customer is not a Project. ``IsProject`` is read-only on the REST
Customer entity and comes back ``false`` however it was sent, so the record
never appears in the Projects tab or in project profitability. The fallback
exists so the money is still tracked; the flag exists so nobody discovers
the difference at the end of the quarter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .client import DUPLICATE_NAME, Created, ProjectsApiUnavailable, QboClient, QboRejected
from .ledger import (
    PROJECT_MODE_FALLBACK,
    PROJECT_MODE_GRAPHQL,
    REJECTED,
    STEP_CUSTOMER,
    STEP_INVOICE,
    STEP_PAYMENT,
    STEP_PROJECT,
    SYNC_ERROR,
    SYNC_SYNCED,
    Deal,
    Intent,
    Ledger,
    Payment,
    display_name_for,
    marker_for,
)

log = logging.getLogger("qbo_sync")

# How many DisplayName variants to try before escalating: the bare names,
# then the names with the wedding date. A third collision is not a naming
# problem, it is a data problem for a human.
MAX_NAME_VARIANTS = 2


class UnresolvableStep(Exception):
    """A step is in flight and QBO offers no safe way to learn its fate.

    Raised instead of guessing. A duplicate customer is a cleanup; a duplicate
    payment is money booked twice. The exception carries the deal and the
    step so a human can settle it with the exact ids.
    """

    def __init__(self, deal_id: str, step: str, detail: str):
        super().__init__(f"deal {deal_id}, step {step}: {detail}")
        self.deal_id = deal_id
        self.step = step
        self.detail = detail


def project_name_for(deal: Deal) -> str:
    """The reference planted on the project: derived, so any process finds it."""
    return f"Wedding {deal.wedding_date} - {deal.qbo_customer_display_name}"


@dataclass
class SyncEngine:
    ledger: Ledger
    client: QboClient
    # Test hook: called at named seams so a scenario can kill the process
    # exactly where it hurts. Production code passes nothing.
    on_seam: Callable[[str], None] | None = None
    # Anything the business must be told, beyond the ledger row itself.
    notices: list[str] = field(default_factory=list)

    def _seam(self, name: str) -> None:
        if self.on_seam is not None:
            self.on_seam(name)

    def _surface(self, message: str) -> None:
        log.warning(message)
        self.notices.append(message)

    # -- public -----------------------------------------------------------

    def apply(self, webhook: dict[str, Any]) -> Deal:
        """Record one webhook delivery and bring its deal fully into QBO.

        Idempotent by construction: a duplicate delivery, a reordered one, or
        a retry after a crash all end in the same state.
        """
        deal_id = self.ledger.record_webhook(webhook)
        return self.sync(deal_id)

    def sync(self, deal_id: str) -> Deal:
        """Complete whatever is left for one deal, in order, skipping finished steps."""
        try:
            deal = self.ledger.get_deal(deal_id)
            if deal.qbo_customer_id is None:
                self._ensure_customer(deal)
            deal = self.ledger.get_deal(deal_id)
            if deal.qbo_project_id is None:
                self._ensure_project(deal)
            deal = self.ledger.get_deal(deal_id)
            if deal.qbo_invoice_id is None:
                self._ensure_invoice(deal)
            deal = self.ledger.get_deal(deal_id)
            for payment in self.ledger.payments_for(deal_id):
                if payment.qbo_payment_id is None:
                    self._ensure_payment(deal, payment)
            self._read_back_balance(deal)
            self.ledger.set_status(deal_id, SYNC_SYNCED)
        except UnresolvableStep as exc:
            self.ledger.set_status(deal_id, SYNC_ERROR, str(exc))
            raise
        return self.ledger.get_deal(deal_id)

    def recover_all(self) -> list[Deal]:
        """Settle every deal left in doubt by a previous run."""
        return [self.sync(d.deal_id) for d in list(self.ledger.unresolved())]

    # -- customer ---------------------------------------------------------

    def _ensure_customer(self, deal: Deal) -> None:
        marker = marker_for(deal.deal_id)
        for variant in range(MAX_NAME_VARIANTS):
            name = display_name_for(deal.couple_names, deal.wedding_date, variant)
            ref = str(variant)
            intent = self.ledger.get_intent(deal.deal_id, STEP_CUSTOMER, ref)
            if intent is not None and intent.state == REJECTED:
                continue  # settled earlier: that name belongs to someone else
            if intent is None:
                intent = self._begin(deal.deal_id, STEP_CUSTOMER, ref)
            outcome = self._create_customer_like(
                intent, {"DisplayName": name, "Notes": marker}, marker
            )
            if outcome is None:
                self.ledger.reject_step(deal.deal_id, STEP_CUSTOMER, ref)
                log.info(
                    "DisplayName %r is taken by a stranger -> variant %d", name, variant + 1
                )
                continue
            self._seam("after_customer_response")
            self.ledger.finish_step(deal.deal_id, STEP_CUSTOMER, ref, outcome["Id"])
            self.ledger.bind_customer(deal.deal_id, outcome["Id"], outcome["DisplayName"])
            return
        raise UnresolvableStep(
            deal.deal_id,
            STEP_CUSTOMER,
            f"every DisplayName variant of {display_name_for(deal.couple_names, deal.wedding_date)!r} "
            "is taken by a customer that does not carry this deal's marker",
        )

    def _create_customer_like(
        self, intent: Intent, body: dict[str, Any], marker: str
    ) -> dict[str, Any] | None:
        """Create (or replay) a Customer-shaped entity under a requestid.

        Returns the entity, or ``None`` when the name is held by a stranger.
        A ``6240`` is a definite answer, so it is safe to act on -- but it is
        not, on its own, proof that the holder is not ours: the planted marker
        settles that.
        """
        try:
            created = self.client.create_customer(body, requestid=intent.requestid)
        except QboRejected as exc:
            if exc.code != DUPLICATE_NAME:
                raise
            holders = self.client.customers_named(body["DisplayName"])
            ours = [c for c in holders if marker in (c.get("Notes") or "")]
            if len(ours) == 1:
                log.info(
                    "resolve %s step=%s -> 6240, holder carries our marker -> ADOPT (Id=%s)",
                    intent.deal_id,
                    intent.step,
                    ours[0]["Id"],
                )
                return ours[0]
            return None
        self._log_verdict(intent, created)
        return created.entity

    # -- project ----------------------------------------------------------

    def _ensure_project(self, deal: Deal) -> None:
        assert deal.qbo_customer_id is not None
        name = project_name_for(deal)
        intent = self.ledger.get_intent(deal.deal_id, STEP_PROJECT)
        project: dict[str, Any] | None
        if intent is None:
            intent = self._begin(deal.deal_id, STEP_PROJECT, mode=PROJECT_MODE_GRAPHQL)
            project = self._create_project_graphql(deal, name)
        elif intent.mode == PROJECT_MODE_GRAPHQL:
            project = self._settle_project_graphql(deal, name)
        else:
            project = None  # a fallback intent in flight: replay it below

        if project is not None:
            mode = PROJECT_MODE_GRAPHQL
            log.info("project step for %s -> GRAPHQL (id=%s)", deal.deal_id, project["id"])
            project_id = project["id"]
        else:
            intent = self.ledger.set_step_mode(
                deal.deal_id, STEP_PROJECT, "", PROJECT_MODE_FALLBACK
            )
            marker = marker_for(deal.deal_id)
            body = {
                "DisplayName": name,
                "Job": True,
                "ParentRef": {"value": deal.qbo_customer_id},
                "Notes": marker,
                # Sent on purpose, to show it is ignored: IsProject is read-only.
                "IsProject": True,
            }
            sub = self._create_customer_like(intent, body, marker)
            if sub is None:
                raise UnresolvableStep(
                    deal.deal_id,
                    STEP_PROJECT,
                    f"sub-customer name {name!r} is held by a stranger",
                )
            mode = PROJECT_MODE_FALLBACK
            project_id = sub["Id"]
            is_project = self.client.get_customer(project_id).get("IsProject")
            log.info(
                "project step for %s -> SUBCUSTOMER FALLBACK (flagged) Id=%s IsProject=%s",
                deal.deal_id,
                project_id,
                is_project,
            )
            if mode == PROJECT_MODE_FALLBACK:
                self._surface(
                    f"deal {deal.deal_id}: the Projects API is not available to this app "
                    f"(tier), so {name!r} was created as a sub-customer (Id={project_id}, "
                    f"IsProject={is_project}). It will NOT appear in the Projects tab."
                )

        self._seam("after_project_response")
        self.ledger.finish_step(deal.deal_id, STEP_PROJECT, "", project_id)
        self.ledger.bind_project(deal.deal_id, project_id, mode)

    def _create_project_graphql(self, deal: Deal, name: str) -> dict[str, Any] | None:
        try:
            project = self.client.create_project(deal.qbo_customer_id, name)
        except ProjectsApiUnavailable as exc:
            log.info("Projects API for %s -> 403 (%s)", deal.deal_id, exc.message or "not eligible")
            return None
        log.info("resolve %s step=project -> CREATED NOW (id=%s)", deal.deal_id, project["id"])
        return project

    def _settle_project_graphql(self, deal: Deal, name: str) -> dict[str, Any] | None:
        """Settle a GraphQL create whose outcome was never observed.

        There is no requestid here, so the question has to be asked
        separately: which projects carry the name we planted, under this
        customer? Exactly one is ours. Zero means it never landed. More than
        one means somebody already duplicated, and picking one is a guess.
        """
        try:
            found = self.client.find_projects(deal.qbo_customer_id, name)
        except ProjectsApiUnavailable:
            # A definite answer: this app cannot have created one.
            return None
        if len(found) > 1:
            raise UnresolvableStep(
                deal.deal_id,
                STEP_PROJECT,
                f"{len(found)} projects named {name!r} exist under customer "
                f"{deal.qbo_customer_id}; refusing to pick one",
            )
        if found:
            log.info(
                "resolve %s step=project by planted name -> ADOPT (id=%s)",
                deal.deal_id,
                found[0]["id"],
            )
            return found[0]
        log.info("resolve %s step=project by planted name -> PROVEN ABSENT", deal.deal_id)
        return self._create_project_graphql(deal, name)

    # -- invoice ----------------------------------------------------------

    def _ensure_invoice(self, deal: Deal) -> None:
        intent = self.ledger.get_intent(deal.deal_id, STEP_INVOICE) or self._begin(
            deal.deal_id, STEP_INVOICE
        )
        body = {
            "CustomerRef": {"value": deal.qbo_project_id},
            "PrivateNote": marker_for(deal.deal_id),
            "Line": [
                {
                    "Amount": deal.contract_total,
                    "DetailType": "SalesItemLineDetail",
                    "Description": f"Wedding {deal.wedding_date} - contract total",
                    "SalesItemLineDetail": {"ItemRef": {"value": "1"}},
                }
            ],
        }
        created = self.client.create_invoice(body, requestid=intent.requestid)
        self._log_verdict(intent, created)
        self._seam("after_invoice_response")
        self.ledger.finish_step(deal.deal_id, STEP_INVOICE, "", created.entity["Id"])
        self.ledger.bind_invoice(deal.deal_id, created.entity["Id"])

    # -- payments ---------------------------------------------------------

    def _ensure_payment(self, deal: Deal, payment: Payment) -> None:
        ref = payment.hubspot_payment_id
        intent = self.ledger.get_intent(deal.deal_id, STEP_PAYMENT, ref) or self._begin(
            deal.deal_id, STEP_PAYMENT, ref
        )
        body = {
            "CustomerRef": {"value": deal.qbo_project_id},
            "TotalAmt": payment.amount,
            "TxnDate": payment.paid_at[:10] or None,
            "PrivateNote": f"{marker_for(deal.deal_id)} hubspot_payment_id={ref}",
            "Line": [
                {
                    "Amount": payment.amount,
                    "LinkedTxn": [{"TxnId": deal.qbo_invoice_id, "TxnType": "Invoice"}],
                }
            ],
        }
        created = self.client.create_payment(body, requestid=intent.requestid)
        self._log_verdict(intent, created)
        self._seam("after_payment_response")
        self.ledger.finish_step(deal.deal_id, STEP_PAYMENT, ref, created.entity["Id"])
        self.ledger.bind_payment(deal.deal_id, ref, created.entity["Id"])

    def _read_back_balance(self, deal: Deal) -> None:
        """State the balance from QBO; never add the webhook's amount to a counter.

        The invoice knows what has been applied to it. A local ``+= amount``
        is one more non-idempotent write, and it drifts on the first
        redelivered webhook.
        """
        assert deal.qbo_invoice_id is not None
        invoice = self.client.get_invoice(deal.qbo_invoice_id)
        amount_paid = round(float(invoice["TotalAmt"]) - float(invoice["Balance"]), 2)
        amount_owed = round(float(invoice["Balance"]), 2)
        booked = [p for p in self.ledger.payments_for(deal.deal_id) if p.qbo_payment_id]
        last_payment_at = max((p.paid_at for p in booked), default=None) or None
        self.ledger.set_balance(deal.deal_id, amount_paid, amount_owed, last_payment_at)

    # -- helpers ----------------------------------------------------------

    def _begin(self, deal_id: str, step: str, ref: str = "", mode: str | None = None) -> Intent:
        # Durable intent first. If the process dies on the next line, the
        # next run still knows this step was attempted, and with which id.
        intent = self.ledger.begin_step(deal_id, step, ref, mode)
        log.info(
            "intent committed for %s step=%s%s requestid=%s (durable, pre-send)",
            deal_id,
            step,
            f"[{ref}]" if ref else "",
            intent.requestid,
        )
        return intent

    @staticmethod
    def _log_verdict(intent: Intent, created: Created) -> None:
        verdict = "REPLAYED" if created.replayed else "CREATED NOW"
        log.info(
            "resolve %s step=%s%s requestid=%s -> %s (Id=%s)",
            intent.deal_id,
            intent.step,
            f"[{intent.ref}]" if intent.ref else "",
            intent.requestid,
            verdict,
            created.entity["Id"],
        )


# -- the anti-patterns, implemented rather than described --------------------


def naive_customer_sync(ledger: Ledger, client: QboClient, deal_id: str) -> dict[str, Any]:
    """The customer create as it is usually written.

    No requestid, nothing durable before the call, and the instinctive
    handler for ``6240``: append a number and try again. After a lost
    response the retry finds the name taken -- by the customer it created a
    moment ago -- and cheerfully makes a second one.
    """
    deal = ledger.get_deal(deal_id)
    base = display_name_for(deal.couple_names, deal.wedding_date)
    name = base
    for attempt in range(1, 4):
        try:
            created = client.create_customer(
                {"DisplayName": name, "Notes": marker_for(deal_id)},
                requestid=None,  # no idempotency on the wire at all
            )
        except QboRejected as exc:
            if exc.code != DUPLICATE_NAME:
                raise
            name = f"{base} ({attempt + 1})"
            continue
        ledger.bind_customer(deal_id, created.entity["Id"], name)  # too late to be durable
        return created.entity
    raise RuntimeError("could not find a free name")


def naive_payment_sync(
    ledger: Ledger, client: QboClient, deal_id: str, hubspot_payment_id: str
) -> dict[str, Any]:
    """The payment create with the same two omissions.

    Payments have no unique name to collide on, so nothing stops the retry:
    the invoice simply gets paid twice.
    """
    deal = ledger.get_deal(deal_id)
    payment = next(
        p for p in ledger.payments_for(deal_id) if p.hubspot_payment_id == hubspot_payment_id
    )
    created = client.create_payment(
        {
            "CustomerRef": {"value": deal.qbo_project_id},
            "TotalAmt": payment.amount,
            "Line": [
                {
                    "Amount": payment.amount,
                    "LinkedTxn": [{"TxnId": deal.qbo_invoice_id, "TxnType": "Invoice"}],
                }
            ],
        },
        requestid=None,  # no idempotency on the wire at all
    )
    ledger.bind_payment(deal_id, hubspot_payment_id, created.entity["Id"])  # too late
    return created.entity
