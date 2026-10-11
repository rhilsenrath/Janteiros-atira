"""The run contract: read the task (brief, logins, schema) from the platform, or the scouting data locally."""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

UA = "janteiros-rfq/0.1 (+https://github.com)"


@dataclass
class RunContext:
    brief: str
    access: list[dict]
    submission: dict
    answer_schema: dict
    expires_at: float | None  # epoch seconds
    started: float  # monotonic
    local: bool

    def seconds_left(self) -> float:
        if self.expires_at is None:
            return 20 * 60 - (time.monotonic() - self.started)
        return self.expires_at - time.time()


def _get(url: str, token: str | None = None) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "accept": "application/json"})
    if token:
        req.add_header("x-api-key", token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _expiry(v) -> float | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def load() -> RunContext:
    started = time.monotonic()
    api = os.environ.get("CHALLENGE_API_URL", "https://ehl-challenge.atira.ai").rstrip("/")
    token = os.environ.get("CHALLENGE_RUN_TOKEN")
    if token:
        for attempt in range(5):
            try:
                data = _get(f"{api}/v1/runs/current", token)
                break
            except Exception:  # noqa: BLE001 - retry the platform a few times
                if attempt == 4:
                    raise
                time.sleep(2 + attempt)
        exp = _expiry(data.get("expires_at"))
        return RunContext(data["brief"], data["access"], data.get("submission") or {}, data.get("answer_schema") or {},
                          exp, started, False)
    raw = os.environ.get("CHALLENGE_SCOUTING")
    data = json.loads(raw) if raw else _get(os.environ.get("CHALLENGE_SCOUTING_URL", f"{api}/v1/challenges/rfq/scouting"))
    return RunContext(data["brief"], data["access"], data.get("submission") or {}, data.get("answer_schema") or {},
                      None, started, True)
