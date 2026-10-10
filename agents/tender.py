from agents.base import BaseAgent, AgentContext
from core.contracts import AgentTask, AgentResult

class TenderAgent(BaseAgent):
    agent_id = "tender"
    responsibility = "tender investigation and signed portal submission"

    async def execute(self, task: AgentTask, context: AgentContext) -> AgentResult:
        return self.blocked()
