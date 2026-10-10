"""Conservative structural gate; challenge business validation remains to implement."""
from decimal import Decimal
from jsonschema import Draft202012Validator
from core.contracts import ValidationResult, is_synthetic
from services.submission import to_answer
from services.pricing_engine import round_money

class BidValidator:
    def validate(self, bid, schema, evidence=()) -> ValidationResult:
        issues = []
        if is_synthetic(bid) or is_synthetic(list(evidence)):
            issues.append("Synthetic evidence cannot be submitted")
        if not bid.complete or bid.missing:
            issues.append("Bid is incomplete")
        if not bid.evidence:
            issues.append("Bid lacks provenance")
        try:
            answer = to_answer(bid)
            issues.extend(error.message for error in Draft202012Validator(schema).iter_errors(answer))
            if bid.decision == "no_bid" and (bid.positions or bid.total_net != Decimal("0")):
                issues.append("no_bid requires empty positions and zero total")
            if bid.decision == "bid":
                if not bid.positions:
                    issues.append("bid requires positions")
                if len({p.position for p in bid.positions}) != len(bid.positions):
                    issues.append("Duplicate positions")
                if any(p.quantity <= 0 or p.unit_price is None or p.unit_price < 0 or
                       p.lead_time_weeks is None or p.lead_time_weeks < 0 for p in bid.positions):
                    issues.append("Incomplete or invalid position")
                elif round_money(sum((p.quantity * p.unit_price for p in bid.positions), Decimal("0"))) != bid.total_net:
                    issues.append("Total is inconsistent")
        except (ValueError, TypeError, ArithmeticError) as error:
            issues.append(str(error))
        # Explicitly fail closed until challenge-wide evidence/business checks exist.
        issues.append("Challenge business validation is not implemented")
        return ValidationResult(valid=not issues, issues=issues)
