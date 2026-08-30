from dataclasses import dataclass, field
from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]

def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.getenv(name, default))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value

@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    api_url: str = field(default_factory=lambda: os.getenv("API_URL", "https://llm-1.d4done.com/v1/chat/completions"))
    model: str = field(default_factory=lambda: os.getenv("MODEL_NAME", "google/gemma-4-26b-a4b-qat"))
    api_key: str = field(default_factory=lambda: os.getenv("API_KEY", ""))
    seed_url: str = field(default_factory=lambda: os.getenv("SEED_URL", ""))
    crawl_max_depth: int = field(default_factory=lambda: _int("CRAWL_MAX_DEPTH", 3, 0, 20))
    crawl_max_pages: int = field(default_factory=lambda: _int("CRAWL_MAX_PAGES", 100, 1, 5000))
    headless: bool = field(default_factory=lambda: os.getenv("HEADLESS", "true").lower() != "false")
    model_timeout_ms: int = field(default_factory=lambda: _int("MODEL_TIMEOUT_MS", 300000, 1000, 900000))
    model_retries: int = field(default_factory=lambda: _int("MODEL_RETRIES", 4, 0, 10))
    retry_base_ms: int = field(default_factory=lambda: _int("RETRY_BASE_MS", 3000, 100, 120000))
    navigation_timeout_ms: int = field(default_factory=lambda: _int("NAV_TIMEOUT_MS", 60000, 1000, 300000))
    urls_file: Path = field(default_factory=lambda: ROOT / os.getenv("URLS_FILE", "urls.txt"))
    tests_dir: Path = field(default_factory=lambda: ROOT / "python_tests")
    artifacts_dir: Path = field(default_factory=lambda: ROOT / "python_artifacts")

    def prepare(self) -> None:
        self.tests_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
