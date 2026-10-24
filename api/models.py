from __future__ import annotations
from typing import Literal

from pydantic import BaseModel, Field


class JobOptions(BaseModel):
    max_pages: int | None = Field(default=None, ge=1, le=500, description="pages to explore (capped by the server)")
    probe_max: int | None = Field(default=None, ge=0, le=20, description="buttons to click per page")
    reuse_workspace: bool = Field(default=False, description="keep working in this site's folder (flows, ratings) instead of a fresh one")
    ask_human: bool = Field(default=True, description="ask the person for a CAPTCHA code or sign-in details; false skips those steps")


class ModelChoice(BaseModel):
    """Which model this run uses. Omit it to use the server's own configuration. The key is never stored in the database
    or echoed back: it goes to a private file the run reads once and the server deletes when the run ends."""
    provider: Literal["openai", "anthropic", "google"]
    model: str = Field(min_length=1, max_length=200, pattern=r"^[\w./:@+-]+$")
    api_url: str = Field(default="", max_length=2048, description="blank = the provider's own address (required for openai)")
    api_key: str = Field(default="", max_length=500, repr=False)


class ProfileIn(BaseModel):
    """A saved model configuration: yours alone. The key is stored encrypted and can never be read back."""
    name: str = Field(min_length=1, max_length=60)
    provider: Literal["openai", "anthropic", "google"]
    model: str = Field(min_length=1, max_length=200, pattern=r"^[\w./:@+-]+$")
    api_url: str = Field(default="", max_length=2048)
    api_key: str = Field(default="", max_length=500, repr=False)


class ProfilePatch(BaseModel):
    """Only what is given changes. api_key: leave it out to keep the saved key, send a new one to replace it."""
    name: str | None = Field(default=None, min_length=1, max_length=60)
    provider: Literal["openai", "anthropic", "google"] | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"^[\w./:@+-]+$")
    api_url: str | None = Field(default=None, max_length=2048)
    api_key: str | None = Field(default=None, max_length=500, repr=False)


class ProfileOut(BaseModel):
    id: str
    name: str
    provider: str
    model: str
    api_url: str
    key_set: bool
    created_at: str
    updated_at: str

    @classmethod
    def of(cls, row: dict) -> "ProfileOut":
        return cls(id=row["id"], name=row["name"], provider=row["provider"], model=row["model"], api_url=row["api_url"],
                   key_set=bool(row["key_enc"]), created_at=row["created_at"], updated_at=row["updated_at"])


class RequirementFile(BaseModel):
    """What the site should do, in a person's words: user stories, use cases, a spec (txt, md, docx, pdf). base64, <= 6 MB."""
    name: str = Field(min_length=1, max_length=120)
    data: str = Field(min_length=1, max_length=8_500_000, repr=False)


class JobCreate(BaseModel):
    url: str
    options: JobOptions = JobOptions()
    requirements: list[RequirementFile] = Field(default_factory=list, max_length=5)
    model: ModelChoice | None = None                     # a one-off choice for this run
    model_id: str | None = None                          # or one of your saved model configurations


class Login(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=400, repr=False)


class SignupIn(BaseModel):
    """Anyone may create their own account."""
    username: str = Field(max_length=64)
    password: str = Field(max_length=400, repr=False)


class PasswordChange(BaseModel):
    current: str = Field(max_length=400, repr=False)
    new: str = Field(max_length=400, repr=False)




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
    summary: dict | None = None                    # what the run came to (verdict, tests, duration, model ...), once it has ended

    @classmethod
    def of(cls, job: dict) -> "JobOut":
        return cls(**{name: job.get(name) for name in cls.model_fields})
