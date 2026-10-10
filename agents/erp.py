from agents.base import BaseAgent, AgentContext
from core.contracts import AgentTask, AgentResult

class ErpAgent(BaseAgent):
    agent_id = "erp"
    responsibility = "active materials and price conditions"

    async def execute(self, task: AgentTask, context: AgentContext) -> AgentResult:
        return self.blocked()
