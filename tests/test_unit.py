"""Unit tests for the pure helpers in galpal (no HTTP, no argparse)."""

from __future__ import annotations

import re

import pytest

# `graph_mod` and not `graph` because the `graph` pytest fixture (the FakeGraph
# instance) is a positional parameter on most tests in this file, and the
# parameter would shadow a module-level `graph` import inside the function body.
from _galpal import graph as graph_mod
from _galpal import model
from _galpal.filters import FilterConfig, contact_passes

from .conftest import make_contact, make_user


def _filters(
    *,
    exclude_patterns=(),
    require_comma=False,
    require_email=False,
    require_phone=False,
    require_full_name=False,
):
    """Tiny constructor so test calls don't have to spell out FilterConfig() every time."""
    return FilterConfig(
        exclude_patterns=tuple(exclude_patterns),
        require_comma=require_comma,
        require_email=require_email,
        require_phone=require_phone,
        require_full_name=require_full_name,
    )


# --------------------------------------------------------------------------- _norm / _norm_list


def test_norm_strips_and_normalizes_unicode():
    # Decomposed (NFD): u + combining diaeresis → precomposed (NFC) ü
    nfd = "Müller"
    nfc = "Müller"
    assert model._norm(nfd) == model._norm(nfc) == nfc


@pytest.mark.parametrize(
    ("v", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("  hi  ", "hi"),
        (42, 42),  # non-strings pass through
    ],
)
def test_norm_edge_cases(v, expected):
    assert model._norm(v) == expected


def test_norm_list_drops_empties():
    """Empty / whitespace-only entries are dropped, not preserved as None.

    This is what `gal_already_pulled` actually wants: a businessPhones list
    `["+1-555", ""]` from a partially-populated GAL row should compare equal
    to an existing `["+1-555"]` so a re-pull doesn't trigger a needless PATCH.
    """
    assert model._norm_list(None) == ()
    assert model._norm_list([]) == ()
    assert model._norm_list(["  a", "", "b"]) == ("a", "b")
    assert model._norm_list(["+1-555", "  ", None]) == ("+1-555",)


# --------------------------------------------------------------------------- gal_to_payload


def test_gal_to_payload_picks_mail_over_upn():
    u = make_user(mail="real@x.com", upn="upn@x.com")
    payload, mail = model.gal_to_payload(u)
    assert mail == "real@x.com"
    assert payload["displayName"] == u["displayName"]
    assert payload["businessAddress"]["countryOrRegion"] == "US"


def test_gal_to_payload_falls_back_to_upn():
    u = make_user(mail=None, upn="upn@x.com")
    _, mail = model.gal_to_payload(u)
    assert mail == "upn@x.com"


def test_gal_to_payload_business_phones_normalized_to_list():
    u = make_user(phone=None)
    u["businessPhones"] = None
    payload, _ = model.gal_to_payload(u)
    assert payload["businessPhones"] == []


# --------------------------------------------------------------------------- merge_emails


def test_merge_emails_gal_first_then_user_added_dedup():
    out = model.merge_emails(
        "gal@x.com",
        "Jane Doe",
        existing=[
            {"address": "personal@y.com", "name": "Jane"},
            {"address": "GAL@x.com", "name": "Old"},
        ],  # case-insensitive dedupe
    )
    # On a case-insensitive match between the GAL email and an existing
    # address, the user's casing is preserved (the user may have set it
    # deliberately and Graph treats addresses case-insensitively anyway).
    # The `name` still comes from the GAL display_name — names are
    # GAL-authoritative.
    assert [e["address"] for e in out] == ["GAL@x.com", "personal@y.com"]
    assert out[0]["name"] == "Jane Doe"


def test_merge_emails_uses_gal_email_when_no_user_match():
    """When there's no user-side match, the GAL casing is the only choice."""
    out = model.merge_emails(
        "gal@x.com",
        "Jane Doe",
        existing=[{"address": "personal@y.com", "name": "Jane"}],
    )
    assert [e["address"] for e in out] == ["gal@x.com", "personal@y.com"]


def test_merge_emails_no_gal_keeps_user_emails():
    out = model.merge_emails(None, None, existing=[{"address": "p@y.com", "name": "P"}])
    assert out == [{"address": "p@y.com", "name": "P"}]


def test_merge_emails_handles_missing_address_field():
    out = model.merge_emails("gal@x.com", "Jane", existing=[{"name": "no-address"}])
    assert [e["address"] for e in out] == ["gal@x.com"]


# --------------------------------------------------------------------------- stamp


