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
from core.config import RunConfig
from core.contracts import (AgentTask, AgentResult, AgentStatus,
    TenderData, TechnicalRequirements, ERPData, CommercialPolicies, BidResult, is_synthetic)
from core.state import RunState
from core.events import ProgressEvent
from services.bid_validator import BidValidator
from services.submission import GuardedSubmission
from core.access import AccessManager
from tools.logging import RunLogger

class Planner(Protocol):
    def stages(self) -> tuple[tuple[str, ...], ...]: ...

class DeterministicPlanner:
    def stages(self):
        return (("tender", "supplier", "mail"), ("erp",), ("pricing",))

class Orchestrator:
    def __init__(self, runtime, browser_manager, config=None, agents=None, llm=None,
                 planner=None, validator=None, submission=None, on_event=None, access_manager=None, logger=None):
        self.config = config or RunConfig()
        self.runtime = runtime
        self.browsers = browser_manager
        self.llm = llm
        self.access = access_manager
        self.logger = logger or RunLogger()
        self.changed = asyncio.Event()
        self.finished = set()
        self.planner = planner or DeterministicPlanner()
        self.validator = validator or BidValidator()
        self.submission = submission or GuardedSubmission()
        self.on_event = on_event or (lambda event: None)
        self.agents = agents if agents is not None else {
            a.agent_id: a for a in (TenderAgent(), SupplierAgent(), ErpAgent(), MailAgent(), PricingAgent())}
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
            page=self.browsers.page_for(agent_id) if agent_id != "pricing" else None,
            access_manager=self.access, logger=self.logger, report=self.report,
            get_task=lambda task: self.refresh_task(agent_id, task))

    async def report(self, result):
        expected = {"tender": TenderData, "supplier": TechnicalRequirements, "erp": ERPData,
                    "mail": CommercialPolicies, "pricing": BidResult}
        if result.agent_id not in expected or (result.output is not None and
                not isinstance(result.output, expected[result.agent_id])):
            raise ValueError("Progress must match registered worker output contract")
        if self.config.mode == "production" and is_synthetic(result):
            raise ValueError("Synthetic progress refused")
        self.state.collect(result)
        self.changed.set()
        self.logger.emit("partial_result", agent=result.agent_id, status=result.status.value,
                         count=len(result.evidence))

    async def refresh_task(self, agent_id, task):
        # Wait on events, never poll the pages or hold a browser lock while awaiting peers.
        dependency = {"mail": "tender", "erp": "supplier"}.get(agent_id)
        while dependency:
            ready = bool(self.state.tender and self.state.tender.reference) if agent_id == "mail" else bool(
                self.state.technical and self.state.technical.requirements)
            if ready or dependency in self.finished:
                break
            self.changed.clear()
            self.logger.emit("dependency_wait", agent=agent_id, operation=dependency)
            await self.changed.wait()
        current = self.task(task.action, task.question)
        return current

    def task(self, action="investigate", question=None):
        return AgentTask(action=action, question=question, tender=self.state.tender,
            technical=self.state.technical, erp=self.state.erp, commercial=self.state.commercial,
            bid=self.state.bid).model_copy(deep=True)

    async def execute(self, agent_id, task):
        self.state.statuses[agent_id] = AgentStatus.running
        self.on_event(ProgressEvent(agent_id, AgentStatus.running, task.action))
        self.logger.emit("agent_running", agent=agent_id, operation=task.action)
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
            except Exception as error:
                self.logger.emit("worker_error", agent=agent_id, error_type=type(error).__name__)
                # Exception strings may contain runtime credentials; do not retain them.
                result = AgentResult(agent_id=agent_id, status=AgentStatus.failed,
                    error="Worker failed; inspect locally without exposing runtime secrets")
            if result.status != AgentStatus.failed or not result.retryable or attempt == self.config.retries:
                self.logger.emit("agent_finished", agent=agent_id, status=result.status.value,
                                 count=len(result.evidence))
                return result
        raise AssertionError("unreachable")

    def pricing_ready(self):
        values = (self.state.tender, self.state.technical, self.state.erp, self.state.commercial)
        return all(value is not None for value in values) and (
            self.config.mode == "test" or all(value.evidence and not is_synthetic(value) for value in values))

    async def stage(self, names):
        results = await asyncio.gather(*(self.execute(name,
            self.task("price" if name == "pricing" else "investigate")) for name in names))
        for result in results:
            self.state.collect(result)
            self.on_event(ProgressEvent(result.agent_id, result.status, "collected"))

    async def investigate(self, name):
        try:
            result = await self.execute(name, self.task())
            self.state.collect(result)
            self.on_event(ProgressEvent(name, result.status, "collected"))
        finally:
            self.finished.add(name)
            self.changed.set()

    async def run(self):
        try:
            remaining = contract.seconds_left(self.runtime) - self.config.deadline_reserve
            if remaining <= 0:
                for agent_id in self.agents:
                    self.state.collect(AgentResult(agent_id=agent_id, status=AgentStatus.blocked,
                        error="Execution deadline reached"))
                return None
            await asyncio.wait_for(self.browsers.initialize(), min(self.config.task_timeout, remaining))
            self.logger.emit("browser_sessions_ready", count=4)
            if self.config.mode == "production":
                self.access = self.access or AccessManager(self.runtime)
                if self.access.assignments is None:
                    if self.llm is None:
                        raise ValueError("Model client required for dynamic runtime role assignment")
                    await asyncio.wait_for(self.access.resolve(self.llm, self.runtime.get("brief", "")),
                                           min(self.config.task_timeout,
                                               max(.001, contract.seconds_left(self.runtime) - self.config.deadline_reserve)))
                self.logger.emit("runtime_access_resolved", count=4)
                # All sessions authenticate concurrently; Mail/ERP wait for incremental dependencies
                # before business searches, not before opening/login to their own sites.
                await asyncio.gather(*(self.investigate(name) for name in ("tender","supplier","erp","mail")))
                stages = (("pricing",),)
            else:
                # Injected local test workers only; no production simulator or synthetic defaults.
                stages = self.planner.stages()
            for names in stages:
                if "erp" in names and self.state.technical is None:
                    self.state.collect(AgentResult(agent_id="erp", status=AgentStatus.blocked,
                        error="Technical requirements unavailable"))
                    names = tuple(n for n in names if n != "erp")
                if "pricing" in names and not self.pricing_ready():
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
                if self.pricing_ready():
                    await self.stage(("pricing",))
            if self.state.bid is None:
                return None
            self.state.validation = self.validator.validate(self.state.bid,
                self.runtime.get("answer_schema", {}), self.state.evidence)
            if self.state.validation.valid and self.config.enable_submission:
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
            self.logger.emit("run_error", error_type="TimeoutError")
            return None
        except Exception as error:
            self.state.errors.append(f"Run integration failed: {type(error).__name__}")
            self.logger.emit("run_error", error_type=type(error).__name__)
            return None
        finally:
            await self.browsers.close()
