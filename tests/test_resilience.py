"""Tests for the HTTP resilience layer in `_galpal/graph.py`.

Three failure shapes that other test files don't exercise:
  - `@odata.nextLink` pagination across multiple pages (a test that issues only
    one /users page would never notice a regression in `graph_paged`'s "drop
    params after the first page" rule)
  - `Retry-After` parsing for malformed / HTTP-date / negative / huge values
    (the old `int(value)` shape raised ValueError on RFC-legal HTTP-date)
  - Top-level 429 / 5xx / connection-error retry on GET (the $batch path is
    covered elsewhere; this file covers `graph_paged`)

Plus `chunked_batch`'s success-status set, per-chunk progress accounting, and
the MAX_BATCH_429_RETRIES bail-out in `send_batch`.
"""

from __future__ import annotations

import pytest
import requests

from _galpal import graph as graph_mod
from _galpal.filters import FilterConfig

from .conftest import FakeResponse, make_user

# --------------------------------------------------------------------------- _parse_retry_after


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, graph_mod.DEFAULT_RETRY_AFTER_S),
        ("", graph_mod.DEFAULT_RETRY_AFTER_S),
        ("   ", graph_mod.DEFAULT_RETRY_AFTER_S),
        ("3", 3),
        ("0", 0),
        ("-3", 0),  # negative clamped to 0; doesn't raise time.sleep ValueError
        ("abc", graph_mod.DEFAULT_RETRY_AFTER_S),  # garbage falls back, doesn't crash
        ("999999999", graph_mod.MAX_RETRY_AFTER_S),  # huge number capped
    ],
)
def test_parse_retry_after_handles_all_shapes(value, expected):
    assert graph_mod._parse_retry_after(value) == expected


def test_parse_retry_after_understands_http_date():
    """RFC 9110 §10.2.3 lets Retry-After be an HTTP-date — Graph rarely sends this
    but proxies in the path can rewrite. Must not crash."""
    # An HTTP-date in the future should produce a positive (capped) wait.
    n = graph_mod._parse_retry_after("Wed, 31 Dec 2099 23:59:59 GMT")
    assert 0 < n <= graph_mod.MAX_RETRY_AFTER_S
    # An HTTP-date in the past should produce 0 (don't sleep, but don't crash).
    assert graph_mod._parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0


# --------------------------------------------------------------------------- pagination


def test_graph_paged_follows_next_link(graph):
    """Seed enough users to span 3 pages at the GAL's $top=200, then verify
    graph_paged yields all of them and made the right number of GET calls."""
    for i in range(450):
        graph.users.append(make_user(f"u{i}", mail=f"u{i}@x.com"))
    out = list(graph_mod.fetch_gal("t", FilterConfig()))
    assert len(out) == 450
    user_calls = [c for c in graph.calls if c[1].endswith("/users") or "/users?" in c[1]]
    # 3 pages: ⌈450/200⌉ = 3.
    assert len(user_calls) == 3


