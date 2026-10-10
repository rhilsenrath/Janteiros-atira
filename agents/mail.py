from agents.base import BaseAgent, AgentContext
from core.contracts import AgentTask, AgentResult

class MailAgent(BaseAgent):
    agent_id = "mail"
    responsibility = "authorized commercial evidence and signing TAN"

    async def execute(self, task: AgentTask, context: AgentContext) -> AgentResult:
        return self.blocked()
