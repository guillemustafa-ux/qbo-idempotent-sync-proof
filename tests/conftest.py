from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from qbo_idempotent_sync_proof._crash_child import CRASH_EXIT_CODE
from qbo_idempotent_sync_proof.auth import Authenticator, TokenStore
from qbo_idempotent_sync_proof.client import QboClient
from qbo_idempotent_sync_proof.fake_qbo import CLIENT_ID, CLIENT_SECRET, REALM_ID, FakeQbo
from qbo_idempotent_sync_proof.ledger import Ledger
from qbo_idempotent_sync_proof.sync import SyncEngine


@pytest.fixture
def qbo_factory():
    """A QBO stand-in with the partner tier a scenario needs."""
    running: list[FakeQbo] = []

    def make(*, partner_tier: str = "builder", rotate_refresh_on_use: bool = True) -> FakeQbo:
        qbo = FakeQbo(partner_tier=partner_tier, rotate_refresh_on_use=rotate_refresh_on_use)
        qbo.start()
        running.append(qbo)
        return qbo

    yield make
    for qbo in running:
        qbo.stop()


@pytest.fixture
def qbo(qbo_factory) -> FakeQbo:
    """The default: a Builder-tier app, which is what a private internal app is."""
    return qbo_factory()


@pytest.fixture
def silver_qbo(qbo_factory) -> FakeQbo:
    return qbo_factory(partner_tier="silver")


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "sync.db")


@pytest.fixture
def ledger_factory(db_path):
    """Opens the ledger fresh each time.

    Every recovery in this suite is driven from a **new** ``Ledger`` object.
    In-memory state is never allowed to help across a crash boundary -- that
    is the whole point.
    """
    opened: list[Ledger] = []

    def make(path: str | None = None) -> Ledger:
        ledger = Ledger(path or db_path)
        opened.append(ledger)
        return ledger

    yield make
    for ledger in opened:
        ledger.close()


@pytest.fixture
def auth_factory(db_path):
    """An authenticator over a token store in the same database file.

    The first one for a database seeds it with the refresh token the
    authorisation flow would have produced -- exactly once, so later
    "processes" against the same file find whatever the previous one left.
    """
    opened: list[TokenStore] = []

    def make(qbo: FakeQbo, path: str | None = None) -> Authenticator:
        store = TokenStore(path or db_path)
        opened.append(store)
        if store.load() is None:
            store.bootstrap(qbo.refresh_token)
        return Authenticator(
            store,
            token_url=qbo.token_url,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
        )

    yield make
    for store in opened:
        store.close()


@pytest.fixture
def client_factory(auth_factory):
    def make(qbo: FakeQbo, path: str | None = None) -> QboClient:
        # No sleeping in the proof: the throttle backoff is zero.
        return QboClient(
            qbo.base_url,
            REALM_ID,
            auth_factory(qbo, path),
            timeout=5.0,
            backoff=lambda _attempt: 0.0,
            sleep=lambda _seconds: None,
        )

    return make


@pytest.fixture
def engine_factory(ledger_factory, client_factory):
    """A brand-new engine: fresh ledger connection, fresh client, no memory."""

    def make(qbo: FakeQbo, path: str | None = None) -> SyncEngine:
        return SyncEngine(ledger=ledger_factory(path), client=client_factory(qbo, path))

    return make


@pytest.fixture
def make_webhook():
    """A HubSpot "deal paid" webhook, as the sync receives it."""

    def make(
        deal_id: str = "deal-1001",
        couple_names: tuple[str, str] = ("Ana Perez", "Tomas Ruiz"),
        wedding_date: str = "2027-03-14",
        contract_total: float = 12500.0,
        payment_id: str = "pay-1",
        amount: float = 2500.0,
        paid_at: str = "2026-09-01T10:00:00Z",
    ) -> dict[str, Any]:
        return {
            "deal_id": deal_id,
            "couple_names": list(couple_names),
            "wedding_date": wedding_date,
            "contract_total": contract_total,
            "payment": {
                "hubspot_payment_id": payment_id,
                "amount": amount,
                "paid_at": paid_at,
            },
        }

    return make


@pytest.fixture
def run_child(db_path, auth_factory):
    """Run one sync in a separate process and kill it mid-flight.

    Returns the completed process so a test can assert the death was the
    scripted one (exit code 137) and not a stray exception.
    """

    def run(
        qbo: FakeQbo,
        deal_id: str,
        crash_at: str,
        strategy: str = "durable",
        payment_id: str = "",
    ):
        auth_factory(qbo)  # the child authenticates from the shared token store
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "qbo_idempotent_sync_proof._crash_child",
                "--db",
                db_path,
                "--base-url",
                qbo.base_url,
                "--deal-id",
                deal_id,
                "--crash-at",
                crash_at,
                "--strategy",
                strategy,
                "--payment-id",
                payment_id,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert proc.returncode == CRASH_EXIT_CODE, (
            f"child was supposed to be killed at '{crash_at}', but exited "
            f"{proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
        return proc

    return run
