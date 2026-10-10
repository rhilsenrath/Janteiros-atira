import os
from typing import Literal
from pydantic import Field
from core.contracts import Record

class RunConfig(Record):
    mode: Literal["production", "mock"] = "production"
    task_timeout: float = Field(default=120, gt=0)
    deadline_reserve: float = Field(default=60, ge=0)
    retries: int = Field(default=1, ge=0, le=3)
    max_reinvestigations: int = Field(default=2, ge=0, le=8)

    @classmethod
    def from_env(cls):
        return cls(mode=os.environ.get("ATIRA_MODE", "production"))
