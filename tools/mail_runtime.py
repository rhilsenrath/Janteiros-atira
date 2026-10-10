"""Isolated mailbox context, diagnostics and schema-compatible model calls."""
import asyncio
import json
import time
import os
import math
from dataclasses import dataclass
from uuid import uuid4

from openai import APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import ValidationError

from agents.base import AgentContext
from tools.llm_client import LLMClient
from tools.logging import RunLogger
from tools.model_context import business_schema, current_call
import models


@dataclass(frozen=True)
class MailContext(AgentContext):
    save_artifact: object = None
    restart_between_topics: bool = False


class MailLogger(RunLogger):
    def __init__(self, sink=print):
        super().__init__(sink)
        self.run_id = uuid4().hex

    def emit(self, event, **kwargs):
        permitted = {'agent','status','operation','count','error_type','phase','step',
                     'duration_s','input_tokens','output_tokens','attempt','evidence_refs','validation_errors','provider','model'}
        if set(kwargs) - permitted:
            raise TypeError('Unsupported mailbox event metadata')
        payload = {'event':event,'run_id':self.run_id,'elapsed_s':round(time.monotonic()-self.started,2)}
        payload.update({k:v for k,v in kwargs.items() if v is not None})
        self.sink(json.dumps(payload,ensure_ascii=False))


class MailLLMClient(LLMClient):
    def __init__(self, *, logger, client=None, fallback_client=None, qwen_timeout=None):
        super().__init__(client=client, retries=0)
        self.logger = logger
        self.schema_supported = True
        self.fallback_client = fallback_client
        self.owns_fallback = fallback_client is None
        self.fallback_model = os.environ.get('MAIL_OPENAI_MODEL','gpt-4.1-mini')
        self.qwen_timeout = float(qwen_timeout if qwen_timeout is not None else os.environ.get('MAIL_QWEN_TIMEOUT','30'))
        if not math.isfinite(self.qwen_timeout) or self.qwen_timeout<=0:
            raise ValueError('MAIL_QWEN_TIMEOUT must be a positive finite number')

    def fallback_available(self):
        return self.fallback_client is not None or bool(os.environ.get('OPENAI_API_KEY'))

    def openai_client(self):
        if self.fallback_client is None:
            self.fallback_client=AsyncOpenAI(api_key=os.environ['OPENAI_API_KEY'],
                base_url=os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1',max_retries=0)
        return self.fallback_client

    async def completion(self,client,options,timeout,metadata):
        started=time.monotonic()
        async with asyncio.timeout(timeout):
            pending=asyncio.create_task(client.chat.completions.create(**options))
            try:
                while not pending.done():
                    done,_=await asyncio.wait({pending},timeout=min(15,timeout))
                    if not done:
                        self.logger.emit('model_wait',duration_s=round(time.monotonic()-started,2),**metadata)
                return await pending
            finally:
                if not pending.done():pending.cancel()
                await asyncio.gather(pending,return_exceptions=True)

    async def close(self):
        try:
            await super().close()
        finally:
            if self.owns_fallback and self.fallback_client is not None:
                await self.fallback_client.close()

    async def ask(self, system, user, *, output_type=None, tools=None):
        from tools.llm_client import ModelReply
        if self.client is None:
            self.client = models.qwen().with_options(max_retries=0)
        settings = current_call.get()
        metadata = {k:settings[k] for k in ('agent','phase','step') if k in settings}
        schema = business_schema(output_type.model_json_schema()) if output_type else None
        messages = [{'role':'system','content':system + ('\nReturn exactly JSON matching this schema:\n'+json.dumps(schema) if schema else '')},
                    {'role':'user','content':user}]
        corrections = 0
        provider='qwen'
        for attempt in range(3):
            options = {'model':self.model,'messages':messages,'max_tokens':settings.get('max_tokens',4096),
                       'timeout':settings.get('timeout',90),
                       'extra_body':{'chat_template_kwargs':{'enable_thinking':False}}}
            if schema:
                options['response_format'] = ({'type':'json_schema','json_schema':{'name':output_type.__name__,'schema':schema}}
                    if self.schema_supported else {'type':'json_object'})
            timeout=settings.get('timeout',90)
            if provider=='openai':
                options.pop('extra_body',None)
                options['model']=self.fallback_model
                if schema:options['response_format']={'type':'json_object'}
            elif self.fallback_available():
                timeout=min(timeout,self.qwen_timeout)
            options['timeout']=timeout
            call_metadata={**metadata,'provider':provider,'model':options['model']}
            started=time.monotonic()
            self.logger.emit('model_call_started',attempt=attempt+1,**call_metadata)
            try:
                try:
                    reply=await self.completion(self.openai_client() if provider=='openai' else self.client,
                                                options,timeout,call_metadata)
                except (TimeoutError,APITimeoutError):
                    if provider!='qwen' or not self.fallback_available():raise
                    self.logger.emit('model_provider_fallback',error_type='TimeoutError',**call_metadata)
                    provider='openai'
                    options.pop('extra_body',None)
                    options['model']=self.fallback_model
                    options['timeout']=settings.get('timeout',90)
                    if schema:options['response_format']={'type':'json_object'}
                    call_metadata={**metadata,'provider':provider,'model':self.fallback_model}
                    started=time.monotonic()
                    self.logger.emit('model_call_started',attempt=attempt+1,**call_metadata)
                    reply=await self.completion(self.openai_client(),options,options['timeout'],call_metadata)
            except APIStatusError as error:
                if provider=='qwen' and schema and self.schema_supported and error.status_code in (400,422):
                    self.schema_supported=False
                    self.logger.emit('model_schema_fallback',**metadata)
                    continue
                self.logger.emit('model_error',error_type=type(error).__name__,**metadata)
                raise
            content=reply.choices[0].message.content or ''
            usage=reply.usage.model_dump() if reply.usage else {}
            self.logger.emit('model_call',duration_s=round(time.monotonic()-started,2),
                input_tokens=usage.get('prompt_tokens'),output_tokens=usage.get('completion_tokens'),**call_metadata)
            try:
                parsed=output_type.model_validate_json(content) if output_type else None
                return ModelReply(content,parsed,usage,[])
            except ValidationError as error:
                self.logger.emit('model_response_invalid',error_type='ValidationError',
                    validation_errors=[{'loc':list(e['loc']),'type':e['type']}
                                       for e in error.errors(include_input=False,include_url=False)],**metadata)
                if corrections >= 1:
                    raise
                corrections += 1
                messages += [{'role':'assistant','content':content},
                    {'role':'user','content':'Correct the JSON to match the schema. Errors: '+json.dumps(error.errors(include_input=False,include_url=False),default=str)}]
        raise ValueError('Mailbox model did not return valid JSON')
