"""Microsoft Graph HTTP layer: pagination, $batch dispatch, and Graph constants.

Owns everything related to talking to Graph at the protocol level: page following,
429 retries, the $batch sub-request fan-out, and the constants that describe
Graph entities (extended-property id format, $select field list). Pure protocol —
no GAL semantics, no presentation.
"""

from __future__ import annotations

import logging
import os
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests

from _galpal.filters import FilterConfig, gal_user_passes

# Module-level logger. `cli.py` configures a handler that routes records to
# the active Reporter (so cron operators see retry/throttle events alongside
# the rest of the output). Tests can `caplog` against this name for free.
log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
# `@odata.nextLink` is server-controlled. We follow it with the bearer token
# attached, so a hypothetical Graph response that returned a non-Microsoft URL
# would leak the delegated token. The realistic chain is narrow (Graph doesn't
# do this today), but the cost of a defense-in-depth check is one regex.
GRAPH_HOSTS = frozenset({"graph.microsoft.com"})
HTTP_TOO_MANY_REQUESTS = 429
MAX_BATCH_SIZE = 20  # Microsoft Graph $batch hard limit
DEFAULT_RETRY_AFTER_S = 5
# Cap server-instructed back-offs so a hostile or buggy `Retry-After` (e.g.
# `Retry-After: 999999999`) can't freeze a destructive op for years. Default
# 300s matches Microsoft's documented tenant-wide throttle guidance — capping
# lower than that would mean galpal hammers Graph faster than asked, becoming
# the bad citizen the cap was meant to prevent. Override via env to fit
# your tenant's known throttle ceiling.
MAX_RETRY_AFTER_S = int(os.environ.get("GALPAL_MAX_RETRY_AFTER_S", "300"))
# Hard ceiling on per-subrequest 429 retries inside `send_batch`; without this,
# `Retry-After: 0` from a misbehaving server turns into an unbounded request
# loop that makes the throttle worse.
MAX_BATCH_429_RETRIES = int(os.environ.get("GALPAL_MAX_BATCH_429_RETRIES", "8"))
# Per-call retry budget for transient failures (5xx + ConnectionError + Timeout).
# Each retry sleeps `2**attempt + jitter` seconds, capped by MAX_RETRY_AFTER_S.
MAX_TRANSIENT_RETRIES = int(os.environ.get("GALPAL_MAX_TRANSIENT_RETRIES", "5"))
RETRYABLE_5XX = frozenset({500, 502, 503, 504})
TRANSIENT_EXCEPTIONS = (requests.ConnectionError, requests.Timeout)
# Per-request HTTP timeout for both GETs and the $batch POST. Override via env
# when a tenant legitimately needs longer than 120s (rare but not unheard of
# for very large `/users` enumerations).
HTTP_TIMEOUT_S = int(os.environ.get("GALPAL_HTTP_TIMEOUT_S", "120"))
# `$top` for /users (the GAL fetch). 200 trades a small drop in total throughput
# for far more frequent progress feedback than $top=999 — but on a small tenant
# 999 finishes faster and on a giant one 200 may be too aggressive. Tunable.
GAL_PAGE_SIZE = int(os.environ.get("GALPAL_GAL_PAGE_SIZE", "200"))
# `$top` for /me/contacts. Personal contact lists are usually 100s, not 1000s.
CONTACTS_PAGE_SIZE = int(os.environ.get("GALPAL_CONTACTS_PAGE_SIZE", "100"))


