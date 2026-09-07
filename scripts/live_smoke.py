"""One find-or-create against a real Intuit sandbox, if credentials are present.

This is deliberately tiny. It does not run the suite against the sandbox and
it does not touch the Projects API (which a Builder-tier app cannot call at
all). It sends one Customer create with a derived ``requestid``, sends it
again, and prints whether QBO handed back the same Id -- the single fact the
whole design rests on.

Set these and run it::

    QBO_CLIENT_ID       the app's client id (Intuit developer portal)
    QBO_CLIENT_SECRET   the app's client secret
    QBO_REFRESH_TOKEN   a refresh token from the OAuth playground or your app
    QBO_REALM_ID        the sandbox company id
    QBO_BASE_URL        optional; defaults to the sandbox

    python scripts/live_smoke.py

Without them it prints a skip line and exits 0, so it is safe in CI.

The refresh token you pass in is consumed: Intuit may rotate it, and the
rotated one is persisted to ``live_smoke.db`` in the working directory.
Read it back from there for the next run.
"""

from __future__ import annotations

import datetime as dt
import os
import sys

from qbo_idempotent_sync_proof.auth import Authenticator, TokenStore
from qbo_idempotent_sync_proof.client import DUPLICATE_NAME, QboClient, QboRejected
from qbo_idempotent_sync_proof.ledger import requestid_for

SANDBOX = "https://sandbox-quickbooks.api.intuit.com"
REQUIRED = ("QBO_CLIENT_ID", "QBO_CLIENT_SECRET", "QBO_REFRESH_TOKEN", "QBO_REALM_ID")


def main() -> int:
    env = {k: (os.environ.get(k) or "").replace('"', "") for k in REQUIRED}
    missing = [k for k, v in env.items() if not v]
    if missing:
        print(f"live smoke skipped: {', '.join(missing)} not set")
        return 0

    store = TokenStore("live_smoke.db")
    if store.load() is None:
        store.bootstrap(env["QBO_REFRESH_TOKEN"])
    auth = Authenticator(
        store, client_id=env["QBO_CLIENT_ID"], client_secret=env["QBO_CLIENT_SECRET"]
    )
    client = QboClient(os.environ.get("QBO_BASE_URL", SANDBOX), env["QBO_REALM_ID"], auth)

    today = dt.date.today().isoformat()
    name = f"qbo-idempotent-sync-proof smoke {today}"
    requestid = requestid_for(f"smoke-{today}", "customer", "0")
    body = {"DisplayName": name, "Notes": "created by scripts/live_smoke.py"}

    try:
        first = client.create_customer(body, requestid=requestid)
        second = client.create_customer(body, requestid=requestid)
    except QboRejected as exc:
        if exc.code == DUPLICATE_NAME:
            holders = client.customers_named(name)
            print(f"6240 on {name!r}: already exists as Id={holders[0]['Id'] if holders else '?'}")
            print("(a previous run today created it; QBO no longer replays that requestid)")
            return 0
        print(f"QBO rejected the create: HTTP {exc.status} code={exc.code} {exc.message}")
        return 1

    same = first.entity["Id"] == second.entity["Id"]
    print(f"first send  -> Customer Id={first.entity['Id']}")
    print(f"second send -> Customer Id={second.entity['Id']} (same requestid)")
    print("requestid replay: " + ("CONFIRMED, one customer" if same else "NOT honoured, two customers"))
    print(f"rotated refresh token persisted in live_smoke.db")
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
