"""Shared test setup: report tests must not launch a real browser just to read its version."""
import os

os.environ.setdefault("WTP_SKIP_BROWSER_PROBE", "1")