def test_stamp_attaches_extended_property():
    payload = {"displayName": "Jane"}
    out = model.stamp(payload, "azure-id-123")
    assert out is payload  # mutates in place
    assert payload["singleValueExtendedProperties"] == [
        {"id": graph_mod.EP_AZURE_ID, "value": "azure-id-123"},
    ]


def test_ep_azure_id_uses_lowercase_guid():
    """Regression: Outlook canonicalizes the GUID lowercase, and the Graph $filter
    on extended-property id is case-sensitive — so this constant MUST be lowercase."""
    assert "c000-000000000046" in graph_mod.EP_AZURE_ID
    assert "C000" not in graph_mod.EP_AZURE_ID


# --------------------------------------------------------------------------- build_request


def test_build_request_creates_when_no_existing():
    u = make_user("u1")
    req = model.build_request(None, u)
    assert req["method"] == "POST"
    assert req["url"] == "/me/contacts"
    assert req["body"]["singleValueExtendedProperties"][0]["value"] == "u1"
    assert req["body"]["emailAddresses"][0]["address"] == u["mail"]


def test_build_request_patches_when_existing_and_merges_emails():
    u = make_user("u1")
    existing = make_contact(
        "c1",
        emails=[
            {"address": "extra@personal.com", "name": "Personal"},
        ],
    )
    req = model.build_request(existing, u)
    assert req["method"] == "PATCH"
    assert req["url"] == "/me/contacts/c1"
    addrs = [e["address"] for e in req["body"]["emailAddresses"]]
    assert addrs[0] == u["mail"]  # GAL email always first
    assert "extra@personal.com" in addrs  # user-added preserved


def test_build_request_does_not_wipe_existing_emails_when_gal_has_no_mail():
    # Regression: if both the GAL row and the existing contact have no usable
    # email, merge_emails returns []. Without the empty-list guard in
    # build_request, that [] would land in the PATCH body and Graph would
    # interpret it as "wipe all addresses". The merged-empty case is a strict
    # no-op: emailAddresses is omitted from the PATCH entirely.
    #
    # (The richer case where the GAL row has no mail but the existing contact
    # already has user-added entries returns those entries unchanged from
    # merge_emails, so the assignment is a faithful round-trip.)
    u = make_user("u1", mail=None, upn=None)
    # make_contact's `emails or [...]` falls back to a default list on `[]`,
    # so build the contact directly to actually exercise "no emails on either side".
    existing = {"id": "c1", "displayName": "Doe, Jane", "emailAddresses": []}
    req = model.build_request(existing, u)
    assert req["method"] == "PATCH"
    assert "emailAddresses" not in req["body"]


# --------------------------------------------------------------------------- gal_already_pulled


def test_gal_already_pulled_true_when_all_fields_match():
    u = make_user("u1")
    existing = {
        "displayName": u["displayName"],
        "givenName": u["givenName"],
        "surname": u["surname"],
        "jobTitle": u["jobTitle"],
        "department": u["department"],
        "companyName": u["companyName"],
        "officeLocation": u["officeLocation"],
        "mobilePhone": u["mobilePhone"],
        "businessPhones": u["businessPhones"],
        "businessAddress": {
            "street": u["streetAddress"],
            "city": u["city"],
            "state": u["state"],
            "postalCode": u["postalCode"],
            "countryOrRegion": u["country"],
        },
        "emailAddresses": [{"address": u["mail"], "name": u["displayName"]}],
    }
    assert model.gal_already_pulled(u, existing)


def test_gal_already_pulled_false_on_field_mismatch():
    u = make_user("u1", title="Engineer")
    existing = {
        "displayName": u["displayName"],
        "givenName": u["givenName"],
        "surname": u["surname"],
        "jobTitle": "Manager",
        "department": u["department"],
        "companyName": u["companyName"],
        "officeLocation": u["officeLocation"],
        "mobilePhone": None,
        "businessPhones": u["businessPhones"],
        "businessAddress": {},
        "emailAddresses": [{"address": u["mail"]}],
    }
    assert not model.gal_already_pulled(u, existing)


def test_gal_already_pulled_treats_nfd_and_nfc_as_equal():
    u = make_user("u1", surname="Müller")  # NFC
    existing = {
        "displayName": u["displayName"],
        "givenName": u["givenName"],
        "surname": "Müller",  # NFD
        "jobTitle": u["jobTitle"],
        "department": u["department"],
        "companyName": u["companyName"],
        "officeLocation": u["officeLocation"],
        "mobilePhone": None,
        "businessPhones": u["businessPhones"],
        "businessAddress": {
            "street": u["streetAddress"],
            "city": u["city"],
            "state": u["state"],
            "postalCode": u["postalCode"],
            "countryOrRegion": u["country"],
        },
        "emailAddresses": [{"address": u["mail"]}],
    }
    assert model.gal_already_pulled(u, existing)


