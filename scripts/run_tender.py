"""Run only the Tender Agent once against the live tender portal and save what it extracted.

Credentials are never stored in the repository. They come, in this order, from:
  1. CHALLENGE_SITE_ACCESS / CHALLENGE_SCOUTING (exported by `atira scout --env`), the entry whose siteId/url
     names the tender portal;
  2. TENDER_URL + TENDER_USERNAME, with the password typed at a hidden prompt (or TENDER_PASSWORD).
The model needs QWEN_BASE_URL and QWEN_API_KEY (`atira models proxy` running, then `atira scout --env`).

    .venv/bin/python scripts/run_tender.py [--steps 60] [--out runs/tender_result.json]
"""
import argparse
import asyncio
import getpass
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright  # noqa: E402

import browsers  # noqa: E402
import models  # noqa: E402
from agents.base import AgentContext  # noqa: E402
from agents.tender import TenderAgent  # noqa: E402
from core.access import SiteAccess  # noqa: E402
from core.config import RunConfig  # noqa: E402
from core.contracts import AgentTask  # noqa: E402
from tools.llm_client import LLMClient  # noqa: E402
from tools.logging import RunLogger, redact  # noqa: E402


import time  # noqa: E402
import agents.website as website  # noqa: E402
from agents.website import AuthenticationStep  # noqa: E402
from tools.navigation import BrowserAction, BrowserNavigator  # noqa: E402

STARTED = time.monotonic()
SECRETS: list[str] = []
RUNS = ROOT / "runs"  # git-ignored local outputs
LOG_FILE = RUNS / "tender_run.log"


def say(message):
    """Human-readable progress for local console runs only; every line is redacted and also kept in
    runs/tender_run.log so a run can be analysed afterwards."""
    line = f"[{time.monotonic() - STARTED:6.1f}s] {redact(message, SECRETS)}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def save(result, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact(result.model_dump_json(indent=2), SECRETS), encoding="utf-8")


class ConsoleLogger(RunLogger):
    def __init__(self):
        super().__init__(sink=lambda line: None)

    def emit(self, event, **kwargs):
        super().emit(event, **kwargs)
        details = " ".join(f"{k}={v}" for k, v in kwargs.items() if v is not None and k != "evidence_refs")
        say(f"event  {event} {details}".rstrip())


class ConsoleNavigator(BrowserNavigator):
    logged_urls: set = set()
    async def initialize(self):
        say(f"browser opening {self.site.url}")
        await super().initialize()

    async def observe(self, offset=0):
        observation = await super().observe(offset)
        frames = observation.get("frames", [])
        first = next((f.get("text", "").strip() for f in frames if f.get("text", "").strip()), "")
        controls = sum(len(f.get("controls", [])) for f in frames)
        url = observation.get("url")
        if url not in self.logged_urls:  # first visit: record the page's controls for later diagnosis
            self.logged_urls.add(url)
            for f in frames:
                for c in f.get("controls", [])[:40]:
                    say(f"control {c.get('target')} <{c.get('tag')}> \"{(c.get('label') or '')[:60]}\""
                        f"{' expanded=' + str(c['expanded']) if c.get('expanded') is not None else ''}"
                        f"{' overlay' if c.get('in_overlay') else ''}")
        for f in frames:
            if any(c.get("type") == "password" for c in f.get("controls", [])):
                say(f"page   login form visible in frame {f.get('frame')} ({f.get('url')})")
        say(f"page   {observation.get('url')} | {len(frames)} frame(s), {controls} controls | "
            f"\"{' '.join(first.split())[:90]}\"")
        return observation

    async def act(self, action, **kwargs):
        label = ""
        if action.target in self.targets:
            control = self.targets[action.target][2]
            label = f" \"{control.get('label') or control.get('name') or control.get('tag')}\""
        detail = action.url or (f"secret:{action.secret}" if action.secret else action.value) or ""
        say(f"action {action.operation}{label} {detail}".rstrip())
        result = await super().act(action, **kwargs)
        if result:
            say(f"document read: {result.get('name')} ({len(result.get('text', ''))} chars"
                f"{', members: ' + str(len(result['members'])) if 'members' in result else ''})")
        return result


