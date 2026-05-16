# 2026-05-11 — ijson-based row-streaming for `graph_paged`

**Status:** Accepted. Shipped in 1.1.0 ([CHANGELOG.md](../../CHANGELOG.md)).

## Context

`graph_paged` previously used `r.json()` to parse each Graph page, which
materialized the whole response body (`CONTACTS_PAGE_SIZE × max-per-contact-payload`)
into Python objects before yielding any rows. For typical mailboxes this
was fine (a few hundred KB per page). For users with megabyte-sized
`personalNotes` or large photo attachments, peak per-page memory could
reach tens of MB — the last remaining OOM ceiling that the otherwise
streaming `run_dedupe` couldn't address.

`run_dedupe` was already streaming at the iteration level (one full
contact dict alive per loop iteration); the bottleneck moved up the
stack to the `r.json()` call inside `graph_paged`.

## Decision

Switch `graph_paged` to:

1. `requests` `stream=True` so the body isn't pre-read.
2. `ijson.parse` (SAX-style events) over `_IterReader(r.iter_content(chunk_size=65536))`.
3. Per-row `ObjectBuilder` for `value.item` events — yield each contact as
   its closing `}` is seen on the wire.
4. Sibling `@odata.nextLink` captured in the same single forward pass
   (Graph doesn't fix the relative order of the two keys).
5. `try/finally: r.close()` per page for connection-lifetime hygiene.

Peak per-page memory drops from `O(page_size × max_payload)` to
`O(max_payload)`.

## Alternatives considered

- **Smaller `CONTACTS_PAGE_SIZE`** — already env-tunable
  (`GALPAL_CONTACTS_PAGE_SIZE`). Doesn't remove the ceiling, just lowers
  it linearly. Kept as the operator-level mitigation; not a substitute.
- **`ijson.items(prefix='value.item')`** — simpler than `ijson.parse`, but
  `ijson.items` doesn't expose sibling keys. Capturing
  `@odata.nextLink` from the same stream would require a second pass
  (defeating the purpose) or guessing key order (fragile across
  endpoints).
- **`r.raw` instead of `r.iter_content`** — `r.raw` is the urllib3
  `HTTPResponse`, which requires `decode_content=True` for gzip/deflate
  decompression. Easy to forget; tests would have to mirror the attribute
  exactly. `r.iter_content` decompresses transparently. The `_IterReader`
  adapter is the only added concept and it's tiny.

## Consequences

### Positive

- One row of payload alive at a time on the wire path. Combined with
  `run_dedupe`'s streaming structure, peak memory is now bounded by a
  single contact's payload throughout the dedupe pass, regardless of
  page size or mailbox size.
- Connection-lifetime is deterministic. Previously, a caller `break`ing
  out of the iterator would leak the underlying urllib3 connection until
  GC; now `try/finally` closes it immediately.
- Key-order independence proves out — see
  [conventions/streaming.md](../conventions/streaming.md) for the
  regression test.

### Negative

- New runtime dependency: `ijson>=3.3,<4`. Pure-stdlib install path
  forfeited.
- A small adapter class (`_IterReader`) is required to bridge
  `iter_content` (a bytes iterator) to `ijson.parse` (a file-like). The
  alternative — passing the iterator directly — silently selects the slow
  pure-Python ijson backend. The adapter exists specifically to keep the
  fast C backend (`yajl2_c`) on the happy path.
- Slightly more parsing surface to maintain than `r.json()`. Mitigated by
  a tight implementation (~25 lines) and a dedicated regression test for
  the key-order edge case.

## Trade-offs

- **Wire perf:** `r.iter_content(chunk_size=65536)` vs `r.json()` reading
  the whole body. Throughput is comparable for typical responses;
  `iter_content` is marginally faster on huge responses (no double-buffer).
- **Test fixture churn:** `FakeResponse` grew `iter_content` and
  `close()`. One-time cost; no downstream test changes after that.
- **Pyright friction:** `ObjectBuilder.value` is set lazily inside
  `event()` after `end_map`; the ijson stubs don't expose it. A
  `# pyright: ignore[reportAttributeAccessIssue]` is suppressed on the
  one access site. Tolerable.

## Implementation pointers

- [_galpal/graph.py — `graph_paged`](../../_galpal/graph.py)
- [_galpal/graph.py — `_IterReader`](../../_galpal/graph.py)
- [tests/test_resilience.py — `test_graph_paged_handles_next_link_before_value`](../../tests/test_resilience.py)
- [tests/conftest.py — `FakeResponse.iter_content` / `.close`](../../tests/conftest.py)
