"""A small client for the pipeline's own job API (api/main.py).

The MCP server adds no testing logic of its own: every tool is one call to the API that already exists, so the
web UI and an AI agent drive the same engine, with the same sign-in, limits and per-user privacy. This module has
no MCP dependency, so it can be tested without one.
"""
from __future__ import annotations
import base64
import json
from typing import Any

import httpx

REPORT_DATA = "artifacts/report/report-data.json"
# What an agent needs from a finished run, never the whole document: the verdict and why, the counts, the open defects.
_DEFECT_FIELDS = ("id", "category", "severity", "status", "title", "url", "business_impact")


class PipelineError(Exception):
    """The API refused or could not do what was asked; the message is safe to show an agent."""


class PipelineClient:
    def __init__(self, base_url: str, username: str = "", password: str = "", *, transport: httpx.BaseTransport | None = None,
                 timeout: float = 30.0):
        self.username, self.password = username, password
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)
        self._signed_in = False

    # -- plumbing -------------------------------------------------------------------------------------------
    def _sign_in(self) -> None:
        if not (self.username and self.password):
            raise PipelineError("The adapter has no account: set PIPELINE_USERNAME and PIPELINE_PASSWORD.")
        response = self._send("POST", "/api/login", json={"username": self.username, "password": self.password})
        if response.status_code != 200:
            raise PipelineError(f"Sign-in failed ({response.status_code}): {self._detail(response)}")
        self._signed_in = True

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        try:
            detail = response.json().get("detail")
            return detail if isinstance(detail, str) else json.dumps(detail)
        except Exception:
            return (response.text or "").strip()[:200] or "no detail"

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:     # connection refused, timeout ...: say where, not just "Error executing tool"
            raise PipelineError(f"Could not reach the pipeline API at {self._http.base_url}: {type(exc).__name__}. "
                                "Is it running (docker compose up)?") from exc

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self._signed_in:
            self._sign_in()
        response = self._send(method, path, **kwargs)
        if response.status_code == 401:                       # the session expired: sign in once more, then retry once
            self._sign_in()
            response = self._send(method, path, **kwargs)
        if response.status_code >= 400:
            raise PipelineError(f"{method} {path} -> {response.status_code}: {self._detail(response)}")
        return response

    # -- what the tools use -----------------------------------------------------------------------------------
    def health(self) -> dict:
        return self._send("GET", "/api/health").json()

    def models(self) -> list[dict]:
        return self._request("GET", "/api/models").json()

    def start_run(self, url: str, *, model_id: str | None = None, max_pages: int | None = None, probe_max: int | None = None,
                  ask_human: bool = False, reuse_workspace: bool = False, requirements: str = "",
                  requirements_name: str = "user-stories.md") -> dict:
        body: dict[str, Any] = {"url": url, "options": {"ask_human": ask_human, "reuse_workspace": reuse_workspace}}
        if requirements.strip():     # what the site SHOULD do, as text; the run's journeys then come from it
            body["requirements"] = [{"name": requirements_name, "data": base64.b64encode(requirements.encode("utf-8")).decode("ascii")}]
        if max_pages is not None:
            body["options"]["max_pages"] = max_pages
        if probe_max is not None:
            body["options"]["probe_max"] = probe_max
        if model_id:
            body["model_id"] = model_id
        return self._request("POST", "/api/jobs", json=body).json()

    def runs(self, limit: int = 20) -> list[dict]:
        return self._request("GET", "/api/jobs", params={"limit": limit}).json()

    def run(self, job_id: str) -> dict:
        return self._request("GET", f"/api/jobs/{job_id}").json()

    def cancel(self, job_id: str) -> dict:
        return self._request("POST", f"/api/jobs/{job_id}/cancel").json()

    def files(self, job_id: str) -> list[dict]:
        return self._request("GET", f"/api/jobs/{job_id}/files").json()

    def report(self, job_id: str) -> dict:
        """The distilled verdict of a finished run, from the report's own machine-readable data."""
        job = self.run(job_id)
        if job["status"] not in {"done"}:
            raise PipelineError(f"Run {job_id} is {job['status']} ({job.get('stage') or 'no stage'}), so there is no report yet.")
        data = self._request("GET", f"/api/jobs/{job_id}/files/{REPORT_DATA}").json()
        counts = data.get("counts", {})
        return {
            "run_id": data.get("run_id"), "site": data.get("site"), "verdict": data.get("recommendation"),
            "confidence": data.get("confidence") or {},
            "conditions": data.get("conditions", []),
            "tests": {k: counts.get(k) for k in ("executed_tests", "passed", "failed", "skipped", "pass_rate_percent")},
            "open_defects": [{k: d.get(k) for k in _DEFECT_FIELDS} for d in data.get("defects", [])
                             if d.get("category") == "defect" and d.get("status") == "Open"],
            "other_items": {c: sum(d.get("category") == c for d in data.get("defects", []))
                            for c in ("triage", "unverified", "limitation", "test_defect", "tooling")},
            "coverage": {k: (data.get("coverage") or {}).get(k) for k in ("pages_total", "pages_visited", "controls_total",
                                                                           "controls_touched", "controls_swept")},
            "files": [f["path"] for f in self.files(job_id)],
        }

    def close(self) -> None:
        self._http.close()
