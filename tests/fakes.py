"""Synthetic resources exist only under tests and are never default production workers."""
from types import SimpleNamespace
from agents.base import BaseAgent
from core.contracts import AgentResult, AgentStatus, Evidence, TenderData, TechnicalRequirements, ERPData, CommercialPolicies, BidResult

class FakeBrowserManager:
    def __init__(self): self.pages={role:object() for role in ('tender','supplier','erp','mail')}
    async def initialize(self): pass
    def page_for(self, role): return self.pages[role]
    async def close(self): pass

class FakeAgent(BaseAgent):
    is_mock=True
    def __init__(self, role): self.agent_id=role; self.responsibility='unit test fixture'
    async def execute(self, task, context):
        e=Evidence(id='fixture:'+self.agent_id, agent_id=self.agent_id, source_site='unit-test',mock=True)
        kind={'tender':TenderData,'supplier':TechnicalRequirements,'erp':ERPData,'mail':CommercialPolicies}
        output=kind[self.agent_id](evidence=[e]) if self.agent_id in kind else BidResult(mock=True)
        return AgentResult(agent_id=self.agent_id,status=AgentStatus.complete,output=output,mock=True)

class ScriptedModel:
    def __init__(self, replies): self.replies=list(replies); self.requests=[]
    async def ask(self, system, user, *, output_type=None, tools=None):
        self.requests.append((system,user))
        reply=self.replies.pop(0)
        structured=reply if isinstance(reply, output_type) else output_type.model_validate(reply)
        return SimpleNamespace(structured=structured)
