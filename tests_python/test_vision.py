import base64
import http.server
import io
import json
import logging
import socketserver
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from website_test_pipeline.flowgen import file_name
from website_test_pipeline.flowreport import build_flow_report
from website_test_pipeline.llm import ModelClient, ModelError
from website_test_pipeline.ratings import derive_status
from website_test_pipeline.review import _rating_line
from website_test_pipeline.vision import (
    MAX_SIDE, VisionError, _slug, encode_image, image_id, judge_flow, known_text, latest_vision, outcome_screenshot,
    parse_verdict, prompt_for, read_check, run_judge,
)

LOG = logging.getLogger("vision")
GOOD = {"verdict": "shows_expected", "scores": {"matches_sentence": 4, "content_visible": 5},
        "visible_text": ["Here are the results for your country", "Terms and Conditions"], "reason": "A results table is visible."}


def _flow(fid="x--search", status="verified"):
    return {"id": fid, "status": status, "goal": "A visitor picks a country and sees the results.", "start_url": "https://x.test/en",
            "steps": [{"kind": "select", "selector": "#c", "name": "Country", "value": "Egypt"},
                      {"kind": "click", "selector": None, "name": "Search"}],
            "outcome": {"effect": "navigates", "to": "/en/find"},
            "observed": {"effect": "navigates", "url": "https://x.test/en/find?c=Egypt", "new_headings": ["Here are the results for your country"],
                         "new_controls": ["button:Subscribe Now"]}}


INVENTORY = {"url": "https://x.test/en/find", "headings": [{"text": "Satellite Frequencies"}],
             "controls": [{"name": "Terms and Conditions", "tag": "a"}, {"name": "Subscribe Now", "tag": "button"}]}


def _png(path, size=(1200, 3000)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, "white").save(path)
    return path


def _workspace(tmp_path, statuses=("verified",)):
    artifacts = tmp_path / "artifacts"
    (artifacts).mkdir()
    (artifacts / "x.inventory.json").write_text(json.dumps(INVENTORY), encoding="utf-8")
    flows = [_flow(f"x--f{n}", status) for n, status in enumerate(statuses)]
    rows = []
    for flow in flows:
        nodeid = f"tests/{file_name(flow)}::test_flow[chromium]"
        rows.append({"nodeid": nodeid, "status": "passed", "error": None})
        _png(artifacts / "evidence" / _slug(nodeid) / "01-select.png", (300, 200))
        _png(artifacts / "evidence" / _slug(nodeid) / "99-outcome.png")
    (artifacts / "test_results.json").write_text(json.dumps({"tests": rows}), encoding="utf-8")
    (tmp_path / "flows.json").write_text(json.dumps({"version": 1, "flows": flows}), encoding="utf-8")
    return SimpleNamespace(flows_file=tmp_path / "flows.json", ratings_file=tmp_path / "r.json", artifacts_dir=artifacts, model="vm")


class _Client:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def generate(self, prompt, system, images=None):
        self.calls.append({"prompt": prompt, "images": images})
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else json.dumps(answer)


# ------------------------------------------------------------------ the picture

def test_a_huge_screenshot_becomes_a_small_jpeg_data_url(tmp_path):
    url = encode_image(_png(tmp_path / "big.png", (1280, 5000)))
    assert url.startswith("data:image/jpeg;base64,")
    image = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert image.format == "JPEG" and max(image.size) <= MAX_SIDE and image.size[0] < image.size[1]
    assert Image.open(io.BytesIO(base64.b64decode(encode_image(_png(tmp_path / "tiny.png", (50, 40))).split(",", 1)[1]))).size == (50, 40)


def test_the_final_screenshot_is_the_outcome_one_else_the_last_step(tmp_path):
    settings = _workspace(tmp_path)
    assert outcome_screenshot(settings, _flow("x--f0")).name == "99-outcome.png"
    folder = next((tmp_path / "artifacts" / "evidence").iterdir())
    (folder / "99-outcome.png").unlink()
    assert outcome_screenshot(settings, _flow("x--f0")).name == "01-select.png"


def test_no_results_no_row_or_no_evidence_means_no_screenshot(tmp_path):
    settings = _workspace(tmp_path)
    assert outcome_screenshot(settings, _flow("unknown--flow")) is None
    (tmp_path / "artifacts" / "test_results.json").unlink()
    assert outcome_screenshot(settings, _flow("x--f0")) is None
    (tmp_path / "artifacts" / "test_results.json").write_text("not json", encoding="utf-8")
    assert outcome_screenshot(settings, _flow("x--f0")) is None


def test_an_image_id_changes_when_the_picture_does(tmp_path):
    path = _png(tmp_path / "a.png", (10, 10))
    first = image_id(path)
    Image.new("RGB", (10, 10), "black").save(path)
    assert first.startswith("a.png#") and image_id(path) != first


