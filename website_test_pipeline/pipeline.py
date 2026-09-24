"""`flows run`: the flow pipeline as one command, with each stage still runnable on its own.

    intents  ->  expand  ->  verify  ->  flowgen  ->  execute
    (AI writes   (AI + code   (real       (template   (pytest;
     sentences)   -> steps)    browser)    -> tests)   results feed
                                                       the ratings)

Two stages need the model (intents, expand). When it cannot be reached (working from home, a VPN that
is down, an outage) they are skipped with a clear line instead of hanging on retries, and the stages
that need only a browser and the saved files still run: verify, flowgen, execute. A stage that fails does
not silently hide the others; the summary at the end says what happened to each one and what is
still untested (see coverage.py).

Exit code: 0 all fine, 1 the generated tests ran and some failed, 2 a stage could not run (missing
prerequisite, unreadable file, crash) or there are no explored pages yet.
"""
from __future__ import annotations
import os
import socket
import subprocess
import sys
from urllib.parse import urlsplit

from .authflow import has_session, session_path
from .coverage import compute_coverage, render_coverage
from .expand import run_expand
from .flowgen import run_flowgen
from .flowresults import feed_results
from .flows import FlowsFileError, load_flows
from .intents import run_intents
from .runner import run_verify

STAGES = ("intents", "expand", "verify", "flowgen", "execute")
MODEL_STAGES = {"intents", "expand"}


def model_reachable(url: str, timeout: float = 3.0) -> bool:
    """Can we open a connection to the model endpoint? A quick check so an unreachable model skips its
    stages at once instead of after every retry and back-off. Only connectivity is tested, never a request."""
    try:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if not parts.hostname:
            return False
        with socket.create_connection((parts.hostname, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def run_execute(settings, log) -> int:
    """Run every generated spec under pytest and feed the flow results back into flow_ratings.json."""
    env = {**os.environ, "WTP_ARTIFACTS": str(settings.artifacts_dir)}
    if has_session(settings):
        env["WTP_STORAGE_STATE"] = str(session_path(settings))
    result = subprocess.run([sys.executable, "-m", "pytest", str(settings.tests_dir), "-q"], cwd=settings.root, env=env)
    feed_results(settings, log)
    return result.returncode


def parse_stage_list(text: str) -> list[str]:
    return [w.strip().lower() for w in (text or "").replace(";", ",").split(",") if w.strip()]


def _coverage_line(settings) -> str:
    try:
        from .sitemap import load_inventories
        inventories = load_inventories(settings.artifacts_dir)
        flows = load_flows(settings.flows_file)["flows"]
        text = render_coverage(compute_coverage(inventories, flows))
        return text.split("\n", 1)[0]
    except (FlowsFileError, OSError, ValueError):
        return ""


def run_chain(settings, urls: list[str], log, skip=(), only=(), client_factory=None) -> int:
    """Run the stages in order. `skip` / `only` are stage names; `client_factory()` builds the model client
    (a parameter so tests need no network)."""
    unknown = [s for s in [*skip, *only] if s not in STAGES]
    if unknown:
        log.error("flows run: unknown stage %s (stages: %s)", ", ".join(unknown), ", ".join(STAGES))
        return 2
    stages = [s for s in STAGES if s not in skip and (not only or s in only)]
    if not stages:
        log.error("flows run: nothing to run (every stage was skipped)")
        return 2
    results: list[tuple[str, str]] = []
    exit_code = 0
    client = None
    reachable = None
    site_down = False
    model_down = False
    for stage in STAGES:
        if stage in skip or (only and stage not in only):
            results.append((stage, "skipped (as asked)"))
            continue
        try:
            if stage in MODEL_STAGES:
                if reachable is None:
                    reachable = model_reachable(settings.api_url)
                    if not reachable:
                        log.warning("flows run: the model at %s is not reachable from here (offline? VPN?) - "
                                    "skipping the stages that need it; the rest still run", urlsplit(settings.api_url).hostname)
                if not reachable:
                    results.append((stage, "skipped: model not reachable"))
                    continue
                if model_down:
                    results.append((stage, "skipped: the model was unavailable in the previous stage"))
                    continue
                if client is None:
                    client = client_factory() if client_factory else _default_client(settings, log)
                code = run_intents(settings, urls, client, log) if stage == "intents" else run_expand(settings, urls, client, log)
                if code == 2:
                    results.append((stage, "stopped: no explored pages - run `explore` first"))
                    return _finish(settings, log, results, 2)
                if code == 4:
                    model_down = True
                    results.append((stage, "skipped: the model is unavailable (nothing was changed); run it again when it is back"))
                    continue
                results.append((stage, "ok" if code == 0 else "did not complete (see the log above); it can be run again"))
                continue
            if stage == "verify":
                code = run_verify(settings, log)
                if code == 3:
                    site_down = True
                    results.append((stage, "skipped: the site could not be reached (nothing was recorded)"))
                else:
                    results.append((stage, "ok" if code == 0 else "nothing to verify (no flows yet)"))
            elif stage == "flowgen":
                code = run_flowgen(settings, log)
                if code == 1:
                    results.append((stage, "failed: the flows file could not be read"))
                    exit_code = max(exit_code, 2)
                else:
                    results.append((stage, "ok" if code == 0 else "nothing to generate (no flows yet)"))
            elif site_down:
                results.append((stage, "skipped: the site could not be reached"))
            else:
                code = run_execute(settings, log)
                results.append((stage, "ok" if code == 0 else "ran; some tests failed"))
                if code != 0:
                    exit_code = max(exit_code, 1)
        except Exception as exc:
            log.error("flows run: %s crashed (%s)", stage, str(exc).splitlines()[0][:160] if str(exc) else exc.__class__.__name__)
            results.append((stage, "crashed: " + (str(exc).splitlines()[0][:80] if str(exc) else exc.__class__.__name__)))
            exit_code = max(exit_code, 2)
    return _finish(settings, log, results, exit_code)


def _default_client(settings, log):
    from .llm import ModelClient
    return ModelClient(settings, log)


def _finish(settings, log, results: list[tuple[str, str]], exit_code: int) -> int:
    for stage, status in results:
        log.info("FLOWS RUN %-8s %s", stage, status)
    line = _coverage_line(settings)
    if line:
        log.info("FLOWS RUN %s", line)
        log.info("FLOWS RUN details: `flows coverage`; open questions: `flows list --status candidate`")
    return exit_code
