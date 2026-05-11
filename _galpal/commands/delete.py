"""`galpal delete` — DESTRUCTIVE: delete personal contacts.

Two modes:
  - default: delete only contacts that lack a galpal stamp (manual additions);
    confirmation token is `DELETE <count> UNSTAMPED`.
  - --all:   delete EVERY contact regardless of stamp;
    confirmation token is `DELETE <count> ALL`.

Always dry-run by default. --apply requires an interactive prompt; the
confirmation phrase is bound to the scope so a typo can't accidentally take
the more drastic path on the same count.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from _galpal.commands import PRUNE_PREVIEW_LIMIT
from _galpal.graph import (
    EP_AZURE_ID,
    EP_AZURE_ID_NAME,
    GRAPH,
    chunked_batch,
    graph_paged,
)
from _galpal.reporter import default_reporter

if TYPE_CHECKING:
    from _galpal.reporter import Reporter


def run_delete(
    token: str,
    *,
    apply: bool,
    all_contacts: bool,
    reporter: Reporter | None = None,
) -> None:
    """Delete personal contacts. DESTRUCTIVE.

    Two modes:

    - default: delete contacts that lack a galpal stamp — manually-added entries,
      vendors, friends, anything you added by hand.
    - all_contacts=True: delete EVERY contact regardless of stamp.

    Default is dry-run; --apply requires interactive confirmation.
    """
    rep = reporter or default_reporter()
    rep.info("Loading personal contacts...")
    params = {
        "$top": 100,
        "$expand": f"singleValueExtendedProperties($filter=id eq '{EP_AZURE_ID}')",
    }
    candidates = []
    total = 0
    for c in graph_paged(token, f"{GRAPH}/me/contacts", params):
        total += 1
        if all_contacts:
            candidates.append(c)
            continue
        has_stamp = any(
            EP_AZURE_ID_NAME in (ep.get("id") or "") and ep.get("value")
            for ep in c.get("singleValueExtendedProperties") or []
        )
        if not has_stamp:
            candidates.append(c)
    if all_contacts:
        rep.info(f"  {total} total contact(s) — ALL marked for deletion (--all)")
    else:
        rep.info(f"  {total} total; {len(candidates)} not stamped by galpal")

    if not candidates:
        msg = "your address book is already empty." if all_contacts else "every contact is stamped by galpal."
        rep.info(f"\nNothing to delete — {msg}")
        rep.summary(total=total, candidates=0)
        return

    label = "contact(s)" if all_contacts else "unstamped contact(s)"
    rep.info(f"\n{len(candidates)} {label}:")
    for c in candidates[:PRUNE_PREVIEW_LIMIT]:
        emails = ",".join(em.get("address") or "" for em in (c.get("emailAddresses") or [])[:2])
        rep.entry("preview.row", name=c.get("displayName"), emails=emails)
    if len(candidates) > PRUNE_PREVIEW_LIMIT:
        rep.info(f"  ... and {len(candidates) - PRUNE_PREVIEW_LIMIT} more")

    if not apply:
        rep.info(f"\nDry run. Would delete {len(candidates)} contact(s). Pass --apply to execute.")
        rep.summary(total=total, candidates=len(candidates), applied=False)
        return

    rep.info(f"\nAbout to PERMANENTLY DELETE {len(candidates)} contact(s) from your mailbox.")
    if all_contacts:
        rep.info("--all is set: this wipes EVERY contact, including those pulled from the GAL.")
        rep.info("After this, only re-running `pull` can restore the directory contacts.")
    else:
        rep.info("This includes every contact not stamped by galpal — manually-added entries,")
        rep.info("vendors, personal contacts, anything you added by hand.")
    rep.info("(They go to Deleted Items first, recoverable subject to your tenant's retention — typically 14-30 days.)")
    # The candidate list was enumerated before this prompt; contacts added in
    # the meantime by another Outlook client / the web UI / a parallel script
    # won't be in the snapshot and survive this run. There's no airtight fix
    # — Outlook permits concurrent writes — so just say so and let the user
    # decide whether to re-run.
    rep.info("Note: contacts added after this prompt (other Outlook clients, web UI) will survive this run.")
    scope = "ALL" if all_contacts else "UNSTAMPED"
    if not rep.confirm(len(candidates), scope):
        rep.info("Aborted. Nothing was deleted.")
        rep.summary(total=total, candidates=len(candidates), applied=False, aborted=True)
        return

    rep.info(f"\nDeleting {len(candidates)} contact(s)...")
    sub = [{"method": "DELETE", "url": f"/me/contacts/{c['id']}", "headers": {}} for c in candidates]
    with rep.progress(total=len(sub), unit="contact", desc="Deleting") as pbar:
        deleted, errors = chunked_batch(token, sub, label="delete", pbar=pbar, reporter=rep)
    rep.summary(total=total, deleted=deleted, errors=errors, applied=True)