# ------------------------------------------------------------------ trusting the answer

def test_a_well_formed_verdict_is_parsed_even_inside_a_code_fence():
    assert parse_verdict("```json" + chr(10) + json.dumps(GOOD) + chr(10) + "```")["verdict"] == "shows_expected"


def test_a_malformed_verdict_is_refused():
    for bad in ("no json", json.dumps(dict(GOOD, verdict="great")), json.dumps(dict(GOOD, scores={"matches_sentence": 9, "content_visible": 1})),
                json.dumps(dict(GOOD, scores={"matches_sentence": 3})), json.dumps(dict(GOOD, visible_text="text")), json.dumps([1])):
        with pytest.raises(VisionError):
            parse_verdict(bad)


def test_the_words_a_model_says_it_read_must_really_be_on_the_page():
    known = known_text(_flow(), [INVENTORY, {"url": "https://x.test/other", "controls": [{"name": "Elsewhere only"}]}])
    assert "Terms and Conditions" in known and "Satellite Frequencies" in known and "Country" in known
    assert "Elsewhere only" not in known                                        # another page's text does not count
    assert read_check(["Terms and Conditions", "Made up words"], known) == ["Terms and Conditions"]
    assert read_check(["ok", "Sat"], known) == []                              # too short to prove anything
    assert read_check(["here are the RESULTS for your country!"], known) == ["here are the RESULTS for your country!"]


def test_a_model_that_did_not_read_the_picture_is_caught(tmp_path):
    invented = dict(GOOD, visible_text=["Welcome to our shop", "Add to cart", "Free shipping"])
    with pytest.raises(VisionError, match="did not read the screenshot"):
        judge_flow(_Client(invented), _flow(), _png(tmp_path / "s.png", (100, 80)), [INVENTORY], "vm", "t")


def test_a_trusted_verdict_becomes_a_vision_entry_and_the_image_is_sent(tmp_path):
    client = _Client(GOOD)
    image = _png(tmp_path / "s.png", (100, 80))
    entry = judge_flow(client, _flow(), image, [INVENTORY], "vm", "t1")
    assert entry["source"] == "vision" and entry["verdict"] == "shows_expected" and entry["scores"] == GOOD["scores"]
    assert entry["prompt_version"] == "vision-v1" and entry["image"] == image_id(image) and "Terms and Conditions" in entry["verified_text"]
    assert client.calls[0]["images"][0].startswith("data:image/jpeg;base64,")
    assert "A visitor picks a country" in client.calls[0]["prompt"] and "select" in client.calls[0]["prompt"]


# ------------------------------------------------------------------ the command

def test_every_tested_flow_is_judged_once_and_never_changes_a_status(tmp_path):
    settings = _workspace(tmp_path, ("verified", "approved", "candidate"))
    client = _Client(GOOD)
    assert run_judge(settings, client, LOG) == 0
    ratings = json.loads(settings.ratings_file.read_text(encoding="utf-8"))["ratings"]
    assert sorted(ratings) == ["x--f0", "x--f1"]                                        # the candidate is not a tested flow
    assert len(client.calls) == 2
    assert run_judge(settings, client, LOG) == 0 and len(client.calls) == 2              # the same pictures are not judged again
    flows = json.loads(settings.flows_file.read_text(encoding="utf-8"))["flows"]
    assert [f["status"] for f in flows] == ["verified", "approved", "candidate"]
    assert derive_status("verified", ratings["x--f0"]) == "verified"                     # an opinion, never an execution


def test_a_new_screenshot_is_judged_again(tmp_path):
    settings = _workspace(tmp_path)
    client = _Client(GOOD)
    run_judge(settings, client, LOG)
    folder = next((tmp_path / "artifacts" / "evidence").iterdir())
    Image.new("RGB", (1200, 3000), "black").save(folder / "99-outcome.png")
    run_judge(settings, client, LOG)
    assert len(client.calls) == 2 and len(json.loads(settings.ratings_file.read_text(encoding="utf-8"))["ratings"]["x--f0"]) == 2


def test_ids_can_be_fragments(tmp_path):
    settings = _workspace(tmp_path, ("verified", "verified"))
    client = _Client(GOOD)
    run_judge(settings, client, LOG, ["f1"])
    assert list(json.loads(settings.ratings_file.read_text(encoding="utf-8"))["ratings"]) == ["x--f1"]


def test_an_untrustworthy_or_unreadable_answer_records_nothing(tmp_path):
    settings = _workspace(tmp_path, ("verified", "verified"))
    invented = dict(GOOD, visible_text=["Add to cart now", "Free shipping today"])
    assert run_judge(settings, _Client(invented, "no json"), LOG) == 1
    assert not settings.ratings_file.exists()


