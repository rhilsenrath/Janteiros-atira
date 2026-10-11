"""O Despachante: enters the engine's answer in the portal's bid wizard and signs it with the TAN.

It is the only piece allowed to do the irreversible step, and only after the supervisor's go. The model reads
each wizard step and decides which field gets which value; helpers format numbers, request the TAN through the
mail specialist, arm the browser confirmation and read the receipt.
"""

from __future__ import annotations

import asyncio
import re
from decimal import ROUND_HALF_UP, Decimal

from .normalize import norm_id, to_decimal
from .specialist import LoopState, Mission, Specialist
from .tools import NAV_TOOLS, fn

DISPATCH_TOOLS = [t for t in NAV_TOOLS if t["function"]["name"] in
                  ("click", "fill", "select", "check", "set_slider", "press_key", "hover", "goto", "read_more", "look", "wait")] + [
    fn("fill_cascade", "Helper for dependent selects (e.g. bore → stroke → mounting → certification → catalogue number): give the "
       "fields in order (select name or label) with the value for each, and the id of the button that applies a selection; "
       "the helper selects each value and presses apply until all are set.",
       {"steps": {"type": "array", "items": {"type": "object", "properties": {"field": {"type": "string"}, "value": {"type": "string"},
                                                                               "apply": {"type": "boolean", "description": "press apply after this one (default true)"}},
                                             "required": ["field", "value"]}},
        "apply_id": {"type": "integer", "description": "id of the button that applies/confirms a selection"},
        "fields": {"type": "array", "description": "after the cascade: the other fields of this step by name (or label) with their values "
                   "(quantity, prices in German format, lead time, optional calculation fields)",
                   "items": {"type": "object", "properties": {"field": {"type": "string"}, "value": {"type": "string"}}, "required": ["field", "value"]}},
        "then_click": {"type": "string", "description": "label of the button to press at the end, e.g. the continue/next button"}},
       ["steps", "apply_id"]),
    fn("request_tan", "Click the TAN request button (give its id); the mail team fetches the newest TAN and you get it back.",
       {"id": {"type": "integer"}}, ["id"]),
    fn("sign_and_submit", "Final binding submission: click this button (id). Only after the TAN is typed into its field and the summary is checked.",
       {"id": {"type": "integer"}}, ["id"]),
    fn("done", "Finish: the portal shows the receipt (or, in the preparation phase, the bid-type step is reached).",
       {"summary": {"type": "string"}}, ["summary"]),
]

_ADD_RE = re.compile(r"hinzufügen|add (a )?position|neue position|position anlegen|\{action=add\}", re.I)
_BACK_RE = re.compile(r"^\s*(zurück|back)\b|\{action=back\}", re.I)
RECEIPT_RE = re.compile(r"(eingangsbest[äa]tigung|verbindlich eingegangen|eingangsnummer|receipt|empfangsbest[äa]tigung|angebot .{0,30}eingegangen)", re.I)


