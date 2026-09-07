"""A worker that is killed for real, mid-sync.

Mocking a crash with ``raise`` proves less than it looks like: the stack
unwinds, ``finally`` blocks run, buffers flush, and destructors get their say.
None of that happens when a container is evicted, a deploy restarts the pod,
or someone closes the laptop.

So this runs in its own process and calls ``os._exit``, which returns to the
operating system immediately -- no unwinding, no flushing, no cleanup. The
parent process is then left with exactly what a real crash leaves: whatever
made it to disk before the kill, and a QBO company whose state nobody
recorded.

Run as::

    python -m qbo_idempotent_sync_proof._crash_child --db X --base-url Y \\
        --deal-id Z --crash-at after_customer_response
"""

from __future__ import annotations

import argparse
import os
import sys

from .auth import Authenticator, TokenStore
from .client import QboClient
from .fake_qbo import CLIENT_ID, CLIENT_SECRET, REALM_ID
from .ledger import Ledger
from .sync import SyncEngine, naive_customer_sync, naive_payment_sync

# The exit code the child uses, so the parent can assert the kill was ours
# and not an unnoticed traceback.
CRASH_EXIT_CODE = 137


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--deal-id", required=True)
    parser.add_argument("--crash-at", required=True)
    parser.add_argument("--strategy", default="durable")
    parser.add_argument("--payment-id", default="")
    args = parser.parse_args(argv)

    ledger = Ledger(args.db)
    auth = Authenticator(
        TokenStore(args.db),
        token_url=f"{args.base_url}/oauth2/v1/tokens/bearer",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
    )
    client = QboClient(args.base_url, REALM_ID, auth, backoff=lambda _: 0.0, sleep=lambda _: None)

    def kill_here(seam: str) -> None:
        if seam == args.crash_at:
            sys.stdout.write(f"[child] dying at seam '{seam}'\n")
            sys.stdout.flush()
            os._exit(CRASH_EXIT_CODE)

    if args.strategy.startswith("naive"):
        # The naive paths have no seams to arm, so the kill is scheduled by
        # the server instead: it commits, then drops the connection, and the
        # child dies on the resulting transport error before persisting.
        try:
            if args.strategy == "naive-customer":
                naive_customer_sync(ledger, client, args.deal_id)
            else:
                naive_payment_sync(ledger, client, args.deal_id, args.payment_id)
        except Exception as exc:  # noqa: BLE001 -- the crash IS the point
            sys.stdout.write(f"[child] died on {type(exc).__name__}\n")
            sys.stdout.flush()
            os._exit(CRASH_EXIT_CODE)
        return 0

    SyncEngine(ledger=ledger, client=client, on_seam=kill_here).sync(args.deal_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
