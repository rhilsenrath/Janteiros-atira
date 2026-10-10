"""Run only the real mailbox investigator with explicitly supplied upstream inputs."""
import argparse
import asyncio
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright

import browsers
import run as contract
from tools.mail_runtime import MailContext as AgentContext, MailLogger as RunLogger, MailLLMClient as LLMClient
from agents.mail import MailAgent
from core.access import AccessManager
from core.config import RunConfig
from core.contracts import AgentTask, AgentResult, AgentStatus, MissingInformation, TenderData
from tools.logging import redact


class SelectedMailAccess:
    """Expose only the explicitly selected mailbox to the worker."""
    def __init__(self, access, site_id):
        if site_id not in access.sites:
            raise ValueError('Mailbox site ID is not present in runtime access')
        self.access = access
        self.site = access.sites[site_id]

    def for_role(self, role):
        if role != 'mail':
            raise ValueError('Standalone access is restricted to mail')
        return self.site

    def secrets(self):
        return self.access.secrets()


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    inputs = cli.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--reference', help='Exact tender reference in the current scenario')
    inputs.add_argument('--task-file', type=Path, help='JSON AgentTask containing supplied Tender/ERP inputs')
    cli.add_argument('--site-id', help='Explicit mailbox siteId from runtime access; otherwise roles are resolved')
    cli.add_argument('--question', help='Optional targeted investigation request')
    cli.add_argument('--timeout', type=float, default=600, help='Total execution budget in seconds (default: 600)')
    cli.add_argument('--restart-between-topics', action='store_true', help='Close/reopen the mailbox tab and authenticate between investigation topics')
    cli.add_argument('--max-steps', type=int, default=100)
    cli.add_argument('--browser-number', type=int, default=1, help='Managed browser to attach to, numbered from 1')
    return cli


def load_task(args):
    if args.task_file:
        task = AgentTask.model_validate_json(args.task_file.read_text(encoding='utf-8-sig'))
    else:
        reference = args.reference.strip()
        task = AgentTask(tender=TenderData(reference=reference))
    if task.action not in ('investigate', 'reinvestigate'):
        raise ValueError('Standalone Mail supports investigation only')
    if not task.tender or not task.tender.reference or not task.tender.reference.strip():
        raise ValueError('An exact tender reference is required')
    if args.question:
        task = task.model_copy(update={'question': args.question})
    return task


async def investigate(task, context, *, worker=None, timeout=600):
    """No orchestrator, dependency waits, pricing, submission or TAN retrieval."""
    worker = worker or MailAgent()
    try:
        async with asyncio.timeout(timeout):
            return await worker.execute(task, context)
    except TimeoutError:
        latest = worker.latest
        return AgentResult(agent_id='mail', status=AgentStatus.partial if latest else AgentStatus.blocked,
            output=latest.output if latest else None, evidence=latest.evidence if latest else [],
            missing=(latest.missing if latest and latest.missing else [
                MissingInformation(agent_id='mail', question='Continue mailbox investigation after the execution time limit')]),
            error='Standalone mailbox execution time limit reached')


async def main(args):
    task = load_task(args)
    config = RunConfig(enable_submission=False, task_timeout=args.timeout, max_navigation_steps=args.max_steps)
    if args.browser_number < 1:
        raise ValueError('Browser number must be at least 1')
    endpoints = browsers.cdp_urls()
    if os.environ.get('CHALLENGE_RUN_TOKEN') and not endpoints:
        raise ValueError('A managed run requires a supplied CDP endpoint')
    if endpoints and args.browser_number > len(endpoints):
        raise ValueError('Requested managed browser is unavailable')
    runtime, _ = contract.load_run()
    if not runtime.get('brief', '').strip():
        raise ValueError('The current runtime must supply the scenario brief')
    budget = min(args.timeout, contract.seconds_left(runtime) - config.deadline_reserve)
    if budget <= 0:
        raise ValueError('Runtime deadline has already been reached')
    access = AccessManager(runtime)
    logger = RunLogger(sink=lambda line: print(line, flush=True))
    folder = Path('artifacts', 'mail-runs', logger.run_id)
    folder.mkdir(parents=True, exist_ok=True)

    def save(name, data):
        folder.joinpath(name + '.json').write_text(redact(
            json.dumps(data, ensure_ascii=False, indent=2, default=str), access.secrets()), encoding='utf-8')

    def event_sink(line):
        print(line, flush=True)
        with folder.joinpath('events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(line + '\n')

    logger.sink = event_sink
    save('input', {'task': task.model_dump(mode='json'), 'brief': runtime['brief'],
                   'upstream_inputs': 'supplied explicitly; other agents are not executed'})
    llm = LLMClient(logger=logger)
    browser = None
    worker = MailAgent()
    try:
        async with asyncio.timeout(budget):
            if args.site_id:
                worker_access = SelectedMailAccess(access, args.site_id)
            else:
                await access.resolve(llm, runtime['brief'])
                worker_access = access
            save('site', worker_access.for_role('mail').public())
            async with async_playwright() as playwright:
                try:
                    browser, page = await browsers.connect(playwright, args.browser_number - 1)
                    context = AgentContext(runtime, config, page=page, llm=llm,
                        access_manager=worker_access, logger=logger, save_artifact=save,
                        restart_between_topics=args.restart_between_topics)
                    result = await investigate(task, context, worker=worker, timeout=budget)
                finally:
                    if browser is not None and not endpoints:
                        try:
                            await browser.close()
                        except Exception as error:
                            logger.emit('cleanup_error', agent='mail', error_type=type(error).__name__)
        save('result', result.model_dump(mode='json'))
        print('Mail status:', result.status.value, flush=True)
        print('Rules:', len(result.output.rules) if result.output else 0, flush=True)
        print('Diagnostics:', folder.resolve(), flush=True)
        return 0 if result.status == AgentStatus.complete else 1
    except (Exception, asyncio.CancelledError) as error:
        latest = worker.latest
        result = AgentResult(agent_id='mail', status=AgentStatus.partial if latest else AgentStatus.failed,
            output=latest.output if latest else None, evidence=latest.evidence if latest else [],
            missing=latest.missing if latest else [], error=type(error).__name__)
        save('result', result.model_dump(mode='json'))
        logger.emit('standalone_mail_error', agent='mail', error_type=type(error).__name__)
        print('Diagnostics:', folder.resolve(), flush=True)
        if isinstance(error, asyncio.CancelledError):
            raise
        return 1
    finally:
        await llm.close()


if __name__ == '__main__':
    cli = parser()
    try:
        raise SystemExit(asyncio.run(main(cli.parse_args())))
    except ValueError as error:
        cli.error(str(error))
    except KeyboardInterrupt:
        raise SystemExit(130)
