"""`galpal prune` — delete previously-pulled contacts that no longer fit.

A stamped contact is pruned when it fails to satisfy ALL of the active criteria
(equivalently, when it fails ANY single criterion — the helper below is named
`passes` and returns True iff every criterion passes; we delete when not passes).
Active criteria come from two axes:
  - data filters (--require-comma / --require-email / --require-phone /
    --require-full-name / --exclude) — evaluated against the contact's stored data
  - --orphans — additionally requires that the Azure source is still live in
    the directory (only this one needs a /users fetch)

Only contacts stamped by galpal are considered, so manually-added contacts
are never touched.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from _galpal.commands import PRUNE_PREVIEW_LIMIT
from _galpal.filters import FilterConfig, contact_passes
from _galpal.graph import chunked_batch, fetch_existing_contacts, iter_all_user_ids
from _galpal.reporter import default_reporter

if TYPE_CHECKING:
    from _galpal.reporter import Reporter


def run_prune(
    token: str,
    filters: FilterConfig,
    *,
    orphans: bool,
    apply: bool,
    reporter: Reporter | None = None,
) -> None:
    """Delete previously-pulled contacts that no longer pass the active filters.

    Data filters are evaluated against the contact's own stored data (the GAL fields
    galpal wrote during pull). Pass --orphans to additionally require that the
    contact's Azure AD source still exists — that one criterion needs a /users
    fetch; the data filters do not. Only contacts stamped by galpal are considered,
    so manually-added contacts are never touched.
    """
    rep = reporter or default_reporter()
    if not (filters.is_active() or orphans):
        rep.info("Refusing to prune with no filters set — that would delete every pulled contact.")
        rep.info(
            "Pass at least one of: --exclude, --require-comma, --require-email, "
            "--require-phone, --require-full-name, --orphans."
        )
        return

    rep.info("\nIndexing existing contacts...")
    by_azure_id, _ = fetch_existing_contacts(token)
    rep.info(f"  stamped contacts: {len(by_azure_id)}")
    if not by_azure_id:
        rep.info("\nNothing to prune (no pulled contacts found).")
        rep.summary(stamped=0, candidates_to_delete=0)
        return

    # If orphans was requested, fetch the live `/users` set and fold it into a
    # new FilterConfig — the orphan check is now a regular field on the
    # dataclass (see _galpal/filters.py), not an ad-hoc closure.
    if orphans:
        rep.info("\nFetching directory user ids...")
        gal_ids: set[str] = set()
        with rep.progress(total=None, unit="user", desc="Fetching directory") as bar:
            for uid in iter_all_user_ids(token):
                gal_ids.add(uid)
                bar.update(1)
        rep.info(f"  {len(gal_ids)} live GAL entries")
        # Rebuild the FilterConfig with `live_user_ids` populated. Frozen
        # dataclass → use `replace` rather than mutating in place.
        filters = replace(filters, live_user_ids=frozenset(gal_ids))

    rep.info("Active filters:")
    for line in filters.describe():
        rep.info(f"  {line}")

    to_delete = [(aid, c) for aid, c in by_azure_id.items() if not contact_passes(c, filters, azure_id=aid)]
    if not to_delete:
        rep.info("\nNothing to prune — every pulled contact still passes the filters.")
        rep.summary(stamped=len(by_azure_id), candidates_to_delete=0)
        return

    rep.info(f"\n{len(to_delete)} pulled contact(s) no longer pass the filters:")
    for _, c in to_delete[:PRUNE_PREVIEW_LIMIT]:
        emails = ",".join(em.get("address") or "" for em in (c.get("emailAddresses") or [])[:2])
        rep.entry("preview.row", name=c.get("displayName"), emails=emails)
    if len(to_delete) > PRUNE_PREVIEW_LIMIT:
        rep.info(f"  ... and {len(to_delete) - PRUNE_PREVIEW_LIMIT} more")

    if not apply:
        rep.info(f"\nDry run. Would delete {len(to_delete)} contact(s). Pass --apply to execute.")
        rep.summary(stamped=len(by_azure_id), candidates_to_delete=len(to_delete), applied=False)
        return

    rep.info(f"\nAbout to PERMANENTLY DELETE {len(to_delete)} contact(s) from your mailbox.")
    rep.info("(They go to Deleted Items first, recoverable subject to your tenant's retention — typically 14-30 days.)")
    if not rep.confirm(len(to_delete), "PRUNE"):
        rep.info("Aborted. Nothing was deleted.")
        rep.summary(stamped=len(by_azure_id), candidates_to_delete=len(to_delete), applied=False, aborted=True)
        return

    rep.info(f"\nDeleting {len(to_delete)} contact(s)...")
    sub = [{"method": "DELETE", "url": f"/me/contacts/{c['id']}", "headers": {}} for _, c in to_delete]
    with rep.progress(total=len(sub), unit="contact", desc="Deleting") as pbar:
        deleted, errors = chunked_batch(token, sub, label="delete", pbar=pbar, reporter=rep)
    rep.summary(stamped=len(by_azure_id), deleted=deleted, errors=errors, applied=True)
