"""The operational ledger, and the durable intent that has to exist before any send.

One deal becomes four kinds of write against QuickBooks Online: a Customer, a
Project (or its stand-in), an Invoice and one Payment per HubSpot payment.
Each of those has the same gap -- QBO commits, the process dies before the id
is written down -- so each one gets the same treatment:

* the intent to send a step is committed to SQLite **before** the first byte
  leaves, so a process killed mid-call wakes up knowing a call was in flight;
* the ``requestid`` is **derived** from the deal, the step and the payment,
  not generated at call time -- a key that changes on retry is not an
  idempotency key at all;
* ``synchronous=FULL`` so "committed" means committed, not "in the OS page
  cache when the power went out";
* unique indexes on every remote binding, so a second customer, invoice or
  payment for the same local thing is rejected by the database even if the
  code is wrong.

The ``deals`` table is the operational record the business reads (it stands
in for a spreadsheet or a no-code base): what was created, what is owed, and
-- explicitly -- whether the "project" is a real QBO Project or a flagged
sub-customer fallback.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterator

# Steps, in the order they are applied.
STEP_CUSTOMER = "customer"
STEP_PROJECT = "project"
STEP_INVOICE = "invoice"
STEP_PAYMENT = "payment"

# Intent lifecycle.
PENDING = "pending"  # sent, or about to be; fate unknown
DONE = "done"  # remote id known and persisted
REJECTED = "rejected"  # the server answered with a definite no; nothing exists

# Deal sync status.
SYNC_PENDING = "pending"
SYNC_SYNCED = "synced"
SYNC_ERROR = "error"

# How the "project" was materialised. Never left blank once a project id is
# bound: the business must be able to tell a real Project from a stand-in.
PROJECT_MODE_GRAPHQL = "graphql_project"
PROJECT_MODE_FALLBACK = "subcustomer_fallback"

# QBO caps requestid at 50 characters; "wed-" + 40 hex = 44.
REQUESTID_MAX_LEN = 50


def requestid_for(deal_id: str, step: str, ref: str = "") -> str:
    """Derive the requestid -- deterministically, forever.

    Any process, at any later time, with no memory of the attempt, computes
    the same one. ``str(uuid4())`` at call time is not an idempotency key:
    every retry carries a fresh id and QBO has nothing to recognise.
    """
    material = f"{deal_id}:{step}" + (f":{ref}" if ref else "")
    digest = hashlib.sha256(material.encode()).hexdigest()
    return f"wed-{digest[:40]}"


def display_name_for(couple_names: list[str], wedding_date: str, variant: int = 0) -> str:
    """The consistent DisplayName for a couple, plus a deterministic tiebreak.

    QBO requires DisplayName to be unique across customers, vendors and
    employees. Two different couples can share both names, so the second
    variant carries the wedding date. The suffix is derived, not counted, so
    every process picks the same one.
    """
    base = " & ".join(couple_names)
    return base if variant == 0 else f"{base} ({wedding_date})"


def marker_for(deal_id: str) -> str:
    """The reference planted on every QBO entity we create.

    It goes in a free-text note field, so a later lookup can tell "ours" from
    "a stranger with the same name" without guessing.
    """
    return f"hubspot_deal_id={deal_id}"


@dataclass
class Deal:
    deal_id: str
    couple_names: list[str]
    wedding_date: str
    contract_total: float
    qbo_customer_id: str | None
    qbo_customer_display_name: str | None
    qbo_project_id: str | None
    project_mode: str | None
    qbo_invoice_id: str | None
    amount_paid: float
    amount_owed: float
    last_payment_at: str | None
    sync_status: str
    last_error: str | None


@dataclass
class Payment:
    deal_id: str
    hubspot_payment_id: str
    amount: float
    paid_at: str
    qbo_payment_id: str | None


@dataclass
class Intent:
    deal_id: str
    step: str
    ref: str
    requestid: str
    mode: str | None
    state: str
    remote_id: str | None


class Ledger:
    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Durability over throughput: a crash must not lose the last commit.
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deals (
                deal_id                   TEXT PRIMARY KEY,
                couple_names              TEXT NOT NULL,
                wedding_date              TEXT NOT NULL,
                contract_total            REAL NOT NULL,
                qbo_customer_id           TEXT,
                qbo_customer_display_name TEXT,
                qbo_project_id            TEXT,
                project_mode              TEXT,
                qbo_invoice_id            TEXT,
                amount_paid               REAL NOT NULL DEFAULT 0,
                amount_owed               REAL NOT NULL DEFAULT 0,
                last_payment_at           TEXT,
                sync_status               TEXT NOT NULL,
                last_error                TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                deal_id            TEXT NOT NULL,
                hubspot_payment_id TEXT NOT NULL,
                amount             REAL NOT NULL,
                paid_at            TEXT NOT NULL,
                qbo_payment_id     TEXT,
                PRIMARY KEY (deal_id, hubspot_payment_id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS intents (
                deal_id   TEXT NOT NULL,
                step      TEXT NOT NULL,
                ref       TEXT NOT NULL DEFAULT '',
                requestid TEXT NOT NULL,
                mode      TEXT,
                state     TEXT NOT NULL,
                remote_id TEXT,
                PRIMARY KEY (deal_id, step, ref)
            )
            """
        )
        # One QBO entity binds to at most one local thing. The database
        # refuses a second binding even if the code is wrong.
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS deals_customer_uq "
            "ON deals(qbo_customer_id) WHERE qbo_customer_id IS NOT NULL"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS deals_invoice_uq "
            "ON deals(qbo_invoice_id) WHERE qbo_invoice_id IS NOT NULL"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS payments_qbo_uq "
            "ON payments(qbo_payment_id) WHERE qbo_payment_id IS NOT NULL"
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- webhook intake ---------------------------------------------------

    def record_webhook(self, webhook: dict[str, Any]) -> str:
        """Record a "deal paid" webhook. Safe for duplicates and reordering.

        The deal row and the payment row are both ``INSERT OR IGNORE``: a
        second delivery of the same payment changes nothing, and a payment
        arriving before an earlier one simply lands next to it.
        """
        deal_id = str(webhook["deal_id"])
        self._conn.execute(
            "INSERT OR IGNORE INTO deals (deal_id, couple_names, wedding_date, "
            "contract_total, amount_owed, sync_status) VALUES (?, ?, ?, ?, ?, ?)",
            (
                deal_id,
                json.dumps(list(webhook["couple_names"])),
                webhook["wedding_date"],
                float(webhook["contract_total"]),
                float(webhook["contract_total"]),
                SYNC_PENDING,
            ),
        )
        payment = webhook.get("payment")
        if payment:
            self._conn.execute(
                "INSERT OR IGNORE INTO payments (deal_id, hubspot_payment_id, "
                "amount, paid_at) VALUES (?, ?, ?, ?)",
                (
                    deal_id,
                    str(payment["hubspot_payment_id"]),
                    float(payment["amount"]),
                    payment.get("paid_at", ""),
                ),
            )
            # A new payment means the deal has work to do again.
            self._conn.execute(
                "UPDATE deals SET sync_status = ? WHERE deal_id = ? AND sync_status = ?",
                (SYNC_PENDING, deal_id, SYNC_SYNCED),
            )
        return deal_id

    # -- intents ----------------------------------------------------------

    def begin_step(
        self, deal_id: str, step: str, ref: str = "", mode: str | None = None
    ) -> Intent:
        """Commit the intent to send one step, and return the requestid for it.

        This is the load-bearing line of the whole repo. It runs *before* the
        network call, so a hard kill during the call leaves a durable row
        saying "this step was in flight for this deal, with this requestid".
        """
        requestid = requestid_for(deal_id, step, ref)
        assert len(requestid) <= REQUESTID_MAX_LEN
        self._conn.execute(
            "INSERT OR IGNORE INTO intents (deal_id, step, ref, requestid, mode, state) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (deal_id, step, ref, requestid, mode, PENDING),
        )
        intent = self.get_intent(deal_id, step, ref)
        assert intent is not None
        return intent

    def set_step_mode(self, deal_id: str, step: str, ref: str, mode: str) -> Intent:
        """Record a change of strategy for a pending step, before acting on it."""
        self._conn.execute(
            "INSERT INTO intents (deal_id, step, ref, requestid, mode, state) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(deal_id, step, ref) DO UPDATE SET mode = excluded.mode",
            (deal_id, step, ref, requestid_for(deal_id, step, ref), mode, PENDING),
        )
        intent = self.get_intent(deal_id, step, ref)
        assert intent is not None
        return intent

    def finish_step(self, deal_id: str, step: str, ref: str, remote_id: str) -> None:
        self._conn.execute(
            "INSERT INTO intents (deal_id, step, ref, requestid, state, remote_id) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(deal_id, step, ref) DO UPDATE SET state = excluded.state, "
            "remote_id = excluded.remote_id",
            (deal_id, step, ref, requestid_for(deal_id, step, ref), DONE, remote_id),
        )

    def reject_step(self, deal_id: str, step: str, ref: str) -> None:
        """The server said no, definitively. Nothing exists under this intent."""
        self._conn.execute(
            "UPDATE intents SET state = ? WHERE deal_id = ? AND step = ? AND ref = ?",
            (REJECTED, deal_id, step, ref),
        )

    def get_intent(self, deal_id: str, step: str, ref: str = "") -> Intent | None:
        row = self._conn.execute(
            "SELECT * FROM intents WHERE deal_id = ? AND step = ? AND ref = ?",
            (deal_id, step, ref),
        ).fetchone()
        return Intent(**dict(row)) if row else None

    # -- bindings ---------------------------------------------------------

    def bind_customer(self, deal_id: str, customer_id: str, display_name: str) -> None:
        self._conn.execute(
            "UPDATE deals SET qbo_customer_id = ?, qbo_customer_display_name = ? "
            "WHERE deal_id = ?",
            (customer_id, display_name, deal_id),
        )

    def bind_project(self, deal_id: str, project_id: str, mode: str) -> None:
        self._conn.execute(
            "UPDATE deals SET qbo_project_id = ?, project_mode = ? WHERE deal_id = ?",
            (project_id, mode, deal_id),
        )

    def bind_invoice(self, deal_id: str, invoice_id: str) -> None:
        self._conn.execute(
            "UPDATE deals SET qbo_invoice_id = ? WHERE deal_id = ?", (invoice_id, deal_id)
        )

    def bind_payment(self, deal_id: str, hubspot_payment_id: str, qbo_payment_id: str) -> None:
        self._conn.execute(
            "UPDATE payments SET qbo_payment_id = ? "
            "WHERE deal_id = ? AND hubspot_payment_id = ?",
            (qbo_payment_id, deal_id, hubspot_payment_id),
        )

    def set_balance(
        self, deal_id: str, amount_paid: float, amount_owed: float, last_payment_at: str | None
    ) -> None:
        """Absolute, not incremental.

        A counter you bump by the webhook's amount is itself a non-idempotent
        write: replay the webhook and the total drifts. The numbers here come
        from reading the invoice back, and are stated, not added.
        """
        self._conn.execute(
            "UPDATE deals SET amount_paid = ?, amount_owed = ?, last_payment_at = ? "
            "WHERE deal_id = ?",
            (amount_paid, amount_owed, last_payment_at, deal_id),
        )

    def set_status(self, deal_id: str, status: str, last_error: str | None = None) -> None:
        self._conn.execute(
            "UPDATE deals SET sync_status = ?, last_error = ? WHERE deal_id = ?",
            (status, last_error, deal_id),
        )

    # -- reads ------------------------------------------------------------

    def get_deal(self, deal_id: str) -> Deal:
        row = self._conn.execute(
            "SELECT * FROM deals WHERE deal_id = ?", (deal_id,)
        ).fetchone()
        if row is None:
            raise KeyError(deal_id)
        data = dict(row)
        data["couple_names"] = json.loads(data["couple_names"])
        return Deal(**data)

    def payments_for(self, deal_id: str) -> list[Payment]:
        rows = self._conn.execute(
            "SELECT * FROM payments WHERE deal_id = ? ORDER BY paid_at, hubspot_payment_id",
            (deal_id,),
        ).fetchall()
        return [Payment(**dict(r)) for r in rows]

    def unresolved(self) -> Iterator[Deal]:
        """Deals whose remote state is in doubt, for startup recovery."""
        rows = self._conn.execute(
            "SELECT deal_id FROM deals WHERE sync_status != ?", (SYNC_SYNCED,)
        ).fetchall()
        return (self.get_deal(r["deal_id"]) for r in rows)
