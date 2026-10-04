import contextlib
import http.server
import logging
import socketserver
import threading

import pytest

from website_test_pipeline import runner
from website_test_pipeline.review import render_show, _rating_line
from website_test_pipeline.runner import apply_result, find_replacement, run_flow, run_verify

LOG = logging.getLogger("test_heal")


@contextlib.contextmanager
def _serve(pages: dict[str, bytes]):
    """A tiny local site: {path: html} -> base URL. Real navigation and real DOM, so _locate / _do_step /
    find_replacement run exactly as they do against a live site, not against a hand-guessed fake Page."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = pages.get(self.path, b"<html><body>Not found</body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield base
    finally:
        server.shutdown()


@pytest.fixture()
def real_page():
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        pw = sync_api.sync_playwright().start()
        browser = pw.chromium.launch()
    except Exception as exc:                       # no browser installed here
        pytest.skip(f"no Chromium: {exc}")
    page = browser.new_page()
    yield page
    browser.close()
    pw.stop()


def _spy_do_step(monkeypatch):
    """Let _do_step run for real, but record which (selector, name) it was called with."""
    calls = []
    original = runner._do_step

    def spy(page, step, seen=None):
        calls.append((step.get("selector"), step.get("name")))
        return original(page, step, seen)

    monkeypatch.setattr(runner, "_do_step", spy)
    return calls


def _c(name, tag="button", selector=None, **extra):
    return dict({"name": name, "tag": tag, "selector": selector, "hidden": False}, **extra)


# ------------------------------------------------------------------ the matcher is conservative

def test_the_same_name_with_a_new_selector_is_the_same_control():
    step = {"kind": "click", "selector": "#old-id", "name": "Search"}
    rep, how = find_replacement(step, [_c("Search", selector="#new-id"), _c("Home", selector="#home")])
    assert rep == {"selector": "#new-id", "name": "Search", "role": "button"} and "selector or role changed" in how


def test_names_compare_ignoring_case_spacing_and_the_40_character_cut():
    step = {"kind": "click", "selector": None, "name": "use our interactive map to find you"[:40]}
    long_name = "Use our  Interactive Map to find your nearest frequency"
    rep, _ = find_replacement(step, [_c(long_name, tag="a", selector="#map-link")])
    assert rep["selector"] == "#map-link" and rep["role"] == "link" and rep["name"] == long_name[:40]


def test_a_relabelled_control_is_found_when_it_is_nearly_the_same_name():
    rep, how = find_replacement({"kind": "click", "selector": None, "name": "Search"}, [_c("Search now", selector="#go"), _c("Menu")])
    assert rep["name"] == "Search now" and 'renamed (was "Search")' in how


def test_a_completely_different_control_is_never_used():
    rep, why = find_replacement({"kind": "click", "selector": None, "name": "Search"}, [_c("Subscribe Now"), _c("Login")])
    assert rep is None and "no control on the page looks like it" in why


def test_two_candidates_mean_no_healing_because_a_wrong_guess_passes_for_the_wrong_reason():
    step = {"kind": "click", "selector": None, "name": "Search"}
    rep, why = find_replacement(step, [_c("Search", selector="#a"), _c("Search", selector="#b")])
    assert rep is None and "several controls have that name" in why
    rep, why = find_replacement(step, [_c("Search now", selector="#a"), _c("Search all", selector="#b")])
    assert rep is None and "several controls look similar" in why


def test_the_kind_of_step_limits_what_can_stand_in():
    select_step = {"kind": "select", "selector": "#gone", "name": "Country"}
    assert find_replacement(select_step, [_c("Country", tag="div", selector="#x")])[0] is None
    assert find_replacement(select_step, [_c("Country", tag="select", selector="#c")])[0]["selector"] == "#c"
    fill_step = {"kind": "fill", "selector": "#gone", "name": "Email"}
    assert find_replacement(fill_step, [_c("Email", tag="input", selector="#e", type="text")])[0]["role"] == "textbox"


def test_hidden_controls_and_volatile_ids_are_not_used_as_selectors():
    step = {"kind": "click", "selector": "#old", "name": "Search"}
    assert find_replacement(step, [_c("Search", selector="#s", hidden=True)])[0] is None
    rep, _ = find_replacement(step, [_c("Search", selector="#radix-123456", volatile_id=True)])
    assert rep == {"selector": None, "name": "Search", "role": "button"}                 # found by role and name instead


def test_a_control_with_no_stable_way_to_find_it_is_not_healed():
    step = {"kind": "click", "selector": None, "name": "Filter"}
    rep, why = find_replacement(step, [_c("Filter", tag="div", selector=None)])
    assert rep is None and "neither a stable selector nor a role" in why


def test_the_same_control_that_is_already_found_is_not_reported_as_a_heal():
    step = {"kind": "click", "selector": "#s", "name": "Search", "role": "button"}
    assert "another reason" in find_replacement(step, [_c("Search", selector="#s")])[1]
    assert find_replacement({"kind": "click", "name": ""}, [_c("Search")])[0] is None


# ------------------------------------------------------------------ running a flow with healing, against a real
# browser and a real local page: the flow's step always targets "#old-search", which never exists on these
# pages, so _do_step's own real lookup is what raises "control not found" - nothing here is scripted.

def _real_flow(base: str, status="candidate"):
    return {"id": "f", "status": status, "start_url": base + "/en", "outcome": {"effect": "navigates", "to": "/en/find"},
            "steps": [{"kind": "click", "selector": "#old-search", "name": "Search"}]}


def _flow(status="candidate"):
    return {"id": "f", "status": status, "start_url": "https://x.test/en", "outcome": {"effect": "navigates", "to": "/en/find"},
            "steps": [{"kind": "click", "selector": "#old-search", "name": "Search"}]}


def test_with_healing_the_step_is_retried_with_the_replacement_and_recorded(real_page, monkeypatch):
    calls = _spy_do_step(monkeypatch)
    pages = {"/en": b'<html><body><button type="button" id="new-search" '
                     b'onclick="location.href=\'/en/find\'">Search now</button></body></html>',
             "/en/find": b"<html><body><h1>Results</h1></body></html>"}
    with _serve(pages) as base:
        result = run_flow(real_page, _real_flow(base), heal=True)
    assert result["ok"] and result["error"] is None
    assert calls == [("#old-search", "Search"), ("#new-search", "Search now")]
    heal = result["heals"][0]
    assert heal["step"] == 1 and heal["was"]["selector"] == "#old-search" and heal["now"]["selector"] == "#new-search"


def test_without_healing_the_failure_names_the_control_that_looks_like_it(real_page, monkeypatch):
    calls = _spy_do_step(monkeypatch)
    pages = {"/en": b'<html><body><button type="button" id="new-search">Search now</button></body></html>'}
    with _serve(pages) as base:
        result = run_flow(real_page, _real_flow(base, "approved"), heal=False)
    assert not result["ok"] and result["heals"] == []
    assert 'the page has "Search now"' in result["error"] and "decided by a person" in result["error"]
    assert len(calls) == 1                                                               # nothing was retried


def test_no_replacement_gives_a_clear_reason(real_page):
    pages = {"/en": b'<html><body><button type="button" id="subscribe">Subscribe</button></body></html>'}
    with _serve(pages) as base:
        result = run_flow(real_page, _real_flow(base), heal=True)
    assert not result["ok"] and "nothing could stand in for it" in result["error"]


def test_a_replacement_that_also_fails_is_reported_and_not_kept(real_page):
    # "Search now" is a real, visible candidate (find_replacement will pick it), but an overlay sits over
    # the whole page, so a real click on it times out instead of landing - a different, real kind of failure.
    pages = {"/en": b'<html><body><button type="button" id="new-search">Search now</button>'
                     b'<div style="position:fixed;inset:0;z-index:999;"></div></body></html>'}
    with _serve(pages) as base:
        result = run_flow(real_page, _real_flow(base), heal=True)
    assert not result["ok"] and result["heals"] == [] and 'tried "Search now" instead' in result["error"]


def test_other_failures_are_not_treated_as_a_missing_control(real_page):
    # "#old-search" exists (so _do_step never raises "control not found"); the same page-covering overlay
    # makes the real click time out for an unrelated reason, which must not trigger healing.
    pages = {"/en": b'<html><body><button type="button" id="old-search">Search</button>'
                     b'<div style="position:fixed;inset:0;z-index:999;"></div></body></html>'}
    with _serve(pages) as base:
        result = run_flow(real_page, _real_flow(base), heal=True)
    assert not result["ok"] and result["heals"] == [] and "control not found" not in result["error"]


# ------------------------------------------------------------------ keeping a heal

def _passing_result(heals):
    return {"ok": True, "steps_done": 1, "steps_total": 1, "error": None, "step_effects": ["navigates"], "step_urls": ["u"],
            "landed_url": "u", "heals": heals,
            "observed": {"effect": "navigates", "url": "https://x.test/en/find", "new_headings": ["Results"], "new_controls": [], "results": []}}


HEAL = {"step": 1, "how": 'control renamed (was "Search")', "was": {"selector": "#old-search", "name": "Search", "role": None},
        "now": {"selector": "#new-search", "name": "Search now", "role": "button"}}


def test_a_heal_is_kept_only_when_the_whole_run_passed_and_the_old_values_are_remembered():
    flow = _flow()
    evaluation = apply_result(flow, _passing_result([HEAL]), "t1")
    assert evaluation["passed"] and evaluation["healed"] == [HEAL] and flow["status"] == "verified"
    step = flow["steps"][0]
    assert (step["selector"], step["name"], step["role"]) == ("#new-search", "Search now", "button")
    assert flow["heal_history"][0]["was"]["selector"] == "#old-search" and flow["heal_history"][0]["at"] == "t1"


def test_a_heal_is_not_kept_when_the_run_still_failed():
    flow = _flow()
    result = _passing_result([HEAL])
    result.update(ok=False, error="step 2 failed")
    evaluation = apply_result(flow, result, "t1")
    assert not evaluation["passed"] and "healed" not in evaluation and evaluation["heal_not_kept"] == [HEAL]
    assert flow["steps"][0]["selector"] == "#old-search" and "heal_history" not in flow


def test_a_flow_a_person_decided_is_never_rewritten_by_a_heal():
    flow = _flow("approved")
    apply_result(flow, _passing_result([HEAL]), "t1")
    assert flow["steps"][0]["selector"] == "#old-search" and flow["status"] == "approved" and "heal_history" not in flow


def test_a_step_with_a_role_is_found_by_that_role_first():
    class _Loc:
        def __init__(self, found): self.found, self.first = found, self
        def count(self): return 1 if self.found else 0

    class _Page:
        def __init__(self): self.roles = []
        def get_by_role(self, role, name, exact=None):
            self.roles.append(role)
            return _Loc(role == "tab")

    page = _Page()
    assert runner._locate(page, {"kind": "click", "name": "Details", "role": "tab"}) is not None and page.roles == ["tab"]


def test_locate_prefers_the_visible_match_when_a_selector_matches_several(real_page):
    # a wizard where each step's "Next" shares the same name, shown/hidden by CSS as it advances - found live:
    # _locate used to always grab the first DOM match, which after the first click is the now-hidden step-1
    # button, so the second click kept targeting it and timed out instead of clicking the one on screen.
    pages = {"/en": (b"<html><body>"
                      b"<input type=\"button\" name=\"next\" value=\"n1\" onclick=\""
                      b"document.getElementsByName('next')[0].style.display='none';"
                      b"document.getElementsByName('next')[1].style.display='';\">"
                      b"<input type=\"button\" name=\"next\" value=\"n2\" style=\"display:none\" "
                      b"onclick=\"document.body.setAttribute('data-done','1')\">"
                      b"</body></html>")}
    with _serve(pages) as base:
        real_page.goto(base + "/en")
        step = {"kind": "click", "selector": 'input[name="next"]'}
        runner._do_step(real_page, step)          # only one visible match so far - nothing to choose between
        runner._do_step(real_page, step)          # now two DOM matches; only the 2nd is visible
        assert real_page.get_attribute("body", "data-done") == "1"


# ------------------------------------------------------------------ showing it to a person

def test_flows_show_and_history_lines_tell_the_story_of_a_heal():
    flow = dict(_flow(), goal="g", source="intent", observed=None, heal_history=[{"at": "2026-09-21T10:00:00", **HEAL}])
    text = render_show(flow, [])
    assert "healed" in text and "step 1" in text and "'#old-search'" in text and "'#new-search'" in text
    line = _rating_line({"source": "runner", "passed": True, "checks": {"steps_completed": "1/1", "observed_effect": "navigates"},
                         "healed": [HEAL]})
    assert "healed step 1" in line and "Search -> Search now" in line


# ------------------------------------------------------------------ roles and name matching agree with the spec

def test_a_recorded_role_that_is_not_a_real_aria_role_is_replaced_by_one_derived_from_the_tag():
    from website_test_pipeline.runner import _role_for
    assert _role_for({"tag": "button", "role": "submit"}) == "button"           # the explorer can record an input type here
    assert _role_for({"tag": "input", "type": "submit", "role": "submit"}) == "button"
    assert _role_for({"tag": "div", "role": "tab"}) == "tab"                    # a real role is kept
    assert _role_for({"tag": "a"}) == "link" and _role_for({"tag": "textarea"}) == "textbox"
    assert _role_for({"tag": "div"}) is None


def test_flowgen_uses_the_same_role_rule_as_the_runner():
    from website_test_pipeline.flowgen import _role_of
    assert _role_of({"tag": "button", "role": "submit"}) == "button"


def test_names_are_matched_exactly_or_by_prefix_when_cut_like_the_generated_spec():
    import re

    calls = []

    class _Loc:
        first = None
        def count(self): return 1

    class _Page:
        def get_by_role(self, role, name, exact=None):
            calls.append((role, name, exact))
            loc = _Loc()
            loc.first = loc
            return loc

    runner._by_role(_Page(), "button", "Search")
    assert calls[-1] == ("button", "Search", True)                              # not a substring match
    long_name = "x" * 40
    runner._by_role(_Page(), "link", long_name)
    role, name, exact = calls[-1]
    assert role == "link" and isinstance(name, re.Pattern) and name.search(long_name + " and more") and exact is None


# ------------------------------------------------------------------ choosing another option when the default shows nothing

def _promise_flow(status="candidate", value=None):
    return {"id": "f", "source": "intent", "status": status, "goal": "A visitor picks a country and sees the results.",
            "start_url": "https://x.test/en", "outcome": {"effect": "navigates", "to": "/en/find"},
            "steps": [{"kind": "select", "selector": "#country", "name": "Country", "value": value},
                      {"kind": "click", "selector": None, "name": "Search"}]}


def _run_result(content, choices=None):
    observed = {"effect": "navigates", "url": "https://x.test/en/find", "new_headings": ["Results"] if content else [],
                "new_controls": [], "results": []}
    return {"ok": True, "steps_done": 2, "steps_total": 2, "error": None, "step_effects": ["no-visible-change", "navigates"],
            "step_urls": ["a", "b"], "landed_url": "a", "heals": [], "observed": observed,
            "select_choices": choices if choices is not None else {}}


DEFAULTED = {0: {"options": ["Afghanistan", "Albania", "Algeria", "Andorra"], "chosen": "Afghanistan"}}


def test_judge_gives_the_verdict_without_recording_anything():
    from website_test_pipeline.runner import judge
    flow = _promise_flow()
    assert judge(flow, _run_result(True))["passed"] is True
    empty = judge(flow, _run_result(False))
    assert empty["passed"] is False and empty["promised"] is True and empty["matched"] and empty["changed"]
    assert "observed" not in flow and flow["status"] == "candidate"               # judging changes nothing


def test_the_next_options_are_tried_in_order_until_one_shows_content():
    from website_test_pipeline.runner import try_other_options
    tried = []

    def run_once(overrides):
        tried.append(overrides[0])
        return _run_result(content=overrides[0] == "Algeria", choices=DEFAULTED)

    better = try_other_options(_promise_flow(), _run_result(False, DEFAULTED), run_once)
    assert tried == ["Albania", "Algeria"]                                          # the default is not retried; it stops at the first hit
    heal = better["heals"][-1]
    assert heal["kind"] == "option" and heal["step"] == 1 and heal["now"] == {"value": "Algeria"}
    assert '"Afghanistan"' in heal["how"] and '"Algeria"' in heal["how"]


def test_the_search_is_bounded_and_gives_up_without_changing_the_result():
    from website_test_pipeline.runner import try_other_options, MAX_OPTION_TRIES
    calls = []
    original = _run_result(False, DEFAULTED | {0: {"options": [f"C{n}" for n in range(30)], "chosen": "C0"}})
    out = try_other_options(_promise_flow(), original, lambda o: calls.append(o) or _run_result(False))
    assert out is original and len(calls) == MAX_OPTION_TRIES


def test_a_step_with_a_value_already_chosen_by_someone_is_never_changed():
    from website_test_pipeline.runner import try_other_options
    calls = []
    original = _run_result(False, DEFAULTED)
    assert try_other_options(_promise_flow(value="Afghanistan"), original, lambda o: calls.append(o)) is original and calls == []


def test_a_trial_that_crashes_is_skipped_not_fatal():
    from website_test_pipeline.runner import try_other_options

    def run_once(overrides):
        if overrides[0] == "Albania":
            raise RuntimeError("browser died")
        return _run_result(True, DEFAULTED)

    assert try_other_options(_promise_flow(), _run_result(False, DEFAULTED), run_once)["heals"][-1]["now"]["value"] == "Algeria"


def test_a_chosen_option_becomes_the_steps_explicit_value_and_is_remembered():
    from website_test_pipeline.runner import try_other_options
    flow = _promise_flow()
    better = try_other_options(flow, _run_result(False, DEFAULTED), lambda o: _run_result(True, DEFAULTED))
    evaluation = apply_result(flow, better, "t1")
    assert evaluation["passed"] and flow["status"] == "verified" and evaluation["healed"][0]["kind"] == "option"
    assert flow["steps"][0]["value"] == "Albania"                                   # the spec will now select this label
    assert flow["heal_history"][0]["now"] == {"value": "Albania"} and flow["heal_history"][0]["at"] == "t1"


def test_the_option_history_reads_clearly_for_a_person():
    heal = {"at": "2026-09-21T10:00:00", "kind": "option", "step": 1, "how": 'the default option "A" showed no content; "B" does',
            "was": {"value": None, "name": "Country"}, "now": {"value": "B"}}
    flow = dict(_promise_flow(), goal="g", observed=None, heal_history=[heal])
    text = render_show(flow, [])
    assert 'the default option "A" showed no content; "B" does' in text
    line = _rating_line({"source": "runner", "passed": True, "checks": {"steps_completed": "2/2", "observed_effect": "navigates"},
                         "healed": [heal]})
    assert "healed step 1" in line and '"B" does' in line and "None" not in line


def test_real_options_leave_out_placeholders():
    class _Sel:
        def evaluate(self, js):
            return [{"v": "", "t": "Please select a country"}, {"v": "1", "t": "Egypt"}, {"v": "-1", "t": "All"}, {"v": "2", "t": "Qatar"}]

    assert runner._real_options(_Sel()) == ["Egypt", "Qatar"]
    class _Broken:
        def evaluate(self, js): raise RuntimeError("detached")
    assert runner._real_options(_Broken()) == []


# ------------------------------------------------------------------ option-healing end to end through run_verify,
# for a "results" predicted outcome - real bug, found live: outcome_matches's "results" case already requires
# content to be visible, so a guard of "promised (no content) AND matched (outcome achieved)" can never be true
# together for this outcome type, and neither the automatic retry nor the "definite failure" reason ever fired.

def test_option_healing_fires_through_run_verify_for_a_results_outcome_with_a_real_browser(tmp_path):
    import json
    import threading
    import http.server
    import socketserver
    from types import SimpleNamespace

    HOME = (b"<html><body><h1>Find frequencies</h1><select id=c>"
            b"<option value=''>Please select</option><option>Alpha</option><option>Bravo</option><option>Charlie</option></select>"
            b"<button onclick=\"location.href='/results?c='+document.getElementById('c').value\">Search</button></body></html>")

    def results(country: str) -> bytes:
        if country == "Charlie":
            return b"<html><body><h1>Here are the results</h1><table><tr><td>row</td></tr></table></body></html>"
        return b"<html><body><h1>Find frequencies</h1><p>Nothing to show</p></body></html>"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = results(self.path.split("c=")[-1]) if self.path.startswith("/results") else HOME
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        flow = {"id": "demo", "source": "intent", "status": "candidate",
                "goal": "A visitor picks a country and sees the frequency results.", "start_url": base + "/",
                "outcome": {"effect": "results"},                    # the outcome type this bug affected
                "steps": [{"kind": "select", "selector": "#c", "name": "Country", "value": None},
                          {"kind": "click", "selector": None, "name": "Search"}]}
        flows_file = tmp_path / "flows.json"
        flows_file.write_text(json.dumps({"version": 1, "flows": [flow]}), encoding="utf-8")
        settings = SimpleNamespace(flows_file=flows_file, ratings_file=tmp_path / "ratings.json", headless=True,
                                   navigation_timeout_ms=15000, intents_file=tmp_path / "i.json")
        code = run_verify(settings, LOG)
        assert code == 0
        saved = json.loads(flows_file.read_text(encoding="utf-8"))["flows"][0]
        assert saved["status"] == "verified" and saved["steps"][0]["value"] == "Charlie"
        assert saved["observed"]["new_headings"] == ["Here are the results"]
        heal = saved["heal_history"][-1]
        assert heal["kind"] == "option" and 'showed no content; "Charlie" does' in heal["how"]
    finally:
        server.shutdown()


# ------------------------------------------------------------------ a session that dies mid-flow (short-lived or
# shared-account sites, e.g. a public demo whose single account gets evicted by another visitor) - found live
# against OrangeHRM's public demo.

def test_run_flow_recognises_a_login_wall_instead_of_healing_onto_it(real_page, monkeypatch):
    # a real, visible password field is the actual signal _now_showing_a_login_wall checks for - no need to
    # fake that function itself.
    calls = _spy_do_step(monkeypatch)
    pages = {"/en": b'<html><body><h1>Sign in</h1><input type="password" name="pw"></body></html>'}
    with _serve(pages) as base:
        result = run_flow(real_page, _real_flow(base), heal=True)
    assert result.get("session_expired") is True
    assert "session expired mid-flow" in result["error"] and "not a defect" in result["error"]
    assert result["heals"] == [] and len(calls) == 1                 # never tried to heal onto the login page


def test_run_verify_refreshes_an_expired_session_and_retries_the_flow_with_a_real_browser(tmp_path, monkeypatch):
    import json
    import threading
    import http.server
    import socketserver
    from types import SimpleNamespace
    from urllib.parse import parse_qs

    LOGIN = ("<html><body><h1>Sign in</h1><form method='post' action='/login'>"
            "<input name='u'><input name='pw' type='password'><button>Go</button></form></body></html>")
    DASHBOARD = "<html><body><h1>Dashboard</h1><a href='/admin'>Admin</a></body></html>"
    ADMIN = "<html><body><h1>Admin</h1></body></html>"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            signed_in = "sid=1" in (self.headers.get("Cookie") or "")
            if self.path == "/dashboard" and not signed_in:
                self.send_response(302); self.send_header("Location", "/login"); self.end_headers(); return
            body = {"/login": LOGIN, "/dashboard": DASHBOARD, "/admin": ADMIN}.get(self.path, LOGIN).encode()
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            fields = parse_qs(self.rfile.read(length).decode())
            self.send_response(302)
            self.send_header("Location", "/dashboard")
            if fields.get("pw") == ["right"]:
                self.send_header("Set-Cookie", "sid=1; Path=/")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        from website_test_pipeline.authflow import env_names
        wall = {"fields": [{"type": "text", "name": "u"}, {"type": "password", "name": "pw"}]}
        for name in env_names("127.0.0.1", "default", wall):
            monkeypatch.setenv(name, {"AUTH_127_0_0_1_DEFAULT_U": "admin", "AUTH_127_0_0_1_DEFAULT_PW": "right"}[name])

        flow = {"id": "demo", "source": "intent", "status": "candidate",
                "goal": "A visitor opens Admin from the dashboard.", "start_url": base + "/dashboard",
                "outcome": {"effect": "navigates", "to": "/admin"},
                "steps": [{"kind": "click", "selector": None, "name": "Admin", "page": "/dashboard"}]}
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        (tmp_path / ".gitignore").write_text("w/\n", encoding="utf-8")
        flows_file = tmp_path / "flows.json"
        flows_file.write_text(json.dumps({"version": 1, "flows": [flow]}), encoding="utf-8")
        settings = SimpleNamespace(flows_file=flows_file, ratings_file=tmp_path / "ratings.json", headless=True,
                                   navigation_timeout_ms=15000, intents_file=tmp_path / "i.json",
                                   workspace=tmp_path / "w", site="127.0.0.1", root=tmp_path,
                                   auth_mode="auto", auth_account="default", seed_url=base + "/login")
        code = run_verify(settings, LOG)          # no session file exists yet: the first attempt lands on /login
        assert code == 0
        saved = json.loads(flows_file.read_text(encoding="utf-8"))["flows"][0]
        assert saved["status"] == "verified" and saved["observed"]["effect"] == "navigates"
        ratings = json.loads((tmp_path / "ratings.json").read_text(encoding="utf-8"))
        entries = ratings["ratings"]["demo"]
        assert len(entries) == 1 and entries[-1]["passed"] is True     # only the retry's verdict is recorded, not the transient failure
    finally:
        server.shutdown()
