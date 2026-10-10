"""Your agent's entry point: agent.json runs `uv run --no-sync python agent.py`, in the sandbox and on your machine.

The kit only does the plumbing: run.py reads the run and submits an answer, browsers.py attaches to the run's
browsers, models.py calls the hosted Qwen and Clef. Everything else is yours to build, in `solve` below.
Docs: $API/docs/building-an-agent.md, $API/docs/run-contract.md, and the rules: $API/docs/fair-play.md
"""
import asyncio
import json
import sys

from playwright.async_api import async_playwright

import browsers
import models  # noqa: F401  models.ask_qwen, models.ask_clef: see models.py
import run as contract
from core.config import RunConfig
from core.orchestrator import Orchestrator
from tools.browser_manager import BrowserManager
from tools.llm_client import LLMClient


async def solve(task: dict, page) -> dict | None:
    """Delegate to the typed coordinator, preserving Atira's solve contract.

    `task` is the run: task["brief"] (the task and its rules), task["access"] (each website's url and login),
    task["submission"] (where the answer goes) and task["answer_schema"]. `page` is a Playwright page in browser 1.

    For an "api" submission, return the answer and main() submits it. For a "site" submission, enter the answer on
    that website yourself, then return None.

    Mind the clock: a scored run has 20 minutes, and `contract.seconds_left(task)` says how many are left. Every part
    of the answer scores on its own, so stop and submit your best answer about a minute before the end; an answer
    that comes too late scores 0.
    """
    config = RunConfig.from_env()
    llm = LLMClient()
    # Attach additional sessions within solve while retaining its two-argument API.
    async with async_playwright() as playwright:
        manager = BrowserManager(playwright, first_page=page)
        try:
            orchestrator = Orchestrator(task, manager, config, llm=llm)
            answer = await orchestrator.run()
            print("Agent statuses:", {k: v.value for k, v in orchestrator.state.statuses.items()}, flush=True)
            return answer
        finally:
            await llm.close()


async def main() -> int:
    models.load_local_env()  # local .env only (git-ignored, never packed); runs use team secrets
    task, token = contract.load_run()
    sites = f"{len(task['access'])} runtime entries"
    print(f"Run {task.get('run_id') or '(local: the scouting logins, nothing counts)'}; websites: {sites}; "
          f"answer via {task['submission']['kind']}", flush=True)
    RunConfig.from_env()  # Reject mock/test modes before browser access.
    BrowserManager.validate_environment()
    async with async_playwright() as playwright:
        browser, page = await browsers.connect(playwright)
        try:
            answer = await solve(task, page)
        finally:
            if not browsers.cdp_urls() and hasattr(browser, "close"):
                await browser.close()
    if answer is None:
        print("No answer to submit: inspect agent statuses; website and business rules remain to implement", flush=True)
        return 0
    if not token:
        print("Your answer (locally nothing is submitted):", json.dumps(answer, ensure_ascii=False), flush=True)
        return 0
    status, body = contract.submit_answer(task, token, answer)
    print(f"Answer: HTTP {status} {body[:2000]}", flush=True)
    return 0 if status == 202 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
