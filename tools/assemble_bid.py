#!/usr/bin/env python3
"""
assemble_bid.py - merge the agent results (tender, supplier, mail, erp) into
the RFQ answer format and compute the bid with the challenge's rules.

    python assemble_bid.py --tender tender.json --supplier supplier.json \
        --mail mail.json [--erp erp.json] [--out out/]

Writes
    out/bid_worksheet.json   resolved inputs per position, provenance,
                             cross-check flags and what is still pending
    out/bid_answer.json      the answer in the challenge schema; a draft with
                             nulls until every input is known

Nothing from the websites is hard-coded here: every value is read from the
agent results at run time (the challenge rejects copied values). This is a
helper that merges, calculates and validates; the agents decide what to read.

Expected ERP agent output (one entry per candidate material it found for a
Kataloggruppe; the script picks the active exact match itself):

    {"agent_id": "erp", "output": {"materials": [{
        "material_number": str, "catalogue_group": str, "active": bool,
        "bore": str, "stroke": str, "pressure": str,
        "mounting": str, "certification": str,
        "stock": int,                          # pieces
        "lead_time_stock_weeks": int, "lead_time_production_weeks": int,
        "pieces_per_carton": int,              # Packinhalt
        "allocation_mass_g": number,           # Umlagegewicht g/Stueck (setup pool)
        "surcharge_mass_kg": number,           # mass for Materialteuerung
        "metal_index": str | null,             # e.g. the MET-... key the ERP names
        "price_conditions": [{
            "valid_from": "YYYY-MM-DD" | "DD.MM.YYYY",
            "valid_to":   "YYYY-MM-DD" | "DD.MM.YYYY" | null,
            "currency": "EUR" | "USD" | ..., "fx_key": str | null,
            "price_per": int,                  # price unit, e.g. 1 or 100 pieces
            "scales": [{"min_qty": int, "price": number}]}]}]}}

Expected mail approved_discount (output.approved_discount), or null:
    {"percent": number, "min_order_value": number | null,
     "tender_reference": str, "sender": str, "dated_at": iso}
"""
from __future__ import annotations

