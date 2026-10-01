from __future__ import annotations
from typing import Literal

from pydantic import BaseModel, Field


class JobOptions(BaseModel):
    max_pages: int | None = Field(default=None, ge=1, le=500, description="pages to explore (capped by the server)")
    probe_max: int | None = Field(default=None, ge=0, le=20, description="buttons to click per page")
    reuse_workspace: bool = Field(default=False, description="keep working in this site's folder (flows, ratings) instead of a fresh one")
    ask_human: bool = Field(default=True, description="ask the person for a CAPTCHA code or sign-in details; false skips those steps")


class JobCreate(BaseModel):
    url: str
    options: JobOptions = JobOptions()


class Login(BaseModel):
    code: str


class HumanAnswerIn(BaseModel):
    """What the person did about the open question. `values` is never stored by the API - it is handed to the run once."""
    seq: int | None = None
    action: Literal["submit", "skip", "refresh"]
    code: str = Field(default="", max_length=64)
    values: dict[str, str] = Field(default_factory=dict, max_length=50)


class JobOut(BaseModel):
    id: str
    url: str
    site: str
    status: str
    stage: str
    progress_pct: int
    message: str
    error: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None

    @classmethod
    def of(cls, job: dict) -> "JobOut":
        return cls(**{name: job.get(name) for name in cls.model_fields})
