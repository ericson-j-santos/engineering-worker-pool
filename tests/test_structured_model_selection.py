"""Regression of the real blocked response and sequential model selection."""
import json
from pathlib import Path

import pytest

from app import structured_code_worker as worker
from test_structured_code_worker import BEFORE, task


def test_observed_model_response_missing_def_is_rejected_without_repair():
    malformed = {"function": "_property(properties, name):\n    return ''\n"}
    with pytest.raises(worker.StructuredBlocked, match="^candidate_syntax$"):
        worker.render(BEFORE, task(), json.dumps(malformed))


def test_structured_model_is_separate_and_workflow_is_sequential():
    base = Path(__file__).resolve().parents[1]
    provider = (base / "app/structured_ollama.py").read_text()
    pipeline = (base / ".github/workflows/ci.yml").read_text()
    assert 'MODEL = "qwen2.5-coder:7b"' in provider
    assert "must include def" in provider
    assert "Do not output tests or Markdown" in provider
    assert pipeline.index("run: python scripts/local_worker_e2e.py") < pipeline.index("ollama stop")
    assert pipeline.index("ollama stop") < pipeline.index("ollama rm")
    assert pipeline.index("ollama rm") < pipeline.index("ollama pull qwen2.5-coder:7b")
    assert pipeline.index("ollama pull qwen2.5-coder:7b") < pipeline.index("run: python scripts/structured_worker_e2e.py")
    assert "runs-on: ubuntu-latest" in pipeline


def test_generation_request_does_not_retranscribe_defective_source():
    import ast
    source = (Path(__file__).resolve().parents[1] / "app/structured_ollama.py").read_text()
    tree = ast.parse(source)
    assignment = next(node for node in ast.walk(tree)
                      if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "prompt" for t in node.targets))
    assert not any(isinstance(n, ast.Name) and n.id == "before"
                   for n in ast.walk(assignment.value))
    expression = ast.Expression(body=assignment.value)
    request = eval(compile(expression, "<prompt-contract-test>", "eval"),
                   {"task": task(), "json": json})
    assert request.endswith(task()["instruction"])
    assert "\\n" not in request.split("Trusted input/output examples:", 1)[0]
    assert "\n" in request
    assert "must include def" in request and "no imports" in request
    assert json.dumps(task()["cases"], ensure_ascii=False, separators=(",", ":")) in request
    assert "return str(" not in request


def test_output_pattern_blocks_observed_subscripts_not_valid_get_code():
    import ast
    import re
    from test_structured_code_worker import FIXED
    source = (Path(__file__).resolve().parents[1] / "app/structured_ollama.py").read_text()
    tree = ast.parse(source)
    pattern_node = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "FUNCTION_PATTERN"
                                for t in n.targets))
    pattern = ast.literal_eval(pattern_node)
    correct = FIXED.replace("parts = []", "parts = list()")
    assert re.fullmatch(pattern, correct)
    assert not re.fullmatch(pattern, "def _property(properties, name):\\n    return properties[name]\\n")
    assert not re.fullmatch(pattern, "_property(properties, name):\\n    return ''\\n")
    # Existing execution guards still reject prohibited code even if a format constraint misses it.
    with pytest.raises(worker.StructuredBlocked):
        worker.render(BEFORE, task(), json.dumps({"function": "def _property(properties, name):\\n    import os\\n    return ''\\n"}))
    assert worker.validate(correct, task())["passed"] == len(task()["cases"])

    assert not re.fullmatch(pattern, "def _property(properties, name):\n    result = ''\n    result += name\n    return result\n")
