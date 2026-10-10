"""Map to Atira's RFQ schema; API transport stays exclusively in agent.main/run.py."""
from typing import Protocol
from agents.base import BaseAgent, SubmissionContext
from core.contracts import BidResult, AgentTask, SubmissionReceipt, ValidationResult, is_synthetic

def to_answer(bid: BidResult) -> dict:
    if is_synthetic(bid) or not bid.complete or bid.missing:
        raise ValueError("Only complete genuine bids can become platform answers")
    def number(value):
        if value is None or not value.is_finite():
            raise ValueError("Missing/nonfinite required monetary value")
        # JSON transport only; no financial arithmetic uses float.
        return float(value)
    positions = []
    for line in bid.positions:
        if line.quantity != line.quantity.to_integral_value():
            raise ValueError("Bid quantity must be integral pieces")
        item = {"position": line.position, "material_number": line.material_number,
            "quantity": int(line.quantity), "unit_price": number(line.unit_price),
            "lead_time_weeks": line.lead_time_weeks}
        for field in ("base_unit_price", "surcharge_per_unit", "packaging_per_unit", "rebate_total"):
            value = getattr(line, field)
            if value is not None:
                item[field] = number(value)
        positions.append(item)
    return {"tender_reference": bid.tender_reference, "decision": bid.decision,
        "currency": "EUR", "discount_percent": number(bid.discount_percent),
        "positions": positions, "total_net": number(bid.total_net)}

class SubmissionService(Protocol):
    async def prepare(self, bid: BidResult, validation: ValidationResult,
                      context: SubmissionContext, tender_agent: BaseAgent) -> dict | None: ...

class GuardedSubmission:
    async def prepare(self, bid: BidResult, validation: ValidationResult,
                      context: SubmissionContext, tender_agent: BaseAgent):
        if context.config.mode == "mock":
            if not bid.mock or not tender_agent.is_mock:
                raise ValueError("Mock simulation requires synthetic bid and mock tender")
            result = await tender_agent.execute(AgentTask(action="submit", bid=bid), context.agent_context or context)
            if not isinstance(result.output, SubmissionReceipt) or not result.output.simulated:
                raise ValueError("Mock tender did not return a simulated receipt")
            self.receipt = result.output
            return None
        if is_synthetic(bid) or is_synthetic(context.commercial) or tender_agent.is_mock or not validation.valid:
            raise ValueError("Submission refused: synthetic or invalid bid")
        if not context.commercial or not context.commercial.signing_token:
            raise ValueError("No signing token available")
        if context.runtime["submission"]["kind"] == "api":
            return to_answer(bid)  # main() retains authentication and actual POST
        result = await tender_agent.execute(AgentTask(action="submit", bid=bid,
            commercial=context.commercial), context.agent_context)
        if result.status.value != "complete" or not isinstance(result.output, SubmissionReceipt) or \
                not result.output.confirmed or result.output.simulated or result.mock:
            raise ValueError("Tender agent did not confirm signed submission receipt")
        self.receipt = result.output
        return None
