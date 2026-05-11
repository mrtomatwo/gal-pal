"""`galpal list-folders` and `galpal remove-folder` — contact-folder operations.

Two close-cousin subcommands grouped together: one prints folders + counts, the
other deletes folders by name. Both use the same `$count` GET pattern and folder
listing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import requests

from _galpal.graph import GRAPH, HTTP_TIMEOUT_S, _retrying_request, graph_paged, send_batch
from _galpal.reporter import default_reporter

if TYPE_CHECKING:
    from _galpal.reporter import Reporter

# Status-code labels for `_folder_count` — searchable names instead of magic
# literals. The count is purely cosmetic, so non-2xx returns a label rather
# than raising; "auth?" / "denied" specifically helps a user notice that
# Contacts.ReadWrite was revoked mid-run.
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_HTTP_CLIENT_ERROR = 400


def _folder_count(token: str, folder_id: str) -> str:
    """Return the contact count for a folder as a display string.

    On any error returns a short label that explains *why* — distinct labels
    for "auth?" (401) / "denied" (403) / "?" (everything else) help a user
    debug a tenant that has revoked Contacts.ReadWrite mid-run, where every
    folder otherwise just shows "?" with no hint.

    Routes through `_retrying_request` so 5xx + transient network errors get
    the same backoff treatment as the rest of the codebase. The count is
    purely cosmetic — folder deletion still works regardless.
    """
    count_url = f"{GRAPH}/me/contactFolders/{folder_id}/contacts/$count"
    headers = {"Authorization": f"Bearer {token}", "ConsistencyLevel": "eventual"}
    try:
        r = _retrying_request("GET", count_url, headers=headers, timeout=HTTP_TIMEOUT_S)
    except requests.RequestException:
        return "?"
    if r.status_code == _HTTP_UNAUTHORIZED:
        return "auth?"
    if r.status_code == _HTTP_FORBIDDEN:
        return "denied"
    if r.status_code >= _HTTP_CLIENT_ERROR:
        return "?"
    return r.text.strip()


def run_list_folders(token: str, *, reporter: Reporter | None = None) -> None:
    """Print the user's contact folders with contact counts.

    Outlook's web UI lists contact folders under 'Kategorien' in the sidebar, which is
    easy to confuse with the Outlook categories feature. This is the disambiguator.
    """
    rep = reporter or default_reporter()
    folders = list(graph_paged(token, f"{GRAPH}/me/contactFolders"))
    if not folders:
        rep.info("No personal contact folders found (only the default 'Contacts' folder exists).")
        rep.summary(folders=0)
        return
    rep.info(f"{len(folders)} contact folder(s):")
    for f in folders:
        count = _folder_count(token, f["id"])
        rep.entry("folder.entry", name=f.get("displayName"), id=f.get("id"), contacts=count)
    rep.summary(folders=len(folders))


def run_remove_folders(
    token: str,
    names: list[str],
    *,
    apply: bool,
    reporter: Reporter | None = None,
) -> None:
    """Delete the named contact folders (matched case-insensitively).

    Outlook moves the folder and its contacts into Deleted Items, so this is recoverable
    subject to your tenant's retention (typically 14-30 days).
    """
    rep = reporter or default_reporter()
    targets = {n.casefold() for n in names}
    folders = list(graph_paged(token, f"{GRAPH}/me/contactFolders"))
    hits = [f for f in folders if (f.get("displayName") or "").casefold() in targets]

    rep.info(f"Matched {len(hits)} folder(s):")
    for f in hits:
        count = _folder_count(token, f["id"])
        rep.entry("folder.match", name=f.get("displayName"), contacts=count)

    missing = sorted(targets - {(f.get("displayName") or "").casefold() for f in hits})
    if missing:
        rep.info(f"\nNo folder found for: {missing}")
        rep.info("Run `galpal list-folders` to see what folders exist.")

    if not hits:
        rep.summary(matched=0, deleted=0)
        return
    if not apply:
        rep.info("\nDry run. Pass --apply to delete the folders (contents go to Deleted Items).")
        rep.summary(matched=len(hits), deleted=0, applied=False)
        return

    rep.info(f"\nDeleting {len(hits)} folder(s)...")
    sub = [{"method": "DELETE", "url": f"/me/contactFolders/{f['id']}", "headers": {}} for f in hits]
    deleted = errors = 0
    for f, resp in zip(hits, send_batch(token, sub), strict=True):
        name = f.get("displayName")
        if resp.get("status") in (200, 204):
            deleted += 1
            rep.entry("folder.deleted", name=name)
        else:
            errors += 1
            rep.entry(
                "subrequest.error",
                action="delete folder",
                name=name,
                status=resp.get("status"),
                body=resp.get("body"),
            )
    rep.summary(matched=len(hits), deleted=deleted, errors=errors, applied=True)
