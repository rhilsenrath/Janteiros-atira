"""Tests for the helpers around the model: option picking, German number format, the fact auditor, brief parsing.

All values are invented."""

from datetime import datetime, timezone

from janteiros.browser.kit import _pick_option
from janteiros.dispatcher import de_num, de_percent
from janteiros.normalize import norm_cert, parse_datetime, to_decimal, unit_kind
from janteiros.notebook import Notebook
from janteiros.planner import _regex_company, _regex_date, _regex_reference
from janteiros.tools import Ctx, Recorder, _short_id

CTX = Ctx(agent="t", site="s", url="u", step=1)


def recorder():
    nb = Notebook()
    return nb, Recorder(nb, "QQ-2040-0001", lambda *a, **k: None)


def test_pick_option():
    opts = [["", "Bitte wählen"], ["none", "Ohne Zertifizierung"], ["ISO 1", "ISO 1"]]
    assert _pick_option(opts, "none") == "none"
    assert _pick_option(opts, "none (choose the option without certification)") == "none"
    assert _pick_option(opts, "keine") == "none"
    assert _pick_option(opts, "ISO 1") == "ISO 1"
    assert _pick_option([["", "–"], ["M-7", "M-7 · 99 bar"]], "M-7") == "M-7"
    assert _pick_option(opts, "XYZ") is None


def test_german_numbers():
    assert de_num(1234567.5) == "1.234.567,50"
    assert de_num(0) == "0,00"
    assert de_num(12.345) == "12,35"  # half-up via Decimal quantize of the float repr
    assert de_percent(7) == "7"
    assert de_percent(2.5) == "2,5"


def test_number_parsing():
    assert str(to_decimal("1.234,56")) == "1234.56"
    assert str(to_decimal("1,234.56")) == "1234.56"
    assert str(to_decimal("12,345", "en-US")) == "12345"
    assert str(to_decimal("12.345", "de-DE")) == "12345"
    assert str(to_decimal("3,50 EUR")) == "3.50"
    assert unit_kind("Satz") == "set" and unit_kind("Stk") == "piece"
    assert norm_cert("Keine gefordert") == "none" and norm_cert("ISO 9001") == "iso9001"
    assert parse_datetime("05.02.2040, 10:00", "Europe/Berlin") == datetime(2040, 2, 5, 9, 0, tzinfo=timezone.utc)


def test_condition_audit_infers_and_rejects():
    nb, r = recorder()
    ok = r.record("record_condition", {"material": "M-1", "valid_from": "01.01.2040", "valid_to": "31.12.2040",
                                       "evidence": "Währung: CHF · Devisenschlüssel KX-9 · Preiseinheit: 10 · Zahlenformat: de-DE",
                                       "scales": [{"from_qty": "1", "price": "1.000,5"}]}, CTX)
    assert ok.startswith("recorded condition CHF per 10")
    f = nb.facts[-1].data
    assert f["fx_key"] == "KX-9" and f["scales"][0]["price"] == "1000.5"
    bad = r.record("record_condition", {"material": "M-1", "currency": "EUR", "price_unit": 1, "valid_from": "01.01.2040",
                                        "valid_to": "31.12.2040", "evidence": "Währung: CHF",
                                        "scales": [{"from_qty": "1", "price": "5"}]}, CTX)
    assert bad.startswith("not recorded")
    missing_unit = r.record("record_condition", {"material": "M-2", "currency": "EUR", "valid_from": "01.01.2040",
                                                 "valid_to": "31.12.2040", "scales": [{"from_qty": "1", "price": "5"}]}, CTX)
    assert "price unit" in missing_unit


def test_reference_check_and_ids():
    nb, r = recorder()
    assert r.record("record_tender", {"reference": "QQ-2040-0001-B", "evidence": "x"}, CTX).startswith("rejected")
    assert r.record("record_tender", {"reference": "QQ-2040-0001", "evidence": "x"}, CTX) == "recorded tender"
    assert _short_id("R-12: bore 40 mm, stroke 100 mm") == ("R-12", "R-12: bore 40 mm, stroke 100 mm")
    assert _short_id("Anforderung B-3")[0] == "B-3"


def test_mail_fact_key_tolerance():
    nb, r = recorder()
    r.record("record_message", {"message_id": "m1", "subject": "rules", "sender": "calc@firm.test", "reference": "QQ-2040-0001",
                                "date_utc": "2040-01-01T00:00:00Z", "body": "...",
                                "facts": [{"kind": "condition_rule", "key": "G-1", "scope": "none"}]}, CTX)
    rule = [f for f in nb.facts if f.kind == "mail_fact"][0]
    assert rule.data["group"] == "G-1"


def test_brief_regex_fallbacks():
    brief = ("Prepare a bid for City X: Supply of pumps (QQ-2040-0001). The scenario date is 2040-02-01T08:00:00Z; "
             "the tender deadline is 2040-03-01T10:00:00Z. Register the bidder using the company name Pump Works GmbH "
             "and USt-IdNr. DE123456789. Choose ...")
    assert _regex_reference(brief) == "QQ-2040-0001"
    assert _regex_date(brief, "scenario date") == "2040-02-01T08:00:00Z"
    assert _regex_company(brief) == ("Pump Works GmbH", "DE123456789")
