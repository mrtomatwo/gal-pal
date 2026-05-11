"""Pure data transforms — GAL ⇄ Graph contact projection, normalization, equality.

No I/O, no presentation. Every function is a small total computation over plain
dicts. This is the layer that's easiest to unit-test in isolation, and where most
of the GAL-specific business logic lives.
"""

from __future__ import annotations

import unicodedata

from _galpal.graph import EP_AZURE_ID


def gal_to_payload(u: dict):
    """Project a GAL user into the contact-shaped payload Graph expects, plus the chosen mail."""
    mail = u.get("mail") or u.get("userPrincipalName")
    payload = {
        "displayName": u.get("displayName"),
        "givenName": u.get("givenName"),
        "surname": u.get("surname"),
        "jobTitle": u.get("jobTitle"),
        "department": u.get("department"),
        "companyName": u.get("companyName"),
        "officeLocation": u.get("officeLocation"),
        "businessPhones": u.get("businessPhones") or [],
        "mobilePhone": u.get("mobilePhone"),
        "businessAddress": {
            "street": u.get("streetAddress"),
            "city": u.get("city"),
            "state": u.get("state"),
            "postalCode": u.get("postalCode"),
            "countryOrRegion": u.get("country"),
        },
    }
    return payload, mail


def merge_emails(gal_email: str | None, display_name: str | None, existing: list | None):
    """GAL address goes first; any extra addresses the user added are kept.

    When the GAL email matches an existing address case-insensitively (the user
    already had `John.Smith@x.com` and the GAL serves `john.smith@x.com`), the
    existing casing is preserved — the user may have set it deliberately and
    Graph treats addresses as case-insensitive anyway, so overwriting with the
    GAL casing is silent data loss. The `name` field still comes from the GAL
    `display_name` because names are GAL-authoritative.
    """
    out: list[dict] = []
    seen: set[str] = set()
    existing = existing or []
    if gal_email:
        gal_lc = gal_email.lower()
        # Preserve user-stored casing on a case-insensitive match.
        user_match = next(
            (e.get("address") for e in existing if (e.get("address") or "").lower() == gal_lc),
            None,
        )
        address = user_match or gal_email
        out.append({"address": address, "name": display_name or address})
        seen.add(gal_lc)
    for e in existing:
        addr = (e.get("address") or "").lower()
        if addr and addr not in seen:
            out.append({k: e[k] for k in ("address", "name") if k in e})
            seen.add(addr)
    return out


def stamp(payload: dict, azure_id: str) -> dict:
    """Attach the GalpalAzureId extended property to a contact payload (idempotent)."""
    payload["singleValueExtendedProperties"] = [{"id": EP_AZURE_ID, "value": azure_id}]
    return payload


def _norm(v):
    """Normalize a comparison value.

    - None / "" / whitespace-only → None
    - strings: strip Unicode whitespace and normalize to NFC, so "Müller" reads
      the same whether stored precomposed (one codepoint) or decomposed
      (u + combining diaeresis). macOS-touched Exchange contacts can be NFD.
    """
    if v is None:
        return None
    if isinstance(v, str):
        v = unicodedata.normalize("NFC", v.strip())
        return v or None
    return v


def _norm_list(v):
    """Normalize each entry in a list, dropping ones that normalize to None.

    Empty strings, whitespace-only, and explicit None entries collapse out;
    without that, a GAL row with `["+1-555-0100", ""]` would not equal an
    existing contact's `["+1-555-0100"]` even though Graph treats them
    identically, and `gal_already_pulled` would trigger a needless PATCH.
    """
    return tuple(n for n in (_norm(x) for x in (v or [])) if n is not None)


