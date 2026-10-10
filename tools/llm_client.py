"""Inject SDK clients; retain all authentication in the existing models module/SDK config."""
import asyncio
import os
from dataclasses import dataclass
from typing import Protocol
from pydantic import BaseModel, ValidationError
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
    # Fact extraction on gpt-6.1-sol: "low" was ~30% faster than the default with identical facts in a replay of a
    # live Q&A extraction (18 s vs 26 s). Navigation decisions keep the model default. OPENAI_EXTRACTION_EFFORT
    # overrides it ("default" leaves it unset); Qwen has its own no-thinking switch.
    extraction_effort = os.environ.get("OPENAI_EXTRACTION_EFFORT", "low")

    def __init__(self, client=None, model=None, retries=1):
        if not 0 <= retries <= 3:
            raise ValueError("retries must be between 0 and 3")
        self.client = client
        self.model = model
        self.extra_body = {}
        self.retries = retries
        self.owns_client = client is None

    async def ask(self, system, user, *, output_type=None, tools=None):
        if self.client is None:
            client, default_model, self.extra_body = models.agent_model()
            self.client = client.with_options(max_retries=0)
            self.model = self.model or default_model
        options = {"model": self.model or models.QWEN_MODEL, "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": user}]}
        if tools:
            options["tools"] = tools
        if self.extra_body:
            options["extra_body"] = self.extra_body
        if (output_type and output_type.__name__.endswith("Extraction") and self.extraction_effort != "default"
                and options["model"] != models.QWEN_MODEL):
            options["reasoning_effort"] = self.extraction_effort
        if output_type:
            options["response_format"] = {"type": "json_schema", "json_schema": {
                "name": output_type.__name__, "schema": output_type.model_json_schema()}}
        repaired = False
        for attempt in range(self.retries + 2):
            try:
                reply = await self.client.chat.completions.create(**options)
                message = reply.choices[0].message
                text = message.content or ""
                try:
                    structured = output_type.model_validate_json(text) if output_type else None
                except ValidationError as error:
                    if repaired:
                        raise
                    # One corrective round trip instead of discarding the whole step.
                    repaired = True
                    options["messages"] = [*options["messages"], {"role": "assistant", "content": text},
                        {"role": "user", "content": "That JSON does not match the schema: "
                         f"{str(error)[:1500]}\nReturn the corrected JSON only."}]
                    continue
                return ModelReply(text, structured,
                    reply.usage.model_dump() if reply.usage else None, message.tool_calls or [])
            except (APIConnectionError, APITimeoutError, APIStatusError) as error:
                if isinstance(error, APIStatusError) and error.status_code not in (408, 429) and error.status_code < 500:
                    raise
                if attempt >= self.retries:
                    raise
                await asyncio.sleep(0.25 * (2 ** attempt))

    async def close(self):
        if self.owns_client and self.client is not None:
            await self.client.close()