def test_no_screenshots_yet_is_exit_2(tmp_path):
    settings = _workspace(tmp_path)
    (tmp_path / "artifacts" / "test_results.json").unlink()
    assert run_judge(settings, _Client(GOOD), LOG) == 2


def test_an_unavailable_model_stops_the_command_and_keeps_what_was_recorded(tmp_path):
    settings = _workspace(tmp_path, ("verified", "verified"))
    client = _Client(GOOD, ModelError("gateway", status=524))
    assert run_judge(settings, client, LOG) == 4
    assert list(json.loads(settings.ratings_file.read_text(encoding="utf-8"))["ratings"]) == ["x--f0"]


def test_a_model_that_refuses_images_is_reported_and_nothing_is_written(tmp_path):
    settings = _workspace(tmp_path)
    assert run_judge(settings, _Client(ModelError("this model does not support image input", status=400)), LOG) == 5
    assert not settings.ratings_file.exists()


# ------------------------------------------------------------------ the real client against a local vision-style server

class _Server:
    """A tiny OpenAI-compatible endpoint that records what it was sent."""
    def __init__(self, reply, status=200):
        outer = self
        self.requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                content = body["messages"][1]["content"]
                if status != 200:
                    self.send_response(status); self.end_headers(); self.wfile.write(b'{"error":"no images"}'); return
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(json.dumps({"choices": [{"message": {"content": reply if isinstance(reply, str) else json.dumps(reply)}}]}).encode())

            def log_message(self, *a): pass

        self.server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/chat/completions"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _client_for(url):
    settings = SimpleNamespace(api_key="", api_url=url, model="vm", model_retries=0, model_timeout_ms=5000, retry_base_ms=100)
    return ModelClient(settings, LOG)


def test_the_real_client_sends_a_screenshot_as_an_image_part_and_text_prompts_stay_plain(tmp_path):
    server = _Server(GOOD)
    try:
        client = _client_for(server.url)
        assert client.generate("plain question", "sys") and client.generate("look", "sys", images=[encode_image(_png(tmp_path / "s.png", (60, 40)))])
        plain, vision = (r["messages"][1]["content"] for r in server.requests)
        assert plain == "plain question"                                                # unchanged for every existing caller
        assert [part["type"] for part in vision] == ["text", "image_url"] and vision[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert Image.open(io.BytesIO(base64.b64decode(vision[1]["image_url"]["url"].split(",", 1)[1]))).size == (60, 40)
    finally:
        server.close()


def test_judging_end_to_end_through_the_real_client(tmp_path):
    settings = _workspace(tmp_path)
    server = _Server(GOOD)
    try:
        assert run_judge(settings, _client_for(server.url), LOG) == 0
    finally:
        server.close()
    entry = latest_vision(json.loads(settings.ratings_file.read_text(encoding="utf-8"))["ratings"]["x--f0"])
    assert entry["verdict"] == "shows_expected" and len(server.requests) == 1


def test_a_server_that_rejects_images_gives_exit_5_through_the_real_client(tmp_path):
    settings = _workspace(tmp_path)
    server = _Server({}, status=400)
    try:
        assert run_judge(settings, _client_for(server.url), LOG) == 5
    finally:
        server.close()
    assert not settings.ratings_file.exists()


# ------------------------------------------------------------------ what a person sees

def test_the_history_and_the_report_show_the_vision_opinion_as_a_reason_to_look_not_a_failure():
    shows = {"source": "vision", "at": "2026-09-21T10:00:00", "verdict": "shows_expected", "scores": GOOD["scores"], "reason": "A results table is visible."}
    broken = dict(shows, verdict="looks_broken", scores={"matches_sentence": 1, "content_visible": 1}, reason="Only a loading spinner is visible.")
    assert "[vision shows_expected] matches 4/5, content 5/5 - A results table is visible." in _rating_line(shows)
    outcome = SimpleNamespace(status="passed", passed=True, evidence=["01.png"], attachments=[], error=None, duration=1.0, assertions=[])
    good = build_flow_report(_flow(), [shows], outcome)
    bad = build_flow_report(_flow(), [shows, broken], outcome)
    assert not good.vision_broken and not any("vision" in w for w in good.warnings)
    assert bad.vision_broken and bad.passed                                            # the test still passed
    assert any("looks broken (Only a loading spinner is visible.)" in w and "not a test failure" in w for w in bad.warnings)
    assert any("[vision looks_broken]" in line for line in bad.history)


def test_the_prompt_asks_for_words_that_can_be_read_and_json_only():
    text = prompt_for(_flow())
    assert "visible_text" in text and "READ" in text and text.endswith("starting with {.")
