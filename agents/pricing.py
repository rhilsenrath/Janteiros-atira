from agents.base import BaseAgent, AgentContext
from core.contracts import AgentTask, AgentResult, AgentStatus
from services.pricing_engine import PricingEngine, PricingInput

class PricingAgent(BaseAgent):
    agent_id = "pricing"
    responsibility = "evidence-based decisions and deterministic pricing"

    def __init__(self, engine=None):
        self.engine = engine or PricingEngine()

    async def execute(self, task: AgentTask, context: AgentContext) -> AgentResult:
        if any(x is None for x in (task.tender, task.technical, task.erp, task.commercial)):
            return self.blocked()
        bid = self.engine.calculate(PricingInput(tender=task.tender, technical=task.technical,
            erp=task.erp, commercial=task.commercial))
        return AgentResult(agent_id=self.agent_id, status=AgentStatus.partial, output=bid)
