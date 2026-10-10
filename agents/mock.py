"""Synthetic empty records exercise wiring; no copied scenario answers or invented TAN."""
import asyncio
from agents.base import BaseAgent
from core.contracts import (AgentResult, AgentStatus, Evidence, TenderData,
    TechnicalRequirements, ERPData, CommercialPolicies, BidResult, SubmissionReceipt)

class MockAgent(BaseAgent):
    is_mock = True
    def __init__(self, agent_id):
        self.agent_id = agent_id
        self.responsibility = "synthetic integration exercise"

    async def execute(self, task, context):
        if context.config.mode != "mock":
            raise RuntimeError("Mock agent refused production context")
        await asyncio.sleep(0)
        evidence = Evidence(id=f"mock:{self.agent_id}", agent_id=self.agent_id,
            source_site="synthetic", mock=True)
        outputs = {"tender": TenderData, "supplier": TechnicalRequirements,
                   "erp": ERPData, "mail": CommercialPolicies}
        if task.action == "submit":
            output = SubmissionReceipt(simulated=True)
        elif self.agent_id == "pricing":
            if any(x is None for x in (task.tender, task.technical, task.erp, task.commercial)):
                raise ValueError("Mock pricing requires all four typed inputs")
            output = BidResult(mock=True, evidence=[evidence], missing=["Synthetic workflow; no actual bid"])
        else:
            output = outputs[self.agent_id](evidence=[evidence])
        return AgentResult(agent_id=self.agent_id, status=AgentStatus.complete,
            output=output, mock=True)
