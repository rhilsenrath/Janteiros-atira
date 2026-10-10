"""Inject SDK clients; retain all authentication in the existing models module/SDK config."""
import asyncio
from dataclasses import dataclass
from typing import Protocol
from pydantic import BaseModel
from openai import APIConnectionError, APIStatusError, APITimeoutError
import models

@dataclass(frozen=True)
class ModelReply:
    text: str
    structured: BaseModel | None
    usage: dict | None
    tool_calls: list

class ModelClient(Protocol):
    async def ask(self, system: str, user: str, *, output_type=None, tools=None) -> ModelReply: ...

class LLMClient:
    def __init__(self, client=None, model=None, retries=1):
        if not 0 <= retries <= 3:
            raise ValueError("retries must be between 0 and 3")
        self.client = client
        self.model = model or models.QWEN_MODEL
        self.retries = retries
        self.owns_client = client is None

    async def ask(self, system, user, *, output_type=None, tools=None):
        if self.client is None:
            self.client = models.qwen()
            self.client = self.client.with_options(max_retries=0)
        options = {"model": self.model, "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": user}]}
        if tools:
            options["tools"] = tools
        if output_type:
            options["response_format"] = {"type": "json_schema", "json_schema": {
                "name": output_type.__name__, "schema": output_type.model_json_schema()}}
        for attempt in range(self.retries + 1):
            try:
                reply = await self.client.chat.completions.create(**options)
                message = reply.choices[0].message
                text = message.content or ""
                return ModelReply(text, output_type.model_validate_json(text) if output_type else None,
                    reply.usage.model_dump() if reply.usage else None, message.tool_calls or [])
            except (APIConnectionError, APITimeoutError, APIStatusError) as error:
                if isinstance(error, APIStatusError) and error.status_code not in (408, 429) and error.status_code < 500:
                    raise
                if attempt == self.retries:
                    raise
                await asyncio.sleep(0.25 * (2 ** attempt))

    async def close(self):
        if self.owns_client and self.client is not None:
            await self.client.close()
