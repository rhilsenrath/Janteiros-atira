"""Live login/access check with the same browser tools and model as the run."""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

import run as contract
from agents.base import AgentContext
from agents.tender import TenderAgent
from agents.supplier import SupplierAgent
from agents.erp import ErpAgent
from agents.mail import MailAgent
from core.access import AccessManager
from core.config import RunConfig
from tools.browser_manager import BrowserManager
from tools.navigation import BrowserNavigator
from tools.llm_client import LLMClient
from tools.logging import RunLogger, redact
from tools.model_context import model_context


async def main():
    runtime, _ = contract.load_run()
    logger = RunLogger()
    llm = LLMClient(logger=logger)
    access = AccessManager(runtime)
    config = RunConfig(enable_submission=False)
    workers = [TenderAgent(),SupplierAgent(),ErpAgent(),MailAgent()]
    folder = Path('artifacts','access-'+logger.run_id)

    def save(name, data):
        folder.mkdir(parents=True, exist_ok=True)
        folder.joinpath(name+'.json').write_text(redact(
            json.dumps(data, ensure_ascii=False, indent=2, default=str), access.secrets()), encoding='utf-8')

    save('runtime', {'brief':runtime.get('brief'), 'answer_schema':runtime.get('answer_schema'),
                     'submission':runtime.get('submission'),
                     'websites':[site.public() for site in access.sites.values()]})

    async with async_playwright() as playwright:
        manager = BrowserManager(playwright)
        try:
            await manager.initialize()
            with model_context(phase='assign_access'):
                await access.resolve(llm, runtime.get('brief',''))

            async def check(worker):
                context = AgentContext(runtime,config,page=manager.page_for(worker.agent_id),
                    llm=llm,access_manager=access,logger=logger,save_artifact=save)
                site = access.for_role(worker.agent_id)
                worker.navigator = BrowserNavigator(context.page,site,access.secrets())
                context.page.set_default_timeout(config.action_timeout_ms)
                try:
                    async with asyncio.timeout(180):
                        await worker.navigator.initialize()
                        await worker.authenticate(context)
                        save(worker.agent_id+'-ready',await worker.navigator.observe())
                    return worker.agent_id, True
                except Exception as error:
                    logger.emit('access_check_failed',agent=worker.agent_id,error_type=type(error).__name__)
                    return worker.agent_id, False

            results = dict(await asyncio.gather(*(check(worker) for worker in workers)))
            save('summary',results)
            print('Access checks:',results,flush=True)
            print('Diagnostics:',folder,flush=True)
            return 0 if all(results.values()) else 1
        finally:
            await manager.close()
            await llm.close()


if __name__ == '__main__':
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print('\nAccess check interrupted.',flush=True)
        raise SystemExit(130)
