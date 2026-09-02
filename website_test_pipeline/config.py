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
    model_timeout_ms: int = field(default_factory=lambda: _int("MODEL_TIMEOUT_MS", 300000, 1000, 900000))
    model_retries: int = field(default_factory=lambda: _int("MODEL_RETRIES", 4, 0, 10))
    retry_base_ms: int = field(default_factory=lambda: _int("RETRY_BASE_MS", 3000, 100, 120000))
    navigation_timeout_ms: int = field(default_factory=lambda: _int("NAV_TIMEOUT_MS", 60000, 1000, 300000))
    # per-site workspace: SITE env, else the SEED_URL host, else "default"
    site: str = field(default_factory=lambda: os.getenv("SITE", "").strip() or _host(os.getenv("SEED_URL", "")) or "default")
    workspace: Path = RUNS
    urls_file: Path = RUNS
    seeds_file: Path = RUNS
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
        override = os.getenv("URLS_FILE", "")
        chosen = Path(override) if override and Path(override).is_absolute() else base / "urls.txt"
        object.__setattr__(self, "urls_file", chosen)

    def prepare(self) -> None:
        self.tests_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.urls_file.parent.mkdir(parents=True, exist_ok=True)
