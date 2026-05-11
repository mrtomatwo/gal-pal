"""Shared filter configuration for `pull`, `audit`, and `prune`.

`FilterConfig` is the immutable dataclass passed through every filter-aware
command. Both predicates — `gal_user_passes` for GAL rows and `contact_passes`
for personal contacts — live here so adding a sixth filter is strictly a
one-file change: add the field, update the two predicates, wire the
argparse arg in `cli.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import re


@dataclass(frozen=True)
class FilterConfig:
    """Filter spec passed through every filter-aware command, frozen for safety.

    Five "data" filters (`exclude_patterns` / `require_comma` / `require_email`
    / `require_phone` / `require_full_name`) inspect the row's stored fields.
    The sixth — `live_user_ids` — is the orphan filter: when set to a frozenset
    of live Azure ids, `contact_passes(c, cfg, azure_id=…)` rejects contacts
    whose source isn't in the set. The orphan check needs *contextual* data
    (the live `/users` index) that the data filters don't, so it lives outside
    the dataclass at None by default; `prune --orphans` builds the config late
    after fetching `gal_ids`.

    `exclude_patterns` is a tuple (not list) so the dataclass stays hashable
    and can't be mutated after construction; the CLI compiles each `--exclude`
    string into a `re.Pattern` once.
    """

    exclude_patterns: tuple[re.Pattern, ...] = field(default_factory=tuple)
    require_comma: bool = False
    require_email: bool = False
    require_phone: bool = False
    require_full_name: bool = False
    # Orphan filter. None = no orphan check (default). A frozenset means
    # "the contact's source Azure id must be in this set or it gets pruned."
    live_user_ids: frozenset[str] | None = None

    def is_active(self) -> bool:
        """Return True iff at least one filter would actually filter something out."""
        return bool(
            self.exclude_patterns
            or self.require_comma
            or self.require_email
            or self.require_phone
            or self.require_full_name
            or self.live_user_ids is not None
        )

    def describe(self) -> list[str]:
        """Human-readable lines describing the active filters; used by `prune` preflight."""
        lines: list[str] = []
        if self.exclude_patterns:
            lines.append(f"--exclude: {[p.pattern for p in self.exclude_patterns]}")
        if self.require_comma:
            lines.append("--require-comma")
        if self.require_email:
            lines.append("--require-email")
        if self.require_phone:
            lines.append("--require-phone")
        if self.require_full_name:
            lines.append("--require-full-name")
        if self.live_user_ids is not None:
            lines.append(f"--orphans ({len(self.live_user_ids)} live GAL entries)")
        return lines


def gal_user_passes(u: dict, cfg: FilterConfig) -> bool:
    """Mirror the GAL-side filter logic. Used inside `fetch_gal`.

    The `live_user_ids` field is ignored here — `fetch_gal` is the GAL itself,
    so by definition every row it yields has a live Azure id. The orphan
    filter is meaningful only on the contact side.
    """
    name = u.get("displayName") or ""
    if not name:
        return False
    if cfg.require_comma and "," not in name:
        return False
    if cfg.require_email and not u.get("mail"):
        return False
    if cfg.require_phone and not (u.get("businessPhones") or u.get("mobilePhone")):
        return False
    if cfg.require_full_name and not ((u.get("givenName") or "").strip() and (u.get("surname") or "").strip()):
        return False
    return not any(p.search(name) for p in cfg.exclude_patterns)


def contact_passes(c: dict, cfg: FilterConfig, *, azure_id: str | None = None) -> bool:
    """Mirror `gal_user_passes` against a personal-contact object.

    Used by `prune` so we don't have to re-walk the GAL just to decide which
    pulled contacts still meet the criteria. Contact-shaped fields differ
    slightly from GAL-shaped fields (emailAddresses array vs `mail` scalar),
    so the predicate isn't identical to `gal_user_passes`, but the criteria
    are: any drift here would let prune disagree with pull.

    `azure_id` is required only when `cfg.live_user_ids is not None` — the
    orphan filter needs the contact's stamped source id. `prune` has it via
    the `by_azure_id` index it just built; other callers pass `None` and
    skip the orphan check.
    """
    name = c.get("displayName") or ""
    first_email = next(
        (em.get("address") for em in (c.get("emailAddresses") or []) if em.get("address")),
        None,
    )
    given = (c.get("givenName") or "").strip()
    surname = (c.get("surname") or "").strip()
    has_phone = bool(c.get("businessPhones") or c.get("mobilePhone"))
    is_orphan = cfg.live_user_ids is not None and azure_id is not None and azure_id not in cfg.live_user_ids
    # Single conjunction so the function has one return point and ruff stops
    # complaining about return-statement count. Order matters only for short-
    # circuit speed (cheap checks first); the result is the same either way.
    # Outer `bool(...)` ensures we return True/False, not the last truthy
    # string from a `given and surname` short-circuit.
    return bool(
        name
        and (not cfg.require_comma or "," in name)
        and (not cfg.require_email or first_email is not None)
        and (not cfg.require_phone or has_phone)
        and (not cfg.require_full_name or (given and surname))
        and not any(p.search(name) for p in cfg.exclude_patterns)
        and not is_orphan,
    )
