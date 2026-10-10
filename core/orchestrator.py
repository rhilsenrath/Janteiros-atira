"""Deterministic bounded scheduler; planning can be replaced without changing agents."""
import asyncio
from typing import Protocol
import run as contract
from agents.base import AgentContext, SubmissionContext
from agents.tender import TenderAgent
from agents.supplier import SupplierAgent
from agents.erp import ErpAgent
from agents.mail import MailAgent
from agents.pricing import PricingAgent
from agents.mock import MockAgent
from core.config import RunConfig
from core.contracts import (AgentTask, AgentResult, AgentStatus, SubmissionReceipt,
    TenderData, TechnicalRequirements, ERPData, CommercialPolicies, BidResult, is_synthetic)
from core.state import RunState
from core.events import ProgressEvent
from services.bid_validator import BidValidator
from services.submission import GuardedSubmission
from tools.browser_manager import MockBrowserManager

class Planner(Protocol):
    def stages(self) -> tuple[tuple[str, ...], ...]: ...

class DeterministicPlanner:
    def stages(self):
        return (("tender", "supplier", "mail"), ("erp",), ("pricing",))

class Orchestrator:
    def __init__(self, runtime, browser_manager, config=None, agents=None, llm=None,
                 planner=None, validator=None, submission=None, on_event=None):
        self.config = config or RunConfig()
        self.runtime = runtime
        self.browsers = browser_manager
        self.llm = llm
        self.planner = planner or DeterministicPlanner()
        self.validator = validator or BidValidator()
        self.submission = submission or GuardedSubmission()
        self.on_event = on_event or (lambda event: None)
        self.agents = agents if agents is not None else (
            {name: MockAgent(name) for name in ("tender", "supplier", "erp", "mail", "pricing")}
            if self.config.mode == "mock" else
            {a.agent_id: a for a in (TenderAgent(), SupplierAgent(), ErpAgent(), MailAgent(), PricingAgent())})
        required = {"tender", "supplier", "erp", "mail", "pricing"}
        if set(self.agents) != required or any(k != a.agent_id for k, a in self.agents.items()):
            raise ValueError("Register all five uniquely identified workers")
        if self.config.mode == "production" and any(a.is_mock for a in self.agents.values()):
            raise ValueError("Mock agents cannot run in production")
        self.state = RunState(metadata={k: runtime.get(k) for k in
            ("run_id", "brief", "started_at", "expires_at", "answer_schema", "submission")},
            config=self.config, statuses={k: AgentStatus.pending for k in self.agents})

    def context(self, agent_id):
        return AgentContext(runtime=self.runtime, config=self.config, llm=self.llm,
            page=self.browsers.page_for(agent_id) if agent_id != "pricing" else None)

    def task(self, action="investigate", question=None):
        return AgentTask(action=action, question=question, tender=self.state.tender,
            technical=self.state.technical, erp=self.state.erp, commercial=self.state.commercial,
            bid=self.state.bid).model_copy(deep=True)

    async def execute(self, agent_id, task):
        self.state.statuses[agent_id] = AgentStatus.running
        self.on_event(ProgressEvent(agent_id, AgentStatus.running, task.action))
        expected = {"tender": TenderData, "supplier": TechnicalRequirements,
                    "erp": ERPData, "mail": CommercialPolicies, "pricing": BidResult}
        for attempt in range(self.config.retries + 1):
            timeout = min(self.config.task_timeout,
                contract.seconds_left(self.runtime) - self.config.deadline_reserve)
            if timeout <= 0:
                return AgentResult(agent_id=agent_id, status=AgentStatus.blocked, error="Execution deadline reached")
            try:
                async with asyncio.timeout(timeout):
                    result = await self.agents[agent_id].execute(task, self.context(agent_id))
                if result.agent_id != agent_id:
                    raise ValueError("Worker returned a different agent identifier")
                if result.output is not None and not isinstance(result.output, expected[agent_id]):
                    raise ValueError("Worker returned an incorrect output contract")
                if self.config.mode == "production" and is_synthetic(result):
                    raise ValueError("Synthetic output refused in production")
            except asyncio.CancelledError:
                self.state.statuses[agent_id] = AgentStatus.cancelled
                raise
            except TimeoutError:
                result = AgentResult(agent_id=agent_id, status=AgentStatus.failed,
                    error="Worker exceeded execution timeout", retryable=True)
            except Exception:
                # Exception strings may contain runtime credentials; do not retain them.
                result = AgentResult(agent_id=agent_id, status=AgentStatus.failed,
                    error="Worker failed; inspect locally without exposing runtime secrets")
            if result.status != AgentStatus.failed or not result.retryable or attempt == self.config.retries:
                return result
        raise AssertionError("unreachable")

    async def stage(self, names):
        results = await asyncio.gather(*(self.execute(name,
            self.task("price" if name == "pricing" else "investigate")) for name in names))
        for result in results:
            self.state.collect(result)
            self.on_event(ProgressEvent(result.agent_id, result.status, "collected"))

    async def run(self):
        try:
            remaining = contract.seconds_left(self.runtime) - self.config.deadline_reserve
            if remaining <= 0:
                for agent_id in self.agents:
                    self.state.collect(AgentResult(agent_id=agent_id, status=AgentStatus.blocked,
                        error="Execution deadline reached"))
                return None
            await asyncio.wait_for(self.browsers.initialize(), min(self.config.task_timeout, remaining))
            for names in self.planner.stages():
                if "erp" in names and self.state.technical is None:
                    self.state.collect(AgentResult(agent_id="erp", status=AgentStatus.blocked,
                        error="Technical requirements unavailable"))
                    names = tuple(n for n in names if n != "erp")
                if "pricing" in names and any(value is None for value in
                        (self.state.tender, self.state.technical, self.state.erp, self.state.commercial)):
                    self.state.collect(AgentResult(agent_id="pricing", status=AgentStatus.blocked,
                        error="Required pricing inputs unavailable"))
                    names = tuple(n for n in names if n != "pricing")
                await self.stage(names)
            # Pricing can ask individual workers for targeted missing evidence.
            for _ in range(self.config.max_reinvestigations):
                requests = self.state.missing.copy()
                if not requests:
                    break
                for request in requests:
                    if request.agent_id in self.agents and request.agent_id != "pricing":
                        result = await self.execute(request.agent_id,
                            self.task("reinvestigate", request.question))
                        self.state.collect(result)
                await self.stage(("pricing",))
            if self.state.bid is None:
                return None
            self.state.validation = self.validator.validate(self.state.bid,
                self.runtime.get("answer_schema", {}), self.state.evidence)
            if self.config.mode == "mock" or self.state.validation.valid:
                context = SubmissionContext(self.runtime, self.config, self.state.commercial, self.context("tender"))
                remaining = contract.seconds_left(self.runtime) - self.config.deadline_reserve
                if remaining <= 0:
                    self.state.errors.append("Submission deadline reached")
                    return None
                answer = await asyncio.wait_for(self.submission.prepare(self.state.bid, self.state.validation,
                    context, self.agents["tender"]), min(self.config.task_timeout, remaining))
                self.state.submission_receipt = getattr(self.submission, "receipt", None)
                return answer
            return None
        except TimeoutError:
            self.state.errors.append("Resource initialization or submission exceeded execution timeout")
            return None
        finally:
            await self.browsers.close()

async def demo():
    config = RunConfig(mode="mock")
    orchestrator = Orchestrator({"brief": "Synthetic integration exercise", "access": [],
        "answer_schema": {}, "submission": {"kind": "site"}}, MockBrowserManager(), config)
    await orchestrator.run()
    print("Mock workflow completed; no genuine evidence or submission.")
    print({k: v.value for k, v in orchestrator.state.statuses.items()})

if __name__ == "__main__":
    asyncio.run(demo())
