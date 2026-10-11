import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
from openai import BadRequestError
from pydantic import create_model
from tools.llm_client import LLMClient


class ModelOptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsupported_reasoning_effort_is_removed_and_retried(self):
        error = BadRequestError('Unrecognized request argument supplied: reasoning_effort',
            response=httpx.Response(400, request=httpx.Request('POST', 'https://model.test')),
            body={'error': {'message': 'Unrecognized request argument supplied: reasoning_effort'}})
        reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}', tool_calls=[]))], usage=None)
        calls = []
        async def create(**options):
            calls.append(options)
            if len(calls) == 1:
                raise error
            return reply
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        llm = LLMClient(client=client, model='gpt-4.1-mini')
        llm.extraction_effort = 'low'
        result = await llm.ask('Return JSON', '{}', output_type=create_model('ERPExtraction', ok=(bool, ...)))
        self.assertTrue(result.structured.ok)
        self.assertIn('reasoning_effort', calls[0])
        self.assertNotIn('reasoning_effort', calls[1])