import argparse
import json
import math
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def load(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def num(value) -> Fraction | None:
    """'2,75' / '13.25' / '1.234,50' / 482 -> exact Fraction."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return Fraction(str(value))
    t = str(value).strip().replace(" ", "").replace(" ", "")
    t = re.sub(r"[^\d,.\-]", "", t)
    if "," in t and "." in t:          # German thousands + decimal comma
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    return Fraction(t)


def half_up(x: Fraction, places: int = 2) -> Decimal:
    scale = Fraction(10) ** places
    v = x * scale
    n = math.floor(abs(v) + Fraction(1, 2))
    return Decimal(n if v >= 0 else -n).scaleb(-places)


def dstr(x) -> str | None:
    """Fraction -> plain decimal string (exact when it terminates)."""
    if x is None:
        return None
    x = Fraction(x)
    s = format(Decimal(x.numerator) / Decimal(x.denominator), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def money(x) -> float | None:
    return None if x is None else float(half_up(x) if isinstance(x, Fraction) else x)


def parse_dt(s) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_day(s) -> date | None:
    if not s:
        return None
    s = str(s).strip()
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", s)
    if m:
        return date(int(m[3]), int(m[2]), int(m[1]))
    return date.fromisoformat(s[:10])


def norm(v) -> str:
    """Normalise an attribute for exact matching (numbers compare as numbers)."""
    if v is None:
        return ""
    s = str(v).strip().casefold()
    s = re.sub(r"\s+(mm|bar)$", "", s)
    if s in {"keine gefordert", "keine", "ohne", "-", "—", "n/a", "none"}:
        return "keine"
    try:
        return str(num(s)) if re.fullmatch(r"[\d.,]+", s) else s
    except Exception:
        return s


CODE_RE = r"[A-Z]{1,4}-\d{3,5}"

# local part of the sender that may issue each kind of key (rule 21)
EXPECTED_SENDER = {"ER": "technik", "VP": "logistik", "TZ": "logistik",
                   "FX": "finanzen", "RP": "kalkulation", "PB": "finanzen",
                   "K": "kalkulation", "MET": "kalkulation"}


class Ctx:
    def __init__(self, tender, supplier, mail, erp):
        self.t = tender["output"]
        self.s = supplier["output"]
        self.m = (mail or {}).get("output") or {}
        self.e = (erp or {}).get("output") if erp else None
        self.ref = self.t["reference"]
        self.tz = ZoneInfo(self.t.get("timezone") or "UTC")
        self.scenario = parse_dt(self.t["scenario_date"])
        self.scenario_day = self.scenario.astimezone(self.tz).date()
        self.flags: list[str] = []
        self.pending: list[str] = []
        self.rules = self._index_rules()

    # ---------------------------------------------------------------- mail
    def _index_rules(self) -> dict:
        idx: dict[str, list] = {}
        for r in self.m.get("rules", []):
            code = None
            key = r.get("key") or ""
            if re.fullmatch(CODE_RE, key):
                code = key
            else:
                m = re.match(rf"\s*({CODE_RE})\s*:", r.get("text") or "")
                code = m[1] if m else None
            if code:
                idx.setdefault(code, []).append(r)
        return idx

    def rule(self, code: str):
        """Latest authorised record for the exact reference, not after the scenario date."""
        if not code:
            return None
        ok = []
        for r in self.rules.get(code, []):
            if r.get("authorized") is False:
                continue
            if r.get("tender_reference") not in (None, self.ref):
                continue
            eff = parse_dt(r.get("effective_at"))
            if eff and eff > self.scenario:
                continue
            ok.append((eff or datetime.min.replace(tzinfo=timezone.utc), r))
        if not ok:
            return None
        r = max(ok, key=lambda x: x[0])[1]
        prefix = code.split("-")[0]
        want = EXPECTED_SENDER.get(prefix)
        sender = (r.get("authorized_sender") or "").split("@")[0]
        if want and sender and sender != want:
            self.flags.append(f"{code}: sender {sender}@ is not the expected {want}@")
        return r

    # ------------------------------------------------------------- tender
    def clarifications(self):
        return self.t.get("clarifications", [])

    def withdrawn_ids(self) -> set:
        out = set()
        for c in self.clarifications():
            out |= set(c.get("withdraws") or [])
        return out


# --------------------------------------------------------------------------
# resolution of tender + supplier + mail per position
# --------------------------------------------------------------------------

def resolve_quantity(ctx: Ctx, pos: dict) -> dict:
    pid = pos["position_id"]
    gone = ctx.withdrawn_ids()
    cands = []
    for c in ctx.clarifications():
        if c["identifier"] in gone or c.get("withdrawn"):
            continue
        dt = parse_dt(c.get("dated_at"))
        if dt and dt > ctx.scenario:
            continue
        for ch in c.get("changes", []):
            if ch["change_type"] == "quantity" and ch["position_id"] == pid and ch.get("applies", True):
                cands.append((dt, c["identifier"], ch))
    if cands:
        if any(d is None for d, _, _ in cands) and len(cands) > 1:
            ctx.flags.append(f"{pid}: quantity answer without date among several; check order")
        dt, ident, ch = max(cands, key=lambda x: (x[0] or datetime.min.replace(tzinfo=timezone.utc)))
        value, unit, basis = int(num(ch["value"])), ch.get("unit") or pos["unit"], f"answer:{ident}"
    else:
        value, unit, basis = int(num(pos["original_quantity"])), pos["original_unit"], "original"
    if str(value) != str(pos.get("quantity")):
        ctx.flags.append(f"{pid}: tender agent says quantity {pos.get('quantity')}, recomputed {value} ({basis})")
    withdrawn_here = [c["identifier"] for c in ctx.clarifications()
                      if c["identifier"] in gone and any(ch["position_id"] == pid for ch in c.get("changes", []))]
    return {"value": value, "unit": unit, "basis": basis, "ignored_withdrawn": withdrawn_here}


ATTR = {"hub/stellweg": "stroke", "hub": "stroke", "stellweg": "stroke",
        "bohrung": "bore", "betriebsdruck": "pressure", "druck": "pressure",
        "befestigung": "mounting", "zertifizierung": "certification"}


def scoped_amendments(ctx: Ctx, req_id: str, revision: str) -> dict:
    """Latest dated technical amendment per attribute for (revision, requirement)."""
    best: dict[str, tuple] = {}
    gone = ctx.withdrawn_ids()
    seen = set()
    for c in ctx.clarifications():
        if c["identifier"] in gone or c.get("withdrawn"):
            continue
        if c.get("specification_revision") not in (None, revision):
            continue
        dt = parse_dt(c.get("dated_at"))
        if dt and dt > ctx.scenario:
            continue
        for ch in c.get("changes", []):
            if ch["change_type"] != "technical_amendment":
                continue
            ref = (ch.get("specification_reference") or "").replace(" ", "")
            if not ref.endswith(req_id) or ch.get("revision") not in (None, revision):
                continue
            m = re.match(r"\s*([^=]+?)\s*=\s*(.+?)\s*$", ch.get("value") or "")
            if not m or (c["identifier"], m[1]) in seen:
                continue
            seen.add((c["identifier"], m[1]))
            attr = ATTR.get(m[1].strip().casefold())
            if not attr:
                ctx.flags.append(f"{req_id}: unknown amended attribute '{m[1]}' in {c['identifier']}")
                continue
            key = dt or datetime.min.replace(tzinfo=timezone.utc)
            if attr not in best or key > best[attr][0]:
                best[attr] = (key, re.sub(r"\s*(mm|bar)$", "", m[2]), c["identifier"])
    return {a: {"value": v, "answer": i, "dated_at": d.isoformat()} for a, (d, v, i) in best.items()}


def resolve_requirement(ctx: Ctx, req_id: str) -> dict | None:
    gov = ctx.s.get("governing_revision")
    current = [r for r in ctx.s.get("revisions", []) if r.get("current")]
    if current and current[0]["revision"] != gov:
        ctx.flags.append(f"governing_revision {gov} differs from revision marked current {current[0]['revision']}")
    reqs = [r for r in ctx.s.get("requirements", []) if r["requirement_id"] == req_id and r["revision"] == gov]
    if not reqs:
        ctx.pending.append(f"supplier: requirement {req_id} in revision {gov}")
        return None
    r = reqs[0]
    orig = r.get("original_values") or {}
    out = {k: orig.get(k, r.get(k)) for k in ("catalogue_group", "bore", "pressure", "mounting", "certification")}
    amd = scoped_amendments(ctx, req_id, gov)
    for a in ("bore", "pressure", "mounting", "certification"):
        if a in amd:
            out[a] = amd[a]["value"]
    pieces_per_set = int(num(r.get("pieces_per_set") or orig.get("pieces_per_set") or 1))
    derivation = {"revision": gov, "amendments": amd}

    stroke_kind = r.get("stroke_kind") or orig.get("stroke_kind")
    sheet_stroke = orig.get("stroke") or r.get("stroke")
    if stroke_kind and "nutzbar" in stroke_kind.casefold():
        usable = num(amd["stroke"]["value"]) if "stroke" in amd else num(sheet_stroke)
        if r.get("usable_travel") and num(r["usable_travel"]) != usable:
            ctx.flags.append(f"{req_id}: supplier usable_travel {r['usable_travel']} != recomputed {usable}")
        er_key = next((m for c in r.get("constraints", []) for m in re.findall(r"ER-\d+", c)), None)
        rule = ctx.rule(er_key)
        reserve = None
        if rule:
            if gov not in rule["text"]:
                ctx.flags.append(f"{er_key}: Technikfreigabe does not name revision {gov}")
            m = re.search(r"Endreserve\s+([\d.,]+)\s*mm", rule["text"])
            reserve = num(m[1]) if m else None
        if reserve is None:
            ctx.pending.append(f"mail: Technikfreigabe {er_key} for {req_id} (Endreserve je Ende)")
            out["stroke"] = None
        else:
            out["stroke"] = dstr(usable + 2 * reserve)
        derivation.update(stroke_kind=stroke_kind, sheet_usable=sheet_stroke, usable=dstr(usable),
                          reserve_key=er_key, reserve_per_end=None if reserve is None else dstr(reserve),
                          formula="nominal = usable + 2 x reserve")
    else:
        out["stroke"] = amd["stroke"]["value"] if "stroke" in amd else sheet_stroke
    return {"requirement_id": req_id, **out, "pieces_per_set": pieces_per_set,
            "units": r.get("units"), "derivation": derivation}


def clause_scope(rule: dict) -> tuple[str | None, str]:
    """Which parts of the price the order discount reduces (rule 12)."""
    if rule.get("discount_scope") in ("base", "base_and_surcharge", "none"):
        return rule["discount_scope"], "structured"
    t = rule["text"].lower()          # not casefold(): it turns "ß" into "ss"
    if re.search(r"\bweder\b.*\bnoch\b", t) or "festpreis" in t:
        return "none", "keyword"
    if "materialteuerung" in t and ("gemeinsam" in t or "rabattfähige basis" in t) and "ausschließlich" not in t:
        return "base_and_surcharge", "keyword"
    if "ausschließlich" in t and "grundpreis" in t:
        return "base", "keyword"
    return None, "unresolved"


def resolve_commercial(ctx: Ctx, pos: dict) -> dict:
    pid = pos["position_id"]
    out: dict = {}
    grp = pos.get("condition_group")
    if grp:
        r = ctx.rule(grp)
        if r:
            scope, how = clause_scope(r)
            out["condition"] = {"group": grp, "scope": scope, "scope_source": how, "text": r["text"]}
            if scope is None:
                ctx.pending.append(f"{pid}: interpret Konditionsgruppe {grp}")
        else:
            out["condition"] = {"group": grp, "scope": None}
            ctx.pending.append(f"mail: Konditionsregeln for {grp}")
    else:
        out["condition"] = {"group": None, "scope": "base", "scope_source": "default rule"}

    pk = pos.get("packaging_key")
    r = ctx.rule(pk)
    m = re.search(r"([\d.,]+)\s*EUR\s+je\s+angefangenem\s+Karton", r["text"]) if r else None
    out["packaging"] = {"key": pk, "eur_per_carton": dstr(num(m[1])) if m else None}
    if pk and not m:
        ctx.pending.append(f"mail: Verpackungstarif {pk}")

    dk = pos.get("delivery_key")
    r = ctx.rule(dk)
    if r:
        art = re.search(r"Artikel\s+(\S+?);", r["text"])
        wk = re.search(r"Zusage\s+(\d+)\s+Wochen", r["text"])
        cap = re.search(r"höchstens\s+(\d+)\s+Stück", r["text"])
        out["delivery"] = {"key": dk, "material": art and art[1], "weeks": wk and int(wk[1]),
                           "max_pooled_pieces": cap and int(cap[1]), "inclusive": "einschließlich" in r["text"]}
    else:
        out["delivery"] = {"key": dk, "material": None}
        if dk:
            ctx.pending.append(f"mail: Terminfreigabe {dk}")

    out["setup_fee"] = pos.get("setup_fee")
    if pos.get("setup_pool_member"):
        r = ctx.rule(pos["setup_pool"])
        m = re.search(r"einmalig\s+([\d.,]+)\s*EUR", r["text"]) if r else None
        out["setup_pool"] = {"key": pos["setup_pool"], "amount": dstr(num(m[1])) if m else None}
        if not m:
            ctx.pending.append(f"mail: Rüstpool {pos['setup_pool']}")
    else:
        out["setup_pool"] = None
    if pos.get("bonus_member"):
        r = ctx.rule(pos["bonus_plan"])
        bands = []
        if r:
            for lo, hi, pct in re.findall(r"([\d.,]+)\s+bis\s+([\d.,]+|unbegrenzt)\s+EUR:\s*([\d.,]+)\s*%", r["text"]):
                bands.append({"from": dstr(num(lo)), "to": None if hi == "unbegrenzt" else dstr(num(hi)),
                              "percent": dstr(num(pct))})
        out["bonus"] = {"key": pos["bonus_plan"], "bands": bands or None}
        if not bands:
            ctx.pending.append(f"mail: Projektbonus {pos['bonus_plan']}")
    else:
        out["bonus"] = None
    return out


def metal_indices(ctx: Ctx) -> dict:
    """Rate per index whose inclusive window contains the scenario day (rule 28)."""
    best: dict[str, tuple] = {}
    pat = re.compile(r"(MET-\d+)\s*=\s*([\d.,]+)\s*EUR/kg\s*\(gültig\s+(?:(\d{2}\.\d{2}\.\d{4})\s+bis\s+"
                     r"(\d{2}\.\d{2}\.\d{4})|ab\s+(\d{2}\.\d{2}\.\d{4}))\)")
    for r in ctx.m.get("rules", []):
        if r.get("authorized") is False or r.get("tender_reference") not in (None, ctx.ref):
            continue
        eff = parse_dt(r.get("effective_at"))
        if eff and eff > ctx.scenario:
            continue
        for code, rate, a, b, since in pat.findall(r.get("text") or ""):
            lo, hi = parse_day(a or since), parse_day(b) if b else None
            if lo <= ctx.scenario_day and (hi is None or ctx.scenario_day <= hi):
                key = eff or datetime.min.replace(tzinfo=timezone.utc)
                if code not in best or key > best[code][0]:
                    best[code] = (key, num(rate), f"{a or since}..{b or 'open'}")
    return {c: {"eur_per_kg": dstr(v), "window": w} for c, (_, v, w) in best.items()}


def fx_rate(ctx: Ctx, key: str | None, currency: str) -> Fraction | None:
    r = ctx.rule(key) if key else None
    if not r:
        return None
    m = re.search(rf"1\s*{currency}\s*=\s*([\d.,]+)\s*EUR", r["text"])
    return num(m[1]) if m else None


def approved_discount(ctx: Ctx) -> dict | None:
    a = ctx.m.get("approved_discount")
    if not a:
        return None
    if a.get("tender_reference") not in (None, ctx.ref):
        ctx.flags.append(f"approved discount is for {a.get('tender_reference')}, not {ctx.ref}; ignored")
        return None
    pct = a.get("percent", a.get("discount_percent"))
    thr = a.get("min_order_value", a.get("threshold"))
    return {"percent": num(pct), "min_order_value": num(thr), "raw": a}


# --------------------------------------------------------------------------
# ERP-dependent calculation
# --------------------------------------------------------------------------

def pick_material(ctx: Ctx, req: dict) -> tuple[dict | None, list]:
    cands = [m for m in ctx.e.get("materials", [])
             if norm(m.get("catalogue_group")) in ("", norm(req["catalogue_group"]))]
    keys = ("bore", "stroke", "pressure", "mounting", "certification")
    report, hits = [], []
    for m in cands:
        diff = [k for k in keys if norm(m.get(k)) != norm(req.get(k))]
        report.append({"material_number": m.get("material_number"), "active": m.get("active"), "mismatch": diff})
        if not diff and m.get("active"):
            hits.append(m)
    if len(hits) > 1:
        ctx.flags.append(f"{req['requirement_id']}: {len(hits)} active exact matches; took the first")
    return (hits[0] if hits else None), report


def price_condition(ctx: Ctx, mat: dict):
    valid = []
    for pc in mat.get("price_conditions", []):
        lo, hi = parse_day(pc.get("valid_from")), parse_day(pc.get("valid_to"))
        if (lo is None or lo <= ctx.scenario_day) and (hi is None or ctx.scenario_day <= hi):
            valid.append((lo or date.min, pc))
    if len(valid) > 1:
        ctx.flags.append(f"{mat['material_number']}: several conditions valid on {ctx.scenario_day}; took latest start")
    return max(valid, key=lambda x: x[0])[1] if valid else None


def allocate_cents(total_cents: int, weights: dict[str, Fraction]) -> dict[str, int]:
    """Floor, then remaining cents to the largest remainders; ties by ascending position id."""
    W = sum(weights.values())
    if total_cents == 0 or W == 0:
        return {k: 0 for k in weights}
    raw = {k: Fraction(total_cents) * w / W for k, w in weights.items()}
    out = {k: math.floor(v) for k, v in raw.items()}
    for k in sorted(weights, key=lambda k: (-(raw[k] - out[k]), k))[: total_cents - sum(out.values())]:
        out[k] += 1
    return out


def compute(ctx: Ctx, rows: list[dict]) -> dict:
    """Full bid once ERP data is in (rules 03-08, 12, 15-17, 22-28)."""
    metals = metal_indices(ctx)
    for row in rows:
        req = row["requirement"]
        mat, report = pick_material(ctx, req) if req else (None, [])
        row["erp"] = {"candidates": report, "material": mat and mat.get("material_number")}
        row["_mat"] = mat
    if any(r["_mat"] is None for r in rows):
        missing = [r["position"] for r in rows if r["_mat"] is None]
        return {"decision": "no_bid", "reason": f"no active exact material for {missing}"}

    for row in rows:
        mat, pieces = row["_mat"], row["pieces"]
        pc = price_condition(ctx, mat)
        if not pc:
            ctx.pending.append(f"erp: price condition valid on {ctx.scenario_day} for {mat['material_number']}")
            return {"decision": None}
        scale = max((s for s in pc["scales"] if int(num(s["min_qty"])) <= pieces),
                    key=lambda s: num(s["min_qty"]), default=None)
        per = num(pc.get("price_per") or 1)
        raw = num(scale["price"]) / per
        cur = (pc.get("currency") or "EUR").upper()
        if cur != "EUR":
            rate = fx_rate(ctx, pc.get("fx_key"), cur)
            if rate is None:
                ctx.pending.append(f"mail: Devisenfixing {pc.get('fx_key')} ({cur}->EUR) from finanzen@")
                return {"decision": None}
            raw *= rate
        base = Fraction(half_up(raw))            # EUR base, HALF_UP to cents (rule 22)
        idx = mat.get("metal_index") or (next(iter(metals)) if len(metals) == 1 else None)
        mass = num(mat.get("surcharge_mass_kg")) or Fraction(0)
        if mass and (idx not in metals):
            ctx.pending.append(f"mail: metal index {idx} valid on {ctx.scenario_day}")
            return {"decision": None}
        sur = mass * num(metals[idx]["eur_per_kg"]) if mass else Fraction(0)
        row["calc"] = {"scale_min_qty": scale["min_qty"], "currency": cur, "base": base,
                       "surcharge": sur, "metal_index": idx if mass else None}

    # order-level discount with optional threshold (rules 05, 16)
    disc = approved_discount(ctx)
    goods_total = sum(Fraction(r["pieces"]) * r["calc"]["base"] for r in rows)
    pct = Fraction(0)
    if disc is None:
        ctx.pending.append("mail: sales-director discount approval for this exact reference (else 0 %)")
    elif disc["min_order_value"] is None or Fraction(half_up(goods_total)) >= disc["min_order_value"]:
        pct = disc["percent"]
    d = pct / 100

    # shared setup pool and project bonus, allocated in cents
    pool_alloc: dict[str, Fraction] = {}
    for key in {r["commercial"]["setup_pool"]["key"] for r in rows if r["commercial"]["setup_pool"]}:
        members = [r for r in rows if r["commercial"]["setup_pool"] and r["commercial"]["setup_pool"]["key"] == key]
        amount = num(members[0]["commercial"]["setup_pool"]["amount"])
        w = {r["position"]: Fraction(r["pieces"]) * num(r["_mat"]["allocation_mass_g"]) for r in members}
        for p, c in allocate_cents(int(half_up(amount) * 100), w).items():
            pool_alloc[p] = Fraction(c, 100)
    bonus_alloc: dict[str, Fraction] = {}
    for key in {r["commercial"]["bonus"]["key"] for r in rows if r["commercial"]["bonus"]}:
        members = [r for r in rows if r["commercial"]["bonus"] and r["commercial"]["bonus"]["key"] == key]
        value = {r["position"]: Fraction(r["pieces"]) * r["calc"]["base"] for r in members}
        V = sum(value.values())
        credit = Fraction(0)
        for b in members[0]["commercial"]["bonus"]["bands"]:
            lo, hi = num(b["from"]), (num(b["to"]) if b["to"] else None)
            part = max(Fraction(0), (V if hi is None else min(V, hi)) - lo)
            credit += part * num(b["percent"]) / 100
        cents = int(half_up(credit) * 100)
        for p, c in allocate_cents(cents, {k: v * 100 for k, v in value.items()}).items():
            bonus_alloc[p] = Fraction(c, 100)

    # lead time with pooled demand per material (rules 06, 15, 24)
    pooled: dict[str, int] = {}
    for r in rows:
        pooled[r["_mat"]["material_number"]] = pooled.get(r["_mat"]["material_number"], 0) + r["pieces"]

    positions, total = [], Fraction(0)
    for r in rows:
        mat, n, c, com = r["_mat"], r["pieces"], r["calc"], r["commercial"]
        mn = mat["material_number"]
        dl = com["delivery"]
        if dl.get("material") == mn and dl.get("max_pooled_pieces") is not None and pooled[mn] <= dl["max_pooled_pieces"]:
            weeks, why = dl["weeks"], f"commitment {dl['key']} ({pooled[mn]} <= {dl['max_pooled_pieces']})"
        elif int(mat.get("stock") or 0) >= pooled[mn]:
            weeks, why = int(mat["lead_time_stock_weeks"]), f"stock {mat.get('stock')} covers {pooled[mn]}"
        else:
            weeks, why = int(mat["lead_time_production_weeks"]), f"stock {mat.get('stock')} < {pooled[mn]}"
        scope = com["condition"]["scope"]
        base, sur = c["base"], c["surcharge"]
        base_d = base * (1 - d) if scope in ("base", "base_and_surcharge") else base
        sur_d = sur * (1 - d) if scope == "base_and_surcharge" else sur
        cartons = math.ceil(Fraction(n, int(mat["pieces_per_carton"]))) if com["packaging"]["eur_per_carton"] else 0
        pack = cartons * (num(com["packaging"]["eur_per_carton"]) or 0)
        setup = num(com["setup_fee"]) or Fraction(0)
        pool = pool_alloc.get(r["position"], Fraction(0))
        bonus = bonus_alloc.get(r["position"], Fraction(0))
        unit = half_up(base_d + sur_d + (setup + pool + pack - bonus) / n)       # rule 27
        total += n * Fraction(unit)
        r["calc"].update(cartons=cartons, packaging_total=dstr(pack), setup_fee=dstr(setup), pool_alloc=dstr(pool),
                         bonus_alloc=dstr(bonus), discount_scope=scope, lead_time_reason=why,
                         base=dstr(base), surcharge=dstr(sur),
                         unit_price_unrounded=dstr(base_d + sur_d + (setup + pool + pack - bonus) / n))
        positions.append({
            "position": r["position"], "material_number": mn, "quantity": n,
            "unit_price": float(unit), "lead_time_weeks": weeks,
            "base_unit_price": money(base),
            "surcharge_per_unit": money(sur + (setup + pool) / n),   # rule 25: before discount
            "packaging_per_unit": money(pack / n),
            "rebate_total": money(bonus),
        })
    return {"decision": "bid", "discount_percent": float(pct), "positions": positions,
            "total_net": float(half_up(total)), "goods_total_undiscounted": str(half_up(goods_total))}


# --------------------------------------------------------------------------
# schema check (no third-party deps)
# --------------------------------------------------------------------------

POS_KEYS = {"position", "material_number", "quantity", "unit_price", "lead_time_weeks",
            "base_unit_price", "surcharge_per_unit", "packaging_per_unit", "rebate_total"}
POS_REQ = {"position", "material_number", "quantity", "unit_price", "lead_time_weeks"}


def validate(ans: dict) -> list[str]:
    errs = []
    for k in ("tender_reference", "decision", "currency", "discount_percent", "positions", "total_net"):
        if ans.get(k) is None:
            errs.append(f"missing {k}")
    if set(ans) - {"tender_reference", "decision", "currency", "discount_percent", "positions", "total_net", "notes"}:
        errs.append("unexpected top-level keys")
    if ans.get("decision") not in ("bid", "no_bid"):
        errs.append("decision must be bid/no_bid")
    if ans.get("currency") != "EUR":
        errs.append("currency must be EUR")
    if len(ans.get("notes") or "") > 4000:
        errs.append("notes > 4000 chars")
    for p in ans.get("positions") or []:
        if set(p) - POS_KEYS:
            errs.append(f"{p.get('position')}: unexpected keys {set(p) - POS_KEYS}")
        for k in POS_REQ:
            if p.get(k) is None:
                errs.append(f"{p.get('position')}: missing {k}")
        for k in ("base_unit_price", "surcharge_per_unit", "packaging_per_unit", "rebate_total"):
            if k in p and p[k] is None:
                errs.append(f"{p.get('position')}: {k} is null (drop it or fill it)")
    return errs


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--tender", required=True)
    ap.add_argument("--supplier", required=True)
    ap.add_argument("--mail", required=True)
    ap.add_argument("--erp")
    ap.add_argument("--out", default="out")
    a = ap.parse_args()

    ctx = Ctx(load(a.tender), load(a.supplier), load(a.mail), load(a.erp))
    rows = []
    for pos in ctx.t["positions"]:
        q = resolve_quantity(ctx, pos)
        req = resolve_requirement(ctx, pos["requirement_id"])
        unit = (q["unit"] or "").casefold()
        per_set = req["pieces_per_set"] if req and unit.startswith(("satz", "sätze", "set")) else 1
        pieces = q["value"] * per_set
        conv = next((c for c in ctx.s.get("conversions", []) if c["position_id"] == pos["position_id"]), None)
        if conv and int(num(conv["pieces"])) != pieces:
            ctx.flags.append(f"{pos['position_id']}: supplier conversion says {conv['pieces']} pieces, recomputed {pieces}")
        rows.append({"position": pos["position_id"], "description": pos.get("description"),
                     "quantity": q, "pieces": pieces, "requirement": req,
                     "commercial": resolve_commercial(ctx, pos)})

    # unresolved timestamps the tender agent reported (only matters if they compete)
    for c in ctx.clarifications():
        if not c.get("dated_at"):
            ctx.flags.append(f"Q&A {c['identifier']} has no date (only matters if another answer competes with it)")

    metals = metal_indices(ctx)
    disc = approved_discount(ctx)
    result = compute(ctx, rows) if ctx.e else {"decision": None}
    if not ctx.e:
        ctx.pending.append("erp: materials per Kataloggruppe (see erp_requests)")
        if disc is None:
            ctx.pending.append("mail: sales-director discount approval for this exact reference (else 0 %)")

    erp_requests = []
    for req_id in dict.fromkeys(r["requirement"]["requirement_id"] for r in rows if r["requirement"]):
        rr = [r for r in rows if r["requirement"] and r["requirement"]["requirement_id"] == req_id]
        q = rr[0]["requirement"]
        erp_requests.append({
            "catalogue_group": q["catalogue_group"],
            "match_exactly": {k: q[k] for k in ("bore", "stroke", "pressure", "mounting", "certification")},
            "positions": {r["position"]: r["pieces"] for r in rr},
            "pooled_pieces": sum(r["pieces"] for r in rr),
            "need": ["material_number", "active", "stock", "lead_time_stock_weeks", "lead_time_production_weeks",
                     "price_conditions (validity, currency, fx_key, price_per, scales)", "pieces_per_carton",
                     "surcharge_mass_kg + metal_index"]
                    + (["allocation_mass_g"] if any(r["commercial"]["setup_pool"] for r in rr) else []),
        })

    if result.get("decision") in ("bid", "no_bid"):
        answer = {"tender_reference": ctx.ref, "decision": result["decision"], "currency": "EUR",
                  "discount_percent": result.get("discount_percent", 0) if result["decision"] == "bid" else 0,
                  "positions": result.get("positions", []) if result["decision"] == "bid" else [],
                  "total_net": result.get("total_net", 0) if result["decision"] == "bid" else 0}
    else:
        answer = {"tender_reference": ctx.ref, "decision": "bid", "currency": "EUR",
                  "discount_percent": None,
                  "positions": [{"position": r["position"], "material_number": None, "quantity": r["pieces"],
                                 "unit_price": None, "lead_time_weeks": None, "base_unit_price": None,
                                 "surcharge_per_unit": None, "packaging_per_unit": None,
                                 "rebate_total": None if r["commercial"]["bonus"] else 0.0} for r in rows],
                  "total_net": None}
    errors = validate(answer)

    for r in rows:
        r.pop("_mat", None)
    worksheet = {
        "tender_reference": ctx.ref, "title": ctx.t.get("title"), "buyer": ctx.t.get("buyer"),
        "scenario_date": ctx.t["scenario_date"], "scenario_day_local": str(ctx.scenario_day),
        "timezone": str(ctx.tz), "deadline": next((d["at"] for d in ctx.t.get("dates", [])
                                                  if d.get("label") == "Angebotsfrist"), None),
        "governing_specification": ctx.s.get("governing_specification"),
        "governing_revision": ctx.s.get("governing_revision"),
        "order": {"approved_discount": disc and {k: dstr(v) if isinstance(v, Fraction) else v
                                                 for k, v in disc.items() if k != "raw"},
                  "metal_index_on_scenario_day": metals,
                  "calc": {k: v for k, v in result.items() if k not in ("positions",)}},
        "positions": rows,
        "erp_requests": erp_requests,
        "pending": list(dict.fromkeys(ctx.pending)),
        "flags": list(dict.fromkeys(ctx.flags)),
        "answer_ready": not errors,
        "answer_errors": errors,
    }

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "bid_worksheet.json").write_text(json.dumps(worksheet, ensure_ascii=False, indent=2, default=str))
    (out / "bid_answer.json").write_text(json.dumps(answer, ensure_ascii=False, indent=2))
    print(f"answer_ready={not errors}  pending={len(worksheet['pending'])}  flags={len(worksheet['flags'])}")
    for p in worksheet["pending"]:
        print("  pending:", p)
    for f in worksheet["flags"]:
        print("  flag:   ", f)


if __name__ == "__main__":
    main()
