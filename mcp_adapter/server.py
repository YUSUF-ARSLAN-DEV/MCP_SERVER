"""MCP server for the pipeline: lets an AI client run site tests and read the verdict.

A thin layer. Each tool is one call to the job API (client.py), so an agent and the web UI use the same engine, the
same sign-in, the same limits and the same per-user privacy. Nothing here tests anything itself.

  PIPELINE_URL        where the API runs                      (default http://127.0.0.1:8000)
  PIPELINE_USERNAME   the account the agent acts as           (required)
  PIPELINE_PASSWORD
  MCP_TRANSPORT       stdio (default) | streamable-http
  MCP_HOST / MCP_PORT for streamable-http                     (default 127.0.0.1:8765)

Run:  python -m mcp_adapter.server
"""
from __future__ import annotations
import functools
import os
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from .client import PipelineClient, PipelineError

INSTRUCTIONS = (
    "Runs AI-assisted smoke tests of a website and reports whether it looks releasable. Start with `list_models` and "
    "`start_run`, poll `run_status` until the status is done, then read `get_report`. A report states a verdict "
    "(GO / CONDITIONAL GO / NO-GO / NO VERDICT) AND a confidence: it checks that pages load and journeys work "
    "mechanically, not that the business logic is right, so say what the confidence line says. Only test sites you "
    "are allowed to test: runs click real buttons on the target."
)
_READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def _guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Turn the API's refusals into a clean tool error an agent can read and act on."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except PipelineError as exc:
            raise ToolError(str(exc)) from exc
    return wrapper


def build_server(client: PipelineClient) -> MCPServer:
    server = MCPServer("website-smoke-tester", instructions=INSTRUCTIONS)

    @server.tool(annotations=_READ)
    @_guard
    def list_models() -> list[dict]:
        """The model configurations saved in the pipeline (name, provider, model, id). Keys are never shown."""
        return [{k: m.get(k) for k in ("id", "name", "provider", "model", "key_set")} for m in client.models()]

    @server.tool()
    @_guard
    def start_run(url: str, model_id: str = "", max_pages: int = 0, probe_max: int = -1, reuse_workspace: bool = False,
                  requirements: str = "") -> dict:
        """Start a test run of a website and return its job id. One run per user at a time.

        model_id: an id from list_models (blank = the server's default model). max_pages: pages to explore (0 = server
        default). probe_max: buttons to click per page (-1 = server default). Steps that need a person (a CAPTCHA, a
        sign-in) are skipped and reported, because an agent cannot solve them.

        requirements: what the site SHOULD do, as plain text (user stories, use cases, a spec). When given, the tested
        journeys come from it, so the report says whether the site does what it should, not just that it loads."""
        job = client.start_run(url, model_id=model_id or None, max_pages=max_pages or None,
                               probe_max=None if probe_max < 0 else probe_max, ask_human=False, reuse_workspace=reuse_workspace,
                               requirements=requirements)
        return {"job_id": job["id"], "status": job["status"], "site": job.get("site")}

    @server.tool(annotations=_READ)
    @_guard
    def run_status(job_id: str) -> dict:
        """Where a run is: status (queued, running, done, failed, cancelled), stage, percent, message, and the error if any."""
        job = client.run(job_id)
        return {k: job.get(k) for k in ("id", "url", "status", "stage", "progress_pct", "message", "error", "finished_at")}

    @server.tool(annotations=_READ)
    @_guard
    def list_runs(limit: int = 10) -> list[dict]:
        """Your most recent runs, newest first."""
        return [{k: j.get(k) for k in ("id", "url", "status", "stage", "progress_pct", "created_at")} for j in client.runs(limit)]

    @server.tool(annotations=_READ)
    @_guard
    def get_report(job_id: str) -> dict:
        """The result of a finished run: verdict, confidence, why, test counts, open defects, other items, coverage, and the report files."""
        return client.report(job_id)

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True))
    @_guard
    def cancel_run(job_id: str) -> dict:
        """Stop a run that is queued or running. A finished run is left as it is."""
        job = client.cancel(job_id)
        return {"id": job["id"], "status": job["status"]}

    return server


def client_from_env(env: dict[str, str] | None = None) -> PipelineClient:
    env = os.environ if env is None else env
    return PipelineClient(env.get("PIPELINE_URL", "http://127.0.0.1:8000"), env.get("PIPELINE_USERNAME", ""),
                          env.get("PIPELINE_PASSWORD", ""))


def main() -> None:
    server = build_server(client_from_env())
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        server.run("streamable-http", host=os.environ.get("MCP_HOST", "127.0.0.1"), port=int(os.environ.get("MCP_PORT", "8765")))
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
