"""Run the basic ERP reader independently of all other agents."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import browsers
import run as contract
from core.access import SiteAccess
from core.config import RunConfig
from core.contracts import AgentTask,AgentResult,AgentStatus,ERPData
from agents.erp_standalone import StandaloneErpAgent,ErpContext
from tools.mail_runtime import MailLLMClient,MailLogger
from tools.logging import redact

ROOT=Path(__file__).resolve().parents[1]


class ErpAccess:
    def __init__(self,site):self.site=site
    def for_role(self,role):
        if role!='erp':raise ValueError('Standalone ERP cannot access other websites')
        return self.site
    def secrets(self):return [self.site.username.get_secret_value(),self.site.password.get_secret_value()]


def parser():
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--task-file',type=Path,default=ROOT/'examples/erp-task.json')
    cli.add_argument('--group',help='Override the example catalogue group')
    cli.add_argument('--timeout',type=float,default=300)
    cli.add_argument('--max-materials',type=int,choices=range(1,21),default=2)
    cli.add_argument('--browser-number',type=int,default=1)
    return cli


def load_task(args):
    task=AgentTask.model_validate_json(args.task_file.read_text(encoding='utf-8-sig'))
    if task.action not in ('investigate','reinvestigate'):raise ValueError('ERP supports investigation only')
    if not task.technical or len(task.technical.requirements)!=1:
        raise ValueError('Basic ERP run requires exactly one supplied technical requirement')
    if args.group:
        requirement=task.technical.requirements[0].model_copy(update={'catalogue_group':args.group.strip()})
        task=task.model_copy(update={'technical':task.technical.model_copy(update={'requirements':[requirement]})})
    if not task.technical.requirements[0].catalogue_group:raise ValueError('A catalogue group is required')
    return task


def load_access():
    if os.environ.get('CHALLENGE_RUN_TOKEN') or os.environ.get('CHALLENGE_SCOUTING'):
        runtime,_=contract.load_run()
        url=os.environ.get('ERP_URL','https://atira-rfq-erp.kyora.run/')
        entries=[x for x in runtime.get('access',[]) if x.get('url','').rstrip('/')==url.rstrip('/')]
        if len(entries)!=1:raise ValueError('ERP_URL must identify one current runtime website')
        entry=entries[0]
        site=SiteAccess(site_id=entry['siteId'],url=entry['url'],**entry['credentials'])
        return runtime,ErpAccess(site)
    username=os.environ.get('ERP_USERNAME');password=os.environ.get('ERP_PASSWORD')
    if not username or not password:raise ValueError('Set ERP_USERNAME and ERP_PASSWORD in .env')
    site=SiteAccess(site_id='erp',url=os.environ.get('ERP_URL','https://atira-rfq-erp.kyora.run/'),
                    username=username,password=password)
    return {'brief':'Standalone ERP development test. Supplied Tender/Technical inputs are mocks, not verified upstream data.'},ErpAccess(site)


async def main(args):
    load_dotenv(ROOT/'.env',override=False)
    task=load_task(args)
    config=RunConfig(task_timeout=args.timeout,enable_submission=False)
    runtime,access=load_access()
    endpoints=browsers.cdp_urls()
    if args.browser_number<1 or (endpoints and args.browser_number>len(endpoints)):
        raise ValueError('Requested browser is unavailable')
    if os.environ.get('CHALLENGE_RUN_TOKEN') and not endpoints:raise ValueError('Managed runs require a CDP endpoint')
    budget=min(args.timeout,contract.seconds_left(runtime)-config.deadline_reserve) if 'expires_at' in runtime else args.timeout
    if budget<=0:raise ValueError('Runtime deadline reached')
    logger=MailLogger()
    folder=ROOT/'artifacts/erp-runs'/logger.run_id
    folder.mkdir(parents=True,exist_ok=True)
    def save(name,data):
        (folder/(name+'.json')).write_text(redact(json.dumps(data,ensure_ascii=False,indent=2,default=str),access.secrets()),encoding='utf8')
    def emit(line):
        line=redact(line,access.secrets())
        print(line,flush=True)
        with (folder/'events.jsonl').open('a',encoding='utf8') as stream:stream.write(line+'\n')
    logger.sink=emit
    llm=MailLLMClient(logger=logger)
    worker=StandaloneErpAgent()
    browser=None
    save('input',{'task':task.model_dump(mode='json'),'upstream_inputs':'explicit development fixture; no other agents executed'})
    save('site',access.site.public())
    try:
        async with asyncio.timeout(budget):
            async with async_playwright() as playwright:
                try:
                    browser,page=await browsers.connect(playwright,args.browser_number-1)
                    context=ErpContext(runtime,config,page=page,llm=llm,access_manager=access,
                        logger=logger,save_artifact=save,max_materials=args.max_materials)
                    result=await worker.execute(task,context)
                finally:
                    if browser is not None and not endpoints:await browser.close()
    except (Exception,asyncio.CancelledError) as error:
        latest=worker.latest
        result=AgentResult(agent_id='erp',status=AgentStatus.partial if latest else AgentStatus.failed,
            output=latest.output if latest else ERPData(),evidence=latest.evidence if latest else [],
            error=type(error).__name__)
        save('result',result.model_dump(mode='json'))
        if isinstance(error,asyncio.CancelledError):raise
    finally:
        await llm.close()
    save('result',result.model_dump(mode='json'))
    print('ERP status:',result.status.value,flush=True)
    print('Materials:',len(result.output.materials) if result.output else 0,flush=True)
    print('Diagnostics:',folder,flush=True)
    return 1 if result.status==AgentStatus.failed else 0


if __name__=='__main__':
    cli=parser()
    try:raise SystemExit(asyncio.run(main(cli.parse_args())))
    except ValueError as error:cli.error(str(error))
    except KeyboardInterrupt:raise SystemExit(130)
