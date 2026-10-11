"""ERP-only read navigation, using the existing bounded DOM reader."""
import asyncio
import re
from urllib.parse import urljoin, urlsplit
from tools.mail_navigation import MailNavigator


class ErpNavigator(MailNavigator):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.observed_urls=set()

    async def observe(self,offset=0):
        observation=await super().observe(offset)
        for frame in observation.get('frames',[]):
            for control in frame.get('controls',[]):
                href=control.get('href')
                if href and self.allowed(href):self.observed_urls.add(href)
        return observation

    async def act(self,action,**kwargs):
        if action.operation=='goto':
            url=urljoin(self.page.url,action.url)
            if url not in self.observed_urls or not self.allowed(url):
                raise ValueError('ERP navigation requires an observed same-site link')
            if urlsplit(url).path not in ('/','/mm03','/mmbe','/vk13','/search'):
                raise ValueError('ERP transaction is outside the read-only workflow')
            await self.page.goto(url,wait_until='domcontentloaded',timeout=self.action_timeout)
            await asyncio.sleep(.15)
            return
        if action.operation=='click' and action.target in self.targets:
            label=self.targets[action.target][2].get('label','')
            if re.search(r'Speichern|Ändern|Löschen|Buchen|Save|Delete',label,re.I):
                raise ValueError('ERP business changes are forbidden')
        return await super().act(action,**kwargs)