def _retrying_request(method: str, url: str, **kwargs) -> requests.Response:
    """Issue a Graph HTTP request, retrying on transient failures.

    Retries cover ConnectionError, Timeout, and the 5xx subset in RETRYABLE_5XX
    (500 / 502 / 503 / 504). 429 is NOT handled here — the callers
    (`graph_paged` / `send_batch`) handle 429 separately so they can honor
    `Retry-After` and surface per-subrequest 429s in the batch case.

    Three terminal shapes:
      - 2xx / 3xx / 4xx response → returned to the caller.
      - 5xx that exhausted MAX_TRANSIENT_RETRIES → raises `requests.HTTPError`
        via `raise_for_status()` so the failure is consistently a raised
        exception, not a returned response the caller has to inspect.
      - Transient exception that exhausted the budget → re-raised on the
        final iteration.

    Between attempts: `Retry-After` is honored when present (Graph 503s
    frequently set it); otherwise exponential backoff `2**attempt + jitter`,
    capped at MAX_RETRY_AFTER_S. The transient-exception branch always falls
    through to the same backoff sleep — this used to be implicit in the
    try/except/else structure; now it's explicit.

    Dispatches via `requests.get` / `requests.post` so the
    `_galpal.graph.requests.{get,post}` monkeypatches used in tests still
    intercept the call.
    """
    fn = {"GET": requests.get, "POST": requests.post}.get(method)
    if fn is None:
        msg = f"Unsupported HTTP method for retrying request: {method!r}"
        raise ValueError(msg)

    for attempt in range(MAX_TRANSIENT_RETRIES + 1):
        retry_after_hint: int | None = None
        try:
            r = fn(url, **kwargs)
        except TRANSIENT_EXCEPTIONS as e:
            if attempt == MAX_TRANSIENT_RETRIES:
                log.warning(
                    "graph %s %s: transient error %s exhausted retry budget",
                    method,
                    url,
                    e.__class__.__name__,
                )
                raise
            log.info(
                "graph %s %s: transient error %s, retry %d/%d",
                method,
                url,
                e.__class__.__name__,
                attempt + 1,
                MAX_TRANSIENT_RETRIES,
            )
        else:
            # Non-retryable status — caller decides what to do with it (4xx
            # usually surfaces via raise_for_status() at the call site).
            if r.status_code not in RETRYABLE_5XX:
                return r
            if attempt == MAX_TRANSIENT_RETRIES:
                # Final 5xx: surface as a raised HTTPError, consistent with
                # the transient-exception path. Returning a 5xx response
                # would silently collapse two error categories at the
                # caller (which then raises the same way via
                # raise_for_status, but only on paths that call it).
                log.warning(
                    "graph %s %s: %d exhausted retry budget",
                    method,
                    url,
                    r.status_code,
                )
                r.raise_for_status()
                return r  # pragma: no cover  -- raise_for_status raises on 5xx
            ra = r.headers.get("Retry-After")
            if ra:
                retry_after_hint = _parse_retry_after(ra)
            log.info(
                "graph %s %s: %d, retry %d/%d (Retry-After=%s)",
                method,
                url,
                r.status_code,
                attempt + 1,
                MAX_TRANSIENT_RETRIES,
                ra,
            )
        # Same backoff for both transient-exception and 5xx paths.
        if retry_after_hint is not None:
            wait: float = retry_after_hint
        else:
            wait = min(2**attempt + random.uniform(0, 0.5), MAX_RETRY_AFTER_S)  # noqa: S311  -- not crypto
        time.sleep(wait)
    # Defensive: the loop above either returns or raises on the final
    # iteration. Reaching here would mean MAX_TRANSIENT_RETRIES is negative,
    # which the env-var parsing forbids in practice.
    msg = "retry budget exhausted with no recorded outcome"  # pragma: no cover
    raise RuntimeError(msg)  # pragma: no cover


