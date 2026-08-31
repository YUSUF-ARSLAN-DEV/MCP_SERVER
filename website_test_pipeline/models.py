from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

@dataclass
class PageInventory:
    url: str
    title: str
    headings: list[dict[str, Any]] = field(default_factory=list)
    controls: list[dict[str, Any]] = field(default_factory=list)
    accessibility: str = ""
    forms: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass
class GenerationResult:
    url: str
    status: str
    spec_path: str | None = None
    error: str | None = None
    attempts: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass
class RunManifest:
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    urls: dict[str, dict[str, Any]] = field(default_factory=dict)
    finished_at: str | None = None

    def finish(self) -> None:
        self.finished_at = datetime.now(timezone.utc).isoformat()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
