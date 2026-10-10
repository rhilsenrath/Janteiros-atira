from dataclasses import dataclass
from core.contracts import AgentStatus

@dataclass(frozen=True)
class ProgressEvent:
    agent_id: str
    status: AgentStatus
    action: str