def _parse_retry_after(value: str | None) -> int:
    """Parse a `Retry-After` header value and return seconds-to-wait.

    Tolerates everything RFC 9110 §10.2.3 permits — and a few things it doesn't:
      - missing / empty                       → DEFAULT_RETRY_AFTER_S
      - delta-seconds (the common case)       → that many seconds, capped
      - float-shaped (e.g. `2.5`, non-RFC but seen in the wild from proxies)
                                              → ceil to int, capped
      - HTTP-date (legal but rare from Graph) → seconds-until-then, capped
      - garbage strings                       → DEFAULT_RETRY_AFTER_S
      - negative values                       → 0 (don't sleep, but don't crash)
      - values exceeding MAX_RETRY_AFTER_S    → capped
    """
    if not value:
        return DEFAULT_RETRY_AFTER_S
    raw = value.strip()
    try:
        return max(0, min(int(raw), MAX_RETRY_AFTER_S))
    except ValueError:
        pass
    # Float middle case: `Retry-After: 2.5` isn't strictly RFC, but proxies
    # in the path occasionally rewrite to it. Don't fall through to the date
    # parser (which would then return DEFAULT silently).
    try:
        as_float = float(raw)
    except ValueError:
        pass
    else:
        return max(0, min(int(as_float), MAX_RETRY_AFTER_S))
    try:
        when = parsedate_to_datetime(raw)
        delta = (when - datetime.now(UTC)).total_seconds()
        return int(max(0, min(delta, MAX_RETRY_AFTER_S)))
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_S


# Named property in the public-strings namespace; same id is used to write and read.
# Outlook stores the GUID lowercased, and the Graph $filter on extended-property id
# is case-sensitive, so we MUST write and read with a lowercase guid — otherwise the
# server-side `id eq '...'` in fetch_existing_contacts returns no expanded property
# for any contact, by_azure_id ends up empty, and re-runs re-create everyone.
# (PSETID_PublicStrings = 00020329-0000-0000-c000-000000000046)
EP_AZURE_ID_NAME = "Name GalpalAzureId"
EP_AZURE_ID = "String {00020329-0000-0000-c000-000000000046} " + EP_AZURE_ID_NAME

GAL_USER_FIELDS = [
    "id",
    "userType",
    "displayName",
    "givenName",
    "surname",
    "mail",
    "userPrincipalName",
    "jobTitle",
    "department",
    "companyName",
    "officeLocation",
    "businessPhones",
    "mobilePhone",
    "streetAddress",
    "city",
    "state",
    "postalCode",
    "country",
]


def fetch_gal(token: str, cfg: FilterConfig):
    """Yield usable GAL entries that pass all active filters.

    Two non-filter preconditions are applied unconditionally before `cfg`:
    `userType` must be Member (or unset), and the entry must have at least
    a displayName and either a `mail` or `userPrincipalName`. Entries
    failing those are silently skipped — they're not really people-shaped
    rows we can write a contact for. Then `cfg` (--require-*, --exclude)
    decides which of the remaining rows to yield.
    """
    # GAL_PAGE_SIZE=200 default trades total throughput for more frequent
    # progress feedback than $top=999 — env-tunable when those tradeoffs differ.
    params = {"$select": ",".join(GAL_USER_FIELDS), "$top": GAL_PAGE_SIZE}
    for u in graph_paged(token, f"{GRAPH}/users", params):
        if u.get("userType") not in (None, "Member"):
            continue
        # Two non-filter preconditions for a usable GAL entry — applied
        # before the user's filter config so an empty FilterConfig still
        # rejects unnamed and email-less rows. (`require_email` checks
        # `mail` specifically; this gate accepts either `mail` or `upn`.)
        if not u.get("displayName"):
            continue
        if not (u.get("mail") or u.get("userPrincipalName")):
            continue
        if not gal_user_passes(u, cfg):
            continue
        yield u


