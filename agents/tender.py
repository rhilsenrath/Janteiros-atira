"""Tender portal worker: model-directed investigation plus deterministic amendment resolution.

The shared WebsiteAgent loop authenticates with runtime credentials and lets the model choose every navigation
target. This class adds the tender-specific contract: Q&A coverage, withdrawal/supersession resolution
(services.tender_resolution) and a separate, guarded `submit_bid()` interface.
"""
import re
from datetime import datetime
from decimal import Decimal
from agents.website import WebsiteAgent
from core.contracts import AgentResult, AgentStatus, BidResult, MissingInformation, TenderData, is_synthetic
from services.tender_resolution import normalize_id, resolve_tender


SCENARIO_DATE = re.compile(r"(?:scenario date|stichtag|szenariodatum)\D{0,40}?"
                           r"(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?(?:Z|[+-]\d{2}:?\d{2})?)", re.I)


def scenario_date_from_brief(brief: str | None) -> datetime | None:
    """The brief states the scenario date (records dated later never apply)."""
    match = SCENARIO_DATE.search(brief or "")
    if not match:
        return None
    try:
        return datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
    except ValueError:
        return None


class TenderAgent(WebsiteAgent):
    agent_id = "tender"
    max_premature_finishes = 3  # an incomplete finish goes back to investigating while steps remain
    incremental_extraction = True
    stall_steps = 20
    responsibility = (
        "Locate the active RFQ on the tender portal and extract: reference (Vergabenummer), title, buyer, "
        "dates/deadlines with timezone; every bill-of-quantities position with description, original quantity, "
        "unit (Stück/Satz), specification reference and requirement id; every bidder Q&A answer "
        "(Bieterfragen/Antworten) with number, publication date, question and complete answer text, which "
        "answers it withdraws, and its structured changes (quantity, condition_group/Konditionsgruppe, "
        "packaging_key, delivery_key/Terminfreigabe, setup_pool, technical_amendment); documents with "
        "revisions; and the bid configurator fields, signing procedure and submission steps. Report facts "
        "as published; effective values are resolved by the application.")
    extraction_guidance = (
        " Tender rules: put bill-of-quantities values in original_quantity/original_unit and leave quantity, "
        "unit, quantity_basis, amendments, withdrawn and withdrawn_by to the application. Normalize German "
        "numbers (1.200 = 1200, 2,5 = 2.5). One clarification per published answer number, with the complete "
        "answer text, its question and dated_at (as displayed; put the portal timezone, e.g. Europe/Athens, in "
        "timezone). List answer numbers an answer withdraws/replaces in withdraws, even if the withdrawn answer "
        "is still displayed. Every effect on a position becomes one changes entry: quantity (value = new "
        "quantity, unit), condition_group, packaging_key, delivery_key, setup_pool, technical_amendment "
        "(specification_reference, revision, value 'Attribute = new value'), bonus_plan (project bonus "
        "plan key), setup_fee (ordinary one-off setup fee for the position, value = net EUR amount) or other. "
        "When an answer says a position is NOT in a setup pool or bonus plan ('kein Rüstpool', 'kein "
        "Projektbonus'), add that change_type with applies=false and value null. Set dated_at from the "
        "answer's publication/receipt date as displayed in its row, cell, tooltip (control hint) or detail and "
        "copy that text into date_text; never take dates from elsewhere. When an answer names a tender "
        "reference, put it in tender_reference (also on its changes) even if it differs from the investigated "
        "tender. Set scenario_date from the brief. Use a citation per answer and per position with "
        "reference set to that answer number or position id. Set qa_complete=true only after every Q&A page "
        "was read; record deadlines in dates and the configurator fields/signing/steps in submission.")

    def task_context(self, task):
        """Compact progress instead of the full record with evidence: smaller, faster model calls, and a
        clear view of what is already covered so the model does not revisit it."""
        tender = (self.latest.output if self.latest and isinstance(self.latest.output, TenderData)
                  else task.tender)
        if tender is None:
            return {'found_so_far': None}
        return {'found_so_far': {
            'reference': tender.reference, 'title': tender.title, 'buyer': tender.buyer,
            'scenario_date': tender.scenario_date.isoformat() if tender.scenario_date else None,
            'dates': [d.label for d in tender.dates],
            'positions': [f"{p.position_id}: {p.quantity} {p.unit} ({p.requirement_id or p.specification_reference})"
                          for p in tender.positions],
            'answers_read': [c.identifier + (' (withdrawn)' if c.withdrawn else '')
                             + ('' if c.dated_at else ' (date missing)') for c in tender.clarifications],
            'position_keys': {p.position_id: {k: v for k, v in {
                'condition_group': p.condition_group, 'packaging': p.packaging_key, 'delivery': p.delivery_key,
                'setup_pool': p.setup_pool if p.setup_pool_member is not False else 'none',
                'bonus_plan': p.bonus_plan if p.bonus_member is not False else 'none',
                'setup_fee': str(p.setup_fee) if p.setup_fee is not None else None}.items() if v}
                for p in tender.positions},
            'qa_complete': tender.qa_complete,
            'documents': [d.title for d in tender.documents],
            'configurator_read': bool(tender.submission and tender.submission.configurator_fields)},
            'still_missing': [m.question for m in (self.latest.missing if self.latest else [])][:8]}

    def finalize(self, output):
        if not isinstance(output, TenderData):
            return output, []
        if output.scenario_date is None and getattr(self, 'brief', None):
            output = output.model_copy(update={'scenario_date': scenario_date_from_brief(self.brief)})
        if self.latest and isinstance(self.latest.output, TenderData):
            # Keep verified bill-of-quantities values when a later extraction only restates effective values.
            known = {normalize_id(p.position_id): p for p in self.latest.output.positions}
            positions = []
            for position in output.positions:
                previous = known.get(normalize_id(position.position_id))
                if previous and position.original_quantity is None and previous.original_quantity is not None:
                    position = position.model_copy(update={"original_quantity": previous.original_quantity,
                        "original_unit": position.original_unit or previous.original_unit,
                        "quantity_basis": previous.quantity_basis})
                positions.append(position)
            output = output.model_copy(update={"positions": positions})
        resolved, missing = resolve_tender(output)
        if resolved.positions and not resolved.qa_complete:
            missing.append(MissingInformation(agent_id="tender",
                question="Read every bidder Q&A page/answer (Bieterfragen) and set qa_complete"))
        if resolved.submission is None or not resolved.submission.configurator_fields:
            missing.append(MissingInformation(agent_id="tender",
                question="Inspect the bid configurator fields, signing procedure and submission steps (do not save)"))
        return resolved, missing

    def objective_met(self):
        """Checklist: reference, every position resolved, all Q&A read, competing answers dated, withdrawn
        answers known, and the bid configurator read. The resolver's derived questions encode exactly this."""
        output = self.latest.output if self.latest else None
        if not isinstance(output, TenderData) or not self.covered(output):
            return False
        _, missing = self.finalize(output)
        return not missing

    def covered(self, output):
        return bool(output.reference and output.positions and output.qa_complete) and all(
            p.quantity is not None and p.unit and (p.requirement_id or p.specification_reference)
            for p in output.positions)

    async def execute(self, task, context):
        self.brief = context.runtime.get('brief', '')
        if task.action == "submit":
            token = task.commercial.signing_token if task.commercial else None
            return await self.submit_bid(task.bid, token, context, tender=task.tender)
        return await super().execute(task, context)

    def bid_issues(self, bid: BidResult | None, tender: TenderData | None) -> list[str]:
        """Preconditions a bid must meet before anything is typed into the configurator."""
        if bid is None:
            return ["No bid supplied"]
        issues = []
        if is_synthetic(bid):
            issues.append("Synthetic bid refused")
        if not bid.complete or bid.missing or bid.decision is None:
            issues.append("Bid is incomplete")
        if not bid.evidence:
            issues.append("Bid lacks provenance")
        if tender is None or not tender.reference:
            issues.append("No investigated tender reference")
            return issues
        if bid.tender_reference != tender.reference:
            issues.append("Bid reference does not match the investigated tender reference")
        if bid.decision == "no_bid":
            if bid.positions or bid.total_net != Decimal("0"):
                issues.append("no_bid requires no positions and a zero total")
            return issues
        effective = {normalize_id(p.position_id): p for p in tender.positions}
        for line in bid.positions:
            position = effective.get(normalize_id(line.position))
            if position is None:
                issues.append(f"Bid position {line.position} is not a tender position")
            elif position.quantity is None:
                issues.append(f"Effective quantity of {line.position} is unknown")
            elif position.unit and position.unit.casefold().startswith("st") and line.quantity != position.quantity:
                # Satz positions are converted to pieces by Supplier/Pricing; Stück must match exactly.
                issues.append(f"Bid quantity of {line.position} differs from the effective tender quantity")
        return issues

    async def submit_bid(self, bid: BidResult | None, signing_token, context, *, tender: TenderData | None = None):
        """Future signed submission: fill the configurator, sign with the Mail TAN (via the Orchestrator), submit,
        and confirm the receipt. Until that model-directed flow is implemented and live-tested it validates the
        inputs and reports blocked, so no incomplete or fabricated bid can ever be entered."""
        tender = tender or (self.latest.output if self.latest else None)
        issues = self.bid_issues(bid, tender)
        if not context.config.enable_submission or context.config.mode != "production":
            issues.append("Submission is not explicitly enabled for this run")
        if signing_token is None or not signing_token.get_secret_value():
            issues.append("No signing TAN supplied by the Orchestrator")
        if issues:
            return AgentResult(agent_id=self.agent_id, status=AgentStatus.blocked,
                error="Submission refused: " + "; ".join(issues))
        self.emit(context, "submission_unavailable")
        return AgentResult(agent_id=self.agent_id, status=AgentStatus.blocked,
            error="Bid configurator filling and signed submission are not implemented yet")