class ConsoleLLM(LLMClient):
    async def ask(self, system, user, *, output_type=None, tools=None):
        purpose = {AuthenticationStep: "login step", BrowserAction: "next navigation step"}.get(
            output_type, "extract tender facts")
        try:
            feedback = (json.loads(user).get("feedback") or {}) if output_type is BrowserAction else {}
        except ValueError:
            feedback = {}
        if feedback.get("error"):
            say(f"error  {feedback.get('error_type')}: {' '.join(str(feedback['error']).split())[:200]}")
        say(f"model  thinking: {purpose} ...")
        started = time.monotonic()
        reply = await super().ask(system, user, output_type=output_type, tools=tools)
        value, took = reply.structured, time.monotonic() - started
        if isinstance(value, AuthenticationStep):
            say(f"model  ({took:.1f}s) login -> {value.action.operation} authenticated={value.authenticated}")
        elif isinstance(value, BrowserAction):
            details = (("value", value.value), ("url", value.url), ("download", value.download),
                       ("member", value.member), ("offset", value.offset or None))
            extra = " ".join(f"{k}={v!r}" for k, v in details if v is not None)
            say(f"model  ({took:.1f}s) chose {value.operation} target={value.target} {extra} "
                f"sources={len(value.source_ids)}")
        elif value is not None and hasattr(value, "output"):
            out = value.output
            say(f"model  ({took:.1f}s) extracted status={value.status} reference={out.reference} "
                f"positions={len(out.positions)} answers={len(out.clarifications)} "
                f"citations={len(value.citations)} missing={len(value.missing)}")
        return reply


class TenderOnlyAccess:
    """The AccessManager surface the website worker uses, for a single pre-assigned site."""
    def __init__(self, site):
        self.site = site

    def for_role(self, role):
        if role != "tender":
            raise ValueError("Only the tender role is configured for this run")
        return self.site

    def secrets(self):
        return [self.site.username.get_secret_value(), self.site.password.get_secret_value()]


def scouting_from_cli(timeout=60):
    """The public scouting run from `atira scout --env`. The CLI may keep running after printing, and buffers
    piped output, so read its output from a private temporary file instead of a pipe."""
    import shlex, shutil, subprocess, tempfile
    cli = shutil.which("atira") or str(Path.home() / ".local" / "bin" / "atira")
    with tempfile.TemporaryDirectory() as folder:
        output = Path(folder) / "scout.env"
        with output.open("w") as handle:
            try:
                process = subprocess.Popen([cli, "scout", "--env"], stdout=handle, stderr=subprocess.DEVNULL)
            except OSError:
                return None
            try:
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    exited = process.poll() is not None
                    for line in output.read_text(errors="replace").splitlines():
                        if line.startswith("export CHALLENGE_SCOUTING="):
                            try:
                                return json.loads(shlex.split(line.partition("=")[2])[0])
                            except ValueError:
                                pass  # line still being written
                    if exited:  # finished and its complete output was read above
                        break
                    time.sleep(0.5)
            finally:
                process.kill()
    return None


def load_brief(brief):
    """The brief names the tender to find; without it the agent can only browse aimlessly."""
    if os.environ.get("TENDER_BRIEF"):
        return os.environ["TENDER_BRIEF"]
    if brief:
        return brief
    say("brief: not exported; fetching the scouting brief with `atira scout --env` ...")
    run = scouting_from_cli()
    return (run or {}).get("brief", "")


def runtime_entries():
    raw = os.environ.get("CHALLENGE_SITE_ACCESS")
    if raw:
        return json.loads(raw), ""
    if os.environ.get("CHALLENGE_SCOUTING"):
        run = json.loads(os.environ["CHALLENGE_SCOUTING"])
        return run.get("access", []), run.get("brief", "")
    return [], ""