def fetch_existing_contacts(token: str, reporter=None):
    """Index existing personal contacts by Azure id stamp and (lowercased) email.

    The Azure-id index is the fast path on re-runs. The email index is a
    fallback used by `pull` to adopt-and-stamp pre-existing contacts whose
    address matches a GAL row but that lack the stamp.

    On email collisions (multiple personal contacts share the same email —
    typically because the user has duplicates) the index keeps the first
    contact seen and emits a `reporter.warning(...)` per colliding address.
    Without the warning, pull would silently stamp whichever copy Graph
    happened to return first, and that pick is not stable across runs — a
    user with duplicates would see the stamp wander between their copies.
    `dedupe` is the right tool to clean the underlying duplication.
    """
    params = {
        "$top": CONTACTS_PAGE_SIZE,
        "$expand": f"singleValueExtendedProperties($filter=id eq '{EP_AZURE_ID}')",
    }
    by_azure_id: dict[str, dict] = {}
    by_email: dict[str, dict] = {}
    # Count distinct contacts per (lowercased) email so the warning below can
    # report the true contact count rather than an off-by-one trick.
    contacts_per_email: dict[str, set[str]] = {}
    for c in graph_paged(token, f"{GRAPH}/me/contacts", params):
        for ep in c.get("singleValueExtendedProperties") or []:
            if EP_AZURE_ID_NAME in (ep.get("id") or "") and ep.get("value"):
                by_azure_id[ep["value"]] = c
                break
        for em in c.get("emailAddresses") or []:
            addr = (em.get("address") or "").lower()
            if not addr:
                continue
            contacts_per_email.setdefault(addr, set()).add(c["id"])
            by_email.setdefault(addr, c)
    if reporter is not None:
        collisions = {addr: len(ids) for addr, ids in contacts_per_email.items() if len(ids) > 1}
        for addr, count in sorted(collisions.items()):
            reporter.warning(
                f"email {addr!r} appears on {count} personal contacts; "
                f"adoption will pick one arbitrarily — run `dedupe` to clean."
            )
    return by_azure_id, by_email


def iter_all_user_ids(token: str):
    """Yield every Azure AD user id (string), one per directory row.

    Used by `prune --orphans` to detect pulled contacts whose source has
    been deleted from the directory. Uses GAL_PAGE_SIZE so the caller can
    drive a progress bar at the same per-page cadence as `fetch_gal`.
    """
    params = {"$select": "id", "$top": GAL_PAGE_SIZE}
    for u in graph_paged(token, f"{GRAPH}/users", params):
        uid = u.get("id")
        if uid:
            yield uid


def fetch_all_user_ids(token: str) -> set[str]:
    """Materialize every Azure AD user id into a set.

    Thin convenience wrapper around `iter_all_user_ids` for callers that don't
    need streaming. Prune uses `iter_all_user_ids` directly so it can drive a
    progress bar at the page-fetch cadence.
    """
    return set(iter_all_user_ids(token))


def graph_paged(token: str, url: str, params: dict | None = None):
    """Yield items from a Graph collection, transparently following @odata.nextLink.

    Retries cover the full transient-failure set: 429 honors `Retry-After`
    (with a per-page cap so a misbehaving server stuck on `Retry-After: 0`
    can't loop forever); 5xx / ConnectionError / Timeout go through
    `_retrying_request` with exponential backoff. Anything else (4xx, json
    decode error) propagates.

    Defense in depth: the `@odata.nextLink` URL is server-controlled, so we
    refuse to follow it if it points anywhere other than `graph.microsoft.com`.
    Following an attacker-controlled host with the bearer token attached
    would leak the delegated Graph token.
    """
    headers = {"Authorization": f"Bearer {token}"}
    page_429_attempts = 0
    while url:
        host = urlparse(url).hostname or ""
        if host not in GRAPH_HOSTS:
            msg = f"refusing to follow @odata.nextLink to non-Graph host {host!r}"
            raise ValueError(msg)
        r = _retrying_request("GET", url, headers=headers, params=params, timeout=HTTP_TIMEOUT_S)
        params = None  # only on the first call; @odata.nextLink already carries them
        if r.status_code == HTTP_TOO_MANY_REQUESTS:
            page_429_attempts += 1
            if page_429_attempts >= MAX_BATCH_429_RETRIES:
                # Sustained per-page throttling — surface as HTTPError rather
                # than loop forever. The cap reuses MAX_BATCH_429_RETRIES
                # because the underlying behavior (server tells us to wait,
                # we wait, server tells us to wait again) is the same shape.
                log.warning("graph GET %s: 429 exhausted retry budget", url)
                r.raise_for_status()
                return  # pragma: no cover -- raise_for_status raises on 429
            wait = _parse_retry_after(r.headers.get("Retry-After"))
            log.info(
                "graph GET %s: 429, sleeping %ds (retry %d/%d)",
                url,
                wait,
                page_429_attempts,
                MAX_BATCH_429_RETRIES,
            )
            time.sleep(wait)
            continue
        r.raise_for_status()
        body = r.json()
        yield from body.get("value", [])
        url = body.get("@odata.nextLink")


