"""A QuickBooks Online stand-in on a real socket, encoding the documented behaviours.

This is not a generic mock. It models the specific things QBO does that
decide whether an integration is exactly-once or merely usually-once:

* **requestid replay** on REST creates: a second request with the same
  ``requestid`` for the same company returns the *original* response --
  including when the body is different. No second entity.
* **DisplayName uniqueness** across Customer, Vendor and Employee:
  ``HTTP 400``, ``ValidationFault``, code ``6240``.
* **Query** language subset: ``select * from Customer where DisplayName =
  '...'`` with ``\\'`` escaping, ``Active=true`` by default, and
  ``Active in (true, false)`` to include inactive rows.
* **IsProject is read-only**: sent on a create, it is ignored, and a REST
  sub-customer is never a Project.
* **Projects API is GraphQL and tier-gated**: ``/graphql`` honours
  ``projectManagementCreateProject`` and ``projectManagementProjects`` only
  when the server is started as a Silver-or-above partner; otherwise 403.
* **OAuth refresh rotation**: a refresh returns a new refresh token and the
  previous one is rejected afterwards (immediately here; the real service
  gives 24 hours of grace, which only changes *when* the stale store dies).
* **Throttling**: ``HTTP 429`` with ``errorCode=003001 ThrottleExceeded``.

Faults are armed per request and cut the connection at the exact seam where
the outcome becomes unknowable to the caller.

Where this file goes beyond the documentation (the exact GraphQL argument
names, the shape of the 403 body, whether error responses are cached under a
requestid) it says so in the README and errs on the side of the behaviour
that makes the client's job *harder*.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

# Credentials the fake token endpoint accepts (Basic auth on the refresh call).
CLIENT_ID = "proof-client-id"
CLIENT_SECRET = "proof-client-secret"
REALM_ID = "9130351234567890"

# Tiers of the Intuit App Partner Program. Only Silver and above can use the
# Projects API; Builder (free) cannot.
TIER_BUILDER = "builder"
TIER_SILVER = "silver"
PROJECTS_ELIGIBLE = (TIER_SILVER, "gold", "platinum")

# Routes a fault can be armed on.
ROUTE_CUSTOMER = "customer"
ROUTE_INVOICE = "invoice"
ROUTE_PAYMENT = "payment"
ROUTE_PROJECT = "project"  # the GraphQL create

FAULT_BEFORE_COMMIT = "before_commit"  # nothing was created; safe to retry
FAULT_AFTER_COMMIT = "after_commit"  # created, but the caller never hears so

_QUERY_RE = re.compile(
    r"^\s*select\s+\*\s+from\s+Customer\s+where\s+DisplayName\s*=\s*'((?:[^'\\]|\\')*)'"
    r"(?P<active>\s+and\s+Active\s+in\s*\(\s*true\s*,\s*false\s*\))?\s*$",
    re.IGNORECASE,
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S-07:00", time.gmtime())


@dataclass
class QboState:
    """The service's own storage, plus its configuration."""

    partner_tier: str = TIER_BUILDER
    rotate_refresh_on_use: bool = True

    customers: dict[str, dict[str, Any]] = field(default_factory=dict)
    invoices: dict[str, dict[str, Any]] = field(default_factory=dict)
    payments: dict[str, dict[str, Any]] = field(default_factory=dict)
    # (realm, requestid) -> the original (status, body) of a successful create.
    requestid_replays: dict[tuple[str, str], tuple[int, dict[str, Any]]] = field(
        default_factory=dict
    )
    # OAuth.
    access_tokens: set[str] = field(default_factory=set)
    refresh_token: str = field(default_factory=lambda: "rt-" + secrets.token_hex(12))
    retired_refresh_tokens: set[str] = field(default_factory=set)
    # Faults armed for the next matching request, popped as they fire.
    armed: list[tuple[str, str]] = field(default_factory=list)
    throttle_remaining: int = 0

    request_log: list[str] = field(default_factory=list)
    _seq: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def next_id(self) -> str:
        self._seq += 1
        return str(self._seq)

    def arm(self, route: str, fault: str) -> None:
        self.armed.append((route, fault))

    def take_fault(self, route: str) -> str | None:
        for i, (target, fault) in enumerate(self.armed):
            if target == route:
                self.armed.pop(i)
                return fault
        return None

    def name_taken(self, display_name: str) -> bool:
        return any(c["DisplayName"] == display_name for c in self.customers.values())

    def add_customer(self, body: dict[str, Any], *, is_project: bool = False) -> dict[str, Any]:
        customer_id = self.next_id()
        customer = {
            "Id": customer_id,
            "SyncToken": "0",
            "DisplayName": body["DisplayName"],
            "Notes": body.get("Notes", ""),
            "Job": bool(body.get("Job", False)) or is_project,
            "ParentRef": body.get("ParentRef"),
            # Read-only: whatever the request said, REST creates are not projects.
            "IsProject": is_project,
            "Active": True,
            "Balance": 0.0,
            "MetaData": {"CreateTime": _now_iso()},
        }
        self.customers[customer_id] = customer
        return customer

    def projects_under(self, customer_id: str, name: str | None = None) -> list[dict[str, Any]]:
        return [
            c
            for c in self.customers.values()
            if c["IsProject"]
            and (c.get("ParentRef") or {}).get("value") == customer_id
            and (name is None or c["DisplayName"] == name)
        ]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> QboState:
        return self.server.state  # type: ignore[attr-defined]

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # silence stderr
        pass

    def _read_body(self) -> bytes:
        # Read once and cache. The request body must be consumed before any
        # early reply (401, 429, 403): closing a socket with unread request
        # bytes makes the OS answer with RST, and on Windows the client then
        # sees ConnectionAbortedError instead of the status we sent.
        if not hasattr(self, "_body_cache"):
            length = int(self.headers.get("Content-Length") or 0)
            self._body_cache = self.rfile.read(length) if length else b""
        return self._body_cache

    def _send_json(
        self, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _fault(self, status: int, code: str, message: str, detail: str, kind: str) -> None:
        self._send_json(
            status,
            {
                "Fault": {
                    "Error": [{"Message": message, "Detail": detail, "code": code}],
                    "type": kind,
                },
                "time": _now_iso(),
            },
        )

    def _cut_the_wire(self) -> None:
        """Drop the connection with no response at all.

        The caller sees a transport error. It cannot distinguish "the request
        never landed" from "the request landed and the answer was lost" --
        which is the entire problem this repo is about.
        """
        self.close_connection = True
        try:
            self.wfile.close()
            self.connection.close()
        except OSError:
            pass

    def _authorised(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        token = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""
        if token in self.state.access_tokens:
            return True
        self._send_json(
            401,
            {
                "fault": {
                    "error": [
                        {
                            "message": "message=AuthenticationFailed; errorCode=003200; "
                            "statusCode=401",
                            "detail": "Token expired or invalid",
                            "code": "3200",
                        }
                    ],
                    "type": "AUTHENTICATION",
                }
            },
        )
        return False

    def _throttled(self) -> bool:
        if self.state.throttle_remaining <= 0:
            return False
        self.state.throttle_remaining -= 1
        self.state.request_log.append(f"429 ThrottleExceeded {self.path}")
        self._fault(
            429,
            "3001",
            "message=ThrottleExceeded; errorCode=003001; statusCode=429",
            "Throttle exceeded (500 requests per minute per realm, 10 concurrent)",
            "ValidationFault",
        )
        return True

    # -- routing ----------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        self._read_body()  # drain the request before any early reply
        if parsed.path == "/oauth2/v1/tokens/bearer":
            self._token_endpoint()
            return
        with self.state._lock:
            if not self._authorised() or self._throttled():
                return
            if parsed.path == "/graphql":
                self._graphql()
                return
            m = re.match(r"^/v3/company/([^/]+)/(customer|invoice|payment)$", parsed.path)
            if not m:
                self._send_json(404, {"error": "no such route"})
                return
            realm, entity = m.group(1), m.group(2)
            requestid = (query.get("requestid") or [None])[0]
            body = json.loads(self._read_body() or b"{}")
            self._create(realm, entity, requestid, body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        with self.state._lock:
            if not self._authorised() or self._throttled():
                return
            m = re.match(
                r"^/v3/company/([^/]+)/(query|customer/[^/]+|invoice/[^/]+|payment/[^/]+)$",
                parsed.path,
            )
            if not m:
                self._send_json(404, {"error": "no such route"})
                return
            what = m.group(2)
            if what == "query":
                self._query((query.get("query") or [""])[0])
                return
            kind, entity_id = what.split("/")
            table = {
                "customer": self.state.customers,
                "invoice": self.state.invoices,
                "payment": self.state.payments,
            }[kind]
            entity = table.get(entity_id)
            if entity is None:
                self._fault(400, "610", "Object Not Found", f"{kind} {entity_id}", "ValidationFault")
                return
            self._send_json(200, {kind.capitalize(): entity, "time": _now_iso()})

    # -- OAuth ------------------------------------------------------------

    def _token_endpoint(self) -> None:
        state = self.state
        form = parse_qs(self._read_body().decode("utf-8"))
        auth = self.headers.get("Authorization") or ""
        expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        with state._lock:
            state.request_log.append("POST /oauth2/v1/tokens/bearer")
            if auth != f"Basic {expected}":
                self._send_json(401, {"error": "invalid_client"})
                return
            if (form.get("grant_type") or [""])[0] != "refresh_token":
                self._send_json(400, {"error": "unsupported_grant_type"})
                return
            presented = (form.get("refresh_token") or [""])[0]
            if presented != state.refresh_token:
                reason = "retired" if presented in state.retired_refresh_tokens else "unknown"
                self._send_json(
                    400,
                    {"error": "invalid_grant", "error_description": f"refresh token {reason}"},
                )
                return
            access = "at-" + secrets.token_hex(12)
            state.access_tokens.add(access)
            if state.rotate_refresh_on_use:
                state.retired_refresh_tokens.add(state.refresh_token)
                state.refresh_token = "rt-" + secrets.token_hex(12)
            self._send_json(
                200,
                {
                    "token_type": "bearer",
                    "access_token": access,
                    "expires_in": 3600,
                    "refresh_token": state.refresh_token,
                    "x_refresh_token_expires_in": 8726400,
                },
            )

    # -- REST creates -----------------------------------------------------

    def _create(self, realm: str, entity: str, requestid: str | None, body: dict) -> None:
        state = self.state
        state.request_log.append(f"POST /{entity} requestid={requestid or '-'}")
        if requestid is not None and len(requestid) > 50:
            self._fault(400, "2010", "Invalid requestid", "max 50 characters", "ValidationFault")
            return

        key = (realm, requestid) if requestid else None
        if key is not None and key in state.requestid_replays:
            # Replay. Note what a faithful implementation does NOT do: it does
            # not apply the new body. The original response is returned
            # verbatim, however different this payload is.
            status, original = state.requestid_replays[key]
            self._send_json(status, original, {"X-Fake-Requestid-Replay": "1"})
            return

        route = {"customer": ROUTE_CUSTOMER, "invoice": ROUTE_INVOICE, "payment": ROUTE_PAYMENT}[
            entity
        ]
        fault = state.take_fault(route)
        if fault == FAULT_BEFORE_COMMIT:
            self._cut_the_wire()
            return

        if entity == "customer":
            if state.name_taken(body.get("DisplayName", "")):
                # Definite, and NOT cached under the requestid: a rejected
                # request created nothing, so there is nothing to replay.
                self._fault(
                    400,
                    "6240",
                    "Duplicate Name Exists Error",
                    "The name supplied already exists. : Another customer, vendor or "
                    "employee is already using this name. Please use a different name.",
                    "ValidationFault",
                )
                return
            created = state.add_customer(body)
            payload = {"Customer": created, "time": _now_iso()}
        elif entity == "invoice":
            total = round(sum(float(line.get("Amount", 0)) for line in body.get("Line", [])), 2)
            invoice_id = state.next_id()
            created = {
                "Id": invoice_id,
                "SyncToken": "0",
                "CustomerRef": body.get("CustomerRef"),
                "PrivateNote": body.get("PrivateNote", ""),
                "Line": body.get("Line", []),
                "TotalAmt": total,
                "Balance": total,
                "LinkedTxn": [],
                "MetaData": {"CreateTime": _now_iso()},
            }
            state.invoices[invoice_id] = created
            payload = {"Invoice": created, "time": _now_iso()}
        else:
            payment_id = state.next_id()
            amount = round(float(body.get("TotalAmt", 0)), 2)
            created = {
                "Id": payment_id,
                "SyncToken": "0",
                "CustomerRef": body.get("CustomerRef"),
                "TxnDate": body.get("TxnDate"),
                "PrivateNote": body.get("PrivateNote", ""),
                "TotalAmt": amount,
                "Line": body.get("Line", []),
                "MetaData": {"CreateTime": _now_iso()},
            }
            for line in body.get("Line", []):
                for linked in line.get("LinkedTxn", []):
                    if linked.get("TxnType") == "Invoice":
                        invoice = state.invoices.get(str(linked.get("TxnId")))
                        if invoice is None:
                            self._fault(
                                400, "610", "Object Not Found", "linked invoice", "ValidationFault"
                            )
                            return
                        invoice["Balance"] = round(
                            invoice["Balance"] - float(line.get("Amount", 0)), 2
                        )
                        invoice["LinkedTxn"].append({"TxnId": payment_id, "TxnType": "Payment"})
            state.payments[payment_id] = created
            payload = {"Payment": created, "time": _now_iso()}

        if key is not None:
            state.requestid_replays[key] = (200, payload)

        if fault == FAULT_AFTER_COMMIT:
            # Committed on this side; the caller will never find out.
            self._cut_the_wire()
            return
        self._send_json(200, payload)

    # -- query ------------------------------------------------------------

    def _query(self, sql: str) -> None:
        state = self.state
        state.request_log.append(f"GET /query {sql}")
        m = _QUERY_RE.match(sql)
        if not m:
            self._fault(400, "4000", "Error parsing query", sql, "ValidationFault")
            return
        wanted = m.group(1).replace("\\'", "'")
        include_inactive = m.group("active") is not None
        rows = [
            c
            for c in state.customers.values()
            if c["DisplayName"] == wanted and (include_inactive or c["Active"])
        ]
        response: dict[str, Any] = {"QueryResponse": {}, "time": _now_iso()}
        if rows:
            response["QueryResponse"] = {
                "Customer": rows,
                "startPosition": 1,
                "maxResults": len(rows),
            }
        self._send_json(200, response)

    # -- Projects (GraphQL) -----------------------------------------------

    def _graphql(self) -> None:
        state = self.state
        body = json.loads(self._read_body() or b"{}")
        query = body.get("query", "")
        variables = body.get("variables", {})
        if state.partner_tier not in PROJECTS_ELIGIBLE:
            state.request_log.append("POST /graphql -> 403 (tier)")
            self._send_json(
                403,
                {
                    "errors": [
                        {
                            "message": "This app is not eligible for the Projects API. "
                            "Access requires the Intuit App Partner Program, Silver tier "
                            "or above.",
                            "extensions": {"code": "FORBIDDEN"},
                        }
                    ]
                },
            )
            return

        if "projectManagementCreateProject" in query:
            payload = variables.get("input", {})
            state.request_log.append("POST /graphql projectManagementCreateProject")
            fault = state.take_fault(ROUTE_PROJECT)
            if fault == FAULT_BEFORE_COMMIT:
                self._cut_the_wire()
                return
            project = state.add_customer(
                {
                    "DisplayName": payload["name"],
                    "ParentRef": {"value": payload["customerId"]},
                    "Job": True,
                },
                is_project=True,
            )
            if fault == FAULT_AFTER_COMMIT:
                self._cut_the_wire()
                return
            self._send_json(
                200,
                {
                    "data": {
                        "projectManagementCreateProject": {
                            "id": project["Id"],
                            "name": project["DisplayName"],
                            "customer": {"id": payload["customerId"]},
                        }
                    }
                },
            )
            return

        if "projectManagementProjects" in query:
            state.request_log.append("POST /graphql projectManagementProjects")
            found = state.projects_under(variables.get("customerId"), variables.get("name"))
            self._send_json(
                200,
                {
                    "data": {
                        "projectManagementProjects": {
                            "edges": [
                                {
                                    "node": {
                                        "id": p["Id"],
                                        "name": p["DisplayName"],
                                        "customer": {"id": p["ParentRef"]["value"]},
                                    }
                                }
                                for p in found
                            ]
                        }
                    }
                },
            )
            return

        self._send_json(400, {"errors": [{"message": "unsupported operation"}]})


class FakeQbo:
    """A running QuickBooks Online stand-in on localhost."""

    def __init__(self, *, partner_tier: str = TIER_BUILDER, rotate_refresh_on_use: bool = True):
        self.state = QboState(partner_tier=partner_tier, rotate_refresh_on_use=rotate_refresh_on_use)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.state = self.state  # type: ignore[attr-defined]
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> "FakeQbo":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def token_url(self) -> str:
        return f"{self.base_url}/oauth2/v1/tokens/bearer"

    @property
    def refresh_token(self) -> str:
        """The refresh token the authorisation flow would have handed over."""
        return self.state.refresh_token

    # -- fault injection --------------------------------------------------

    def drop_response_after_commit(self, route: str) -> None:
        self.state.arm(route, FAULT_AFTER_COMMIT)

    def drop_response_before_commit(self, route: str) -> None:
        self.state.arm(route, FAULT_BEFORE_COMMIT)

    def throttle_next(self, n: int) -> None:
        self.state.throttle_remaining = n

    def rotate_refresh_token_now(self) -> None:
        """Retire the current refresh token, as another instance's refresh would."""
        with self.state._lock:
            self.state.retired_refresh_tokens.add(self.state.refresh_token)
            self.state.refresh_token = "rt-" + secrets.token_hex(12)

    def revoke_access_tokens_now(self) -> None:
        """Every access token expires at once: the next API call gets a 401."""
        with self.state._lock:
            self.state.access_tokens.clear()

    def forget_requestids(self) -> None:
        """Model the replay window closing: nothing is recognised any more."""
        with self.state._lock:
            self.state.requestid_replays.clear()

    def plant_project(self, customer_id: str, name: str) -> dict[str, Any]:
        """Test-only: a project that exists without having been created through the API."""
        with self.state._lock:
            return self.state.add_customer(
                {"DisplayName": name, "ParentRef": {"value": customer_id}, "Job": True},
                is_project=True,
            )

    # -- introspection ----------------------------------------------------

    @property
    def customers(self) -> dict[str, dict[str, Any]]:
        """Top-level customers: no parent, not a project."""
        return {
            k: v
            for k, v in self.state.customers.items()
            if not v["IsProject"] and not v.get("ParentRef")
        }

    @property
    def sub_customers(self) -> dict[str, dict[str, Any]]:
        """REST sub-customers (``Job=true`` under a parent) -- never projects."""
        return {
            k: v
            for k, v in self.state.customers.items()
            if not v["IsProject"] and v.get("ParentRef")
        }

    @property
    def projects(self) -> dict[str, dict[str, Any]]:
        return {k: v for k, v in self.state.customers.items() if v["IsProject"]}

    @property
    def invoices(self) -> dict[str, dict[str, Any]]:
        return self.state.invoices

    @property
    def payments(self) -> dict[str, dict[str, Any]]:
        return self.state.payments

    @property
    def request_log(self) -> list[str]:
        return self.state.request_log

    def customers_named(self, display_name: str) -> list[dict[str, Any]]:
        return [c for c in self.state.customers.values() if c["DisplayName"] == display_name]
