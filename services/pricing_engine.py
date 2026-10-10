"""No business formulas are approximated here. Implement rules from the runtime brief."""
from decimal import Decimal, ROUND_HALF_UP
from core.contracts import Record, TenderData, TechnicalRequirements, ERPData, CommercialPolicies, BidResult

class PricingInput(Record):
    tender: TenderData
    technical: TechnicalRequirements
    erp: ERPData
    commercial: CommercialPolicies

def round_money(value: Decimal, quantum: Decimal = Decimal("0.01")) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("Use a finite Decimal, never float, for monetary calculations")
    return value.quantize(quantum, rounding=ROUND_HALF_UP)

class PricingEngine:
    def calculate(self, inputs: PricingInput) -> BidResult:
        return BidResult(tender_reference=inputs.tender.reference,
            missing=["Commercial pricing rules are not implemented"])
