"""`galpal dedupe` — find and (with --apply) delete duplicate contacts.

Groups personal contacts by shared email addresses (transitively, via Union-Find),
ranks each group by `user_data_score` to pick a winner, and deletes the losers.
No data is merged from losers into the winner — the winner stays as-is, the rest
go to Deleted Items.

Memory shape: single streaming pass. Each row's full payload is alive only for
the duration of one loop iteration; after extracting the metadata we need
(displayName, score, createdDateTime, emails), the dict goes out of scope and
Python's GC reclaims it. Persistent state is bounded by `N x small constants`
(ids + emails + small per-contact tuples) regardless of how big any individual
contact's `personalNotes` / photo / etc. is. The remaining ceiling is one page's
worth of contacts during `r.json()` page-parse — bounded by CONTACTS_PAGE_SIZE
times the max-per-contact-payload. ijson-based truly-row-streaming is the 1.1.0
fix for that ceiling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from _galpal.graph import CONTACTS_PAGE_SIZE, GRAPH, chunked_batch, graph_paged
from _galpal.model import user_data_score
from _galpal.reporter import default_reporter

if TYPE_CHECKING:
    from _galpal.reporter import Reporter


# Per-contact metadata we keep across the whole run. Tuple instead of a dataclass
# to keep the per-row footprint tiny: 4 strings + 1 int = ~250-400 bytes typical,
# vs. the kilobytes a full Graph contact dict carries. Order: display_name,
# score, created_at_iso, lower_emails_tuple.
ContactMeta = tuple[str, int, str, tuple[str, ...]]


def run_dedupe(token: str, *, apply: bool, reporter: Reporter | None = None) -> None:
    """Group personal contacts by shared emails (transitively), keep one per group, optionally delete the rest."""
    rep = reporter or default_reporter()
    rep.info("Loading all contacts...")
    # `$top` honors the env-tunable CONTACTS_PAGE_SIZE so a user with very
    # large `personalNotes` can dial down peak per-page memory. The ~12 fields
    # `user_data_score` reads aren't pinned via `$select` because Graph 400s
    # on at least one of them on some tenants (and doesn't say which) — pinning
    # the right subset would need a tenant-specific investigation pass; left
    # for 1.1.0 along with the ijson row-streaming refactor.
    params = {"$top": CONTACTS_PAGE_SIZE}

    # Union-Find with path-halving + union-by-rank. Without rank, a long chain
    # of email-sharing duplicates degenerates to O(n) per find and the inner
    # pass becomes quadratic on real mailboxes.
    parent: dict[str, str] = {}
    rank: dict[str, int] = {}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            parent[ra] = rb
        elif rank[ra] > rank[rb]:
            parent[rb] = ra
        else:
            parent[rb] = ra
            rank[ra] += 1

    # Streaming pass: walk every contact exactly once. For each row, extract
    # only the metadata we need and let the full dict be GC'd. Union-Find runs
    # incrementally; group membership stabilizes only after the loop completes.
    email_owner: dict[str, str] = {}
    contact_meta: dict[str, ContactMeta] = {}
    total = 0
    for c in graph_paged(token, f"{GRAPH}/me/contacts", params):
        cid = c["id"]
        total += 1
        parent[cid] = cid
        rank[cid] = 0
        emails_lc = tuple(
            (em.get("address") or "").lower() for em in (c.get("emailAddresses") or []) if em.get("address")
        )
        contact_meta[cid] = (
            c.get("displayName") or "",
            user_data_score(c),
            c.get("createdDateTime") or "",
            emails_lc,
        )
        for addr in emails_lc:
            if not addr:
                continue
            if addr in email_owner:
                union(cid, email_owner[addr])
            else:
                email_owner[addr] = cid
        # `c` falls out of scope on the next iteration; full payload reclaimed.

    rep.info(f"  {total} contacts loaded")

    # Bucket every id by its (now-stable) root. Group lists hold IDs, not
    # contacts — metadata lookups go through `contact_meta`.
    groups: dict[str, list[str]] = {}
    for cid in contact_meta:
        groups.setdefault(find(cid), []).append(cid)
    dup_roots = [root for root, ids in groups.items() if len(ids) > 1]

    if not dup_roots:
        rep.info("\nNo duplicate groups found.")
        rep.summary(groups=0, candidates_to_delete=0)
        return

    rep.info(
        f"  duplicate groups: {len(dup_roots)}  (covering {sum(len(groups[r]) for r in dup_roots)} contacts)",
    )

    # Pick a winner per group. We sort the group's IDs by `(-score, created)`,
    # which is identical to the previous shape — but we sort small tuples, not
    # full contact dicts, so the sort is cheap and the comparison key is
    # constant-size.
    to_delete: list[str] = []
    for root in dup_roots:
        member_ids = groups[root]
        ranked_ids = sorted(member_ids, key=lambda i: (-contact_meta[i][1], contact_meta[i][2]))
        winner_id = ranked_ids[0]
        loser_ids = ranked_ids[1:]
        # Aggregate the union of emails across the group for the preview line.
        emails = sorted({addr for mid in member_ids for addr in contact_meta[mid][3]})
        rep.entry(
            "dedupe.group",
            emails=emails,
            size=len(member_ids),
            keep_name=contact_meta[winner_id][0],
            keep_score=contact_meta[winner_id][1],
            delete_names=[contact_meta[lid][0] for lid in loser_ids],
        )
        to_delete.extend(loser_ids)

    if not apply:
        rep.info(f"\nDry run. Would delete {len(to_delete)} contact(s). Pass --apply to execute.")
        rep.summary(groups=len(dup_roots), candidates_to_delete=len(to_delete), applied=False)
        return

    # The dedupe heuristic ("highest user-data score wins, tiebreak by oldest
    # createdDateTime") can pick the wrong winner on unevenly-enriched contacts.
    # Add a typed confirmation for symmetry with prune/delete and so the user
    # has one last chance to review the dry-run output before committing.
    rep.info(f"\nAbout to delete {len(to_delete)} contact(s) flagged as duplicates.")
    rep.info("(They go to Deleted Items first, recoverable subject to your tenant's retention — typically 14-30 days.)")
    if not rep.confirm(len(to_delete), "DEDUPE"):
        rep.info("Aborted. Nothing was deleted.")
        rep.summary(
            groups=len(dup_roots),
            candidates_to_delete=len(to_delete),
            applied=False,
            aborted=True,
        )
        return

    rep.info(f"\nDeleting {len(to_delete)} contact(s)...")
    sub = [{"method": "DELETE", "url": f"/me/contacts/{cid}", "headers": {}} for cid in to_delete]
    with rep.progress(total=len(sub), unit="contact", desc="Deleting") as pbar:
        deleted, errors = chunked_batch(token, sub, label="delete", pbar=pbar, reporter=rep)
    rep.summary(groups=len(dup_roots), deleted=deleted, errors=errors, applied=True)
