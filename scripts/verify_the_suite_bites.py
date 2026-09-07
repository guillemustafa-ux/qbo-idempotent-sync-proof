"""Break the implementation on purpose, and check the suite notices.

A green suite proves nothing on its own -- tests that assert only what the
code already does are green forever, including after someone deletes the part
that mattered. So this script reintroduces, one at a time, the six defects
this design exists to prevent, and reports which tests catch each one.

Every mutation is applied to a real source file, the suite is run against it,
and the file is restored afterwards (even if the run is interrupted).

    python scripts/verify_the_suite_bites.py
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "qbo_idempotent_sync_proof"


@dataclass
class Mutation:
    name: str
    defect: str
    path: Path
    old: str
    new: str


MUTATIONS = [
    Mutation(
        name="random-requestid",
        defect="Generate the requestid at call time instead of deriving it from "
        "the deal and the step, so a process with no memory of the first "
        "attempt sends an id QBO has never seen.",
        path=PKG / "ledger.py",
        old='    material = f"{deal_id}:{step}" + (f":{ref}" if ref else "")\n'
        "    digest = hashlib.sha256(material.encode()).hexdigest()\n"
        '    return f"wed-{digest[:40]}"',
        new="    import uuid\n\n"
        '    return f"wed-{uuid.uuid4().hex[:40]}"',
    ),
    Mutation(
        name="no-durable-intent",
        defect="Send the step without recording the intent first, so a process "
        "killed mid-call wakes up believing nothing was ever tried.",
        path=PKG / "ledger.py",
        old="        self._conn.execute(\n"
        '            "INSERT OR IGNORE INTO intents (deal_id, step, ref, requestid, mode, state) "\n'
        '            "VALUES (?, ?, ?, ?, ?, ?)",\n'
        "            (deal_id, step, ref, requestid, mode, PENDING),\n"
        "        )\n"
        "        intent = self.get_intent(deal_id, step, ref)\n"
        "        assert intent is not None\n"
        "        return intent",
        new="        return Intent(deal_id, step, ref, requestid, mode, PENDING, None)",
    ),
    Mutation(
        name="blind-retry-on-doubt",
        defect="Resolve an unknown GraphQL outcome by assuming it never landed -- "
        "the instinctive fix, and the one that doubles the project, because "
        "the Projects API has no requestid to collapse the retry.",
        path=PKG / "sync.py",
        old="        try:\n"
        "            found = self.client.find_projects(deal.qbo_customer_id, name)\n"
        "        except ProjectsApiUnavailable:\n"
        "            # A definite answer: this app cannot have created one.\n"
        "            return None",
        new="        found = []",
    ),
    Mutation(
        name="silent-project-fallback",
        defect="Create the sub-customer when the Projects API answers 403, but "
        "record it as a real project and tell nobody.",
        path=PKG / "sync.py",
        old="            mode = PROJECT_MODE_FALLBACK\n"
        '            project_id = sub["Id"]',
        new="            mode = PROJECT_MODE_GRAPHQL\n"
        '            project_id = sub["Id"]',
    ),
    Mutation(
        name="stale-refresh-token",
        defect="After a refresh, keep the stored refresh token instead of the one "
        "that came back, so the app is locked out at the next rotation.",
        path=PKG / "auth.py",
        old='            refresh_token=payload["refresh_token"],\n'
        "            refresh_expires_at=issued_at\n"
        '            + float(payload.get("x_refresh_token_expires_in", 100 * 86400)),',
        new="            refresh_token=current.refresh_token,\n"
        "            refresh_expires_at=current.refresh_expires_at,",
    ),
    Mutation(
        name="increment-amount-paid",
        defect="Compute amount_paid by adding the recorded payments to the "
        "previous total instead of reading the invoice back, so every "
        "redelivered webhook or repeated apply drifts the balance.",
        path=PKG / "sync.py",
        old='        amount_paid = round(float(invoice["TotalAmt"]) - float(invoice["Balance"]), 2)\n'
        '        amount_owed = round(float(invoice["Balance"]), 2)',
        new="        amount_paid = round(\n"
        "            deal.amount_paid\n"
        "            + sum(p.amount for p in self.ledger.payments_for(deal.deal_id)),\n"
        "            2,\n"
        "        )\n"
        "        amount_owed = round(deal.contract_total - amount_paid, 2)",
    ),
]


def run_suite() -> tuple[int, list[str]]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    failed = sorted(
        {
            line.split("::")[1].split(" ")[0]
            for line in proc.stdout.splitlines()
            if line.startswith("FAILED") or line.startswith("ERROR")
        }
    )
    return proc.returncode, failed


def main() -> int:
    baseline_code, baseline_failed = run_suite()
    print(f"baseline: {'PASS' if baseline_code == 0 else 'FAIL'}")
    if baseline_code != 0:
        print("the suite must be green before mutating:", baseline_failed)
        return 1

    survived: list[str] = []
    for mutation in MUTATIONS:
        original = mutation.path.read_text(encoding="utf-8")
        if mutation.old not in original:
            print(f"\n!! {mutation.name}: anchor not found in {mutation.path.name}")
            return 1
        try:
            mutation.path.write_text(
                original.replace(mutation.old, mutation.new, 1), encoding="utf-8"
            )
            code, failed = run_suite()
        finally:
            mutation.path.write_text(original, encoding="utf-8")

        print(f"\n[{mutation.name}]")
        print(f"  defect: {mutation.defect}")
        if code == 0:
            print("  CAUGHT BY: nothing -- the suite does not cover this defect")
            survived.append(mutation.name)
        else:
            for name in failed:
                print(f"  caught by: {name}")

    print()
    if survived:
        print(f"{len(survived)} mutation(s) survived: {', '.join(survived)}")
        return 1
    print(f"all {len(MUTATIONS)} mutations were caught; the suite bites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