def test_graph_paged_handles_next_link_before_value(graph, monkeypatch):
    """`@odata.nextLink` can legally appear before *or* after `value` in the
    JSON response. The pre-ijson `r.json()` shape didn't care about key order;
    the row-streaming forward-pass parser does, so this is a real regression
    point. Drive the order-flipped shape directly and verify both pages still
    chain correctly."""
    calls = {"n": 0}

    class ReorderedResponse(FakeResponse):
        def iter_content(self, chunk_size=65536):
            yield self._raw

    def reorder_get(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # @odata.nextLink before value on purpose.
            body = (
                b'{"@odata.nextLink":"'
                + f"{graph_mod.GRAPH}/users?page=2".encode()
                + b'","value":[{"id":"u1","userType":"Member","displayName":"a",'
                b'"mail":"a@x.com","userPrincipalName":"a@x.com"}]}'
            )
        else:
            # Final page: no @odata.nextLink, value last.
            body = (
                b'{"value":[{"id":"u2","userType":"Member","displayName":"b",'
                b'"mail":"b@x.com","userPrincipalName":"b@x.com"}]}'
            )
        r = ReorderedResponse(200, None)
        r._raw = body
        return r

    monkeypatch.setattr("_galpal.graph.requests.get", reorder_get)
    out = list(graph_mod.fetch_gal("t", FilterConfig()))
    assert [u["id"] for u in out] == ["u1", "u2"]
    assert calls["n"] == 2


def test_graph_paged_drops_initial_params_after_first_page(graph):
    """`@odata.nextLink` already encodes every parameter from the original GET, so
    `graph_paged` zeroes out `params` after the first call. Verify by checking
    that the second call's `params` is None (FakeGraph records the params arg)."""
    for i in range(250):
        graph.users.append(make_user(f"u{i}", mail=f"u{i}@x.com"))
    list(graph_mod.fetch_gal("t", FilterConfig()))
    user_calls = [(method, url, params) for method, url, params in graph.calls if "/users" in url]
    assert len(user_calls) >= 2
    # First call carries params (the $select + $top); subsequent calls don't.
    assert user_calls[0][2] is not None
    assert all(p is None for _, _, p in user_calls[1:])


# --------------------------------------------------------------------------- top-level 429 on GET


def test_graph_paged_retries_on_top_level_429(graph):
    """A 429 on the first /users GET retries transparently and the iteration
    completes. Mirrors `test_pull_retries_on_top_level_429` (which tests the
    $batch side); this one tests graph_paged's retry."""
    graph.users.append(make_user("u1"))
    graph.queue_429(lambda m, u: u.endswith("/users"))
    out = list(graph_mod.fetch_gal("t", FilterConfig()))
    assert [u["id"] for u in out] == ["u1"]
    user_calls = [c for c in graph.calls if "/users" in c[1]]
    assert len(user_calls) == 2  # one 429 + one successful retry


@pytest.mark.parametrize("retry_after", ["", "abc", "Wed, 21 Oct 2015 07:28:00 GMT", "-3"])
def test_graph_paged_tolerates_malformed_retry_after(graph, retry_after):
    """The pre-fix code did `int(r.headers.get("Retry-After", "5"))` directly,
    which raised ValueError on every value below — and aborted the run. Now
    parsing falls back gracefully via `_parse_retry_after`."""
    graph.users.append(make_user("u1"))
    graph.queue_429(lambda m, u: u.endswith("/users"), retry_after=retry_after)
    out = list(graph_mod.fetch_gal("t", FilterConfig()))
    assert [u["id"] for u in out] == ["u1"]


# --------------------------------------------------------------------------- HTTP error bubbling and 5xx retry


def test_graph_paged_raises_on_4xx(graph, monkeypatch):
    """A non-retryable 4xx propagates as `requests.HTTPError`. Production code
    relies on this behavior (no silent swallowing of auth/permission errors)."""

    def boom(*a, **kw):
        return FakeResponse(401, {"error": {"code": "Unauthorized"}})

    monkeypatch.setattr("_galpal.graph.requests.get", boom)
    with pytest.raises(requests.HTTPError):
        list(graph_mod.graph_paged("t", f"{graph_mod.GRAPH}/users"))


def test_graph_paged_retries_5xx_then_succeeds(graph, monkeypatch):
    """A transient 5xx triggers exponential backoff retry; if a later attempt
    returns 200 the iteration completes normally."""
    monkeypatch.setattr(graph_mod, "MAX_TRANSIENT_RETRIES", 3)
    calls = {"n": 0}
    real_get = graph.get

    def flaky_get(url, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            return FakeResponse(503, {"error": {"code": "ServiceUnavailable"}})
        return real_get(url, **kwargs)

    graph.users.append(make_user("u1"))
    monkeypatch.setattr("_galpal.graph.requests.get", flaky_get)
    out = list(graph_mod.fetch_gal("t", FilterConfig()))
    assert [u["id"] for u in out] == ["u1"]
    assert calls["n"] == 3  # two 503s + one success


def test_graph_paged_gives_up_after_exhausting_5xx_retries(graph, monkeypatch):
    """When the 5xx persists past MAX_TRANSIENT_RETRIES, the response surfaces
    via raise_for_status — the caller sees a normal HTTPError."""
    monkeypatch.setattr(graph_mod, "MAX_TRANSIENT_RETRIES", 2)

    def always_500(url, **kwargs):
        return FakeResponse(500, {"error": {"code": "ServerError"}})

    monkeypatch.setattr("_galpal.graph.requests.get", always_500)
    with pytest.raises(requests.HTTPError):
        list(graph_mod.graph_paged("t", f"{graph_mod.GRAPH}/users"))


def test_graph_paged_retries_on_connection_error(graph, monkeypatch):
    """ConnectionError / Timeout raised by the requests library should be
    caught and retried, not propagate after the first failure."""
    monkeypatch.setattr(graph_mod, "MAX_TRANSIENT_RETRIES", 3)
    calls = {"n": 0}
    real_get = graph.get
    blip = requests.ConnectionError("simulated network blip")

    def flaky_get(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise blip
        return real_get(url, **kwargs)

    graph.users.append(make_user("u1"))
    monkeypatch.setattr("_galpal.graph.requests.get", flaky_get)
    out = list(graph_mod.fetch_gal("t", FilterConfig()))
    assert [u["id"] for u in out] == ["u1"]


def test_send_batch_raises_on_4xx(graph, monkeypatch):
    def boom(url, **kwargs):
        return FakeResponse(403, {"error": {"code": "Forbidden"}})

    monkeypatch.setattr("_galpal.graph.requests.post", boom)
    with pytest.raises(requests.HTTPError):
        graph_mod.send_batch("t", [{"method": "POST", "url": "/me/contacts", "body": {}}])


# --------------------------------------------------------------------------- send_batch retry caps


def test_send_batch_gives_up_after_max_429_retries(graph, monkeypatch):
    """A sub-request that 429s indefinitely must NOT loop forever — after
    MAX_BATCH_429_RETRIES the bail-out path surfaces the last 429 in the
    final response list so the caller can see it as a normal error."""
    monkeypatch.setattr(graph_mod, "MAX_BATCH_429_RETRIES", 2)

    def always_throttle(body):
        return FakeResponse(
            200,
            {
                "responses": [
                    {"id": req["id"], "status": 429, "headers": {"Retry-After": "0"}} for req in body["requests"]
                ],
            },
        )

    monkeypatch.setattr(graph, "_handle_batch", always_throttle)
    out = graph_mod.send_batch("t", [{"method": "POST", "url": "/me/contacts", "body": {"a": 1}}])
    # Loop terminated at the cap; final response is the surfaced 429.
    assert len(out) == 1
    assert out[0]["status"] == 429


# --------------------------------------------------------------------------- chunked_batch helper


def test_chunked_batch_counts_2xx_as_ok_and_other_as_errors(graph):
    """Verify the success-status set is {200, 201, 204} and that a 5xx mixed
    in counts as an error rather than crashing."""
    # Seed 3 contacts so we can DELETE 3 in one batch.
    graph.contacts.extend([{"id": f"c{i}", "displayName": f"c{i}"} for i in range(3)])
    sub = [{"method": "DELETE", "url": f"/me/contacts/c{i}", "headers": {}} for i in range(3)]
    ok, errors = graph_mod.chunked_batch("t", sub, label="delete")
    assert ok == 3
    assert errors == 0


def test_chunked_batch_chunks_by_max_batch_size(graph, monkeypatch):
    """Sub-requests beyond MAX_BATCH_SIZE must split into multiple $batch POSTs.
    Verify the call count by looking at how many times send_batch was hit."""
    monkeypatch.setattr(graph_mod, "MAX_BATCH_SIZE", 5)
    # 12 ids → 5 + 5 + 2 (three $batch round-trips).
    graph.contacts.extend([{"id": f"c{i}", "displayName": f"c{i}"} for i in range(12)])
    sub = [{"method": "DELETE", "url": f"/me/contacts/c{i}", "headers": {}} for i in range(12)]
    ok, errors = graph_mod.chunked_batch("t", sub, label="delete")
    assert (ok, errors) == (12, 0)
    batch_posts = [c for c in graph.calls if c[0] == "POST" and c[1].endswith("/$batch")]
    assert [len(c[2]["requests"]) for c in batch_posts] == [5, 5, 2]


# --------------------------------------------------------------------------- send_batch hardening


def test_send_batch_synthesizes_500_when_response_drops_subrequest(graph, monkeypatch):
    """If Graph returns a `responses` array shorter than the request set
    (real bug observed on degraded paths), send_batch fills in synthetic 500
    entries so the returned list stays aligned with the input. Without this
    the caller would `KeyError` on the missing id."""

    def short_response(url, **kwargs):
        body = kwargs["json"]
        # Reply for the FIRST request only; drop the second.
        first = body["requests"][0]
        return FakeResponse(200, {"responses": [{"id": first["id"], "status": 201, "body": {"ok": 1}}]})

    monkeypatch.setattr("_galpal.graph.requests.post", short_response)
    out = graph_mod.send_batch(
        "t",
        [
            {"method": "POST", "url": "/me/contacts", "body": {"a": 1}},
            {"method": "POST", "url": "/me/contacts", "body": {"a": 2}},
        ],
    )
    # Aligned 1:1 with input; the dropped entry is a synthetic 500.
    assert len(out) == 2
    assert out[0]["status"] == 201
    assert out[1]["status"] == 500
    assert "no response" in out[1]["body"]


def test_send_batch_handles_unparseable_body(graph, monkeypatch):
    """If `r.json()` raises (Graph occasionally returns HTML / partial JSON
    on degraded paths), send_batch must not crash — the synthesis pass at
    the end fills in 500s for everything."""

    class BadResponse(FakeResponse):
        def json(self):
            msg = "not json"
            raise ValueError(msg)

    monkeypatch.setattr("_galpal.graph.requests.post", lambda url, **kw: BadResponse(200, {}))
    out = graph_mod.send_batch(
        "t",
        [
            {"method": "POST", "url": "/me/contacts", "body": {"a": 1}},
            {"method": "POST", "url": "/me/contacts", "body": {"a": 2}},
        ],
    )
    assert [r["status"] for r in out] == [500, 500]


def test_send_batch_outer_429_bail_out(graph, monkeypatch):
    """A sustained 429 on the outer batch endpoint (envelope-level throttling)
    must terminate after MAX_BATCH_429_RETRIES with synthetic 429s for every
    still-pending sub-request. Without an outer counter the loop runs forever."""
    monkeypatch.setattr(graph_mod, "MAX_BATCH_429_RETRIES", 2)
    # Speed up the test — _parse_retry_after honors `0` literally.
    monkeypatch.setattr("_galpal.graph.time.sleep", lambda *_: None)
    monkeypatch.setattr(
        "_galpal.graph.requests.post",
        lambda url, **kw: FakeResponse(429, {}, headers={"Retry-After": "0"}),
    )
    out = graph_mod.send_batch(
        "t",
        [
            {"method": "POST", "url": "/me/contacts", "body": {"a": 1}},
            {"method": "POST", "url": "/me/contacts", "body": {"a": 2}},
        ],
    )
    assert [r["status"] for r in out] == [429, 429]


def test_send_batch_outer_429_at_exact_boundary(graph, monkeypatch):
    """Pin the off-by-one on the outer 429 cap.

    With MAX_BATCH_429_RETRIES=3, the call count must be exactly 3 (the
    initial attempt plus 2 retries). A regression to `>` instead of `>=`
    on the budget check would silently produce 4.
    """
    monkeypatch.setattr(graph_mod, "MAX_BATCH_429_RETRIES", 3)
    monkeypatch.setattr("_galpal.graph.time.sleep", lambda *_: None)
    calls = {"n": 0}

    def count_throttle(url, **kw):
        calls["n"] += 1
        return FakeResponse(429, {}, headers={"Retry-After": "0"})

    monkeypatch.setattr("_galpal.graph.requests.post", count_throttle)
    graph_mod.send_batch("t", [{"method": "POST", "url": "/me/contacts", "body": {}}])
    assert calls["n"] == 3, f"expected 3 calls (1 initial + 2 retries), got {calls['n']}"


def test_send_batch_per_subrequest_429_at_exact_boundary(graph, monkeypatch):
    """Pin the off-by-one on the per-subrequest 429 cap.

    With MAX_BATCH_429_RETRIES=3, a sub-request that 429s every time must
    surface its 429 after exactly 3 calls (initial + 2 retries). A
    regression to `>` would issue 4 calls before giving up.
    """
    monkeypatch.setattr(graph_mod, "MAX_BATCH_429_RETRIES", 3)
    monkeypatch.setattr("_galpal.graph.time.sleep", lambda *_: None)
    calls = {"n": 0}

    def per_subrequest_429(url, **kw):
        calls["n"] += 1
        body = kw["json"]
        return FakeResponse(
            200,
            {
                "responses": [
                    {"id": req["id"], "status": 429, "headers": {"Retry-After": "0"}} for req in body["requests"]
                ],
            },
        )

    monkeypatch.setattr("_galpal.graph.requests.post", per_subrequest_429)
    out = graph_mod.send_batch("t", [{"method": "POST", "url": "/me/contacts", "body": {}}])
    assert calls["n"] == 3
    assert out[0]["status"] == 429


def test_iter_all_user_ids_paginates_across_pages(graph):
    """`iter_all_user_ids` walks `@odata.nextLink` for tenants larger than one
    page. A regression that yielded only the first page (e.g. forgot
    `yield from`) would not be caught by the small orphan tests in
    test_e2e.py because they all seed 1-3 users."""
    for i in range(450):  # crosses GAL_PAGE_SIZE=200 → 3 pages
        graph.users.append(make_user(f"u{i}", mail=f"u{i}@x.com"))
    out = list(graph_mod.iter_all_user_ids("t"))
    assert len(out) == 450


def test_graph_paged_refuses_non_graph_nextlink(graph, monkeypatch):
    """Defense in depth: `@odata.nextLink` is server-controlled. We follow
    it with the bearer token attached, so a malicious response that points
    elsewhere would leak the delegated Graph token. Refuse to follow."""
    monkeypatch.setattr(
        "_galpal.graph.requests.get",
        lambda url, **kw: FakeResponse(
            200,
            {"value": [{"id": "u1"}], "@odata.nextLink": "https://attacker.example/leak"},
        ),
    )
    with pytest.raises(ValueError, match="non-Graph host"):
        list(graph_mod.graph_paged("t", f"{graph_mod.GRAPH}/users"))


def test_flush_batch_zip_is_strict(graph, monkeypatch):
    """If `send_batch`'s synthesis pass regresses (a sub-request id missing
    from `final` causes the return list to be shorter than the input),
    `flush_batch`'s `zip(strict=True)` must raise rather than silently miscount.

    The contract between `send_batch` (always returns one response per
    request) and `flush_batch` (zips them 1:1) is checked here so it can't
    silently drift across future refactors.
    """
    from collections import Counter

    from _galpal.commands.pull import (
        STAT_CREATE,
        STAT_ERRORS,
        STAT_SKIP,
        STAT_UPDATE,
        flush_batch,
    )
    from _galpal.reporter import RecordingReporter

    # Stub send_batch to return a SHORT list — pretending the synthesis pass
    # regressed and the alignment guarantee is broken. flush_batch passes raw
    # sub_requests (no id tag); we simulate a 1-response reply for 2-request
    # input, which is exactly the misalignment shape the strict-zip catches.
    monkeypatch.setattr(
        "_galpal.commands.pull.send_batch",
        lambda token, reqs: [{"id": "1", "status": 201, "body": {}}],  # 1 response, 2 requests
    )
    stats: Counter = Counter({STAT_CREATE: 0, STAT_UPDATE: 0, STAT_SKIP: 0, STAT_ERRORS: 0})
    batch = [
        ("CREATE", "Alice", {"method": "POST", "url": "/me/contacts", "body": {}}),
        ("CREATE", "Bob", {"method": "POST", "url": "/me/contacts", "body": {}}),
    ]
    rep = RecordingReporter()
    with pytest.raises(ValueError, match="zip"):
        flush_batch("t", batch, stats, rep)


def test_retrying_request_honors_retry_after_on_5xx(graph, monkeypatch):
    """When a 503 carries `Retry-After`, _retrying_request honors it instead of
    falling back to the exponential-backoff schedule."""
    monkeypatch.setattr(graph_mod, "MAX_TRANSIENT_RETRIES", 3)
    sleeps: list[float] = []
    monkeypatch.setattr("_galpal.graph.time.sleep", sleeps.append)
    calls = {"n": 0}
    real_get = graph.get

    def flaky_get(url, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            return FakeResponse(503, {"error": {}}, headers={"Retry-After": "7"})
        return real_get(url, **kw)

    graph.users.append(make_user("u1"))
    monkeypatch.setattr("_galpal.graph.requests.get", flaky_get)
    list(graph_mod.fetch_gal("t", FilterConfig()))
    # Two server-instructed waits of 7s each (capped, but 7 < cap).
    assert sleeps[0] == 7
    assert sleeps[1] == 7


def test_parse_retry_after_handles_float_values():
    """Some proxies rewrite Retry-After as a float (`2.5`). The float middle
    case keeps that from falling through to the date parser and silently
    returning the default."""
    assert graph_mod._parse_retry_after("2.5") == 2
    assert graph_mod._parse_retry_after("0.9") == 0
    # Above the cap: still capped.
    assert graph_mod._parse_retry_after("99999.5") == graph_mod.MAX_RETRY_AFTER_S


def test_fetch_existing_contacts_warns_on_email_collision(graph):
    """Two contacts sharing an email cause `pull` to stamp an arbitrary one;
    surface a warning so the user knows to run `dedupe`."""

    class Recorder:
        def __init__(self):
            self.warnings: list[str] = []

        def warning(self, msg):
            self.warnings.append(msg)

    graph.contacts.extend(
        [
            {"id": "c1", "displayName": "A", "emailAddresses": [{"address": "shared@x.com"}]},
            {"id": "c2", "displayName": "B", "emailAddresses": [{"address": "Shared@x.com"}]},  # case-insensitive dup
        ],
    )
    rec = Recorder()
    graph_mod.fetch_existing_contacts("t", reporter=rec)
    assert any("shared@x.com" in w for w in rec.warnings)
    assert any("dedupe" in w for w in rec.warnings)


def test_fetch_existing_contacts_silent_when_no_collisions(graph):
    """Steady state: no duplicate emails, no warnings."""

    class Recorder:
        def __init__(self):
            self.warnings: list[str] = []

        def warning(self, msg):
            self.warnings.append(msg)

    graph.contacts.append({"id": "c1", "displayName": "A", "emailAddresses": [{"address": "a@x.com"}]})
    rec = Recorder()
    graph_mod.fetch_existing_contacts("t", reporter=rec)
    assert rec.warnings == []
