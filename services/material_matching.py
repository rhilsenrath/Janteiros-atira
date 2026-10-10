from typing import Protocol
from core.contracts import TechnicalRequirement, MaterialCandidate

class MaterialMatcher(Protocol):
    def match(self, requirement: TechnicalRequirement,
              candidates: list[MaterialCandidate]) -> list[MaterialCandidate]: ...

class UnimplementedMaterialMatcher:
    def match(self, requirement, candidates):
        raise NotImplementedError("Resolve amended/derived requirements before exact active-material matching")
