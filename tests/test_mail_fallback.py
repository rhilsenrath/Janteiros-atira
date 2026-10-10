import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from tools.mail_runtime import MailLLMClient, MailLogger
from tools.navigation import BrowserAction


def reply(text='{"operation":"inspect"}'):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))],usage=None)


def client(fn):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fn)),close=AsyncMock())


class MailFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_error_retries_on_backup(self):
        import httpx
        from openai import InternalServerError
        error=InternalServerError('temporary',response=httpx.Response(500,request=httpx.Request('POST','https://test.invalid')),body=None)
        primary=AsyncMock(side_effect=error)
        backup=AsyncMock(return_value=reply())
        worker=MailLLMClient(logger=MailLogger(sink=lambda _:None),client=client(primary),fallback_client=client(backup))
        result=await worker.ask('Navigate','{}',output_type=BrowserAction)
        self.assertEqual(result.structured.operation,'inspect')
        self.assertEqual(primary.await_count,1)
        self.assertEqual(backup.await_count,1)

    async def test_slow_qwen_cancelled_and_same_request_uses_openai(self):
        cancelled=[]
        async def slow(**kwargs):
            try:await asyncio.Event().wait()
            finally:cancelled.append(True)
        backup=AsyncMock(return_value=reply())
        events=[]
        worker=MailLLMClient(logger=MailLogger(sink=events.append),client=client(slow),
                             fallback_client=client(backup),qwen_timeout=.01)
        result=await worker.ask('Navigate','{}',output_type=BrowserAction)
        self.assertEqual(result.structured.operation,'inspect')
        self.assertEqual(cancelled,[True])
        options=backup.call_args.kwargs
        self.assertNotIn('extra_body',options)
        self.assertEqual(options['messages'][1]['content'],'{}')
        self.assertEqual(options['model'],worker.fallback_model)
        self.assertIn('model_provider_fallback',''.join(events))

    async def test_fast_qwen_does_not_call_backup(self):
        backup=AsyncMock(return_value=reply())
        worker=MailLLMClient(logger=MailLogger(sink=lambda _:None),client=client(AsyncMock(return_value=reply())),
                             fallback_client=client(backup),qwen_timeout=.1)
        await worker.ask('Navigate','{}',output_type=BrowserAction)
        backup.assert_not_awaited()

    async def test_external_cancellation_does_not_trigger_paid_call(self):
        backup=AsyncMock(return_value=reply())
        async def slow(**kwargs):await asyncio.Event().wait()
        worker=MailLLMClient(logger=MailLogger(sink=lambda _:None),client=client(slow),
                             fallback_client=client(backup),qwen_timeout=1)
        task=asyncio.create_task(worker.ask('Navigate','{}',output_type=BrowserAction))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        backup.assert_not_awaited()

    async def test_backup_validation_correction_stays_on_backup(self):
        async def slow(**kwargs):await asyncio.Event().wait()
        backup=AsyncMock(side_effect=[reply('{"operation":"fill"}'),reply()])
        primary=AsyncMock(side_effect=slow)
        worker=MailLLMClient(logger=MailLogger(sink=lambda _:None),client=client(primary),
                             fallback_client=client(backup),qwen_timeout=.01)
        result=await worker.ask('Navigate','{}',output_type=BrowserAction)
        self.assertEqual(result.structured.operation,'inspect')
        self.assertEqual(primary.await_count,1)
        self.assertEqual(backup.await_count,2)

    async def test_without_key_timeout_does_not_create_openai_client(self):
        async def timedout(**kwargs):raise TimeoutError()
        with patch.dict('os.environ',{'OPENAI_API_KEY':''}),patch('tools.mail_runtime.AsyncOpenAI') as sdk:
            worker=MailLLMClient(logger=MailLogger(sink=lambda _:None),client=client(timedout))
            with self.assertRaises(TimeoutError):await worker.ask('Navigate','{}',output_type=BrowserAction)
            sdk.assert_not_called()

    async def test_next_five_calls_use_backup_then_probe_qwen(self):
        async def slow(**kwargs):await asyncio.Event().wait()
        primary=AsyncMock(side_effect=[TimeoutError(),reply()])
        backup=AsyncMock(return_value=reply())
        worker=MailLLMClient(logger=MailLogger(sink=lambda _:None),client=client(primary),
                             fallback_client=client(backup),qwen_timeout=1)
        worker.openai_hold_calls=5
        await worker.ask('Navigate','{}',output_type=BrowserAction)
        for _ in range(5):await worker.ask('Navigate','{}',output_type=BrowserAction)
        self.assertEqual(primary.await_count,1)
        self.assertEqual(backup.await_count,6)
        await worker.ask('Navigate','{}',output_type=BrowserAction)
        self.assertEqual(primary.await_count,2)
        self.assertEqual(backup.await_count,6)
