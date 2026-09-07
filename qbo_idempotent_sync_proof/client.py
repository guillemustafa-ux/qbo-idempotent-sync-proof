"""HTTP transport for QuickBooks Online, with the one distinction that matters.

A call against QBO has three outcomes, not two:

* it **succeeded** and you were told so;
* it **provably failed** -- the server answered with a Fault, so nothing was
  created (``6240 Duplicate Name Exists`` is the interesting one);
* the **outcome is unknown** -- the connection died, the request timed out,
  the process was killed. The write may or may not have landed.

Collapsing the third case into the second is the bug, so this transport
keeps them apart at the type level.

Two QBO-specific behaviours are handled here because they are transport
concerns, not business ones:

* every create carries a ``requestid`` query parameter. If QBO receives a
  second request with the same requestid for the same company, it returns
  the **same response as the original** instead of performing the operation
  again. That makes "resend it" both the question and the fix;
* a ``429`` with ``errorCode=003001 ThrottleExceeded`` is answered by
  resending the *same* request -- same URL, same requestid -- after a
  backoff. The backoff is injectable so the proof does not sleep.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .auth import Authenticator

# A dropped response surfaces as one of these. They do not all descend from
# OSError, so catching OSError alone silently misses the interesting case.
TRANSPORT_ERRORS = (
    urllib.error.URLError,
    http.client.RemoteDisconnected,
    http.client.IncompleteRead,
    http.client.BadStatusLine,
    OSError,
)

# QBO error codes this proof reacts to.
DUPLICATE_NAME = "6240"  # Duplicate Name Exists Error (ValidationFault, HTTP 400)
THROTTLE_EXCEEDED = "003001"  # ThrottleExceeded (HTTP 429)

# The fake marks a requestid replay with this response header so the log can
# say REPLAYED vs CREATED NOW. The real service does not send it; nothing in
# the client *decides* anything based on it.
REPLAY_HEADER = "X-Fake-Requestid-Replay"


class UnknownOutcome(Exception):
    """The request may or may not have been applied. Do not retry blindly."""


class QboRejected(Exception):
    """QBO answered with a Fault. Nothing was applied."""

    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body
        self.code, self.message = _fault_code_and_message(body)


class ProjectsApiUnavailable(QboRejected):
    """The Projects (GraphQL) API answered 403: this app's tier is not eligible."""


def _fault_code_and_message(body: str) -> tuple[str | None, str | None]:
    try:
        parsed = json.loads(body)
    except ValueError:
        return None, None
    fault = parsed.get("Fault") or parsed.get("fault") or {}
    errors = fault.get("Error") or fault.get("error") or []
    if not errors:
        return None, None
    first = errors[0]
    return first.get("code"), first.get("Message") or first.get("message")


def sql_quote(value: str) -> str:
    """Quote a value for the QBO query language: single quotes become ``\\'``."""
    return "'" + value.replace("'", "\\'") + "'"


@dataclass
class Created:
    entity: dict[str, Any]
    replayed: bool


@dataclass
class _Response:
    status: int
    body: dict[str, Any]
    headers: dict[str, str]


