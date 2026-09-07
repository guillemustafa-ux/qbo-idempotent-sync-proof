# qbo-idempotent-sync-proof

[![proof](https://github.com/guillemustafa-ux/qbo-idempotent-sync-proof/actions/workflows/ci.yml/badge.svg)](https://github.com/guillemustafa-ux/qbo-idempotent-sync-proof/actions/workflows/ci.yml)

A harness for the gap that quietly double-books money in a CRM-to-accounting
sync: **QuickBooks Online commits the write, and your process dies before it
can write down the id.** Applied to one concrete flow -- a "deal paid"
webhook that must become a QBO Customer, a Project, an Invoice and one
Payment per instalment -- with runnable evidence that:

1. **no step is performed twice** across a real process kill, including the
   case where the outcome of the call is genuinely unknowable;
2. **two couples who share both names stay two customers**, and a redelivered
   webhook never creates a second Payment;
3. the sync takes the **honest path on QBO Projects** -- which are behind a
   partner-tier gate most private apps cannot pass -- and says so in the
   operational record instead of silently degrading;
4. it **refuses to guess** when QBO offers no way to settle the question, and
   escalates with the exact ids instead.

You can verify all of it in about two minutes, on your machine, with **no
Intuit account and no network access**. There are no runtime dependencies:
the fake QBO, the OAuth client, the transport and the ledger are standard
library, so nothing here can be attributed to a framework being clever.

This is the sibling of [idempotent-writes-proof](https://github.com/guillemustafa-ux/idempotent-writes-proof),
which proves the general mechanism against a generic third-party API. This
repo applies it to the specific things QBO does.

## The failure, precisely

A call to QBO has three outcomes, not two:

* it **succeeded** and you were told so;
* it **provably failed** -- QBO answered with a `Fault`, so nothing was
  created. `6240 Duplicate Name Exists` is the interesting one;
* the **outcome is unknown** -- the connection died, the request timed out,
  the container was evicted mid-flight.

Almost every client library collapses the third case into the second, and
the natural `except ConnectionError: retry` treats "it never landed" and "it
landed and I wasn't told" identically. One of those two retries is a
duplicate. For a Customer that costs a cleanup; for a Payment it is the
couple's deposit booked twice.

```
    intent committed       ─┐
                            │   ← a crash here is free
    POST /payment?requestid ─┤
                            │   ← a crash here books the payment twice on retry
    200 { Payment: { Id } } ─┤
                            │   ← a crash here books the payment twice on retry
    save qbo_payment_id     ─┘
```

## What QBO gives you decides the strategy

| The step | QBO offers | How an in-flight write is settled |
|---|---|---|
| Customer, Invoice, Payment (REST) | a [`requestid`](https://help.developer.intuit.com/s/article/What-is-RequestId-and-its-usage) query parameter on every create | Resend the same requestid. QBO returns the **original response** instead of performing the operation again, or performs it if it truly never landed. One request settles both cases. |
| Project ([GraphQL Projects API](https://developer.intuit.com/app/developer/qbo/docs/workflows/manage-projects/get-started)) | no requestid, but a lookup by name under the customer | Search for the name **you planted** on the create. Exactly one means adopt; zero means it never landed; more than one means somebody already duplicated and picking one is a guess. |
| Project, when the app is not eligible for the Projects API | a REST sub-customer (`Job=true`, `ParentRef`) -- which is **not** a Project | Create it under a requestid like any REST entity, and **flag it** in the ledger as `project_mode = subcustomer_fallback`. Never silently. |
| any step, with neither mechanism | nothing | **Nothing can settle it.** Stop and escalate. |

The last two rows are the honest ones. No amount of retry logic recovers
information the API never exposed, and no amount of naming makes a
sub-customer show up in the Projects tab.

## The design under test

* **Intent before send.** Each step's requestid is committed to SQLite (WAL,
  `synchronous=FULL`) *before* the first byte leaves. Persist after the call
  and a crash leaves nothing to ask about.
* **The requestid is derived, not generated.**
  `"wed-" + sha256(f"{deal_id}:{step}[:{payment_id}]")[:40]` -- 44
  characters, under QBO's limit of 50 -- so any process, at any later time,
  with no memory of the attempt, computes the same one. `uuid4()` at call
  time is not an idempotency key: every retry carries a fresh one and QBO has
  nothing to recognise.
* **A consistent DisplayName, and a deterministic tiebreak.** The customer is
  `"Ana Perez & Tomas Ruiz"`; on a
  [`6240`](https://help.developer.intuit.com/s/article/6240-Duplicate-Name-Exists-Error)
  the sync asks who holds the name. If the holder carries the marker we plant
  in `Notes`, it is ours and is adopted. If not, it is a stranger, and the
  next variant is `"Ana Perez & Tomas Ruiz (2027-11-20)"` -- the wedding
  date, not a counter, so every process picks the same one.
* **Interrogate, then act.** An unresolved GraphQL project is settled by
  lookup before anything is sent. Only a **proven-absent** project is created.
  A blind resend is the bug, not the fix.
* **Read back to state the balance.** `amount_paid` and `amount_owed` come
  from the invoice's `TotalAmt` and `Balance`, never from `+= amount`. A
  counter you bump is itself a non-idempotent write.
* **The refresh token that came back is the only one that still works.**
  Intuit's refresh token
  [rotates](https://help.developer.intuit.com/s/article/Validity-of-Refresh-Token)
  roughly every 24 hours and the previous one expires 24 hours after a new
  one is issued, so the store is overwritten on every refresh.
* **429 means resend the same request.** A
  [`ThrottleExceeded`](https://help.developer.intuit.com/s/article/QuickBooks-Online-API-Best-Practices)
  is retried after backoff with the same URL, therefore the same requestid.
* **Unique indexes on every remote binding** -- `qbo_customer_id`,
  `qbo_invoice_id`, `qbo_payment_id` -- so a future change that tries to
  bind a second QBO entity to one local thing is rejected by the database
  rather than by a code review.

## Verify it in two minutes

```bash
git clone https://github.com/guillemustafa-ux/qbo-idempotent-sync-proof
cd qbo-idempotent-sync-proof
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -v --log-cli-level=INFO
```

The suite runs the sync against a **fake QBO** that speaks real HTTP over a
real socket and encodes the documented behaviours above, and injects faults
at the exact seam where money gets lost. The crash scenarios are not mocked:
a **child process is killed with `os._exit`** mid-step, so no stack unwinds,
no `finally` runs and no buffer flushes -- exactly what an evicted container
leaves behind. Recovery then runs in a fresh process with nothing but the
database.

```text
tests/test_scenario_1_customer_exactly_once.py
  test_naive_retry_after_a_real_kill_makes_a_second_customer PASSED

  resolve deal-1001 step=customer[0] requestid=wed-7ea012b9…c83de -> REPLAYED (Id=1)
  intent committed for deal-1001 step=project requestid=wed-559b1ffb…76550 (durable, pre-send)
  Projects API for deal-1001 -> 403 (not eligible)
  resolve deal-1001 step=project requestid=wed-559b1ffb…76550 -> CREATED NOW (Id=2)
  project step for deal-1001 -> SUBCUSTOMER FALLBACK (flagged) Id=2 IsProject=False
  deal deal-1001: the Projects API is not available to this app (tier), so
    'Wedding 2027-03-14 - Ana Perez & Tomas Ruiz' was created as a sub-customer
    (Id=2, IsProject=False). It will NOT appear in the Projects tab.
  intent committed for deal-1001 step=invoice requestid=wed-7caa7003…b40a6 (durable, pre-send)
  resolve deal-1001 step=invoice requestid=wed-7caa7003…b40a6 -> CREATED NOW (Id=3)
  intent committed for deal-1001 step=payment[pay-1] requestid=wed-e5724cf0…4ee55 (durable, pre-send)
  resolve deal-1001 step=payment[pay-1] requestid=wed-e5724cf0…4ee55 -> CREATED NOW (Id=4)
  test_durable_sync_survives_a_real_kill_between_200_and_persist PASSED

  DisplayName 'Ana Perez & Tomas Ruiz' is taken by a stranger -> variant 1
  intent committed for deal-2 step=customer[1] requestid=wed-3adb9a64…79fee (durable, pre-send)
  resolve deal-2 step=customer[1] requestid=wed-3adb9a64…79fee -> CREATED NOW (Id=5)
  test_two_deals_with_the_same_names_stay_two_customers PASSED

  resolve deal-1001 step=customer -> 6240, holder carries our marker -> ADOPT (Id=1)
  test_when_the_server_forgot_the_requestid_the_planted_marker_still_adopts PASSED

tests/test_scenario_2_projects_honest_path.py
  project step for deal-1001 -> GRAPHQL (id=2)
  test_silver_tier_creates_a_real_project_over_graphql PASSED

  resolve deal-1001 step=project by planted name -> ADOPT (id=2)
  test_crash_between_create_project_and_persist_adopts_by_planted_name PASSED

25 passed
```

Note the two verdicts in scenario 1: **REPLAYED** and **CREATED NOW**. Same
sync code, same recovery path, opposite conclusions -- one entity at QBO
either way. (The verdict word comes from a response header the *fake* adds
on a replay; the real service returns the identical body with no marker.
Nothing in the sync decides anything based on it. It exists so the log can
tell you what happened.)

### What each scenario proves

| # | Test | Cut point | Proven |
|---|---|---|---|
| 1a | `test_naive_retry_after_a_real_kill_makes_a_second_customer` | QBO commits, response lost, worker killed | **the anti-pattern, demonstrated**: the retry gets a `6240`, appends "(2)", and one couple becomes two customers |
| 1b | `test_durable_sync_survives_a_real_kill_between_200_and_persist` | `os._exit` **between** the 200 and the local write | two creates sent under one requestid, **one customer exists**; the second was recognised as a replay |
| 1c | `test_lost_response_is_resolved_without_duplicating` | connection cut after QBO commits | the process survives and still cannot tell -- the replay settles it without a duplicate |
| 1d | `test_a_create_that_never_landed_is_actually_sent` | connection cut **before** QBO commits | the mirror case: recovery proves absence and **does** create -- "do nothing on doubt" is not the answer either |
| 1e | `test_two_deals_with_the_same_names_stay_two_customers` | -- | `6240` answered with a deterministic date suffix; two couples, two customers, two invoices; a fresh process re-derives the same variant |
| 1f | `test_a_display_name_with_an_apostrophe_is_escaped_in_the_query` | -- | `O'Brien` reaches the [query language](https://help.developer.intuit.com/s/article/SQL-Queries) as `O\'Brien`, so the collision lookup returns the holder and not a parse error |
| 1g | `test_replaying_a_requestid_with_a_corrected_body_returns_the_old_customer` | -- | **the quiet one**: a replayed requestid returns the original and silently discards the corrected name, with a 200 and an Id, so nothing looks wrong |
| 1h | `test_a_rebuilt_ledger_re_derives_every_requestid_and_creates_nothing` | ledger wiped, webhook replayed | a process with no memory computes the same four requestids; QBO replays all four; nothing is created twice |
| 1i | `test_when_the_server_forgot_the_requestid_the_planted_marker_still_adopts` | replay window closed | a resend earns a `6240`; the holder carries our marker; adopted, not suffixed |
| 2a | `test_builder_tier_falls_back_to_a_sub_customer_and_says_so` | GraphQL answers 403 | a sub-customer is created, `IsProject` reads back `false`, the ledger says `subcustomer_fallback`, and a notice is surfaced |
| 2b | `test_silver_tier_creates_a_real_project_over_graphql` | -- | the same code on an eligible app creates a real Project and records `graphql_project` |
| 2c | `test_crash_between_create_project_and_persist_adopts_by_planted_name` | `os._exit` after the mutation returns | no requestid to replay; the lookup by planted name finds exactly one and adopts it; one mutation on the wire |
| 2d | `test_two_projects_with_the_same_planted_name_escalate_instead_of_guessing` | same, plus a second project planted | the sync stops with the ids, records the error, books nothing downstream -- and still two projects, not three |
| 2e | `test_the_fallback_sub_customer_replays_its_requestid_after_a_lost_response` | response to the sub-customer create lost | the fallback is REST, so it gets the REST guarantee |
| 3a | `test_refresh_rotation_persists_the_latest_token_for_a_fresh_process` | -- | after a rotation, a brand-new process authenticates from the store alone |
| 3b | `test_a_store_that_kept_the_old_refresh_token_is_locked_out` | -- | **the anti-pattern**: keep the access token, ignore the rotated refresh token, and the next refresh is `invalid_grant` for good |
| 3c | `test_an_expired_access_token_is_refreshed_on_401_and_the_call_retried` | access token revoked behind the client | one refresh, one retry, one customer |
| 3d | `test_a_throttled_create_resends_the_same_requestid_and_makes_one_customer` | two `429 ThrottleExceeded` in a row | every send carried the same requestid; one customer |
| 4a | `test_duplicate_webhook_delivery_records_one_payment` | -- | the same webhook twice, from two workers: one Payment, balance read back |
| 4b | `test_a_second_payment_hits_the_same_customer_project_and_invoice` | -- | later instalments reuse everything and add one Payment; `amount_owed` comes from the invoice |
| 4c | `test_out_of_order_delivery_ends_in_the_same_state` | -- | second payment first, then the first, then both again: two Payments, right balance |
| 4d | `test_naive_payment_retry_after_a_real_kill_pays_the_invoice_twice` | Payment committed, response lost, worker killed | **the anti-pattern where it costs money**: no name to collide on, so the invoice is simply paid twice |
| 4e | `test_a_payment_survives_a_real_kill_between_200_and_persist` | `os._exit` between the 200 and the local write | same requestid on the wire twice, one Payment |
| 4f | `test_the_schema_refuses_a_second_binding` | -- | the database rejects a second customer, invoice or payment binding independently of the code |
| 4g | `test_apply_is_safe_to_call_repeatedly` | -- | idempotence is a property of the operation, not of a retry budget |

## Proof that the suite bites

A green suite proves nothing on its own -- tests that assert only what the
code already does stay green after someone deletes the part that mattered.
So the repo ships the check:

```bash
python scripts/verify_the_suite_bites.py
```

It reintroduces each defect into the real source, runs the suite, reports
what caught it, and restores the file:

```text
baseline: PASS

[random-requestid]
  defect: Generate the requestid at call time instead of deriving it from the deal
          and the step, so a process with no memory of the first attempt sends an
          id QBO has never seen.
  caught by: test_a_rebuilt_ledger_re_derives_every_requestid_and_creates_nothing

[no-durable-intent]
  defect: Send the step without recording the intent first, so a process killed
          mid-call wakes up believing nothing was ever tried.
  caught by: test_a_payment_survives_a_real_kill_between_200_and_persist
  caught by: test_crash_between_create_project_and_persist_adopts_by_planted_name
  caught by: test_durable_sync_survives_a_real_kill_between_200_and_persist
  caught by: test_two_projects_with_the_same_planted_name_escalate_instead_of_guessing

[blind-retry-on-doubt]
  defect: Resolve an unknown GraphQL outcome by assuming it never landed -- the
          instinctive fix, and the one that doubles the project, because the
          Projects API has no requestid to collapse the retry.
  caught by: test_crash_between_create_project_and_persist_adopts_by_planted_name
  caught by: test_two_projects_with_the_same_planted_name_escalate_instead_of_guessing

[silent-project-fallback]
  defect: Create the sub-customer when the Projects API answers 403, but record it
          as a real project and tell nobody.
  caught by: test_builder_tier_falls_back_to_a_sub_customer_and_says_so
  caught by: test_the_fallback_sub_customer_replays_its_requestid_after_a_lost_response

[stale-refresh-token]
  defect: After a refresh, keep the stored refresh token instead of the one that
          came back, so the app is locked out at the next rotation.
  caught by: test_an_expired_access_token_is_refreshed_on_401_and_the_call_retried
  caught by: test_refresh_rotation_persists_the_latest_token_for_a_fresh_process

[increment-amount-paid]
  defect: Compute amount_paid by adding the recorded payments to the previous total
          instead of reading the invoice back, so every redelivered webhook or
          repeated apply drifts the balance.
  caught by: test_a_second_payment_hits_the_same_customer_project_and_invoice
  caught by: test_apply_is_safe_to_call_repeatedly
  caught by: test_duplicate_webhook_delivery_records_one_payment
  caught by: test_out_of_order_delivery_ends_in_the_same_state

all 6 mutations were caught; the suite bites
```

Two of those results say something true rather than something flattering:

* **`random-requestid` is caught only by the rebuilt-ledger test.** Across a
  crash, a random requestid still works -- because `begin_step` persists it
  before the call, so recovery reuses the id rather than re-deriving it.
  Deriving it deterministically earns its place when the ledger is *not*
  there: a restore from backup, a rebuild from the webhook log, a second
  worker with its own database. Durability and determinism cover different
  failures; the design needs both.
* **`no-durable-intent` doubles nothing in the REST tier.** With a derived
  requestid on the wire, a process that forgot it was mid-call simply sends
  the same id again and QBO collapses it. The REST crash tests catch the
  mutation by asserting the intent row survived, not by finding a duplicate.
  The duplicate appears exactly where there is no requestid -- the GraphQL
  project -- and that is where `test_crash_between_create_project_and_persist_adopts_by_planted_name`
  catches it by counting projects. Correct behaviour, not a coverage gap.

## Projects, honestly

This is the part a demo skips and a quarter-end discovers.

* **A REST sub-customer is not a Project.** The REST `Customer` entity has
  writable `Job` and `ParentRef` fields, which is how sub-customers are made.
  It also has an `IsProject` field, and that field is **read-only**: send it
  on a create and it is ignored. A sub-customer therefore never appears in
  the Projects tab or in Project Profitability, whatever it is called.
* **Real Projects are created through the Projects API, which is GraphQL**
  (`https://qb.api.intuit.com/graphql`, sandbox
  `https://qb-sandbox.api.intuit.com/graphql`), scope
  `project-management.project`, mutation `projectManagementCreateProject`,
  query `projectManagementProjects`.
  [Docs](https://developer.intuit.com/app/developer/qbo/docs/workflows/manage-projects/get-started),
  [sample app](https://github.com/IntuitDeveloper/Sampleapp-Project-Nodejs).
* **That API is available only to Silver, Gold and Platinum partners of the
  Intuit App Partner Program**
  ([programme guide, PDF](https://static.developer.intuit.com/resources/Intuit_App_Partner_Program_Guide.pdf)).
  Silver carries a programme fee (US$300/month at the time of writing) and
  access has to be requested; Gold and Platinum have it by default. The free
  Builder tier -- which is what a private internal app is -- gets a **403**.

So a private HubSpot-to-QBO sync has exactly two honest options:

1. **Join the partner programme** at Silver, request Projects API access, and
   create real Projects. This repo's `silver` tier models that path.
2. **Accept sub-customers as the container** for invoices and payments, and
   make sure every record and every report says `subcustomer_fallback`, so
   nobody reads "Project" off a dashboard and looks for it in the Projects
   tab. This repo's default tier models that path, and the ledger field and
   the surfaced notice are the whole point of it.

What is not an option is the third thing, which is what the code does
without the flag: create the sub-customer, call it a project, and let the
difference be found by whoever runs Project Profitability first.

## Layout

```
qbo_idempotent_sync_proof/
  ledger.py        # SQLite operational record: intent-before-send, derived requestid,
                   # unique bindings, project_mode
  auth.py          # OAuth 2.0 refresh with rotation, persisted every time
  client.py        # transport that keeps "failed" and "unknown" apart; requestid on
                   # every create; 429 -> same request again; 401 -> refresh once
  sync.py          # the step machine: customer -> project -> invoice -> payments ->
                   # read-back; interrogate-first recovery; the honest project path
  fake_qbo.py      # QBO on a socket: requestid replay, 6240, query subset, IsProject
                   # read-only, tier-gated /graphql, refresh rotation, 429, faults
  _crash_child.py  # a worker killed for real with os._exit, mid-step
scripts/
  verify_the_suite_bites.py   # mutation harness
  live_smoke.py               # one find-or-create against a real sandbox, if configured
tests/
  test_scenario_1_customer_exactly_once.py
  test_scenario_2_projects_honest_path.py
  test_scenario_3_tokens_and_throttle.py
  test_scenario_4_payments_and_ledger.py
```

Python 3.10+. No runtime dependencies; `pytest` only to run the proof.

## Scope, honestly

The suite runs against a **local fake** that encodes the behaviours Intuit
documents, linked above. It has **not yet been run against an Intuit
sandbox**, and this repo makes no claim of production use. Specifically,
the fake models these documented facts:

* `requestid` on `POST /v3/company/{realmId}/{customer,invoice,payment}`:
  same requestid, same company -> same response, no second entity; maximum
  50 characters.
  [What is RequestId](https://help.developer.intuit.com/s/article/What-is-RequestId-and-its-usage),
  [best practices](https://help.developer.intuit.com/s/article/QuickBooks-Online-API-Best-Practices)
* `DisplayName` unique across Customer, Vendor and Employee -> HTTP 400,
  `ValidationFault`, code `6240`.
  [6240](https://help.developer.intuit.com/s/article/6240-Duplicate-Name-Exists-Error)
* `GET /v3/company/{realmId}/query?query=select * from Customer where
  DisplayName = '...'`, `'` escaped as `\'`, `Active=true` unless
  `Active in (true, false)`.
  [SQL queries](https://help.developer.intuit.com/s/article/SQL-Queries)
* `IsProject` read-only on `Customer`; Projects API is GraphQL and
  partner-tier gated (see above).
* OAuth token endpoint
  `https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer`; access token
  3600 s; refresh token ~100 days, rotating, previous one expiring 24 h
  after a new one is issued.
  [oauth-jsclient](https://github.com/intuit/oauth-jsclient),
  [validity of refresh token](https://help.developer.intuit.com/s/article/Validity-of-Refresh-Token)
* Throttling at 500 requests/minute per realm per app, 10 concurrent ->
  HTTP 429, `errorCode=003001`, `ThrottleExceeded`.
* Payment linked to an Invoice through `Line[].LinkedTxn[{TxnId, TxnType:
  "Invoice"}]`.

And it goes **beyond the documentation** in these places, each modelled on
the side that makes the client's job harder, and each worth checking against
a sandbox before relying on it:

* the exact GraphQL argument names (`input.customerId`, `filter: {customerId,
  name}`) and the `edges { node }` response shape;
* the shape of the 403 body for a non-eligible app;
* whether a GraphQL project's `id` is the same id a REST `CustomerRef` can
  point at (the fake assumes yes -- projects are customer-shaped -- which is
  also why invoices here are billed to the project id);
* whether QBO caches an *error* response under a requestid (the fake does
  not; the design does not depend on it, because a name variant gets its own
  derived requestid);
* how long QBO remembers a requestid (undocumented; `forget_requestids()`
  models the window closing, and only name-bearing entities have a marker to
  fall back on);
* whether project names must be unique (the fake lets a test plant a
  duplicate, which is the condition the escalation exists for);
* the previous refresh token is rejected **immediately** here rather than
  after 24 hours, which changes when the stale store dies, not whether;
* the exact bodies of the 401 and 429 responses, which the client detects
  by status code plus a substring rather than by shape.

Deliberately not covered: the HubSpot side, webhook signature verification,
tax and item configuration on the invoice, multi-currency, and queue
semantics -- those are engineering, not proof.

### LIVE

To run the one thing that can be run live on a Builder-tier app -- a REST
find-or-create with a requestid -- against a real Intuit sandbox:

1. An [Intuit developer account](https://developer.intuit.com/) (free) and an
   app with the `com.intuit.quickbooks.accounting` scope; the sandbox
   company is created with the account.
2. The app's client id and secret, and a refresh token from the OAuth
   playground or your own authorisation flow.
3. Environment variables: `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`,
   `QBO_REFRESH_TOKEN`, `QBO_REALM_ID` (optionally `QBO_BASE_URL`; default
   `https://sandbox-quickbooks.api.intuit.com`).
4. `python scripts/live_smoke.py` -- it creates one customer with a derived
   requestid, sends the identical request again, and prints whether the Ids
   match. Without the variables it prints a skip line and exits 0. The
   rotated refresh token is persisted to `live_smoke.db`; read it back from
   there for the next run, because the one you passed in may be dead.

The GraphQL Projects step **cannot be exercised live on a Builder-tier app
at all**: it is the 403 path by construction. Exercising the `silver` path
for real requires the partner programme.
