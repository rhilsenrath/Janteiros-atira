from agents.base import BaseAgent, AgentContext
from core.contracts import AgentTask, AgentResult, AgentStatus, MissingInformation, is_synthetic
from services.pricing_engine import PricingEngine, PricingInput

class PricingAgent(BaseAgent):
    agent_id = "pricing"
    responsibility = "evidence-based decisions and deterministic pricing"

    def __init__(self, engine=None):
        self.engine = engine or PricingEngine()

    async def execute(self, task: AgentTask, context: AgentContext) -> AgentResult:
        requests = []
        sources = [('tender',task.tender),('supplier',task.technical),('erp',task.erp),('mail',task.commercial)]
        if any(is_synthetic(record) for _,record in sources):
            return AgentResult(agent_id=self.agent_id, status=AgentStatus.blocked,
                error='Synthetic pricing inputs refused')
        for role,record in sources:
            if record is None or not record.evidence:
                requests.append(MissingInformation(agent_id=role, question='Provide verified runtime evidence'))
            elif getattr(record,'conflicts',None):
                requests.append(MissingInformation(agent_id=role, question='Resolve conflicting authoritative records'))
        if task.tender is not None and (not task.tender.reference or not task.tender.positions):
            requests.append(MissingInformation(agent_id='tender', question='Verify tender reference and complete position list'))
        if task.technical is not None and not task.technical.requirements:
            requests.append(MissingInformation(agent_id='supplier', question='Provide applicable current technical requirements'))
        if task.erp is not None and not task.erp.materials:
            requests.append(MissingInformation(agent_id='erp', question='Inspect candidate materials or evidence that no active exact match exists'))
        if task.commercial is not None and not task.commercial.rules:
            requests.append(MissingInformation(agent_id='mail', question='Investigate authorized exact-reference commercial policies'))
        if requests:
            return AgentResult(agent_id=self.agent_id, status=AgentStatus.blocked, missing=requests)
        bid = self.engine.calculate(PricingInput(tender=task.tender, technical=task.technical,
            erp=task.erp, commercial=task.commercial))
        return AgentResult(agent_id=self.agent_id,
            status=AgentStatus.complete if bid.complete else AgentStatus.partial, output=bid)
