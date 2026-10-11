"""Model access: Qwen (OpenAI-compatible) through the run's metering proxy, with retries and a call log."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

QWEN_PRICE_IN = 0.15 / 1e6
QWEN_PRICE_CACHED = 0.016 / 1e6
QWEN_PRICE_OUT = 0.47 / 1e6


@dataclass
class Usage:
    calls: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    by_agent: dict[str, int] = field(default_factory=dict)

    @property
    def cost(self) -> float:
        fresh = self.prompt_tokens - self.cached_tokens
        return fresh * QWEN_PRICE_IN + self.cached_tokens * QWEN_PRICE_CACHED + self.completion_tokens * QWEN_PRICE_OUT


class LLM:
    def __init__(self, log, max_parallel: int = 16):
        base = os.environ.get("QWEN_BASE_URL", "http://127.0.0.1:8765/qwen/v1")
        key = os.environ.get("QWEN_API_KEY", "local")
        self.client = AsyncOpenAI(base_url=base, api_key=key, timeout=180, max_retries=0)
        self.model = os.environ.get("JANTEIROS_MODEL", "qwen")
        self.sem = asyncio.Semaphore(max_parallel)
        self.usage = Usage()
        self.log = log

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        agent: str,
        thinking: bool = False,
        max_tokens: int = 2500,
        tool_choice: str | None = "required",
        json_mode: bool = False,
        timeout: float = 75,
    ):
        """One chat completion with retries; returns the message (content + tool_calls)."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.0 if not thinking else 0.6,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": thinking}},
        }
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        delay = 1.0
        last_err = ""
        for attempt in range(7):
            t0 = time.monotonic()
            try:
                async with self.sem:
                    # a stuck call is abandoned early and retried; later attempts get more time
                    resp = await asyncio.wait_for(self.client.chat.completions.create(**kwargs), timeout=timeout * (1 + 0.5 * min(attempt, 3)))
            except (APIConnectionError, APITimeoutError, asyncio.TimeoutError) as e:
                last_err = f"{type(e).__name__}: {e}"
            except APIStatusError as e:
                last_err = f"HTTP {e.status_code}: {str(e)[:200]}"
                if e.status_code in (400, 401, 403, 404) and attempt >= 1:
                    # a malformed request will not improve; drop tool_choice once, then give up
                    if "tool_choice" in kwargs:
                        kwargs.pop("tool_choice")
                        continue
                    break
            else:
                dt = time.monotonic() - t0
                u = resp.usage
                cached = 0
                if u is not None:
                    details = getattr(u, "prompt_tokens_details", None)
                    cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
                    self.usage.prompt_tokens += u.prompt_tokens or 0
                    self.usage.completion_tokens += u.completion_tokens or 0
                    self.usage.cached_tokens += cached
                self.usage.calls += 1
                self.usage.seconds += dt
                self.usage.by_agent[agent] = self.usage.by_agent.get(agent, 0) + 1
                self.log(
                    "llm_call",
                    agent=agent,
                    seconds=round(dt, 2),
                    prompt=u.prompt_tokens if u else None,
                    cached=cached,
                    completion=u.completion_tokens if u else None,
                    attempt=attempt,
                )
                if not resp.choices:
                    last_err = "empty choices"
                else:
                    return resp.choices[0].message
            self.usage.failures += 1
            self.log("llm_error", agent=agent, attempt=attempt, error=last_err[:300])
            await asyncio.sleep(delay)
            delay = min(delay * 1.8, 12)
        raise RuntimeError(f"model call failed after retries: {last_err}")

    async def json_call(self, system: str, user: str, *, agent: str, thinking: bool = False, max_tokens: int = 3000) -> dict:
        """Ask for a JSON object (no tools) and parse it, tolerating fences."""
        msg = await self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            None,
            agent=agent,
            thinking=thinking,
            max_tokens=max_tokens,
            tool_choice=None,
            json_mode=True,
        )
        return parse_json(msg.content or "")


def parse_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"no JSON object in model reply: {text[:200]}")
    return json.loads(t[start: end + 1])
