import os
from typing import Literal
from pydantic import Field
from core.contracts import Record

class RunConfig(Record):
    mode: Literal["production", "test"] = "production"
    task_timeout: float = Field(default=300, gt=0)
    deadline_reserve: float = Field(default=60, ge=0)
    retries: int = Field(default=1, ge=0, le=3)
    max_reinvestigations: int = Field(default=2, ge=0, le=8)

    max_auth_steps: int = Field(default=12, ge=1, le=30)
    max_navigation_steps: int = Field(default=40, ge=1, le=100)
    action_timeout_ms: int = Field(default=10000, ge=100, le=60000)
    observation_chars: int = Field(default=12000, ge=500, le=24000)
    enable_submission: bool = False

    @classmethod
    def from_env(cls):
        if os.environ.get("ATIRA_MODE", "production") != "production":
            raise ValueError("Production entry point does not support mock or test mode")
        return cls(enable_submission=os.environ.get("ATIRA_ENABLE_SUBMISSION") == "1")
