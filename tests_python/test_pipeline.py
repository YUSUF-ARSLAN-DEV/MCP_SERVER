import pytest
from pathlib import Path
from website_test_pipeline.generator import extract_code
from website_test_pipeline.validator import validate_python_spec, SpecError
from website_test_pipeline.urls import canonicalize

def test_extracts_python_code_block():
    assert extract_code('```python\nassert 1\n```') == 'assert 1\n'

def test_rejects_missing_url():
    with pytest.raises(SpecError, match='target URL'):
        validate_python_spec('assert True\n', 'https://example.test')

def test_rejects_action_without_evidence():
    with pytest.raises(SpecError, match='action_evidence'):
        validate_python_spec("page.click('button')\nassert True\nhttps://example.test", 'https://example.test')

def test_canonicalize_removes_fragment_and_trailing_slash():
    assert canonicalize('HTTPS://Example.TEST/path/#section') == 'https://example.test/path'

def test_rejects_unsafe_import():
    with pytest.raises(SpecError, match='unsafe system import'):
        validate_python_spec('import subprocess\nassert True\n# https://example.test', 'https://example.test')