def test_gal_already_pulled_false_when_gal_email_not_first():
    u = make_user("u1")
    existing = {
        "displayName": u["displayName"],
        "givenName": u["givenName"],
        "surname": u["surname"],
        "jobTitle": u["jobTitle"],
        "department": u["department"],
        "companyName": u["companyName"],
        "officeLocation": u["officeLocation"],
        "mobilePhone": None,
        "businessPhones": u["businessPhones"],
        "businessAddress": {
            "street": u["streetAddress"],
            "city": u["city"],
            "state": u["state"],
            "postalCode": u["postalCode"],
            "countryOrRegion": u["country"],
        },
        "emailAddresses": [{"address": "other@x.com"}, {"address": u["mail"]}],
    }
    assert not model.gal_already_pulled(u, existing)


# --------------------------------------------------------------------------- user_data_score


def testuser_data_score_counts_user_added_fields():
    contact = {
        "homePhones": ["555-0100"],
        "imAddresses": ["jane@chat"],
        "categories": ["personal", "vip"],
        "personalNotes": "met at conf",
        "birthday": "1990-01-01",
        "spouseName": "Pat",
        "children": ["A", "B"],
        "homeAddress": {"city": "Chicago"},
    }
    assert model.user_data_score(contact) == 1 + 1 + 2 + 1 + 1 + 1 + 2 + 1


def testuser_data_score_zero_for_empty_contact():
    assert model.user_data_score({"displayName": "Jane"}) == 0


# --------------------------------------------------------------------------- fetch_gal filters


def _seed_gal(graph, *users):
    graph.users.extend(users)


def test_fetch_gal_skips_guests(graph):
    _seed_gal(
        graph,
        make_user("u1", name="Member"),
        {**make_user("u2", name="Guest"), "userType": "Guest"},
    )
    out = list(graph_mod.fetch_gal("t", _filters()))
    assert [u["id"] for u in out] == ["u1"]


def test_fetch_gal_requires_displayname(graph):
    _seed_gal(graph, {**make_user("u1"), "displayName": ""})
    assert list(graph_mod.fetch_gal("t", _filters())) == []


def test_fetch_gal_require_email_filters_upn_only(graph):
    _seed_gal(
        graph,
        make_user("u1", mail="x@x.com"),
        make_user("u2", mail=None, upn="upn@x.com"),
    )
    out_no = list(graph_mod.fetch_gal("t", _filters()))
    assert {u["id"] for u in out_no} == {"u1", "u2"}  # UPN-only allowed
    out_yes = list(graph_mod.fetch_gal("t", _filters(require_email=True)))
    assert {u["id"] for u in out_yes} == {"u1"}


def test_fetch_gal_require_full_name(graph):
    _seed_gal(
        graph,
        make_user("u1"),  # has both
        make_user("u2", given="", surname="Smith"),  # no first
        make_user("u3", given="John", surname=""),  # no last
        make_user("u4", given="  ", surname="  "),
    )  # whitespace
    out = list(graph_mod.fetch_gal("t", _filters(require_full_name=True)))
    assert {u["id"] for u in out} == {"u1"}


def test_fetch_gal_require_phone(graph):
    _seed_gal(graph, make_user("u1", phone="+1-555-0000"), make_user("u2", phone=None))
    out = list(graph_mod.fetch_gal("t", _filters(require_phone=True)))
    assert {u["id"] for u in out} == {"u1"}


def test_fetch_gal_require_comma(graph):
    _seed_gal(graph, make_user("u1", name="Doe, Jane"), make_user("u2", name="Bob"))
    out = list(graph_mod.fetch_gal("t", _filters(require_comma=True)))
    assert {u["id"] for u in out} == {"u1"}


def test_fetch_gal_exclude_pattern(graph):
    _seed_gal(
        graph,
        make_user("u1", name="Real Person"),
        make_user("u2", name="Test Account"),
        make_user("u3", name="Robot"),
    )
    patterns = [re.compile(r"^Test"), re.compile(r"Robot")]
    out = list(graph_mod.fetch_gal("t", _filters(exclude_patterns=patterns)))
    assert [u["id"] for u in out] == ["u1"]


# --------------------------------------------------------------------------- send_batch retry


