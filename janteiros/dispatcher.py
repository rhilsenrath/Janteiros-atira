"""O Despachante: enters the engine's answer in the portal's bid wizard and signs it with the TAN.

It is the only piece allowed to do the irreversible step, and only after the supervisor's go. The model reads
each wizard step and decides which field gets which value; helpers format numbers, request the TAN through the
mail specialist, arm the browser confirmation and read the receipt.
"""

from __future__ import annotations

import asyncio
import re
from decimal import Decimal

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
        "apply_id": {"type": "integer", "description": "id of the button that applies/confirms a selection"}}, ["steps", "apply_id"]),
    fn("request_tan", "Click the TAN request button (give its id); the mail team fetches the newest TAN and you get it back.",
       {"id": {"type": "integer"}}, ["id"]),
    fn("sign_and_submit", "Final binding submission: click this button (id). Only after the TAN is typed into its field and the summary is checked.",
       {"id": {"type": "integer"}}, ["id"]),
    fn("done", "Finish: the portal shows the receipt (or, in the preparation phase, the bid-type step is reached).",
       {"summary": {"type": "string"}}, ["summary"]),
]

RECEIPT_RE = re.compile(r"(eingangsbest[äa]tigung|verbindlich eingegangen|eingangsnummer|receipt|empfangsbest[äa]tigung|angebot .{0,30}eingegangen)", re.I)


def de_num(x, decimals: int = 2) -> str:
    d = Decimal(str(x)).quantize(Decimal(1).scaleb(-decimals))
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
        if name == "click":
            res = await self.tab.click(int(args.get("id") or 0))
            return res + await self._alerts()
        return None

    async def _alerts(self) -> str:
        msg = await self.tab.alerts()
        return f"; the page shows: {msg}" if msg else ""

    def _is_back(self, jid: int) -> bool:
        if not self.tab.obs:
            return False
        m = re.search(r"\[%d\] (?:button|link) \"([^\"]*)\"([^\n]*)" % jid, self.tab.obs.text)
        if not m:
            return False
        label = (m.group(1) + " " + m.group(2)).lower()
        return bool(re.search(r"\bzurück\b|\bback\b|action=back|\{action=back\}", label)) and "zur bekanntmachung" not in label

    async def _request_tan(self, jid: int) -> str:
        if self.tan_requests >= 3:
            return "refused: too many TAN requests"
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
