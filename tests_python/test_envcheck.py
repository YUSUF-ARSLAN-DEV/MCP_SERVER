from types import SimpleNamespace

from website_test_pipeline.envcheck import check_env, check_model


def test_the_typo_that_broke_pytest_is_caught_before_a_run_starts():
    problems = check_env({"EXECUTE_CONCURRENCY": "1rerun t"})
    assert len(problems) == 1 and "EXECUTE_CONCURRENCY" in problems[0]


def test_good_and_unset_numeric_settings_pass():
    assert check_env({"EXECUTE_CONCURRENCY": "1", "GENERATE_CONCURRENCY": "4"}) == []
    assert check_env({}) == []


def test_an_api_url_that_is_not_one_http_url_is_caught():
    assert check_env({"API_URL": "llm-1.iceroad.net/v1"})
    assert check_env({"API_URL": "https://a.test/v1 'https://b.test'"})
    assert check_env({"API_URL": "https://a.test/v1/chat/completions"}) == []


def test_an_unknown_model_provider_is_caught():
    assert "MODEL_PROVIDER" in check_env({"MODEL_PROVIDER": "skynet"})[0]
    assert check_env({"MODEL_PROVIDER": "Anthropic"}) == [] and check_env({"MODEL_PROVIDER": "openai"}) == []


def _settings(model="m1"):
    return SimpleNamespace(api_url="https://a.test/v1/chat/completions", api_key="k", model=model)


def test_a_model_the_endpoint_does_not_serve_is_reported_with_what_it_does_serve():
    message = check_model(_settings("ghost"), fetch=lambda *a: ["real-1", "real-2"])
    assert "ghost" in message and "real-1, real-2" in message


def test_a_served_model_passes():
    assert check_model(_settings("m1"), fetch=lambda *a: ["m1"]) is None


def test_an_endpoint_that_cannot_answer_is_not_treated_as_a_wrong_model():
    def down(*a):
        raise OSError("unreachable")
    assert check_model(_settings(), fetch=down) is None
    assert check_model(_settings(), fetch=lambda *a: []) is None
