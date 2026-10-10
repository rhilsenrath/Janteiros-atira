from typing import Protocol
from core.contracts import TenderData, CommercialPolicies

class PolicyResolver(Protocol):
    def resolve(self, tender: TenderData, records: CommercialPolicies) -> CommercialPolicies: ...

class UnimplementedPolicyResolver:
    def resolve(self, tender, records):
        raise NotImplementedError("Resolve exact reference, sender authority, scenario date and supersession")