def chunked_batch(
    token: str,
    sub_requests: list[dict],
    *,
    label: str = "request",
    pbar=None,
    reporter=None,
    tags: list | None = None,
    on_response=None,
) -> tuple[int, int]:
    """Send sub_requests in MAX_BATCH_SIZE-sized chunks; return (ok, errors).

    The simple shape — one fixed `label`, no per-row context — covers
    `run_dedupe` / `run_prune` / `run_delete`, where every sub-request is
    "delete <id>" and a missing name on the error line is fine.

    The richer shape — `tags` aligned 1:1 with `sub_requests` plus an
    `on_response(tag, response)` callback — covers `run_pull.flush_batch`
    (the tag carries (action, name) so the error line names the contact) and
    `run_remove_categories` (the tag carries `(contact_id, name)` so the
    error line attributes the failure to the right contact). The callback
    must return `(ok: bool, error_kwargs: dict | None)` — `error_kwargs` is
    spread into `reporter.entry("subrequest.error", ...)` so callers can
    customize the action/name fields the renderer sees.

    Without `on_response`, ok/error counting stays the simple shape; with
    it, callers stop hand-rolling the same chunk + send_batch + zip + count
    loop in three different command modules.
    """
    ok = errors = 0
    if tags is not None and len(tags) != len(sub_requests):
        msg = f"tags length {len(tags)} != sub_requests length {len(sub_requests)}"
        raise ValueError(msg)
    for i in range(0, len(sub_requests), MAX_BATCH_SIZE):
        chunk = sub_requests[i : i + MAX_BATCH_SIZE]
        chunk_tags = tags[i : i + MAX_BATCH_SIZE] if tags is not None else [None] * len(chunk)
        responses = send_batch(token, chunk)
        for tag, resp in zip(chunk_tags, responses, strict=True):
            if on_response is not None:
                is_ok, error_kwargs = on_response(tag, resp)
            else:
                is_ok = resp.get("status") in (200, 201, 204)
                error_kwargs = None
            if is_ok:
                ok += 1
                continue
            errors += 1
            if reporter is None:
                continue
            kwargs = (
                error_kwargs
                if error_kwargs is not None
                else {
                    "action": label,
                    "name": "",
                    "status": resp.get("status"),
                    "body": resp.get("body"),
                }
            )
            reporter.entry("subrequest.error", **kwargs)
        if pbar is not None:
            pbar.update(len(chunk))
    return ok, errors


