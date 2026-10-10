from agents.base import BaseAgent, AgentContext
from core.contracts import AgentTask, AgentResult

class SupplierAgent(BaseAgent):
    agent_id = "supplier"
    responsibility = "current technical specifications"

    async def execute(self, task: AgentTask, context: AgentContext) -> AgentResult:
        return self.blocked()