def de_num(x, decimals: int = 2) -> str:
    d = Decimal(str(x)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    sign = "-" if d < 0 else ""
    d = abs(d)
    whole, _, frac = f"{d:.{decimals}f}".partition(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    return sign + ".".join(groups) + ("," + frac if decimals else "")


def de_percent(x) -> str:
    d = Decimal(str(x))
    if d == d.to_integral_value():
        return str(int(d))
    return de_num(d, 2).rstrip("0").rstrip(",")


def _cert(c) -> str:
    from .normalize import norm_cert
    return "none (choose the option without certification)" if norm_cert(c) == "none" else str(c or "")


class Dispatcher(Specialist):
    def __init__(self, name, tab, team, manual, tan_runner):
        super().__init__(name, tab, team, manual)
        self.tan_runner = tan_runner  # async (request_no, requested_at) -> str | None
        self.tan: str | None = None
        self.tan_requests = 0
        self.receipt: str | None = None
        self.answer: dict | None = None
        self.phase = "prepare"

    def tools(self, mission: Mission) -> list[dict]:
        if self.phase == "prepare":
            return [t for t in DISPATCH_TOOLS if t["function"]["name"] not in ("request_tan", "sign_and_submit")]
        return DISPATCH_TOOLS

    def system_prompt(self, mission: Mission) -> str:
        return self.team.common_manual.split("# Never")[0] + "\n\n# Your job\n" + self.manual

    async def before_step(self, mission: Mission, st: LoopState, obs) -> None:
        if self.phase == "final" and self.tan_requests and RECEIPT_RE.search(obs.text):
            m = re.search(r"(Eingangsnummer|Receipt|Nummer)\s*:?\s*([A-Z0-9-]{5,})", obs.text)
            self.receipt = self.receipt or (m.group(2) if m else "receipt shown")
            st.status, st.summary = "done", f"receipt {self.receipt}"

    def set_answer(self, answer: dict, extra: dict) -> None:
        self.answer, self.extra = answer, extra

    def _current_position(self) -> str | None:
        """Which bid position the wizard step shows (its number appears in the step heading)."""
        if not self.tab.obs or not self.answer:
            return None
        text = self.tab.obs.text
        hits = [p["position"] for p in self.answer.get("positions", []) if re.search(r"Position\s*" + re.escape(p["position"]) + r"\b", text)]
        return hits[0] if len(hits) == 1 else None

    async def _check_position_form(self) -> str:
        """Before continuing a position step: every value in the form must belong to that position's row."""
        pid = self._current_position()
        if not pid:
            return ""
        row = next(p for p in self.answer["positions"] if p["position"] == pid)
        at = (getattr(self, "extra", {}) or {}).get(pid, {})
        nums = set()
        for k in ("quantity", "unit_price", "lead_time_weeks", "base_unit_price", "surcharge_per_unit", "packaging_per_unit", "rebate_total"):
            if k in row:
                nums.add(Decimal(str(row[k])))
        for k in ("bore", "stroke", "pressure"):
            if at.get(k) not in (None, ""):
                d = to_decimal(at.get(k))
                if d is not None:
                    nums.add(d)
        texts = {norm_id(row["material_number"]), norm_id(str(at.get("mounting") or "")), "NONE", "OHNE", norm_id(str(at.get("certification") or ""))}
        try:
            values = await self.tab._current().evaluate("""() => Array.from(document.querySelectorAll('input, select'))
                .filter(e => !['hidden','checkbox','radio','submit','button'].includes((e.type||'').toLowerCase()) && e.name && !e.disabled)
                .map(e => [e.name, e.value])""")
        except Exception:  # noqa: BLE001
            return ""
        wrong = []
        for nm, val in values:
            v = (val or "").strip()
            if not v or nm.lower() in ("csrf", "revision", "tan", "command", "q"):
                continue
            d = to_decimal(v, "de-DE") if re.fullmatch(r"[\d.,]+", v) else None
            if d is not None:
                if d not in nums:
                    wrong.append(f"{nm}={v}")
            elif norm_id(v) not in texts and not norm_id(v).startswith(norm_id(row["material_number"])):
                wrong.append(f"{nm}={v}")
        if wrong:
            return (f"refused to continue: these values are not in the row of position {pid}: {', '.join(wrong)}. "
                    f"Position {pid} needs: quantity {row['quantity']}, unit price {de_num(row['unit_price'])}, material {row['material_number']}"
                    f", lead time {row['lead_time_weeks']}. Fix the fields, then continue.")
        return ""

    def _is_continue(self, label: str) -> bool:
        return bool(re.search(r"weiter|next|continue|fortfahren|speichern|save|\{action=next\}", label, re.I)) and not _ADD_RE.search(label)

    async def execute_special(self, mission: Mission, st: LoopState, name: str, args: dict, obs) -> str | None:
        if name == "done":
            if self.phase == "prepare":
                st.status, st.summary = "done", str(args.get("summary") or "")
                return "preparation finished"
            if self.receipt:
                st.status, st.summary = "done", f"receipt {self.receipt}"
                return "finished"
            return "not finished: the receipt is not shown yet"
        if name == "fill_cascade":
            res = await self.tab.fill_cascade(list(args.get("steps") or []), int(args["apply_id"]) if args.get("apply_id") is not None else None)
            if args.get("fields"):
                res += "; fields: " + ", ".join(await self.tab.fill_named(list(args.get("fields") or [])))
            label = str(args.get("then_click") or "").strip()
            if label and "not offered" not in res and "no such select" not in res and "still disabled" not in res and "not found" not in res:
                if _ADD_RE.search(label) or _BACK_RE.search(label):
                    res += f"; refused to press {label!r}"
                else:
                    problem = await self._check_position_form() if self._is_continue(label) else ""
                    res += "; " + (problem or await self.tab.click_label(label))
            return res + await self._alerts()
        if name == "request_tan":
            return await self._request_tan(int(args.get("id") or 0))
        if name == "sign_and_submit":
            res = await self._sign(int(args.get("id") or 0))
            if self.receipt:
                st.status, st.summary = "done", f"receipt {self.receipt}"
            return res
        if name == "click" and self._is_back(int(args.get("id") or -1)):
            return "refused: going back deletes the later steps of the wizard"
        if name == "click" and self._is_add(int(args.get("id") or -1)):
            return "refused: the bid must contain exactly the tender's positions; never add positions"
        if name == "click":
            jid = int(args.get("id") or 0)
            if self._is_continue(self._label_of(jid)):
                problem = await self._check_position_form()
                if problem:
                    return problem
            res = await self.tab.click(jid)
            return res + await self._alerts()
        return None

    async def _alerts(self) -> str:
        msg = await self.tab.alerts()
        return f"; the page shows: {msg}" if msg else ""

    def _label_of(self, jid: int) -> str:
        if not self.tab.obs:
            return ""
        m = re.search(r"\[%d\] (?:button|link) \"([^\"]*)\"([^\n]*)" % jid, self.tab.obs.text)
        return (m.group(1) + " " + m.group(2)) if m else ""

    def _is_add(self, jid: int) -> bool:
        return bool(_ADD_RE.search(self._label_of(jid)))

    def _is_back(self, jid: int) -> bool:
        if not self.tab.obs:
            return False
        m = re.search(r"\[%d\] (?:button|link) \"([^\"]*)\"([^\n]*)" % jid, self.tab.obs.text)
        if not m:
            return False
        label = (m.group(1) + " " + m.group(2)).lower()
        return bool(re.search(r"\bzurück\b|\bback\b|action=back|\{action=back\}", label)) and "zur bekanntmachung" not in label

    def _summary_problems(self) -> list[str]:
        """Compare the summary page with the bid: each position's row must show its material, quantity and unit price."""
        if not self.tab.obs or not self.answer or self.answer.get("decision") != "bid":
            return []
        rows = [ln for ln in self.tab.obs.text.splitlines() if ln.startswith("|")]
        out = []
        for p in self.answer["positions"]:
            row = next((r for r in rows if re.search(r"\|\s*" + re.escape(p["position"]) + r"\s*\|", r)), None)
            if row is None:
                out.append(f"{p['position']} not in the summary")
                continue
            for want in (p["material_number"], de_num(p["unit_price"])):
                if want not in row:
                    out.append(f"{p['position']}: summary does not show {want}")
        total = de_num(self.answer["total_net"])
        if total not in self.tab.obs.text:
            out.append(f"summary does not show the net total {total}")
        return out

    async def _request_tan(self, jid: int) -> str:
        if self.tan_requests >= 3:
            return "refused: too many TAN requests"
        problems = self._summary_problems()
        if problems:
            self.log("summary_mismatch", problems=problems[:10])
        self.tan_requests += 1
        requested_at = self.team.notebook_time()
        res = await self.tab.click(jid)
        await asyncio.sleep(0.4)
        obs = await self.tab.observe()
        m = re.search(r"(?:Anforderung|Request|Anfrage)\s*(?:Nr\.?|#)?\s*(\d+)", obs.text)
        request_no = m.group(1) if m else None
        self.log("tan_requested", request_no=request_no, click=res)
        tan = await self.tan_runner(request_no, requested_at)
        if not tan:
            return f"TAN requested ({res}; request {request_no}) but the mail team did not find it; you may request again"
        self.tan = tan
        return f"TAN for request {request_no}: {tan} — type it into the TAN field now, then sign_and_submit"

    async def _sign(self, jid: int) -> str:
        if not self.tan:
            return "refused: request the TAN first"
        if self.tab.obs and self.tan not in self.tab.obs.text:
            return f"refused: the TAN field does not show {self.tan} yet; fill it first"
        self.tab.confirm_armed = True
        try:
            res = await self.tab.click(jid)
            await asyncio.sleep(1.5)
        finally:
            pass
        obs = await self.tab.observe()
        if RECEIPT_RE.search(obs.text):
            m = re.search(r"(Eingangsnummer|Receipt|Nummer)\s*:?\s*([A-Z0-9-]{5,})", obs.text)
            self.receipt = m.group(2) if m else "receipt shown"
            self.log("signed", receipt=self.receipt, dialogs=self.tab.dialog_log[-3:])
            return f"signed: {self.receipt}"
        self.tab.confirm_armed = False
        return f"{res}; no receipt yet. Read the page: there may be a validation message, or the TAN expired."

    def bid_table(self, answer: dict, extra: dict) -> str:
        lines = [f"decision: {answer['decision']}", f"tender reference: {answer['tender_reference']}"]
        if answer["decision"] == "no_bid":
            lines.append("No positions. Choose the waiver (Verzichtserklärung) and omit every position; total 0,00.")
            return "\n".join(lines)
        lines.append(f"discount percent: {de_percent(answer['discount_percent'])}")
        lines.append(f"NET total (EUR): {de_num(answer['total_net'])}")
        lines.append("positions (German number format, copy exactly):")
        lines.append("| position | material | quantity (pieces) | net unit price | lead time (weeks) | base price | surcharge | packaging | project bonus (whole position) | bore | stroke | pressure | mounting | certification |")
        for p in answer["positions"]:
            at = extra.get(p["position"], {})
            def opt(k):
                return de_num(p[k]) if k in p else "(leave empty)"
            lines.append(f"| {p['position']} | {p['material_number']} | {p['quantity']} | {de_num(p['unit_price'])} | {p['lead_time_weeks']} | "
                         f"{opt('base_unit_price')} | {opt('surcharge_per_unit')} | {opt('packaging_per_unit')} | {opt('rebate_total')} | "
                         f"{at.get('bore', '')} | {at.get('stroke', '')} | {at.get('pressure', '')} | {at.get('mounting', '')} | {_cert(at.get('certification'))} |")
        omitted = extra.get("_omit") or []
        if omitted:
            lines.append("Positions to OMIT explicitly (not in the table): " + ", ".join(omitted))
        return "\n".join(lines)


def prepare_mission(team, tender_url: str | None) -> Mission:
    p = team.plan
    email = team.bidder_address()

    def ctx():
        return team.base_context() + f"\nContact e-mail: {team.bidder_address() or '(not known yet)'}"

    return Mission(
        id="dispatch-prepare", kind="dispatcher",
        goal=(f"Prepare the bid for tender {p.reference}. Open the tender and its bid submission (Angebotsabgabe). If the portal first "
              f"asks for an expression of interest (Teilnahme bekunden), do that. In the bidder-data step enter company "
              f"'{p.bidder_company}', VAT id '{p.bidder_vat}' and contact e-mail '{email or 'the bidder mailbox address'}', then continue. "
              "Stop as soon as the wizard shows the step where the bid type is chosen (main bid / secondary bid / waiver): call done there "
              "WITHOUT choosing or continuing. Never go back."),
        checklist=["bid submission opened", "bidder data saved", "bid-type step reached (nothing chosen)"],
        context=ctx, max_steps=18, budget_s=240, start_url=tender_url)


def final_mission(team, table: str) -> Mission:
    p = team.plan
    return Mission(
        id="dispatch-final", kind="dispatcher",
        goal=(f"Enter and sign the bid for tender {p.reference} in the open bid wizard, step by step, exactly with this data:\n{table}\n"
              f"Bidder: {p.bidder_company}, VAT {p.bidder_vat}, contact e-mail {team.bidder_address()}.\n"
              "Never go back. On the summary step request the TAN (request_tan), type it, and sign (sign_and_submit)."),
        checklist=["every step saved with the bid data", "summary checked", "TAN typed", "signed: receipt shown"],
        context=team.base_context, max_steps=220, budget_s=420)
