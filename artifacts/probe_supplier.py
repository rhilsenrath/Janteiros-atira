"""Read-only targeted verification using the current observed tender checkpoint."""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import run
from playwright.async_api import async_playwright
from agents.supplier import SupplierAgent
from agents.base import AgentContext
from core.access import AccessManager
from core.config import RunConfig
from core.contracts import AgentTask,TenderData
from tools.browser_manager import BrowserManager
from tools.llm_client import LLMClient
from tools.logging import RunLogger,redact

async def main():
    status,body=run.http('https://ehl-challenge.atira.ai/v1/challenges/rfq/scouting',timeout=30)
    assert status==200
    os.environ['CHALLENGE_SCOUTING']=body
    runtime,_=run.load_run()
    checkpoint=json.loads(Path('artifacts/tender-evidence.json').read_text(encoding='utf-8'))
    tender=TenderData.model_validate(checkpoint['output'])
    logger=RunLogger()
    folder=Path('artifacts','supplier-probe-'+logger.run_id)
    folder.mkdir(parents=True,exist_ok=True)
    access=AccessManager(runtime)
    llm=LLMClient(logger=logger)
    await access.resolve(llm,runtime.get('brief',''))
    def save(name,data):
        folder.joinpath(name+'.json').write_text(redact(json.dumps(data,ensure_ascii=False,indent=2,default=str),access.secrets()),encoding='utf-8')
    async with async_playwright() as p:
        browsers=BrowserManager(p)
        try:
            await browsers.initialize()
            worker=SupplierAgent()
            context=AgentContext(runtime,RunConfig(enable_submission=False,max_navigation_steps=40),
                page=browsers.page_for('supplier'),llm=llm,access_manager=access,logger=logger,save_artifact=save)
            async with asyncio.timeout(480):
                result=await worker.execute(AgentTask(tender=tender),context)
            save('result',result.model_dump(mode='json'))
            print('Supplier result:',result.status.value,'requirements:',len(result.output.requirements) if result.output else 0,flush=True)
            print('Diagnostics:',folder,flush=True)
        finally:
            await browsers.close()
            await llm.close()

if __name__=='__main__':
    asyncio.run(main())
