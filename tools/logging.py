"""Allowlisted diagnostics. No model text, action values, URLs, or exception strings."""
import json
import time
import re

class RunLogger:
    def __init__(self, sink=print):
        self.started = time.monotonic()
        self.sink = sink

    def emit(self, event, *, agent=None, status=None, operation=None, count=None, error_type=None, evidence_refs=None):
        payload = {"event": event, "elapsed_s": round(time.monotonic() - self.started, 2)}
        for key, value in {"agent": agent, "status": status, "operation": operation,
                           "count": count, "error_type": error_type}.items():
            if value is not None:
                payload[key] = value
        if evidence_refs is not None:
            if not all(re.fullmatch(r"(?:tender|supplier|erp|mail):[0-9a-f]{32}", ref) for ref in evidence_refs):
                raise ValueError("Log only application-generated observation references")
            payload["evidence_refs"] = evidence_refs
        self.sink(json.dumps(payload, ensure_ascii=False))

def redact(text, secrets):
    for value in sorted(set(secrets), key=len, reverse=True):
        if value:
            text = text.replace(value, "[REDACTED]")
    return text
