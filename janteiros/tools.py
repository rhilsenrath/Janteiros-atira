"""Tool definitions for the specialists, and the code-side auditor that turns tool calls into facts.

Navigation tools act on the element the model picked. Fact tools are the only way into the notebook:
every fact carries its evidence and passes typed checks before the engine may use it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .normalize import (
    iso,
    norm_id,
    parse_datetime,
    strip_invisible,
    to_decimal,
    unit_kind,
)


def fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []},
        },
    }


S = {"type": "string"}
N = {"type": "number"}
I = {"type": "integer"}
B = {"type": "boolean"}
EVID = {"type": "string", "description": "verbatim snippet from the page that shows these values"}

NAV_TOOLS = [
    fn("click", "Click the element with this id ([n] on the page): links, buttons, tabs, menu entries, summaries.",
       {"id": I, "why": {"type": "string", "description": "a few words: why this click"}}, ["id"]),
    fn("fill", "Type text into an input field (replaces its content).", {"id": I, "text": S}, ["id", "text"]),
    fn("search", "Type a search term into this search/input field and submit it (Enter, or the form's search button).",
       {"id": I, "text": S}, ["id", "text"]),
    fn("select", "Choose an option of a select box, by its value or visible text.", {"id": I, "option": S}, ["id", "option"]),
    fn("check", "Tick (or untick) a checkbox or pick a radio button.", {"id": I, "checked": B}, ["id"]),
    fn("set_slider", "Move a slider to a value.", {"id": I, "value": S}, ["id", "value"]),
    fn("press_key", "Press a key (e.g. Enter, Escape, F4), optionally on an element.", {"key": S, "id": I}, ["key"]),
    fn("hover", "Move the mouse over an element (opens hover menus and tooltips).", {"id": I}, ["id"]),
    fn("goto", "Open a URL or path on this same website (e.g. a link target you saw).", {"url": S}, ["url"]),
    fn("back", "Go back one page in the browser history (never inside a submission wizard).", {}),
    fn("read_more", "Show more of a long page, from this character offset.", {"offset": I}, ["offset"]),
    fn("read_all_pages", "Helper: click this 'next page' link or 'load more' button repeatedly and return the text of every page. Use it on a result list or table you need completely.",
       {"id": I, "max_pages": I}, ["id"]),
    fn("open_document", "Helper: download a document you chose (CSV, TXT, PDF, ZIP...) with this session and return its text (ZIP: every file).",
       {"url": S}, ["url"]),
    fn("read_pages", "Helper: read up to 8 pages of this website at once (links or addresses you saw, e.g. every tab of a record, "
       "every message of a result list) without leaving the current page. Their text comes back in your next turn.",
       {"urls": {"type": "array", "items": S}}, ["urls"]),
    fn("look", "Take a screenshot to see the page (for values only shown as images or when the text is confusing).", {}),
    fn("wait", "Wait a few seconds for the page to update (e.g. a pop-up that closes itself).", {"seconds": N}, ["seconds"]),
]

TALK_TOOLS = [
    fn("site_note", "Leave a short note for teammates who will use this website later in the run (how something works here).",
       {"note": S}, ["note"]),
    fn("ask_supervisor", "Ask the supervisor when you are stuck or unsure what the mission needs.", {"question": S}, ["question"]),
    fn("done", "Finish the mission. Only call it when every item of the checklist is recorded or you searched everywhere sensible.",
       {"summary": S, "not_found": {"type": "array", "items": S, "description": "items you could not find, each with where you searched"}},
       ["summary"]),
]

# ---------------- fact tools per specialist ----------------

TENDER_TOOLS = [
    fn("record_tender", "Record the tender you are working on, exactly as the portal writes its reference.",
       {"reference": S, "title": S, "buyer": S, "deadline": {"type": "string", "description": "deadline as shown, with its timezone"}, "evidence": EVID},
       ["reference", "evidence"]),
    fn("record_positions", "Record bill-of-quantities positions (original tender quantities).",
       {"positions": {"type": "array", "items": {"type": "object", "properties": {
           "position": {"type": "string", "description": "position number exactly as shown, e.g. 01.02"},
           "quantity": N, "unit": {"type": "string", "description": "unit as shown (Stk, Satz, ...)"},
           "requirement": {"type": "string", "description": "requirement / specification reference of the position"},
           "text": S}, "required": ["position", "quantity", "unit", "requirement"]}},
        "source": {"type": "string", "description": "which document or page"}, "evidence": EVID},
       ["positions", "evidence"]),
    fn("record_qa", "Record bidder Q&A answers (Bieterfragen) with what each one changes. Record every answer you read, also ones that change nothing.",
       {"entries": {"type": "array", "items": {"type": "object", "properties": {
           "number": {"type": "string", "description": "number of the question/answer"},
           "date_utc": {"type": "string", "description": "ISO UTC timestamp if shown (e.g. 2030-01-02T08:00:00Z)"},
           "date_text": {"type": "string", "description": "date and time exactly as shown"},
           "question": S, "answer": {"type": "string", "description": "the answer text, verbatim"},
           "effects": {"type": "array", "description": "what this answer states; one item per statement", "items": {"type": "object", "properties": {
               "kind": {"type": "string", "enum": ["quantity", "withdraw", "amendment", "condition_group", "setup_fee", "keys", "other"]},
               "position": {"type": "string", "description": "position number the statement is about"},
               "quantity": {"type": "number", "description": "kind=quantity: the new total quantity, in the position's unit"},
               "unit": S,
               "withdrawn_number": {"type": "string", "description": "kind=withdraw: number of the answer that is withdrawn"},
               "revision": {"type": "string", "description": "kind=amendment: specification revision named"},
               "requirement": {"type": "string", "description": "kind=amendment: requirement named"},
               "attribute": {"type": "string", "enum": ["bore_mm", "stroke_mm", "pressure_bar", "mounting", "certification"]},
               "value": {"type": "string", "description": "kind=amendment: new value"},
               "group": {"type": "string", "description": "kind=condition_group: the Konditionsgruppe key"},
               "amount_eur": {"type": "string", "description": "kind=setup_fee: the one-off setup fee (Rüstkosten) of the position, as written"},
               "packaging_key": {"type": "string", "description": "kind=keys: packaging tariff key, or 'none'"},
               "delivery_key": {"type": "string", "description": "kind=keys: delivery commitment key (Terminschlüssel), or 'none'"},
               "pool_key": {"type": "string", "description": "kind=keys: shared setup pool key (Rüstpool), or 'none'"},
               "bonus_key": {"type": "string", "description": "kind=keys: project bonus plan key, or 'none'"},
               "note": S}, "required": ["kind"]}}},
           "required": ["number", "answer", "effects"]}},
        "page_info": {"type": "string", "description": "e.g. 'page 2 of 6'"}},
       ["entries"]),
]

SUPPLIER_TOOLS = [
    fn("record_revisions", "Record the specification revisions listed (history), with their status.",
       {"document": S, "revisions": {"type": "array", "items": {"type": "object", "properties": {
           "revision": S, "date_text": S, "status_text": {"type": "string", "description": "status exactly as written"},
           "is_current": {"type": "boolean", "description": "true only for the revision marked current/valid (gültig/aktuell/maßgeblich)"},
           "note": S}, "required": ["revision", "status_text", "is_current"]}}, "evidence": EVID},
       ["revisions", "evidence"]),
    fn("record_requirements", "Record requirements of ONE specification revision, with their technical values.",
       {"revision": S, "requirements": {"type": "array", "items": {"type": "object", "properties": {
           "requirement": {"type": "string", "description": "requirement id only, e.g. R-7 (no other text)"},
           "bore_mm": N, "stroke_mm": N, "pressure_bar": N, "mounting": S,
           "certification": {"type": "string", "description": "certification required, or 'none' when none is required"},
           "catalog_group": {"type": "string", "description": "Kataloggruppe / catalogue group named for the ERP search"},
           "stroke_is_usable": {"type": "boolean", "description": "true if the stroke is a usable travel (nutzbarer Weg) that needs end reserves added"},
           "reserve_key": {"type": "string", "description": "end-reserve key named (Endreserve-Schlüssel), if any"},
           "pieces_per_set": {"type": "number", "description": "pieces per set (1 Satz = n Stück), if stated"},
           "original_text": S}, "required": ["requirement"]}}},
       ["revision", "requirements"]),
]

ERP_TOOLS = [
    fn("record_search", "Record an ERP search you ran and every material it listed.",
       {"query": S, "catalog_group": S, "results": {"type": "array", "items": S, "description": "material numbers listed"},
        "complete": {"type": "boolean", "description": "true if the list was complete (not truncated, all pages seen)"},
        "message": {"type": "string", "description": "status message of the search, verbatim"}},
       ["query", "results", "complete"]),
    fn("record_materials", "Record what a material page (or result line) shows. Use the values exactly; leave unknown fields out.",
       {"materials": {"type": "array", "items": {"type": "object", "properties": {
           "material": S, "catalog_group": S, "bore_mm": N, "stroke_mm": N, "pressure_bar": N, "mounting": S,
           "certification": {"type": "string", "description": "certification, or 'none' (Keine)"},
           "status_code": S, "status_text": S,
           "is_active": {"type": "boolean", "description": "true if released/active, false if blocked (gesperrt) or otherwise not usable"},
           "pieces_per_carton": N, "allocation_mass_g": {"type": "number", "description": "allocation weight for setup pools (Umlagegewicht), grams per piece"},
           "surcharge_mass_g": {"type": "number", "description": "mass for the metal surcharge (Zuschlagsmasse), grams per piece"},
           "price_index": S, "lead_time_stock_weeks": N, "lead_time_production_weeks": N,
           "setup_fee_eur": {"type": "number", "description": "only if the ERP states a setup fee for the material"}},
           "required": ["material"]}}, "evidence": EVID},
       ["materials", "evidence"]),
    fn("record_stock", "Record stock of a material: what is available (unrestricted) and what is blocked/in inspection, separately.",
       {"material": S, "available_qty": {"type": "number", "description": "freely usable / available stock in pieces"},
        "blocked_qty": {"type": "number", "description": "blocked / inspection / not available stock"}, "evidence": EVID},
       ["material", "available_qty", "evidence"]),
    fn("record_condition", "Record ONE price condition record of a material with all its quantity scales, numbers exactly as displayed.",
       {"material": S, "record": {"type": "string", "description": "record number or label (e.g. 1 of 2)"},
        "valid_from": {"type": "string", "description": "as shown"}, "valid_to": {"type": "string", "description": "as shown"},
        "currency": S, "fx_key": {"type": "string", "description": "FX / Devisenschlüssel key if named"},
        "price_unit": {"type": "number", "description": "pieces the price refers to (Preiseinheit), e.g. 1 or 100"},
        "number_format": {"type": "string", "enum": ["de-DE", "en-US", "unknown"], "description": "number format the page states for the prices"},
        "scales": {"type": "array", "items": {"type": "object", "properties": {
            "from_qty": {"type": "string", "description": "scale minimum quantity as shown"},
            "price": {"type": "string", "description": "price as shown, digits and separators unchanged"}},
            "required": ["from_qty", "price"]}},
        "evidence": EVID},
       ["material", "valid_from", "valid_to", "currency", "price_unit", "number_format", "scales"]),
]

MAIL_FACT_ITEM = {"type": "object", "properties": {
    "kind": {"type": "string", "enum": ["discount_approval", "condition_rule", "reserve", "metal_index", "fx", "setup_pool",
                                         "packaging", "delivery", "bonus", "tan", "none"],
             "description": "what the statement is"},
    "approves": {"type": "boolean", "description": "discount_approval: true only if the sender approves (genehmigt) a discount; false for requests, reviews, comments, refusals"},
    "percent": {"type": "number", "description": "discount_approval: approved percent (0 if withdrawn)"},
    "min_order_value_eur": {"type": "string", "description": "discount_approval: minimum undiscounted order value required, as written, if any"},
    "key": {"type": "string", "description": "the key/code this statement defines (e.g. a reserve, FX, pool, tariff, commitment or bonus key)"},
    "group": {"type": "string", "description": "condition_rule: the Konditionsgruppe"},
    "scope": {"type": "string", "enum": ["base_only", "base_and_surcharge", "none"],
              "description": "condition_rule: what the discount applies to: only the base price; base price and mass surcharge; or nothing"},
    "revision": {"type": "string", "description": "reserve: specification revision named"},
    "reserve_mm_per_end": {"type": "number"},
    "index": {"type": "string", "description": "metal_index: index code"},
    "rate_eur_per_kg": {"type": "string", "description": "metal_index: rate as written"},
    "valid_from": {"type": "string"}, "valid_to": {"type": "string"},
    "currency": {"type": "string", "description": "fx: foreign currency code"},
    "eur_per_unit": {"type": "string", "description": "fx: EUR for one unit of the currency, as written"},
    "fee_eur": {"type": "string", "description": "setup_pool: the one pool fee, as written"},
    "allocation": {"type": "string", "enum": ["largest_remainder", "remainder_to_first", "remainder_to_last", "remainder_to_largest", "round_each"],
                   "description": "setup_pool: cent allocation rule"},
    "tie_break": {"type": "string", "enum": ["ascending", "descending"], "description": "tie rule by position number"},
    "eur_per_carton": {"type": "string", "description": "packaging: tariff per carton, as written"},
    "material": {"type": "string", "description": "delivery: material the commitment is for"},
    "weeks": {"type": "number", "description": "delivery: committed lead time"},
    "max_total_qty": {"type": "number", "description": "delivery: inclusive ceiling of total demand"},
    "bands": {"type": "array", "description": "bonus: marginal bands", "items": {"type": "object", "properties": {
        "from_eur": {"type": "string"}, "to_eur": {"type": "string", "description": "empty if unlimited"}, "percent": {"type": "number"}}}},
    "tan": {"type": "string"}, "request_no": {"type": "string", "description": "tan: request number (Anforderung)"},
    "note": {"type": "string"}}, "required": ["kind"]}

MAIL_TOOLS = [
    fn("record_identity", "Record the mailbox address you are logged in as.", {"address": S, "evidence": EVID}, ["address", "evidence"]),
    fn("record_message", "Record one message you opened: envelope plus each business statement of its ORIGINAL body (never drafts or previews).",
       {"message_id": {"type": "string", "description": "id from the URL or list"}, "subject": S,
        "sender": {"type": "string", "description": "full sender address exactly as shown"},
        "date_utc": {"type": "string", "description": "ISO UTC timestamp if shown"}, "date_text": S,
        "folder": S, "reference": {"type": "string", "description": "tender reference the message is about, exactly as written"},
        "body": {"type": "string", "description": "the original body text, verbatim (shortened if long)"},
        "facts": {"type": "array", "items": MAIL_FACT_ITEM}},
       ["message_id", "subject", "sender", "reference", "body", "facts"]),
    fn("record_skipped", "Record messages you saw in a result list but did not open, with why (e.g. other reference, other sender).",
       {"items": {"type": "array", "items": {"type": "object", "properties": {"subject": S, "sender": S, "date_text": S, "reason": S}}}},
       ["items"]),
]


@dataclass
class Ctx:
    agent: str
    site: str
    url: str
    step: int
    tz: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _ev(args: dict) -> str:
    return strip_invisible(str(args.get("evidence") or ""))[:400]


def _clean(v):
    if isinstance(v, str):
        v = strip_invisible(v).strip()
        return v or None
    return v


ID_RE = re.compile(r"^\s*([A-Za-zÄÖÜ]{1,8}[-\u2010\u2011 ]?\d+(?:[.-]\d+)*[A-Za-z]?)\b")


def _short_id(raw: str | None) -> tuple[str, str | None]:
    """Keep only the identifier when a model pastes a whole sentence into an id field."""
    raw = _clean(raw) or ""
    m = ID_RE.match(raw)
    if m and len(raw) > len(m.group(1)) + 2:
        return m.group(1).replace(" ", "-"), raw
    if re.search(r"[\s:]", raw):
        m2 = re.search(r"\b([A-Z]{1,4}-\d+[A-Za-z]?)\b", raw)
        if m2:
            return m2.group(1), raw
    return raw, None


def _date(raw_utc, raw_text, tz) -> str | None:
    dt = parse_datetime(raw_utc) if raw_utc else None
    if dt is None and raw_text:
        txt = str(raw_text)
        m = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z", txt)
        dt = parse_datetime(m.group(0)) if m else parse_datetime(txt, tz)
    return iso(dt)


class Recorder:
    """Validates fact tool calls and writes them into the notebook (the auditor in code)."""

    def __init__(self, notebook, plan_ref: str, log):
        self.nb = notebook
        self.ref = plan_ref
        self.log = log
        self.seen: set[str] = set()

    def _dupe(self, key: str) -> bool:
        if key in self.seen:
            return True
        self.seen.add(key)
        return False

    def add(self, kind, data, ctx: Ctx, evidence: str = "", status: str = "audited", reason: str = ""):
        origin = {"site": ctx.site, "url": ctx.url, "evidence": evidence}
        return self.nb.add(kind, data, agent=ctx.agent, step=ctx.step, origin=origin, status=status, reason=reason)

    def record(self, name: str, args: dict, ctx: Ctx) -> str:
        try:
            return getattr(self, "_" + name)(args, ctx)
        except Exception as e:  # noqa: BLE001 - the model gets the problem back and can retry
            self.log("record_error", tool=name, error=repr(e)[:300])
            return f"not recorded: {e!r}"[:300]

    # ----- tender -----
    def _record_tender(self, a, ctx):
        ref = _clean(a.get("reference")) or ""
        data = {"reference": ref, "title": _clean(a.get("title")), "buyer": _clean(a.get("buyer")), "deadline": _clean(a.get("deadline"))}
        if norm_id(ref) != norm_id(self.ref):
            self.add("tender", data, ctx, _ev(a), status="rejected", reason="reference differs from the brief")
            return f"rejected: '{ref}' is not the brief's reference '{self.ref}'. Find the exact tender."
        self.add("tender", data, ctx, _ev(a))
        return "recorded tender"

    def _record_positions(self, a, ctx):
        ok, bad = 0, []
        for p in a.get("positions") or []:
            pos = _clean(p.get("position"))
            qty = to_decimal(p.get("quantity"))
            if not pos or qty is None:
                bad.append(str(p)[:80])
                continue
            req_id, _ = _short_id(p.get("requirement"))
            data = {"position": pos, "quantity": str(qty), "unit": _clean(p.get("unit")) or "",
                    "unit_kind": unit_kind(p.get("unit")), "requirement": req_id,
                    "text": _clean(p.get("text")), "source": _clean(a.get("source"))}
            self.add("position", data, ctx, _ev(a))
            ok += 1
        return f"recorded {ok} positions" + (f"; rejected {len(bad)} without number/quantity" if bad else "")

    def _record_qa(self, a, ctx):
        n_entries, n_eff, problems = 0, 0, []
        for e in a.get("entries") or []:
            num = _clean(str(e.get("number") or ""))
            date = _date(e.get("date_utc"), e.get("date_text"), ctx.tz)
            if not num:
                problems.append("entry without number")
                continue
            if not date:
                problems.append(f"{num}: date not readable (give date_utc)")
            if self._dupe("qa|" + norm_id(num) + "|" + norm_id((e.get("answer") or "")[:120])):
                continue
            self.add("qa", {"number": num, "date_utc": date, "date_text": _clean(e.get("date_text")),
                            "question": _clean(e.get("question")), "answer": _clean(e.get("answer"))}, ctx, (e.get("answer") or "")[:400])
            n_entries += 1
            for eff in e.get("effects") or []:
                kind = eff.get("kind")
                if kind in (None, "other"):
                    continue
                data = {"qa_number": num, "date_utc": date, "kind": kind}
                for k in ("position", "unit", "withdrawn_number", "revision", "requirement", "attribute", "value", "group",
                          "packaging_key", "delivery_key", "pool_key", "bonus_key", "note"):
                    if eff.get(k) not in (None, ""):
                        data[k] = _clean(str(eff.get(k)))
                if kind == "quantity":
                    q = to_decimal(eff.get("quantity"))
                    if q is None or not data.get("position"):
                        problems.append(f"{num}: quantity effect needs position and quantity")
                        continue
                    data["quantity"] = str(q)
                if kind == "setup_fee":
                    amt = to_decimal(eff.get("amount_eur"), "de" if "," in str(eff.get("amount_eur") or "") else None)
                    if amt is None or not data.get("position"):
                        problems.append(f"{num}: setup_fee needs position and amount")
                        continue
                    data["amount_eur"] = str(amt)
                if kind == "withdraw" and not data.get("withdrawn_number"):
                    problems.append(f"{num}: withdraw needs withdrawn_number")
                    continue
                if kind == "amendment" and not (data.get("attribute") and data.get("value") and data.get("requirement")):
                    problems.append(f"{num}: amendment needs requirement, attribute and value")
                    continue
                if kind in ("condition_group", "keys") and not data.get("position"):
                    problems.append(f"{num}: {kind} needs a position")
                    continue
                self.add("qa_effect", data, ctx, (e.get("answer") or "")[:400])
                n_eff += 1
        msg = f"recorded {n_entries} Q&A entries with {n_eff} effects"
        if not (a.get("entries") or []):
            msg = "nothing recorded: the call had no entries (if your reply was cut off, record fewer entries per call)"
        skipped = len(a.get("entries") or []) - n_entries - sum(1 for p in problems if p.endswith("entry without number"))
        if skipped > 0:
            msg += f"; {skipped} already recorded earlier (skipped)"
        return msg + ("; problems: " + "; ".join(problems[:6]) if problems else "")

    # ----- supplier -----
    def _record_revisions(self, a, ctx):
        n = 0
        for r in a.get("revisions") or []:
            rev = _clean(r.get("revision"))
            if not rev:
                continue
            self.add("spec_revision", {"document": _clean(a.get("document")), "revision": rev, "date": _clean(r.get("date_text")),
                                       "status_text": _clean(r.get("status_text")), "is_current": bool(r.get("is_current")),
                                       "note": _clean(r.get("note"))}, ctx, _ev(a))
            n += 1
        cur = [r.get("revision") for r in a.get("revisions") or [] if r.get("is_current")]
        return f"recorded {n} revisions; current: {', '.join(map(str, cur)) or 'none marked'}"

    def _record_requirements(self, a, ctx):
        rev = _clean(a.get("revision"))
        if not rev:
            return "not recorded: give the revision these requirements belong to"
        n, problems = 0, []
        for r in a.get("requirements") or []:
            rid, long_text = _short_id(r.get("requirement"))
            if not rid:
                continue
            data = {"revision": rev, "requirement": rid}
            if long_text and not r.get("original_text"):
                r["original_text"] = long_text
            for k in ("bore_mm", "stroke_mm", "pressure_bar", "pieces_per_set"):
                if r.get(k) not in (None, ""):
                    v = to_decimal(r.get(k))
                    if v is None:
                        problems.append(f"{rid}: {k} not a number")
                    else:
                        data[k] = str(v)
            for k in ("mounting", "certification", "catalog_group", "reserve_key", "original_text"):
                if r.get(k) not in (None, ""):
                    data[k] = _clean(str(r.get(k)))
            data["stroke_is_usable"] = bool(r.get("stroke_is_usable"))
            missing = [k for k in ("bore_mm", "stroke_mm", "pressure_bar", "mounting", "certification") if k not in data]
            if missing:
                problems.append(f"{rid}: missing {', '.join(missing)}")
            if self._dupe("req|" + json.dumps({k: v for k, v in data.items() if k != "original_text"}, sort_keys=True, ensure_ascii=False)):
                continue
            self.add("requirement", data, ctx, (r.get("original_text") or "")[:400])
            n += 1
        return f"recorded {n} requirements of {rev}" + ("; check: " + "; ".join(problems[:6]) if problems else "")

    # ----- ERP -----
    def _record_search(self, a, ctx):
        res = [norm_id(x) for x in (a.get("results") or []) if _clean(str(x))]
        self.add("erp_search", {"query": _clean(a.get("query")), "catalog_group": _clean(a.get("catalog_group")),
                                "results": res, "complete": bool(a.get("complete")), "message": _clean(a.get("message"))}, ctx, _ev(a))
        return f"recorded search with {len(res)} results"

    def _record_materials(self, a, ctx):
        n = 0
        for m in a.get("materials") or []:
            mat = _clean(m.get("material"))
            if not mat:
                continue
            data = {"material": norm_id(mat)}
            for k in ("bore_mm", "stroke_mm", "pressure_bar", "pieces_per_carton", "allocation_mass_g", "surcharge_mass_g",
                      "lead_time_stock_weeks", "lead_time_production_weeks", "setup_fee_eur"):
                if m.get(k) not in (None, ""):
                    v = to_decimal(m.get(k))
                    if v is not None:
                        data[k] = str(v)
            for k in ("catalog_group", "mounting", "certification", "status_code", "status_text", "price_index"):
                if m.get(k) not in (None, ""):
                    data[k] = _clean(str(m.get(k)))
            if m.get("is_active") is not None:
                data["is_active"] = bool(m.get("is_active"))
            self.add("material", data, ctx, _ev(a))
            n += 1
        return f"recorded {n} materials"

    def _record_stock(self, a, ctx):
        mat = _clean(a.get("material"))
        av = to_decimal(a.get("available_qty"))
        if not mat or av is None:
            return "not recorded: need material and available_qty"
        bl = to_decimal(a.get("blocked_qty"))
        self.add("stock", {"material": norm_id(mat), "available_qty": str(av), "blocked_qty": str(bl) if bl is not None else None}, ctx, _ev(a))
        return f"recorded stock of {mat}: {av} available"

    def _record_condition(self, a, ctx):
        mat = _clean(a.get("material"))
        ev = " ".join(str(a.get(k) or "") for k in ("evidence", "record", "currency", "fx_key")) + " " + json.dumps(a.get("scales") or [], ensure_ascii=False)
        cur = (_clean(a.get("currency")) or "").upper()[:3]
        m_cur = re.search(r"(?:Währung|Waehrung|Currency|W\.)\s*:?\s*([A-Z]{3})\b", ev) or re.search(r"\(([A-Z]{3})\s*/\s*Preiseinheit", ev)
        if not cur and m_cur:
            cur = m_cur.group(1)
        if m_cur and cur and m_cur.group(1) != cur and m_cur.group(1) in ("USD", "EUR", "CHF", "GBP", "JPY", "CNY", "PLN", "CZK", "SEK", "DKK", "NOK"):
            return f"not recorded: the evidence says currency {m_cur.group(1)} but you gave {cur}; record it again with the right currency"
        if not cur:
            return "not recorded: give the currency of the condition record (as shown on the page)"
        fx = _clean(a.get("fx_key"))
        if not fx:
            m_fx = re.search(r"(?:Devisenschl(?:ü|ue)ssel|FX[- ]?key|Kurs(?:schlüssel)?)\s*:?\s*([A-Z0-9][A-Z0-9-]{2,})", ev)
            fx = m_fx.group(1) if m_fx else None
        if cur != "EUR" and not fx:
            return "not recorded: a non-EUR condition needs its FX key (Devisenschlüssel / exchange-rate key) as shown"
        unit = to_decimal(a.get("price_unit"))
        if unit is None:
            m_u = re.search(r"Preiseinheit\s*:?\s*(\d+)", ev)
            unit = to_decimal(m_u.group(1)) if m_u else None
        if unit is None or unit <= 0:
            return "not recorded: give the price unit (how many pieces the price refers to, e.g. 1 or 100)"
        fmt = a.get("number_format") or "unknown"
        if fmt == "unknown":
            m_f = re.search(r"(de-DE|en-US)", ev)
            fmt = m_f.group(1) if m_f else None
        scales, problems = [], []
        for sc in a.get("scales") or []:
            q = to_decimal(sc.get("from_qty"), fmt)
            pr = to_decimal(sc.get("price"), fmt)
            if q is None or pr is None:
                problems.append(f"unreadable scale {sc}")
                continue
            scales.append({"from_qty": str(q), "price": str(pr), "raw": f"{sc.get('from_qty')} → {sc.get('price')}"})
        if not mat or not scales:
            return "not recorded: need the material and at least one scale" + ("; " + "; ".join(problems) if problems else "")
        vf, vt = _clean(a.get("valid_from")), _clean(a.get("valid_to"))
        if parse_datetime(vf) is None or parse_datetime(vt) is None:
            return "not recorded: give valid_from and valid_to exactly as shown (dates of the condition record)"
        data = {"material": norm_id(mat), "record": _clean(a.get("record")), "valid_from": vf, "valid_to": vt, "currency": cur,
                "fx_key": fx, "price_unit": str(unit), "number_format": fmt, "scales": scales}
        if self._dupe("cond|" + json.dumps({k: v for k, v in data.items() if k != "record"}, sort_keys=True, default=str)):
            return f"condition of {mat} already recorded (skipped)"
        self.add("price_condition", data, ctx, _ev(a))
        shown = ", ".join(f"from {sc['from_qty']}: {sc['price']}" for sc in scales)
        return f"recorded condition {cur} per {unit} pieces, valid {vf} – {vt} ({shown})" + ("; check: " + "; ".join(problems) if problems else "")

    # ----- mail -----
    def _record_identity(self, a, ctx):
        addr = (_clean(a.get("address")) or "").lower()
        if "@" not in addr:
            return "not recorded: not an e-mail address"
        self.add("mail_identity", {"address": addr, "domain": addr.split("@", 1)[1]}, ctx, _ev(a))
        return f"recorded identity {addr}"

    def _record_message(self, a, ctx):
        mid = _clean(str(a.get("message_id") or "")) or ""
        sender = (_clean(a.get("sender")) or "").lower()
        m = re.search(r"[\w.+-]+@[\w.-]+", sender)
        sender = m.group(0) if m else sender
        date = _date(a.get("date_utc"), a.get("date_text"), ctx.tz)
        ref = _clean(a.get("reference")) or ""
        base = {"message_id": mid, "subject": _clean(a.get("subject")), "sender": sender, "date_utc": date,
                "folder": _clean(a.get("folder")), "reference": ref}
        if mid and self._dupe("msg|" + norm_id(mid) + "|" + json.dumps(a.get("facts") or [], sort_keys=True, ensure_ascii=False)[:400]):
            return f"message {mid} was already recorded with the same statements (skipped)"
        self.add("mail_message", {**base, "body": (_clean(a.get("body")) or "")[:1500]}, ctx, (a.get("body") or "")[:400])
        n, problems = 0, []
        if not date:
            problems.append("date not readable (give date_utc)")
        for f in a.get("facts") or []:
            kind = f.get("kind")
            if not kind or kind == "none":
                continue
            data = dict(base)
            data["kind"] = kind
            for k, v in f.items():
                if k == "kind" or v in (None, ""):
                    continue
                data[k] = _clean(v) if isinstance(v, str) else v
            for k in ("rate_eur_per_kg", "eur_per_unit", "fee_eur", "eur_per_carton", "min_order_value_eur"):
                if k in data:
                    v = to_decimal(data[k])
                    if v is None:
                        problems.append(f"{kind}: {k} not a number")
                        data.pop(k)
                    else:
                        data[k] = str(v)
            if kind == "condition_rule" and not data.get("group") and data.get("key"):
                data["group"] = data["key"]
            if kind == "metal_index" and not data.get("index") and data.get("key"):
                data["index"] = data["key"]
            if kind not in ("condition_rule", "metal_index", "discount_approval", "tan") and not data.get("key"):
                data["key"] = data.get("group") or data.get("index")
            if kind == "condition_rule" and data.get("scope") not in ("base_only", "base_and_surcharge", "none"):
                problems.append(f"condition_rule {data.get('group')}: scope must be base_only, base_and_surcharge or none")
                continue
            if kind == "bonus":
                bands = []
                for b in data.get("bands") or []:
                    lo = to_decimal(b.get("from_eur"))
                    hi = to_decimal(b.get("to_eur")) if b.get("to_eur") not in (None, "", "∞") else None
                    pct = to_decimal(b.get("percent"))
                    if lo is None or pct is None:
                        problems.append(f"bonus band not readable: {b}")
                        continue
                    bands.append({"from_eur": str(lo), "to_eur": str(hi) if hi is not None else None, "percent": str(pct)})
                data["bands"] = bands
            self.add("mail_fact", data, ctx, (a.get("body") or "")[:400])
            n += 1
        return f"recorded message {mid} with {n} statements" + ("; problems: " + "; ".join(problems) if problems else "")

    def _record_skipped(self, a, ctx):
        items = a.get("items") or []
        for it in items:
            self.add("mail_skipped", {k: _clean(str(v)) for k, v in it.items()}, ctx)
        return f"noted {len(items)} skipped messages"


FACT_TOOLS = {"tender": TENDER_TOOLS, "supplier": SUPPLIER_TOOLS, "erp": ERP_TOOLS, "mail": MAIL_TOOLS}
FACT_NAMES = {t["function"]["name"] for ts in FACT_TOOLS.values() for t in ts}


def args_of(call) -> dict:
    try:
        return json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        txt = call.function.arguments or ""
        try:
            return json.loads(txt[: txt.rfind("}") + 1])
        except json.JSONDecodeError:
            return {"_unparsable": txt[:200]}
