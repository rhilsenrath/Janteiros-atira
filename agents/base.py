from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from core.config import RunConfig
from core.contracts import AgentTask, AgentResult, AgentStatus, MissingInformation, CommercialPolicies

@dataclass(frozen=True)
class AgentContext:
    runtime: dict  # unchanged Atira run, including brief and runtime-only credentials
    config: RunConfig
    page: Any = None  # only the assigned page; browser lifecycle belongs to manager
    llm: Any = None

@dataclass(frozen=True)
class SubmissionContext:
    runtime: dict
    config: RunConfig
    commercial: CommercialPolicies | None
    agent_context: AgentContext | None

class BaseAgent(ABC):
    agent_id: str
    responsibility: str
    is_mock = False

    @abstractmethod
    async def execute(self, task: AgentTask, context: AgentContext) -> AgentResult:
        ...

    def blocked(self):
        return AgentResult(agent_id=self.agent_id, status=AgentStatus.blocked,
            missing=[MissingInformation(agent_id=self.agent_id,
                question=f"Implement {self.responsibility}")])