class QboClient:
    def __init__(
        self,
        base_url: str,
        realm_id: str,
        auth: Authenticator,
        *,
        graphql_url: str | None = None,
        timeout: float = 5.0,
        max_throttle_retries: int = 5,
        backoff: Callable[[int], float] = lambda attempt: min(2.0**attempt, 30.0),
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.realm_id = realm_id
        self.auth = auth
        self.graphql_url = graphql_url or f"{self.base_url}/graphql"
        self.timeout = timeout
        self.max_throttle_retries = max_throttle_retries
        self.backoff = backoff
        self.sleep = sleep

    # -- transport --------------------------------------------------------

    def _company(self, path: str, query: dict[str, str] | None = None) -> str:
        url = f"{self.base_url}/v3/company/{self.realm_id}/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    def _request(
        self, method: str, url: str, *, body: dict[str, Any] | None = None
    ) -> _Response:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        refreshed = False
        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Accept", "application/json")
            req.add_header("Authorization", f"Bearer {self.auth.access_token()}")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return _Response(
                        resp.status, json.loads(raw) if raw else {}, dict(resp.headers)
                    )
            except urllib.error.HTTPError as exc:
                # The server spoke. Whatever it says, it is a definite answer.
                raw = exc.read().decode("utf-8", "replace")
                if exc.code == 401 and not refreshed:
                    # Access token expired under us. One refresh, one retry.
                    self.auth.refresh()
                    refreshed = True
                    continue
                if exc.code == 429 and THROTTLE_EXCEEDED in raw:
                    if attempt >= self.max_throttle_retries:
                        raise QboRejected(exc.code, raw) from exc
                    # Same URL, therefore the same requestid: if the first
                    # send did land, QBO replays it instead of doubling it.
                    self.sleep(self.backoff(attempt))
                    attempt += 1
                    continue
                if exc.code == 403 and url == self.graphql_url:
                    raise ProjectsApiUnavailable(exc.code, raw) from exc
                raise QboRejected(exc.code, raw) from exc
            except TRANSPORT_ERRORS as exc:
                # Nobody spoke. The write's fate is unknowable from here.
                raise UnknownOutcome(f"{method} {url}: {exc}") from exc

    def _create(self, entity: str, body: dict[str, Any], requestid: str | None) -> Created:
        # ``requestid=None`` exists only so the anti-patterns can send a
        # create with no idempotency at all. The sync never does.
        params = {"requestid": requestid} if requestid is not None else None
        resp = self._request("POST", self._company(entity.lower(), params), body=body)
        return Created(resp.body[entity], resp.headers.get(REPLAY_HEADER) == "1")

    # -- REST operations --------------------------------------------------

    def create_customer(self, body: dict[str, Any], *, requestid: str | None) -> Created:
        return self._create("Customer", body, requestid)

    def create_invoice(self, body: dict[str, Any], *, requestid: str | None) -> Created:
        return self._create("Invoice", body, requestid)

    def create_payment(self, body: dict[str, Any], *, requestid: str | None) -> Created:
        return self._create("Payment", body, requestid)

    def get_customer(self, customer_id: str) -> dict[str, Any]:
        return self._request("GET", self._company(f"customer/{customer_id}")).body["Customer"]

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        return self._request("GET", self._company(f"invoice/{invoice_id}")).body["Invoice"]

    def query(self, sql: str) -> dict[str, Any]:
        return self._request("GET", self._company("query", {"query": sql})).body.get(
            "QueryResponse", {}
        )

    def customers_named(self, display_name: str, *, include_inactive: bool = False) -> list:
        sql = f"select * from Customer where DisplayName = {sql_quote(display_name)}"
        if include_inactive:
            # Queries return only Active=true unless asked otherwise.
            sql += " and Active in (true, false)"
        return self.query(sql).get("Customer", [])

    # -- Projects (GraphQL) -----------------------------------------------

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        resp = self._request(
            "POST", self.graphql_url, body={"query": query, "variables": variables}
        )
        return resp.body.get("data", {})

    def create_project(self, customer_id: str, name: str) -> dict[str, Any]:
        data = self._graphql(
            "mutation CreateProject($input: ProjectManagementCreateProjectInput!) {"
            " projectManagementCreateProject(input: $input) { id name customer { id } } }",
            {"input": {"name": name, "customerId": customer_id}},
        )
        return data["projectManagementCreateProject"]

    def find_projects(self, customer_id: str, name: str) -> list[dict[str, Any]]:
        data = self._graphql(
            "query FindProjects($customerId: ID!, $name: String!) {"
            " projectManagementProjects(filter: {customerId: $customerId, name: $name})"
            " { edges { node { id name customer { id } } } } }",
            {"customerId": customer_id, "name": name},
        )
        edges = data.get("projectManagementProjects", {}).get("edges", [])
        return [e["node"] for e in edges]
