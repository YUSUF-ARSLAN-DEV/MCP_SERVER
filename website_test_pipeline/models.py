from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

@dataclass
class PageInventory:
    url: str
    title: str
    # the <html> element's declared writing direction / language, as captured live - None when the page
    # never sets the attribute at all (the HTML default is then "ltr", applied where this is read, not here)
    dir: str | None = None
    lang: str | None = None
    headings: list[dict[str, Any]] = field(default_factory=list)
    controls: list[dict[str, Any]] = field(default_factory=list)
    accessibility: str = ""
    forms: list[dict[str, Any]] = field(default_factory=list)
    # what clicking a [content] trigger surfaced during the explore probe:
    # [{"trigger": str, "effect": "reveals"|"navigates", "controls": [...], "to": str}]
    revealed: list[dict[str, Any]] = field(default_factory=list)
    # third-party map / media embeds the page is built around (Google Maps canvas,
    # Leaflet, a maps <iframe>) - not driveable, assert only the container is visible:
    # [{"kind": "map", "provider": str, "selector": str|None, "region": str, "big": bool}]
    embeds: list[dict[str, Any]] = field(default_factory=list)
    # the page's MAIN interaction, completed end-to-end by the explorer (a search /
    # filter widget that may not be a <form>): {"action": str, "action_selector": str|None,
    # "steps": [{"kind": "select"|"fill"|"multiselect", "selector": str|None, "name": str, "value": str}],
    # "effect": "results"|"navigates"|"no-visible-result", "results_selector"/"results_role"/
    # "row_count"/"results_text" or "to"}
    primary_flow: dict[str, Any] | None = None
    # login / sign-up walls: a visible password field plus the fields beside it, so a later step can ask a
    # person for them: [{"kind": "login"|"signup", "selector": str|None, "region": str,
    # "fields": [{"type", "name", "label", "autocomplete", "required"}]}]
    auth: list[dict[str, Any]] = field(default_factory=list)

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