def gal_already_pulled(u: dict, existing: dict) -> bool:
    """Return True iff the contact already reflects every GAL-managed field — safe to skip the PATCH."""
    direct = [
        ("displayName", "displayName"),
        ("givenName", "givenName"),
        ("surname", "surname"),
        ("jobTitle", "jobTitle"),
        ("department", "department"),
        ("companyName", "companyName"),
        ("officeLocation", "officeLocation"),
        ("mobilePhone", "mobilePhone"),
    ]
    for u_key, c_key in direct:
        if _norm(existing.get(c_key)) != _norm(u.get(u_key)):
            return False
    if _norm_list(existing.get("businessPhones")) != _norm_list(u.get("businessPhones")):
        return False

    addr = existing.get("businessAddress") or {}
    address_pairs = [
        ("street", "streetAddress"),
        ("city", "city"),
        ("state", "state"),
        ("postalCode", "postalCode"),
        ("countryOrRegion", "country"),
    ]
    for c_key, u_key in address_pairs:
        if _norm(addr.get(c_key)) != _norm(u.get(u_key)):
            return False

    # GAL email must already be the first entry (we always write it that way).
    # The deeper field-by-field comparison above is intentionally shallow on
    # everything emailAddresses carries beyond `address` (display name, type,
    # etc.) — those are user-controlled and never overwritten by galpal, so
    # they don't gate the skip.
    #
    # No mail / no UPN: a usable GAL row always has at least one (fetch_gal
    # filters out the rest), but this helper is callable from anywhere and
    # shouldn't depend on that upstream invariant. Treat "no email at all"
    # as "needs a write" — there's no positive signal we can match against.
    gal_email = (u.get("mail") or u.get("userPrincipalName") or "").lower()
    if not gal_email:
        return False
    existing_emails = existing.get("emailAddresses") or []
    first_addr = (existing_emails[0].get("address") or "") if existing_emails else ""
    return first_addr.lower() == gal_email


def build_request(existing: dict | None, u: dict) -> dict:
    """Build a Graph $batch sub-request for create or update."""
    payload, mail = gal_to_payload(u)
    if existing:
        merged = merge_emails(mail, u.get("displayName"), existing.get("emailAddresses"))
        # Skip the assignment when the merge yields nothing — Graph treats
        # `"emailAddresses": []` as "wipe all addresses", which would silently
        # destroy user-added entries on a GAL row that has no `mail` field.
        if merged:
            payload["emailAddresses"] = merged
        stamp(payload, u["id"])  # idempotent; also stamps email-matched contacts on first write
        return {
            "method": "PATCH",
            "url": f"/me/contacts/{existing['id']}",
            "headers": {"Content-Type": "application/json"},
            "body": payload,
        }
    if mail:
        payload["emailAddresses"] = [{"address": mail, "name": u.get("displayName") or mail}]
    stamp(payload, u["id"])
    return {
        "method": "POST",
        "url": "/me/contacts",
        "headers": {"Content-Type": "application/json"},
        "body": payload,
    }


def user_data_score(c: dict) -> int:
    """Higher = more user-added data on this contact.

    Used by `dedupe` to pick the winner among contacts sharing an email.
    The weighting is deliberate, not accidental:

      - List-shaped fields (`homePhones`, `imAddresses`, `categories`,
        `children`) contribute their *length*. Categories in particular are
        user-tagged taxonomy — a contact with three categories has measurably
        more user investment than one with one.
      - Singleton fields (`personalNotes`, `birthday`, `anniversary`,
        `spouseName`, `nickName`, `yomi*`, `homeAddress`) contribute 1 each.
        These are presence-only flags; nobody has "more birthday."

    A contact with `categories=["a","b","c"]` therefore beats a contact with
    `birthday + anniversary + spouseName` (3 vs 3 — tie, broken by the
    `createdDateTime` fallback in `run_dedupe`). That's the intended shape:
    list density usually correlates better with "this is the contact I curate."
    """
    score = 0
    score += len(c.get("homePhones") or [])
    score += len(c.get("imAddresses") or [])
    score += len(c.get("categories") or [])
    if c.get("personalNotes"):
        score += 1
    for k in ("birthday", "anniversary", "spouseName", "nickName", "yomiGivenName", "yomiSurname"):
        if c.get(k):
            score += 1
    if c.get("children"):
        score += len(c["children"])
    home = c.get("homeAddress") or {}
    if any(home.values()):
        score += 1
    return score
