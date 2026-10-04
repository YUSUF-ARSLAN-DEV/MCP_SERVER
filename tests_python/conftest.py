"""Shared test setup: report tests must not launch a real browser just to read its version."""
import os

os.environ.setdefault("WTP_SKIP_BROWSER_PROBE", "1")
os.environ.setdefault("REPORT_OWNER", "Test Owner")   # the report refuses to name nobody; tests that check that delete it
