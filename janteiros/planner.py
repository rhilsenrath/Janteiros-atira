"""Planner: the model reads the brief once; code validates every field against the brief text."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .engine import DEFAULT_SENDERS, Plan
from .normalize import norm_id, parse_datetime

ROLES = ("tender", "supplier", "erp", "mail")

PLANNER_SYSTEM = """You read a tender brief for a bid team and return JSON only.
Return an object with these keys:
- reference: the exact tender reference of the tender to bid on
- title: the tender title
- buyer: the contracting authority
- scenario_date: the scenario date as ISO 8601 UTC
- deadline: the tender deadline as ISO 8601 UTC (or null)
- bidder_company: the company name to register the bidder with
- bidder_vat: the VAT id (USt-IdNr.) to register with
- sites: object mapping each website id (as written in backticks in the brief) to one role:
  "tender" (tender portal with the tender, Q&A and the bid form), "supplier" (buyer's supplier/customer portal with
  the technical specification), "erp" (own ERP with materials, stock, prices), "mail" (own webmail with approvals and TAN)
- senders: object mapping record kinds to the authorized sender local part (the part before @) as the brief states:
  discount_approval, condition_rule, metal_index, setup_pool, reserve, packaging, delivery, fx, bonus
- submission_steps: short list of the steps the brief describes for entering and signing the bid
"""


@dataclass
class BriefPlan:
    plan: Plan
    title: str = ""
    buyer: str = ""
    deadline: str | None = None
    sites: dict[str, str] = field(default_factory=dict)  # siteId -> role
    submission_steps: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _regex_reference(brief: str) -> str | None:
    m = re.search(r"\(([A-Z]{1,6}-\d{2,4}-\d{2,6}[A-Z0-9-]*)\)", brief)
    if m:
        return m.group(1)
    m = re.search(r"\b([A-Z]{2,6}-\d{4}-\d{3,6})\b", brief)
    return m.group(1) if m else None


def _regex_date(brief: str, label: str) -> str | None:
    m = re.search(label + r"[^0-9]{0,40}(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z)", brief, re.I)
    return m.group(1) if m else None


def _regex_company(brief: str) -> tuple[str | None, str | None]:
    m = re.search(r"company name\s+(.+?)\s+and\s+USt-IdNr\.?\s*([A-Z]{2}\s?\d{6,12})", brief)
    if m:
        return m.group(1).strip().strip("*`"), m.group(2).replace(" ", "")
    return None, None


def _guess_role(site_id: str, url: str, brief: str) -> str | None:
    s = (site_id + " " + url).lower()
    if any(w in s for w in ("tender", "vergabe", "ausschreib", "procure")):
        return "tender"
    if any(w in s for w in ("customer", "supplier", "liefer", "kunden", "lastenheft")):
        return "supplier"
    if any(w in s for w in ("erp", "sap", "warenwirt", "materials")):
        return "erp"
    if any(w in s for w in ("mail", "post", "inbox")):
        return "mail"
    m = re.search(r"\(([^()`]*)`%s`\)" % re.escape(site_id), brief)
    if m:
        return _guess_role(m.group(1), "", "")
    return None


async def make_plan(llm, brief: str, access: list[dict], submission: dict, log) -> BriefPlan:
    data: dict = {}
    try:
        data = await llm.json_call(PLANNER_SYSTEM, brief[:24000], agent="planner", max_tokens=1500)
    except Exception as e:  # noqa: BLE001 - the regex fallback below still plans the run
        log("planner_error", error=repr(e)[:300])
    problems: list[str] = []
    norm_brief = norm_id(brief)

    ref = str(data.get("reference") or "").strip()
    if not ref or norm_id(ref) not in norm_brief:
        alt = _regex_reference(brief)
        problems.append(f"model reference {ref!r} not in brief; using {alt!r}")
        ref = alt or ref
    sd_raw = data.get("scenario_date")
    sd = parse_datetime(sd_raw) if sd_raw else None
    rx = _regex_date(brief, "scenario date")
    if rx and (sd is None or parse_datetime(rx) != sd):
        problems.append(f"scenario date from brief text: {rx} (model said {sd_raw})")
        sd = parse_datetime(rx)
    if sd is None:
        raise RuntimeError("no scenario date in the brief")
    company, vat = str(data.get("bidder_company") or "").strip(), str(data.get("bidder_vat") or "").replace(" ", "")
    rc, rv = _regex_company(brief)
    if not company or company not in brief:
        company = rc or company
    if not vat or vat not in brief.replace(" ", ""):
        vat = rv or vat

    senders = dict(DEFAULT_SENDERS)
    for k, v in (data.get("senders") or {}).items():
        v = str(v or "").strip().lower().split("@")[0]
        if k in senders and v and re.search(r"\b" + re.escape(v) + r"@", brief.lower()):
            senders[k] = v

    sites: dict[str, str] = {}
    model_sites = data.get("sites") or {}
    for a in access:
        sid = a["siteId"]
        role = model_sites.get(sid) if model_sites.get(sid) in ROLES else None
        guess = _guess_role(sid, a.get("url", ""), brief)
        if role is None or (guess and guess != role and sid == submission.get("siteId") and guess == "tender"):
            role = guess or role
        if submission.get("siteId") == sid:
            role = "tender"
        if role:
            sites[sid] = role
    for r in ROLES:
        if r not in sites.values():
            problems.append(f"no site for role {r}")

    plan = Plan(reference=ref, scenario_date=sd, bidder_company=company, bidder_vat=vat, senders=senders)
    bp = BriefPlan(plan=plan, title=str(data.get("title") or ""), buyer=str(data.get("buyer") or ""),
                   deadline=data.get("deadline"), sites=sites,
                   submission_steps=[str(s) for s in (data.get("submission_steps") or [])][:12], problems=problems)
    log("plan", reference=ref, scenario_date=str(sd), company=company, vat=vat, sites=sites, senders=senders, problems=problems)
    return bp