def test_send_batch_retries_on_per_request_429(graph, monkeypatch):
    """A 429 in a batch sub-response triggers a re-send of just that sub-request.

    Uses `monkeypatch.setattr` (not raw module mutation) so the patch is reverted
    at teardown — earlier versions of this test leaked the mock across tests.
    """
    from .conftest import FakeResponse

    scripted: list[dict] = []

    def post(url, headers=None, json=None, params=None, timeout=None):
        scripted.append(json)
        if len(scripted) == 1:
            return FakeResponse(
                200,
                {
                    "responses": [
                        {"id": "1", "status": 201, "body": {"ok": 1}},
                        {"id": "2", "status": 429, "headers": {"Retry-After": "0"}},
                    ]
                },
            )
        return FakeResponse(
            200,
            {
                "responses": [
                    {"id": "2", "status": 201, "body": {"ok": 2}},
                ]
            },
        )

    monkeypatch.setattr("_galpal.graph.requests.post", post)
    out = graph_mod.send_batch(
        "t",
        [
            {"method": "POST", "url": "/me/contacts", "body": {"a": 1}},
            {"method": "POST", "url": "/me/contacts", "body": {"a": 2}},
        ],
    )
    assert [r["status"] for r in out] == [201, 201]
    assert len(scripted) == 2  # one initial + one retry
    assert len(scripted[1]["requests"]) == 1  # only the throttled sub-request was retried


# --------------------------------------------------------------------------- contact_passes (filters)


def test_contact_passes_filters_default_passes_all():
    """No filters → every contact with a displayName passes."""
    assert contact_passes({"displayName": "Jane"}, _filters())


def test_contact_passes_filters_empty_displayname_always_fails():
    assert not contact_passes({"displayName": ""}, _filters())


@pytest.mark.parametrize(
    ("contact", "passes"),
    [
        ({"displayName": "Jane", "emailAddresses": [{"address": "j@x.com"}]}, True),
        ({"displayName": "Jane", "emailAddresses": []}, False),
        ({"displayName": "Jane"}, False),
        ({"displayName": "Jane", "emailAddresses": [{"address": ""}]}, False),
    ],
)
def test_contact_passes_filters_require_email(contact, passes):
    assert contact_passes(contact, _filters(require_email=True)) is passes


@pytest.mark.parametrize(
    ("contact", "passes"),
    [
        ({"displayName": "Jane", "givenName": "Jane", "surname": "Doe"}, True),
        ({"displayName": "Jane", "givenName": "Jane", "surname": ""}, False),
        ({"displayName": "Jane", "givenName": "", "surname": "Doe"}, False),
        ({"displayName": "Jane", "givenName": "  ", "surname": "Doe"}, False),
        ({"displayName": "Jane"}, False),
    ],
)
def test_contact_passes_filters_require_full_name(contact, passes):
    assert contact_passes(contact, _filters(require_full_name=True)) is passes


@pytest.mark.parametrize(
    ("contact", "passes"),
    [
        ({"displayName": "Jane", "businessPhones": ["+1-555"]}, True),
        ({"displayName": "Jane", "mobilePhone": "+1-555"}, True),
        ({"displayName": "Jane"}, False),
        ({"displayName": "Jane", "businessPhones": [], "mobilePhone": None}, False),
    ],
)
def test_contact_passes_filters_require_phone(contact, passes):
    assert contact_passes(contact, _filters(require_phone=True)) is passes


def test_contact_passes_filters_require_comma():
    cfg = _filters(require_comma=True)
    assert contact_passes({"displayName": "Doe, Jane"}, cfg)
    assert not contact_passes({"displayName": "Jane"}, cfg)


def test_contact_passes_filters_exclude_pattern():
    cfg = _filters(exclude_patterns=[re.compile(r"^Test")])
    assert not contact_passes({"displayName": "Test Account"}, cfg)
    assert contact_passes({"displayName": "Real Person"}, cfg)


# --------------------------------------------------------------------------- FilterConfig helpers


def test_filter_config_is_active_default_is_false():
    assert FilterConfig().is_active() is False


def test_filter_config_is_active_true_when_any_knob_set():
    assert FilterConfig(require_email=True).is_active() is True
    assert FilterConfig(exclude_patterns=(re.compile("x"),)).is_active() is True


def test_filter_config_describe_lists_only_active_knobs():
    cfg = FilterConfig(require_email=True, require_phone=True)
    assert cfg.describe() == ["--require-email", "--require-phone"]


def test_filter_config_describe_includes_exclude_patterns():
    cfg = FilterConfig(exclude_patterns=(re.compile(r"^Test"),))
    assert cfg.describe() == ["--exclude: ['^Test']"]
