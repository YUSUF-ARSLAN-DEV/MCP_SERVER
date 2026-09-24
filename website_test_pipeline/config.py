from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
import os
from dotenv import load_dotenv

load_dotenv(override=True)  # .env is the source of truth, even over a stale shell env

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"

def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.getenv(name, default))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value

def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = (os.getenv(name, default) or default).strip().lower()
    if value not in allowed:
        raise ValueError(f"{name} must be one of: {', '.join(allowed)}")
    return value

def _host(url: str) -> str:
    try:
        return (urlsplit(url).netloc or "").lower()
    except ValueError:
        return ""

@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    api_url: str = field(default_factory=lambda: os.getenv("API_URL", "https://llm-1.d4done.com/v1/chat/completions"))
    model: str = field(default_factory=lambda: os.getenv("MODEL_NAME", "google/gemma-4-26b-a4b-qat"))
    api_key: str = field(default_factory=lambda: os.getenv("API_KEY", ""))
    seed_url: str = field(default_factory=lambda: os.getenv("SEED_URL", ""))
    crawl_max_depth: int = field(default_factory=lambda: _int("CRAWL_MAX_DEPTH", 3, 0, 20))
    crawl_max_pages: int = field(default_factory=lambda: _int("CRAWL_MAX_PAGES", 100, 1, 5000))
    # explore: after the static snapshot, click up to N [content] triggers and
    # record what each one surfaced (0 disables the interaction probe entirely).
    explore_probe_max: int = field(default_factory=lambda: _int("EXPLORE_PROBE_MAX", 5, 0, 20))
    headless: bool = field(default_factory=lambda: os.getenv("HEADLESS", "true").lower() != "false")
    # login / sign-up walls: "auto" asks a person for credentials when a password form is found (interactive
    # runs only); "none" never asks and only reports the wall as not tested.
    auth_mode: str = field(default_factory=lambda: _choice("AUTH_MODE", "auto", ("auto", "none")))
    # which login to use when a site has more than one (admin / customer ...); names the saved session and .env keys
    auth_account: str = field(default_factory=lambda: (os.getenv("AUTH_ACCOUNT", "default").strip() or "default").lower())
    # "off" sends enable_thinking=false (right for non-reasoning models like qwen3-coder); "default" sends nothing,
    # which is what a reasoning model such as GLM needs to keep its reasoning out of the reply text
    model_thinking: str = field(default_factory=lambda: _choice("MODEL_THINKING", "off", ("off", "default")))
    model_max_tokens: int = field(default_factory=lambda: _int("MODEL_MAX_TOKENS", 3072, 256, 65536))
    model_timeout_ms: int = field(default_factory=lambda: _int("MODEL_TIMEOUT_MS", 300000, 1000, 900000))
    model_retries: int = field(default_factory=lambda: _int("MODEL_RETRIES", 4, 0, 10))
    retry_base_ms: int = field(default_factory=lambda: _int("RETRY_BASE_MS", 3000, 100, 120000))
    navigation_timeout_ms: int = field(default_factory=lambda: _int("NAV_TIMEOUT_MS", 60000, 1000, 300000))
    # per-site workspace: SITE env, else the SEED_URL host, else "default"
    site: str = field(default_factory=lambda: os.getenv("SITE", "").strip() or _host(os.getenv("SEED_URL", "")) or "default")
    workspace: Path = RUNS
    urls_file: Path = RUNS
    seeds_file: Path = RUNS
    flows_file: Path = RUNS
    ratings_file: Path = RUNS
    intents_file: Path = RUNS
    heuristics_file: Path = RUNS
    tests_dir: Path = RUNS
    artifacts_dir: Path = RUNS

    def __post_init__(self) -> None:
        base = RUNS / self.site
        object.__setattr__(self, "workspace", base)
        object.__setattr__(self, "tests_dir", base / "tests")
        object.__setattr__(self, "artifacts_dir", base / "artifacts")
        # operator-supplied URLs (deep links the crawler can't reach with real
        # params, e.g. a map result or a wizard step) merged into `crawl` output.
        object.__setattr__(self, "seeds_file", base / "seeds.txt")
        object.__setattr__(self, "flows_file", base / "flows.json")
        object.__setattr__(self, "ratings_file", base / "flow_ratings.json")
        object.__setattr__(self, "intents_file", base / "intents.json")
        object.__setattr__(self, "heuristics_file", base / "heuristics.json")
        override = os.getenv("URLS_FILE", "")
        chosen = Path(override) if override and Path(override).is_absolute() else base / "urls.txt"
        object.__setattr__(self, "urls_file", chosen)

    def prepare(self) -> None:
        self.tests_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.urls_file.parent.mkdir(parents=True, exist_ok=True)
