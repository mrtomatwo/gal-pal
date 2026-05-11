"""`galpal remove-category` — strip categories from contacts and master entries.

Walks every contact folder (not just the default) so categories assigned in
subfolders are also caught. Deletes the matching master-category entries so
they vanish from the Outlook sidebar.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from _galpal.commands import PREVIEW_LIMIT
from _galpal.graph import GRAPH, chunked_batch, graph_paged
from _galpal.reporter import default_reporter

if TYPE_CHECKING:
    from _galpal.reporter import Reporter


def run_remove_categories(
    token: str,
    names: list[str],
    *,
    apply: bool,
    reporter: Reporter | None = None,
) -> None:
    """Strip the named categories from every personal contact.

    With --apply, also delete the matching master-category entries so they vanish
    from the Outlook sidebar.
    """
    rep = reporter or default_reporter()
    targets = {n.casefold() for n in names}
    rep.info(f"Removing categories: {sorted(names)}")

    rep.info("Loading all contacts (default folder + every contact subfolder)...")
    params = {"$top": 100, "$select": "id,displayName,categories"}
    contacts = list(graph_paged(token, f"{GRAPH}/me/contacts", params))
    rep.info(f"  default folder: {len(contacts)} contacts")

    folders = list(graph_paged(token, f"{GRAPH}/me/contactFolders"))
    seen_ids = {c["id"] for c in contacts}
    for f in folders:
        sub = list(graph_paged(token, f"{GRAPH}/me/contactFolders/{f['id']}/contacts", params))
        new = [c for c in sub if c["id"] not in seen_ids]
        seen_ids.update(c["id"] for c in new)
        contacts.extend(new)
        rep.info(f"  folder '{f.get('displayName')}': {len(sub)} contacts ({len(new)} new)")
    rep.info(f"  total scanned: {len(contacts)}")

    updates: list[tuple[str, str, list[str]]] = []  # (id, name, new_categories)
    for c in contacts:
        cats = c.get("categories") or []
        kept = [x for x in cats if x.casefold() not in targets]
        if len(kept) != len(cats):
            updates.append((c["id"], c.get("displayName") or "(no name)", kept))

    rep.info(f"  contacts to update: {len(updates)}")
    for _, name, kept in updates[:PREVIEW_LIMIT]:
        rep.entry("category.update_preview", name=name, kept=kept)
    if len(updates) > PREVIEW_LIMIT:
        rep.info(f"    ... and {len(updates) - PREVIEW_LIMIT} more")

    rep.info("\nLoading master categories...")
    master = list(graph_paged(token, f"{GRAPH}/me/outlook/masterCategories"))
    master_hits = [m for m in master if (m.get("displayName") or "").casefold() in targets]
    rep.info(f"  master categories matching: {len(master_hits)}")
    for m in master_hits:
        rep.entry("category.master_match", name=m.get("displayName"), id=m.get("id"))

    if not apply:
        rep.info("\nDry run. Pass --apply to execute.")
        rep.summary(scanned=len(contacts), updates=len(updates), master_matches=len(master_hits), applied=False)
        return

    if not updates and not master_hits:
        rep.info("\nNothing to do.")
        rep.summary(scanned=len(contacts), updates=0, master_matches=0, applied=True)
        return

    patched = errors = 0
    if updates:
        rep.info(f"\nPatching {len(updates)} contact(s)...")
        sub = [
            {
                "method": "PATCH",
                "url": f"/me/contacts/{cid}",
                "headers": {"Content-Type": "application/json"},
                "body": {"categories": kept},
            }
            for cid, _, kept in updates
        ]
        names = [name for _, name, _ in updates]

        def _patch_response(name: str, resp: dict) -> tuple[bool, dict | None]:
            if resp.get("status") in (200, 204):
                return True, None
            return False, {
                "action": "patch",
                "name": name,
                "status": resp.get("status"),
                "body": resp.get("body"),
            }

        with rep.progress(total=len(updates), unit="contact", desc="Patching") as pbar:
            patched, errors = chunked_batch(
                token,
                sub,
                label="patch",
                pbar=pbar,
                reporter=rep,
                tags=names,
                on_response=_patch_response,
            )

    deleted = master_errors = 0
    if master_hits:
        rep.info(f"\nDeleting {len(master_hits)} master category entry/entries...")
        sub = [
            {
                "method": "DELETE",
                "url": f"/me/outlook/masterCategories/{m['id']}",
                "headers": {},
            }
            for m in master_hits
        ]
        master_tags = [m.get("displayName") for m in master_hits]

        def _master_response(name: str | None, resp: dict) -> tuple[bool, dict | None]:
            if resp.get("status") in (200, 204):
                return True, None
            return False, {
                "action": "delete master",
                "name": name or "",
                "status": resp.get("status"),
                "body": resp.get("body"),
            }

        deleted, master_errors = chunked_batch(
            token,
            sub,
            label="delete master",
            reporter=rep,
            tags=master_tags,
            on_response=_master_response,
        )

    rep.summary(
        scanned=len(contacts),
        patched=patched,
        patch_errors=errors,
        master_deleted=deleted,
        master_errors=master_errors,
        applied=True,
    )
