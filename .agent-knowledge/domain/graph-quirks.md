# Microsoft Graph quirks galpal works around

Each entry below is a real surprise from working against Graph — keep them
in mind when extending the HTTP layer or adding new commands.

## Extended-property GUID must be lowercased

[_galpal/graph.py — EP_AZURE_ID](../../_galpal/graph.py).

Outlook stores the PSETID_PublicStrings GUID lowercased on the server side.
Graph's `$filter` on extended-property `id` is **case-sensitive**, so a
write with the uppercase canonical form (`00020329-0000-0000-C000-…`)
silently makes the subsequent read filter miss every contact — `by_azure_id`
ends up empty, re-runs re-create everyone, and stamping appears to never
take.

Always write and read with the lowercase GUID. The constant in
[graph.py](../../_galpal/graph.py) is correct — don't "fix" it to the
uppercase canonical form.

## `$batch` returns partial response arrays on degraded paths

[_galpal/graph.py — `send_batch`](../../_galpal/graph.py).

Real Graph occasionally drops sub-requests from the response array on
degraded paths (server bug, not in any spec). If we trusted the array to
align 1:1 with the input, callers would `KeyError` on the missing id or
silently miscount. `send_batch` synthesizes a 500 entry for any
sub-request id missing from `final` so the returned list always aligns
with the input — public contract.

Also: the body itself may be malformed (HTML, partial JSON, error
envelope). `r.json()` is wrapped in `try/except ValueError`; on parse
failure, the synthesis pass fills in 500s for everything.

## `@odata.nextLink` is server-controlled — defend against host injection

`graph_paged` parses `urlparse(url).hostname` for every URL it's about to
follow and refuses anything outside `graph.microsoft.com`
(`GRAPH_HOSTS`). The realistic chain is narrow — Graph doesn't currently
return non-Microsoft URLs in `@odata.nextLink` — but the bearer token
travels with the request, so the cost of a defense-in-depth check is
near-zero.

If you ever extend to a different Graph host (e.g. national clouds:
`graph.microsoft.us`, `graph.microsoft.de`), add it to `GRAPH_HOSTS` —
don't bypass the check.

## `Retry-After` is wild in the field

[_galpal/graph.py — `_parse_retry_after`](../../_galpal/graph.py).

Graph mostly sends delta-seconds. Proxies in the path occasionally rewrite
to HTTP-date (RFC 9110 §10.2.3 says they may) or to float ("2.5", not
strictly RFC). The pre-1.0.0 code did `int(value)` and crashed on every
non-integer; the parser now tolerates:

- missing / empty → `DEFAULT_RETRY_AFTER_S`
- delta-seconds → capped at `MAX_RETRY_AFTER_S` (default 300s)
- float → ceil to int, capped
- HTTP-date → seconds-until-then, capped
- garbage → `DEFAULT_RETRY_AFTER_S`
- negative → 0 (don't sleep, but don't crash)
- huge → capped (defense against hostile/buggy `Retry-After: 999999999`)

The cap default of 300s matches Microsoft's documented tenant-wide
throttle guidance. Capping lower means galpal hammers Graph faster than
asked, becoming the bad citizen the cap was meant to prevent. Override
via `GALPAL_MAX_RETRY_AFTER_S` to fit your tenant's known ceiling.

## `@odata.nextLink` and `value` ordering is not stable

Different Graph endpoints emit the two keys in different orders.
`/me/contacts` tends to put `value` first and `@odata.nextLink` second;
some directory endpoints flip it. The ijson-based `graph_paged` (1.1.0+)
handles both via SAX-style forward pass — see
[conventions/streaming.md](../conventions/streaming.md).

## `$select` 400s with no actionable error

`/me/contacts?$select=…` 400s on at least one of the ~12 fields
`user_data_score` reads, on some tenants. Graph doesn't say which field.
Pinning the right subset would need a tenant-specific investigation pass
that nobody's done. `run_dedupe` therefore doesn't `$select` and fetches
the whole contact payload — the wire-cost trade-off is offset by ijson
row-streaming (only one row's payload is alive in memory at a time).

## Personal contacts vs directory users

- **GAL = `/users`** — directory rows. Fields documented under
  `GAL_USER_FIELDS` in graph.py. Members only (skip `userType ≠ Member`).
- **Personal contacts = `/me/contacts`** — Outlook contacts owned by the
  user. Mutable via `$batch`. The Azure-id stamp lives in a
  `singleValueExtendedProperty` with the lowercased PSETID_PublicStrings
  GUID (see top of this file).
- These two collections are **disjoint** — a directory user is not a
  contact until galpal `pull`s it (or first-run adoption matches an
  email).

## `PATCH` merges `singleValueExtendedProperties` by `id`

Real Graph merges entries by `id` rather than overwriting the whole array
when you PATCH a contact with a `singleValueExtendedProperties` field.
Other unrelated EPs survive the PATCH. The FakeGraph in
`tests/conftest.py` simulates this faithfully — several galpal code paths
rely on it.
