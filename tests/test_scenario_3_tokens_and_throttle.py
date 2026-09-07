"""Scenario 3 -- the two ways QBO stops talking to a correct integration.

Intuit rotates refresh tokens and throttles at 500 requests per minute. Both
are boring until the day they are not: the first locks the app out for good
if the rotated token was not persisted, the second turns a retry loop into
a duplicate factory if the retry does not carry the same requestid.
"""

from __future__ import annotations

import pytest

from qbo_idempotent_sync_proof.auth import (
    AuthLockedOut,
    TokenStore,
    refresh_but_keep_the_old_refresh_token,
)
from qbo_idempotent_sync_proof.ledger import SYNC_SYNCED


def test_refresh_rotation_persists_the_latest_token_for_a_fresh_process(
    qbo, auth_factory, engine_factory, make_webhook, db_path
):
    """The refresh token that came back is the only one that still works."""
    first_process = auth_factory(qbo)
    original = first_process.store.load().refresh_token

    first_process.refresh()

    stored = TokenStore(db_path).load()
    assert stored.refresh_token != original, "the rotated token replaced the stored one"
    assert stored.refresh_token == qbo.refresh_token

    # A brand-new process, hours later, with nothing but the database.
    qbo.revoke_access_tokens_now()
    deal = engine_factory(qbo).apply(make_webhook())
    assert deal.sync_status == SYNC_SYNCED
    refreshes = [r for r in qbo.request_log if r.startswith("POST /oauth2")]
    assert len(refreshes) == 2, "one explicit refresh, one triggered by the fresh process"


def test_a_store_that_kept_the_old_refresh_token_is_locked_out(qbo, auth_factory):
    """The anti-pattern: refresh, take the access token, ignore the rest.

    It works exactly once. After rotation the stored refresh token is
    retired, the next refresh is ``invalid_grant``, and no retry policy in
    the world gets the app back in without a human re-authorising it.
    """
    auth = auth_factory(qbo)
    original = auth.store.load().refresh_token

    refresh_but_keep_the_old_refresh_token(auth)
    assert auth.store.load().refresh_token == original, "the rotated token was dropped"
    assert qbo.refresh_token != original, "and the service already retired the old one"

    with pytest.raises(AuthLockedOut) as exc:
        auth.refresh()
    assert "invalid_grant" in str(exc.value)


def test_an_expired_access_token_is_refreshed_on_401_and_the_call_retried(
    qbo, engine_factory, make_webhook
):
    """Clocks disagree; the 401 is the truth. One refresh, one retry, no duplicate."""
    engine = engine_factory(qbo)
    engine.client.auth.access_token()  # obtain one
    qbo.revoke_access_tokens_now()  # and expire it behind our back

    deal = engine.apply(make_webhook())

    assert deal.sync_status == SYNC_SYNCED
    assert len(qbo.customers) == 1
    refreshes = [r for r in qbo.request_log if r.startswith("POST /oauth2")]
    assert len(refreshes) == 2, "bootstrap refresh, then the one forced by the 401"


def test_a_throttled_create_resends_the_same_requestid_and_makes_one_customer(
    qbo, engine_factory, make_webhook
):
    """429 ``ThrottleExceeded`` mid-create.

    The retry after backoff carries the same requestid, so whichever of the
    sends QBO counted, it counted once.
    """
    qbo.throttle_next(2)
    deal = engine_factory(qbo).apply(make_webhook())

    assert deal.sync_status == SYNC_SYNCED
    throttled = [r for r in qbo.request_log if r.startswith("429 ThrottleExceeded")]
    assert len(throttled) == 2
    requestids = {r.split("requestid=")[1] for r in throttled}
    assert len(requestids) == 1, "same requestid on every send"
    (requestid,) = requestids
    posts = [r for r in qbo.request_log if r == f"POST /customer requestid={requestid}"]
    assert len(posts) == 1, "the throttled attempts never reached the create; the third did"
    assert len(qbo.customers) == 1
