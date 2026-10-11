"""The team: shared state (notebook, plan, site notes), the supervisor's model help and the missions.

Missions are built from needs, not from pages: each one says what to find, never where to click.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from .engine import EngineResult, compute
from .llm import LLM
from .normalize import loose_id, norm_cert, norm_mounting, to_decimal
from .notebook import Notebook
from .planner import BriefPlan
from .specialist import Mission
from .tools import Recorder

MANUALS = Path(__file__).parent / "manuals"


class EventLog:
    def __init__(self, path: Path | None):
        self.path = path
        self.t0 = time.monotonic()
        self.quiet = {"llm_call", "step"}

    def __call__(self, event: str, **fields: Any) -> None:
        t = round(time.monotonic() - self.t0, 2)
        rec = {"t": t, "event": event, **fields}
        if self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        if event == "step":
            line = f"[{t:7.1f}] {fields.get('agent')}#{fields.get('step')} {fields.get('tool')}{fields.get('args', '')[:120]} → {str(fields.get('result', ''))[:160]}"
        elif event == "llm_call":
            return
        else:
            body = json.dumps(fields, ensure_ascii=False, default=str)
            line = f"[{t:7.1f}] {event} {body[:400]}"
        print(line, file=sys.stdout, flush=True)


class Team:
    def __init__(self, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir = out_dir
        self.log = EventLog(out_dir / "events.jsonl")
        self.llm = LLM(self.log)
        self.notebook = Notebook(path=out_dir / "notebook.jsonl")
        self.common_manual = (MANUALS / "common.md").read_text(encoding="utf-8")
        self.manuals = {k: (MANUALS / f"{k}.md").read_text(encoding="utf-8") for k in ("tender", "supplier", "erp", "mail", "dispatcher")}
        self.bp: BriefPlan | None = None
        self.recorder: Recorder | None = None
        self._notes: dict[str, list[str]] = {}
        self._hint_calls = 0
        self.brief = ""

    # ---------- plan ----------
    def set_plan(self, bp: BriefPlan) -> None:
        self.bp = bp
        self.recorder = Recorder(self.notebook, bp.plan.reference, self.log)

    @property
    def plan(self):
        return self.bp.plan

    def compute(self) -> EngineResult:
        return compute(self.notebook.ok(), self.plan)

    # ---------- shared memory ----------
    def add_site_note(self, site: str, note: str, agent: str) -> None:
        note = note.strip()[:300]
        if note:
            self._notes.setdefault(site, []).append(f"({agent}) {note}")
            self.log("site_note", site=site, agent=agent, note=note)

    def site_notes(self, site: str) -> str:
        return "\n".join("- " + n for n in self._notes.get(site, [])[-8:])

    def my_facts(self, agent: str) -> str:
        facts = [f for f in self.notebook.facts if f.agent == agent and f.kind not in ("qa", "mail_message")]
        if not facts:
            return ""
        lines = []
        for f in facts[-40:]:
            mark = "" if f.status in ("audited", "confirmed") else f" [{f.status}: {f.reason}]"
            lines.append(f.brief()[:220] + mark)
        extra = [f for f in self.notebook.facts if f.agent == agent and f.kind in ("qa", "mail_message")]
        if extra:
            ids = ", ".join(str(f.data.get("number") or f.data.get("message_id")) for f in extra)
            lines.insert(0, f"Q&A answers / messages recorded: {ids}")
        return "\n".join(lines)

    def base_context(self) -> str:
        bp = self.bp
        p = bp.plan
        s = (f"Tender reference: {p.reference} (exact; look-alike references are other tenders)\n"
             f"Tender: {bp.title} · buyer: {bp.buyer}\n"
             f"Scenario date: {p.scenario_date.strftime('%Y-%m-%dT%H:%M:%SZ')} (records dated later are not valid)\n"
             f"Bidder: {p.bidder_company} (VAT {p.bidder_vat})")
        dom = self.bidder_domain()
        if dom:
            s += f"\nBidder mailbox domain: {dom}"
        return s

    def bidder_domain(self) -> str:
        for f in self.notebook.ok("mail_identity"):
            return f.data.get("domain") or ""
        return ""

    def bidder_address(self) -> str:
        for f in self.notebook.ok("mail_identity"):
            return f.data.get("address") or ""
        return ""

    def notebook_time(self) -> float:
        return round(time.monotonic() - self.notebook.t0, 2)

    # ---------- supervisor help (model) ----------
    async def supervisor_hint(self, agent: str, mission: Mission, history: list[str], page: str) -> str:
        if self._hint_calls > 40:
            return ""
        self._hint_calls += 1
        sys_p = ("You supervise a web agent that seems stuck. Read its mission, its last steps and the current page, "
                 "and give ONE short, concrete hint (one or two sentences) about what to do next. Name element numbers "
                 "from the page when useful. Do not repeat what failed.")
        user = f"MISSION:\n{mission.goal}\n\nCHECKLIST:\n" + "\n".join(mission.checklist) + "\n\nLAST STEPS:\n" + "\n".join(history) + "\n\nPAGE:\n" + page
        try:
            msg = await self.llm.chat([{"role": "system", "content": sys_p}, {"role": "user", "content": user}], None,
                                      agent="supervisor", tool_choice=None, max_tokens=300)
            hint = (msg.content or "").strip()[:400]
        except RuntimeError:
            return ""
        self.log("supervisor_hint", to=agent, hint=hint)
        return hint

    async def supervisor_answer(self, agent: str, mission: Mission, question: str, page: str) -> str:
        sys_p = ("You supervise a team of web agents preparing a tender bid. Answer the agent's question briefly and concretely. "
                 "You know the brief's rules; you cannot see other websites. If the information is not on this website, say so.")
        user = f"BRIEF RULES (excerpt):\n{self.brief_excerpt()}\n\nTEAM CONTEXT:\n{self.base_context()}\n\nMISSION:\n{mission.goal}\n\nQUESTION:\n{question}\n\nPAGE:\n{page}"
        try:
            msg = await self.llm.chat([{"role": "system", "content": sys_p}, {"role": "user", "content": user}], None,
                                      agent="supervisor", tool_choice=None, max_tokens=400)
            ans = (msg.content or "").strip()[:600]
        except RuntimeError:
            ans = "no answer available; use your best judgement"
        self.log("supervisor_answer", to=agent, question=question[:200], answer=ans)
        return ans

    def brief_excerpt(self) -> str:
        return getattr(self, "brief", "")[:6000]


# ---------------- missions ----------------

def _has(team: Team, kind: str, **match) -> list:
    out = []
    for f in team.notebook.ok(kind):
        if all(loose_id(f.data.get(k)) == loose_id(v) for k, v in match.items()):
            out.append(f)
    return out


def tender_mission(team: Team) -> Mission:
    p = team.plan

    def check():
        missing = []
        if not _has(team, "tender"):
            missing.append("record_tender for the exact reference")
        if not team.notebook.ok("position"):
            missing.append("record_positions (bill of quantities)")
        if not team.notebook.ok("qa"):
            missing.append("SOFT: bidder Q&A answers (Bieterfragen) – record them, or call done again if there are none")
        return missing

    return Mission(
        id="tender", kind="tender",
        goal=(f"Find the tender {p.reference} on this portal. Record the tender, every position of its bill of quantities "
              f"(original quantity, unit, requirement reference) and every bidder Q&A answer on every page, with its effects."),
        checklist=["tender recorded with the exact reference", "every position recorded", "every Q&A answer on every page recorded"],
        context=team.base_context, max_steps=40, budget_s=480, done_check=check)


def supplier_mission(team: Team) -> Mission:
    p = team.plan

    def ctx():
        s = team.base_context()
        reqs = sorted({f.data.get("requirement") for f in team.notebook.ok("position") if f.data.get("requirement")})
        if reqs:
            s += "\nThe tender positions refer to these requirements: " + ", ".join(reqs)
        return s

    def check():
        missing = []
        cur = [f for f in team.notebook.ok("spec_revision") if f.data.get("is_current")]
        if not cur:
            missing.append("the revision marked current (record_revisions)")
            return missing
        rev = cur[-1].data.get("revision")
        have = {loose_id(f.data.get("requirement")) for f in team.notebook.ok("requirement") if loose_id(f.data.get("revision")) == loose_id(rev)}
        if not have:
            missing.append(f"requirements of revision {rev}")
        need = {f.data.get("requirement") for f in team.notebook.ok("position") if f.data.get("requirement")}
        lack = [r for r in need if loose_id(r) not in have]
        if lack:
            missing.append(f"requirements {', '.join(sorted(lack))} of revision {rev}")
        return missing

    return Mission(
        id="supplier", kind="supplier",
        goal=(f"Find the buyer's technical specification for tender {p.reference} on this portal, identify the revision marked "
              f"current, and record every requirement of that revision with all its values (bore, stroke, pressure, mounting, "
              f"certification, catalogue group, usable-stroke flag and end-reserve key, pieces per set)."),
        checklist=["all revisions recorded with status", "current revision identified", "every requirement of the current revision recorded"],
        context=ctx, max_steps=40, budget_s=480, done_check=check)


MAIL_SUBJECTS = {
    "discount_approval": ("Rabattfreigabe / discount approval", "leitung", "sales director's discount approvals (also conditional ones with a minimum order value)"),
    "condition_rule": ("Konditionsregeln", "kalkulation", "which part of the price each Konditionsgruppe discounts"),
    "reserve": ("Technikfreigabe", "technik", "end reserves per key and revision"),
    "metal_index": ("Preisindizes", "kalkulation", "metal index rates with validity windows"),
    "setup_pool": ("Rüstpools", "kalkulation", "shared setup pool fees and the cent allocation rule"),
    "packaging": ("Verpackungstarife", "logistik", "packaging tariffs per carton"),
    "delivery": ("Terminfreigabe", "logistik", "delivery commitments: material, weeks, quantity ceiling"),
    "fx": ("Devisenfixing", "finanzen", "exchange rates per FX key"),
    "bonus": ("Projektbonus", "finanzen", "project bonus bands"),
}

MAIL_GROUPS = [
    ("mail-approvals", ["discount_approval", "condition_rule", "metal_index"]),
    ("mail-technical", ["reserve", "setup_pool", "packaging"]),
    ("mail-finance", ["fx", "delivery", "bonus"]),
]


def mail_mission(team: Team, mid: str, kinds: list[str], first: bool) -> Mission:
    p = team.plan
    subjects = "\n".join(f"- {MAIL_SUBJECTS[k][0]} from {team.plan.senders.get(k, MAIL_SUBJECTS[k][1])}@<bidder domain>: {MAIL_SUBJECTS[k][2]}" for k in kinds)

    def check():
        missing = []
        if first and not team.notebook.ok("mail_identity"):
            missing.append("record_identity (the address you are logged in as)")
        for k in kinds:
            if not [f for f in team.notebook.ok("mail_fact") if f.data.get("kind") == k]:
                missing.append(f"SOFT: {MAIL_SUBJECTS[k][0]}: search inbox and archive and record it (call done again if there is none)")
        return missing

    return Mission(
        id=mid, kind="mail",
        goal=(f"In this mailbox, find and record the messages for tender {p.reference} about:\n{subjects}\n"
              + ("Also record the mailbox address you are logged in as.\n" if first else "")
              + "Search the inbox and the archive with the reference plus the subject word. Record every matching message "
                "for the exact reference (also older ones) with all its statements; skip look-alike references."),
        checklist=([ "mailbox identity recorded"] if first else []) + [f"{MAIL_SUBJECTS[k][0]} searched in inbox and archive and recorded" for k in kinds],
        context=team.base_context, max_steps=45, budget_s=540, done_check=check)


def plausible(req: dict, mat: dict, final_stroke=None) -> bool:
    """Could this ERP material still match the requirement? Stroke counts only once it is final."""
    for a in ("bore_mm", "pressure_bar"):
        x, y = to_decimal(req.get(a)), to_decimal(mat.get(a))
        if x is not None and y is not None and x != y:
            return False
    if req.get("mounting") and mat.get("mounting") and norm_mounting(req["mounting"]) != norm_mounting(mat["mounting"]):
        return False
    if req.get("certification") and mat.get("certification") and norm_cert(req["certification"]) != norm_cert(mat["certification"]):
        return False
    if final_stroke is not None and to_decimal(mat.get("stroke_mm")) is not None and to_decimal(mat.get("stroke_mm")) != to_decimal(final_stroke):
        return False
    return True


def final_strokes(team: "Team") -> dict[str, object]:
    """Final stroke per requirement, when the tender Q&A has been read (amendments known) and reserves are resolved."""
    if not team.notebook.ok("qa") or not team.notebook.ok("position"):
        return {}
    try:
        res = team.compute()
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for c in res.positions.values():
        if c.attrs.get("stroke_mm") is not None:
            out[loose_id(c.requirement)] = c.attrs["stroke_mm"]
    return out


def erp_mission(team: Team, group: str, reqs: list) -> Mission:
    def describe(r) -> str:
        d = r.data
        s = (f"requirement {d.get('requirement')}: bore {d.get('bore_mm')} mm, stroke {d.get('stroke_mm')} mm, pressure "
             f"{d.get('pressure_bar')} bar, mounting {d.get('mounting')}, certification {d.get('certification')}")
        if d.get("stroke_is_usable"):
            s += " (the stroke is a usable travel: the ERP nominal stroke is longer by two end reserves)"
        return s

    def ctx():
        s = team.base_context()
        try:
            res = team.compute()
            finals = [f"{c.requirement}: final stroke {c.attrs.get('stroke_mm')} mm" for c in res.positions.values()
                      if loose_id(c.requirement) in {loose_id(r.data.get('requirement')) for r in reqs} and c.attrs.get("stroke_mm") is not None]
            if finals:
                s += "\nFinal requirement values after amendments and reserves: " + "; ".join(sorted(set(finals)))
        except Exception:  # noqa: BLE001 - context is best effort
            pass
        return s

    def check():
        missing = []
        g = loose_id(group)
        searches = [f for f in team.notebook.ok("erp_search") if loose_id(f.data.get("catalog_group")) == g or g in loose_id(f.data.get("query"))]
        if not searches:
            return [f"record_search for {group}"]
        listed = {x for f in searches for x in (f.data.get("results") or [])}
        mats: dict[str, dict] = {}
        for f in team.notebook.ok("material"):
            mats.setdefault(loose_id(f.data.get("material")), {}).update({k: v for k, v in f.data.items() if v not in (None, "")})
        strokes = final_strokes(team)
        for m in sorted(listed):
            md = mats.get(loose_id(m), {})
            if md.get("is_active") is None:
                missing.append(f"{m}: status_code + is_active (material status / planning tab) — record_materials")
            lack = [a for a in ("bore_mm", "stroke_mm", "pressure_bar", "mounting", "certification") if md.get(a) in (None, "")]
            if lack:
                missing.append(f"{m}: {', '.join(lack)} (technical data) — record_materials")
            if md.get("is_active") and any(plausible(r.data, md, strokes.get(loose_id(r.data.get("requirement")))) for r in reqs):
                if not _has(team, "stock", material=m):
                    missing.append(f"{m}: available and blocked stock (stock overview, storage locations) — record_stock")
                if not _has(team, "price_condition", material=m):
                    missing.append(f"{m}: every price condition record with its scales — record_condition")
                lt = [a for a in ("lead_time_stock_weeks", "lead_time_production_weeks") if md.get(a) in (None, "")]
                if lt:
                    missing.append(f"{m}: {', '.join(lt)} (planning tab) — record_materials")
                more = [a for a in ("pieces_per_carton", "allocation_mass_g", "surcharge_mass_g", "price_index") if md.get(a) in (None, "")]
                if more:
                    missing.append(f"SOFT: {m}: {', '.join(more)} if the ERP shows them — record_materials")
        return missing

    return Mission(
        id=f"erp-{group}", kind="erp",
        goal=("Find the ERP material for " + "; ".join(describe(r) for r in reqs) + f".\nSearch the ERP for catalogue group {group}, "
              "record the search and every candidate (technical data, status, lead times, pack size, allocation and surcharge "
              "mass, price index), and for every active candidate that could match: available stock and ALL price condition records with their scales."),
        checklist=[f"search for {group} recorded", "every candidate's technical data and status recorded",
                   "matching active candidates: lead times, pack size, masses, price index, stock, all price conditions"],
        context=ctx, max_steps=45, budget_s=480, done_check=check)


def tan_mission(team: Team, request_no: str | None, requested_at: float) -> Mission:
    p = team.plan

    def check():
        tans = [f for f in team.notebook.ok("mail_fact") if f.data.get("kind") == "tan" and f.at >= requested_at - 1]
        if request_no:
            tans = [f for f in tans if loose_id(f.data.get("request_no")) == loose_id(request_no) or not f.data.get("request_no")]
        return [] if tans else ["the new TAN message (record_message with kind=tan)"]

    rq = f" (request number {request_no})" if request_no else ""
    return Mission(
        id="tan", kind="mail",
        goal=(f"The dispatcher has just requested a signing TAN for tender {p.reference}{rq}. Find the NEWEST TAN message for exactly "
              f"this reference{' and request number' if request_no else ''} in the inbox (new TANs appear without searching; reload the "
              "inbox or open the newest message), open it and record it with record_message (one fact kind=tan with the TAN and the request number)."),
        checklist=["newest TAN for this reference and request recorded"], context=team.base_context,
        max_steps=14, budget_s=150, done_check=check, stop_when_complete=True)