def tender_site():
    """Explicit TENDER_* settings (e.g. from the git-ignored .env) win; otherwise the scout export's entry."""
    entries, brief = runtime_entries()
    url, username = os.environ.get("TENDER_URL"), os.environ.get("TENDER_USERNAME")
    if url and username:
        password = os.environ.get("TENDER_PASSWORD") or getpass.getpass("Tender portal password (hidden): ")
        say("credentials: TENDER_URL/TENDER_USERNAME/TENDER_PASSWORD from environment/.env")
        return SiteAccess(site_id="tender-portal", url=url, username=username, password=password,
                          description="Tender portal (Vergabeportal)"), brief
    matches = [e for e in entries if "tender" in f"{e.get('siteId', '')} {e.get('url', '')}".lower()]
    if len(matches) != 1:
        raise SystemExit("No tender access: set TENDER_URL/TENDER_USERNAME/TENDER_PASSWORD in .env or run "
                         "`eval \"$(atira scout --env)\"`")
    e = matches[0]
    say("credentials: tender entry of the atira scout export")
    return SiteAccess(site_id=e["siteId"], url=e["url"], username=e["credentials"]["username"],
                      password=e["credentials"]["password"],
                      description=str(e.get("description", e.get("name", "")))), brief


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--steps", type=int, default=100, help="maximum model-chosen navigation steps")
    parser.add_argument("--timeout", type=float, default=1200, help="seconds for the whole investigation")
    parser.add_argument("--out", default=str(ROOT / "runs" / "tender_result.json"))
    args = parser.parse_args()
    RUNS.mkdir(exist_ok=True)
    lock = RUNS / "tender_run.pid"
    try:  # one local run at a time: parallel runs would overwrite each other's log and results
        other = int(lock.read_text())
        os.kill(other, 0)
        raise SystemExit(f"Another tender run (pid {other}) is still active; stop it first (Ctrl+C in its terminal).")
    except (OSError, ValueError):
        pass
    lock.write_text(str(os.getpid()))
    LOG_FILE.write_text("", encoding="utf-8")  # one log per run
    models.load_local_env(str(ROOT / ".env"))
    if not os.environ.get("OPENAI_API_KEY") and not (os.environ.get("QWEN_BASE_URL") and os.environ.get("QWEN_API_KEY")):
        raise SystemExit("No model: put OPENAI_API_KEY in .env, or start `atira models proxy` and "
                         "`eval \"$(atira scout --env)\"`")
    site, brief = tender_site()
    access = TenderOnlyAccess(site)
    SECRETS.extend(access.secrets())
    website.BrowserNavigator = ConsoleNavigator  # verbose navigation for this local console run only
    provider = "openai" if os.environ.get("OPENAI_API_KEY") else "qwen"
    model_id = (os.environ.get("OPENAI_MODEL") or models.OPENAI_MODEL) if provider == "openai" else models.QWEN_MODEL
    say(f"tender agent starting: site={site.site_id} model={provider}:{model_id} max_steps={args.steps} "
        f"timeout={args.timeout:.0f}s")
    brief = load_brief(brief)
    if brief:
        import re
        headline = next((line for line in brief.splitlines() if re.search(r"[A-Z]{2,}-\d{4}-\d+", line)), "")
        references = sorted(set(re.findall(r"[A-Z]{2,}-\d{4}-\d+", brief)))
        say(f"brief: {len(brief)} chars; tender reference(s) named: {', '.join(references) or 'none found'}")
        if headline:
            say(f"brief: {headline.strip()[:220]}")
    else:
        say("WARNING brief: none available; the agent does not know which tender to look for. "
            "Run with `eval \"$(atira scout --env)\"` or set TENDER_BRIEF.")
    partials = []

    out = Path(args.out)
    partial_file = RUNS / "tender_partial.json"

    async def report(result):
        partials.append(result)
        save(result, partial_file)  # survives Ctrl+C or a crash
        say(f"PUBLISH status={result.status.value} reference={getattr(result.output, 'reference', None)} "
              f"positions={len(getattr(result.output, 'positions', []))} "
              f"answers={len(getattr(result.output, 'clarifications', []))}")

    llm = ConsoleLLM()
    async with async_playwright() as playwright:
        browser, page = await browsers.connect(playwright)  # local Chromium (HEADLESS=1 hides it)
        try:
            context = AgentContext(runtime={"run_id": "local-tender-only", "brief": brief},
                config=RunConfig(max_navigation_steps=args.steps), page=page, llm=llm, access_manager=access,
                logger=ConsoleLogger(), report=report)
            result = None
            try:
                async with asyncio.timeout(args.timeout):
                    result = await TenderAgent().execute(AgentTask(), context)
            except TimeoutError:
                reason = "Local run timeout; last partial result"
            except (asyncio.CancelledError, KeyboardInterrupt):
                reason = "Interrupted (Ctrl+C); last partial result"
            if result is None:
                say(f"stopped: {reason}")
                if not partials:
                    say("no verified tender facts were published before stopping")
                    return
                result = partials[-1].model_copy(update={"error": reason})
        finally:
            if result is not None:
                save(result, out)
            await llm.close()
            await browser.close()

    data = json.loads(out.read_text(encoding="utf-8"))
    output = data.get("output") or {}
    print(f"\nstatus={data['status']} error={data.get('error')}")
    print(f"reference={output.get('reference')} title={output.get('title')} buyer={output.get('buyer')}")
    for p in output.get("positions", []):
        print(f"  pos {p['position_id']}: {p.get('quantity')} {p.get('unit')} "
              f"(original {p.get('original_quantity')} {p.get('original_unit')}, basis {p.get('quantity_basis')}) "
              f"req={p.get('requirement_id')} spec={p.get('specification_reference')}")
    for c in output.get("clarifications", []):
        print(f"  answer {c['identifier']} {c.get('dated_at')} withdrawn={c.get('withdrawn')} "
              f"withdraws={c.get('withdraws')} changes={len(c.get('changes', []))}")
    for m in data.get("missing", []):
        print(f"  missing: {m['question']}")
    print(f"\nFull result written to {out}; log in {LOG_FILE}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        try:
            if (RUNS / "tender_run.pid").read_text() == str(os.getpid()):
                (RUNS / "tender_run.pid").unlink()
        except OSError:
            pass