def send_batch(token: str, sub_requests: list[dict]) -> list[dict]:
    """POST a Graph $batch and return the per-request responses, retrying any 429s.

    Each sub_request keeps the same id across retries, so the returned list is
    aligned 1:1 with the input. Three retry budgets bound the work:

      - per-subrequest 429: MAX_BATCH_429_RETRIES (configurable). If a single
        sub-request keeps throttling past that, we surface its last 429
        response and move on rather than looping forever.
      - outer-batch 429: MAX_BATCH_429_RETRIES on envelope-level throttling
        (the whole batch endpoint 429s). The two budgets are independent;
        worst-case total iterations is `len(sub_requests) * MAX_BATCH_429_RETRIES`,
        which is bounded but is NOT the simpler "MAX_BATCH_429_RETRIES total
        iterations" some readers would expect. The per-subrequest cap is the
        load-bearing one — that's what makes the function terminate.
      - transient 5xx / connection / timeout: MAX_TRANSIENT_RETRIES, handled
        inside `_retrying_request`.

    Any sub-request id missing from `responses` (Graph occasionally returns a
    partial array, or `r.json()` is shaped unexpectedly) gets a synthetic 500
    entry rather than raising KeyError on the final lookup. This keeps the
    return list aligned with the input even when Graph lies about it.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    tagged = [{**req, "id": str(i)} for i, req in enumerate(sub_requests, 1)]
    final: dict[str, dict] = {}
    pending = tagged
    attempts: dict[str, int] = {req["id"]: 0 for req in tagged}
    outer_429_attempts = 0
    while pending:
        r = _retrying_request(
            "POST", f"{GRAPH}/$batch", headers=headers, json={"requests": pending}, timeout=HTTP_TIMEOUT_S
        )
        if r.status_code == HTTP_TOO_MANY_REQUESTS:
            outer_429_attempts += 1
            if outer_429_attempts >= MAX_BATCH_429_RETRIES:
                # Sustained envelope-level throttling — bail rather than loop
                # forever. Synthesize a 429 entry for every still-pending
                # sub-request so the caller sees the failure shape it expects.
                log.warning(
                    "graph $batch envelope 429 exhausted retry budget; synthesizing 429 for %d pending subrequest(s)",
                    len(pending),
                )
                for req in pending:
                    final[req["id"]] = {"id": req["id"], "status": HTTP_TOO_MANY_REQUESTS, "body": None}
                break
            wait = _parse_retry_after(r.headers.get("Retry-After"))
            log.info(
                "graph $batch envelope 429: sleeping %ds (retry %d/%d)",
                wait,
                outer_429_attempts,
                MAX_BATCH_429_RETRIES,
            )
            time.sleep(wait)
            continue
        r.raise_for_status()
        # Defensive parse: Graph occasionally returns a body shaped like
        # `{"error": {...}}` on degraded paths instead of `{"responses": [...]}`.
        # Treat anything not parseable as an empty response set; the synthesis
        # pass at the end of the loop fills in the missing entries.
        try:
            body = r.json()
        except ValueError:
            body = {}
        responses = body.get("responses") if isinstance(body, dict) else None
        if not isinstance(responses, list):
            responses = []
        retry: list[dict] = []
        max_wait = 0
        # Index pending by id so per-subrequest retry lookup is O(1) instead of O(n*m).
        by_id = {req["id"]: req for req in pending}
        for resp in responses:
            rid = resp.get("id")
            if rid not in by_id:
                continue  # unknown id (shouldn't happen but don't crash if it does)
            if resp.get("status") == HTTP_TOO_MANY_REQUESTS:
                attempts[rid] += 1
                if attempts[rid] >= MAX_BATCH_429_RETRIES:
                    # Give up on this sub-request rather than loop forever — surface
                    # the last 429 response so the caller sees it as a normal error.
                    final[rid] = resp
                    continue
                max_wait = max(max_wait, _parse_retry_after(resp.get("headers", {}).get("Retry-After")))
                retry.append(by_id[rid])
            else:
                final[rid] = resp
        if retry:
            # Floor the sleep at DEFAULT_RETRY_AFTER_S so a throttle storm with
            # `Retry-After: 0` doesn't hammer Graph in a tight loop.
            wait = max(max_wait, DEFAULT_RETRY_AFTER_S)
            log.info("graph $batch: retrying %d throttled subrequest(s) after %ds", len(retry), wait)
            time.sleep(wait)
        pending = retry
    # Synthesize a 500 entry for any sub-request that never landed in `final`
    # (Graph dropped it from the response array, the body wasn't parseable,
    # etc.). Aligning the output list with the input list is part of the
    # public contract — callers `zip(sub_requests, send_batch(...))` and
    # would otherwise either KeyError or silently miscount.
    synthesized = 0
    for req in tagged:
        if req["id"] not in final:
            final[req["id"]] = {
                "id": req["id"],
                "status": 500,
                "body": "no response from $batch (request dropped)",
            }
            synthesized += 1
    if synthesized:
        log.warning("graph $batch dropped %d subrequest(s); synthesized 500", synthesized)
    return [final[req["id"]] for req in tagged]
