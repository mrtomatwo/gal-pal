"""Test fixtures: a `FakeGraph` in-memory simulation of the subset of Microsoft Graph
endpoints that galpal calls, plus monkeypatch helpers that wire it into the module.

The fake honors `$top` and emits `@odata.nextLink` so `graph_paged`'s pagination
loop is exercised. PATCH on a contact merges `singleValueExtendedProperties` by
`id` (mirroring real Graph) instead of overwriting the array. It tracks every
request so tests can assert on traffic shape (e.g. "exactly one $batch with 3
sub-requests")."""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest
import requests

# Make the project's _galpal package importable from tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _galpal import cli
from _galpal import graph as graph_module

# --------------------------------------------------------------------------- helpers


class FakeResponse:
    """Stand-in for requests.Response with just the surface galpal uses.

    `iter_content` serializes `json_data` on demand so `graph_paged`'s
    ijson-based row-streaming sees the same shape it would see from a real
    Graph response. `close` is a no-op; both methods exist so `stream=True`
    code paths in `_galpal.graph` work against the fake without special-casing.
    """

    def __init__(self, status_code: int, json_data=None, *, text: str = "", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json

    def iter_content(self, chunk_size: int = 65536):
        # chunk_size ignored — test bodies fit in a single chunk.
        _ = chunk_size
        if self._json is None:
            return
        yield json.dumps(self._json).encode("utf-8")

    def close(self):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            msg = f"{self.status_code}: {self._json}"
            raise requests.HTTPError(msg)


class FakeGraph:
    """In-memory simulation of /v1.0 endpoints that galpal touches.

    State: directory users (the GAL), personal contacts, contact folders, master
    categories. Mutations come in via $batch sub-requests. Tests can also seed the
    `inject_429` queue to make the next N matching requests reply with 429."""

    BASE = graph_module.GRAPH  # "https://graph.microsoft.com/v1.0"

    def __init__(self):
        self.users: list[dict] = []
        self.contacts: list[dict] = []
        self.contact_folders: list[dict] = []
        self.folder_contacts: dict[str, list[dict]] = {}
        self.master_categories: list[dict] = []
        self._next_id = 1
        self.calls: list[tuple[str, str, dict | None]] = []  # (method, url, params/body)

        # Test hooks: each entry is a callable(method, url) -> bool. When it returns
        # True, the request is answered with 429 once and the entry is consumed.
        self._429_hooks: list = []

    # -- internal helpers ---------------------------------------------------

    def _new_id(self, prefix: str = "obj") -> str:
        v = f"{prefix}{self._next_id}"
        self._next_id += 1
        return v

    def _maybe_429(self, method: str, url: str) -> FakeResponse | None:
        for i, (hook, retry_after) in enumerate(self._429_hooks):
            if hook(method, url):
                self._429_hooks.pop(i)
                return FakeResponse(429, {"error": "throttled"}, headers={"Retry-After": retry_after})
        return None

    def queue_429(self, predicate=lambda m, u: True, *, retry_after: str = "0"):
        """Make the *next* matching request reply with 429.

        `retry_after` is the value put in the `Retry-After` response header — pass
        e.g. `"abc"` or `"Wed, 21 Oct 2015 07:28:00 GMT"` to drive `_parse_retry_after`'s
        malformed/HTTP-date paths.
        """
        self._429_hooks.append((predicate, retry_after))

    def _paged(self, items: list, url: str, params: dict | None) -> FakeResponse:
        """Return a Graph-shaped page of `items` honoring $top and emitting nextLink.

        We accept a `__cursor` query parameter on the URL itself to model server-
        issued nextLinks (Graph's nextLinks are absolute and self-contained — they
        re-encode every parameter the client originally sent).
        """
        from urllib.parse import parse_qs

        # Cursor lives either on the URL (set by us in a prior nextLink) or
        # implicitly at 0 for the first call.
        qs = parse_qs(urlparse(url).query)
        start = int(qs.get("__cursor", ["0"])[0])
        top = (params or {}).get("$top") or 200
        chunk = items[start : start + top]
        body: dict = {"value": chunk}
        nxt = start + top
        if nxt < len(items):
            sep = "&" if "?" in url else "?"
            # Strip any existing __cursor= so we don't accumulate them.
            base = re.sub(r"[?&]__cursor=\d+", "", url)
            sep = "&" if "?" in base else "?"
            body["@odata.nextLink"] = f"{base}{sep}__cursor={nxt}"
        return FakeResponse(200, body)

    # -- HTTP entry points used by monkeypatch -----------------------------

    def get(self, url, headers=None, params=None, timeout=None, *, stream=False):
        # stream/timeout absorbed but not actually used by the fake.
        _ = (headers, timeout, stream)
        self.calls.append(("GET", url, params))
        if (resp := self._maybe_429("GET", url)) is not None:
            return resp

        path = urlparse(url).path  # "/v1.0/me/contacts" etc.

        if path.endswith("/users"):
            return self._paged(list(self.users), url, params)

        if path.endswith("/me/contacts"):
            return self._paged([_with_eps(c) for c in self.contacts], url, params)

        if path.endswith("/me/contactFolders"):
            return FakeResponse(
                200,
                {"value": [{"id": f["id"], "displayName": f["displayName"]} for f in self.contact_folders]},
            )

        m = re.match(r".*/me/contactFolders/([^/]+)/contacts/\$count$", path)
        if m:
            fid = m.group(1)
            return FakeResponse(200, None, text=str(len(self.folder_contacts.get(fid, []))))

        m = re.match(r".*/me/contactFolders/([^/]+)/contacts$", path)
        if m:
            fid = m.group(1)
            return FakeResponse(
                200,
                {"value": [_with_eps(c) for c in self.folder_contacts.get(fid, [])]},
            )

        if path.endswith("/me/outlook/masterCategories"):
            return FakeResponse(200, {"value": list(self.master_categories)})

        return FakeResponse(404, {"error": f"unhandled GET {path}"})

    def post(self, url, headers=None, json=None, params=None, timeout=None):
        self.calls.append(("POST", url, json))
        if (resp := self._maybe_429("POST", url)) is not None:
            return resp

        if url.endswith("/$batch"):
            return self._handle_batch(json or {})

        return FakeResponse(404, {"error": f"unhandled POST {url}"})

    # -- batch dispatch -----------------------------------------------------

    def _handle_batch(self, body: dict) -> FakeResponse:
        responses = []
        for req in body.get("requests", []):
            rid = req["id"]
            method = req["method"]
            target = req["url"]
            sub_body = req.get("body") or {}

            try:
                status, resp_body = self._dispatch(method, target, sub_body)
            except _BatchError as e:
                status, resp_body = (
                    e.status,
                    {"error": {"code": e.code, "message": e.msg}},
                )

            responses.append({"id": rid, "status": status, "body": resp_body})
        return FakeResponse(200, {"responses": responses})

    def _dispatch(self, method: str, target: str, body: dict):
        if method == "POST" and target == "/me/contacts":
            cid = self._new_id("contact")
            contact = {**body, "id": cid}
            self.contacts.append(contact)
            return 201, contact

        m = re.match(r"^/me/contacts/([^/]+)$", target)
        if m:
            cid = m.group(1)
            for c in self.contacts:
                if c["id"] == cid:
                    if method == "PATCH":
                        # Real Graph merges singleValueExtendedProperties entries
                        # by `id` (the property name) — it doesn't overwrite the
                        # whole array. Simulate that, since several galpal code
                        # paths depend on unrelated EPs surviving a PATCH.
                        incoming = body.pop("singleValueExtendedProperties", None)
                        c.update(body)
                        if incoming is not None:
                            existing = {ep["id"]: ep for ep in c.get("singleValueExtendedProperties") or []}
                            for ep in incoming:
                                existing[ep["id"]] = ep
                            c["singleValueExtendedProperties"] = list(existing.values())
                        return 200, c
                    if method == "DELETE":
                        self.contacts.remove(c)
                        return 204, None
            raise _BatchError(404, "ItemNotFound", f"contact {cid} not found")

        m = re.match(r"^/me/contactFolders/([^/]+)$", target)
        if m and method == "DELETE":
            fid = m.group(1)
            self.contact_folders = [f for f in self.contact_folders if f["id"] != fid]
            self.folder_contacts.pop(fid, None)
            return 204, None

        m = re.match(r"^/me/outlook/masterCategories/([^/]+)$", target)
        if m and method == "DELETE":
            mid = m.group(1)
            self.master_categories = [c for c in self.master_categories if c["id"] != mid]
            return 204, None

        raise _BatchError(400, "BadRequest", f"unhandled batch sub-request {method} {target}")


class _BatchError(Exception):
    def __init__(self, status, code, msg):
        self.status, self.code, self.msg = status, code, msg


def _with_eps(contact: dict) -> dict:
    """Return a deep copy of the contact (with singleValueExtendedProperties surfaced).

    Deep-copy rather than shallow: list-valued fields (categories, businessPhones,
    emailAddresses, etc.) would otherwise be shared by reference between the
    fake's stored state and what the code-under-test sees. An accidental mutation
    on either side would corrupt the other and assertions could pass spuriously.
    """
    return copy.deepcopy(contact)


# --------------------------------------------------------------------------- factories


def make_user(
    uid="u1",
    *,
    name="Doe, Jane",
    given="Jane",
    surname="Doe",
    mail="jane@corp.example",
    upn=None,
    phone="+1-555-0100",
    title="Engineer",
    dept="Eng",
    company="Corp",
    office="HQ",
    street="1 Main St",
    city="Springfield",
    state="IL",
    postal="62704",
    country="US",
):
    return {
        "id": uid,
        "userType": "Member",
        "displayName": name,
        "givenName": given,
        "surname": surname,
        "mail": mail,
        "userPrincipalName": upn or mail,
        "jobTitle": title,
        "department": dept,
        "companyName": company,
        "officeLocation": office,
        "businessPhones": [phone] if phone else [],
        "mobilePhone": None,
        "streetAddress": street,
        "city": city,
        "state": state,
        "postalCode": postal,
        "country": country,
    }


def make_contact(cid="c1", *, name="Doe, Jane", emails=None, azure_id=None, categories=None, **extra):
    c = {
        "id": cid,
        "displayName": name,
        "givenName": extra.get("givenName"),
        "surname": extra.get("surname"),
        "emailAddresses": emails or [{"address": "jane@corp.example", "name": name}],
    }
    if categories is not None:
        c["categories"] = categories
    if azure_id is not None:
        c["singleValueExtendedProperties"] = [
            {"id": graph_module.EP_AZURE_ID, "value": azure_id},
        ]
    c.update({k: v for k, v in extra.items() if k not in ("givenName", "surname")})
    return c


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def graph(monkeypatch):
    """A FakeGraph wired into the _galpal package.

    Patches at the use site (`_galpal.graph.requests`, `_galpal.graph.time`,
    `_galpal.cli.get_token`) — i.e. the module that *owns* the symbol, never
    a re-export. This is the only stable patch point: re-imports/aliases
    elsewhere don't see the monkeypatch.
    """
    g = FakeGraph()
    monkeypatch.setattr(graph_module.requests, "get", g.get)
    monkeypatch.setattr(graph_module.requests, "post", g.post)
    monkeypatch.setattr(cli, "get_token", lambda client_id: "fake-token")
    monkeypatch.setattr("_galpal.graph.time.sleep", lambda *_: None)  # don't sleep on 429
    return g


@pytest.fixture
def run_cli(graph, monkeypatch, capsys):
    """Invoke galpal CLI main() with the given argv. Returns (exit_code, stdout, stderr)."""

    def _run(*args, stdin: str = ""):
        monkeypatch.setattr("sys.argv", ["dev_galpal.py", *args])
        # Tests run with non-TTY stdin (pytest captures it). The destructive-
        # confirmation prompt would refuse to run; set the explicit override
        # for tests that drive --apply through stdin scripts.
        monkeypatch.setenv("GALPAL_FORCE_NONINTERACTIVE", "1")
        if stdin:
            from io import StringIO

            monkeypatch.setattr("sys.stdin", StringIO(stdin))
            monkeypatch.setattr("builtins.input", lambda prompt="": stdin.split("\n", 1)[0])
        code = 0
        try:
            cli.main()
        except SystemExit as e:
            # sys.exit("msg") sets e.code to a string; the interpreter would normally
            # print it to stderr on shutdown, but we caught the exception, so emulate.
            if isinstance(e.code, str):
                sys.stderr.write(f"{e.code}\n")
                code = 1
            else:
                code = e.code if e.code is not None else 0
        out = capsys.readouterr()
        return code, out.out, out.err

    return _run


@pytest.fixture
def run_cli_recorded(graph, monkeypatch):
    """Like `run_cli`, but installs a `RecordingReporter` and returns it.

    The recorded shape is what new tests should prefer over `run_cli`'s
    (code, stdout, stderr) tuple — assertions on `rec.summary_kwargs` and
    `rec.events` survive cosmetic rendering changes that would otherwise
    break a `"deleted=2" in out` substring check. See `test_reporter.py`
    for an example of the recording-style assertion.
    """
    from _galpal.reporter import RecordingReporter

    def _run(*args, confirm_response: bool = True, stdin: str = ""):
        rec = RecordingReporter(confirm_response=confirm_response)
        # Force the reporter selector inside cli.main() to take a path that
        # bypasses TTY/JSON construction; we install our recorder directly
        # via monkeypatch on _build_reporter.
        monkeypatch.setattr("_galpal.cli._build_reporter", lambda mode: rec)
        monkeypatch.setattr("sys.argv", ["dev_galpal.py", *args])
        monkeypatch.setenv("GALPAL_FORCE_NONINTERACTIVE", "1")
        if stdin:
            from io import StringIO

            monkeypatch.setattr("sys.stdin", StringIO(stdin))
            monkeypatch.setattr("builtins.input", lambda prompt="": stdin.split("\n", 1)[0])
        code = 0
        try:
            cli.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        return code, rec

    return _run
