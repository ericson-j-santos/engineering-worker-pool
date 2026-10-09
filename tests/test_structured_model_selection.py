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
    assert "include def" in provider
    assert "Do not output tests or Markdown" in provider
    assert pipeline.index("run: python scripts/local_worker_e2e.py") < pipeline.index("ollama stop")
    assert pipeline.index("ollama stop") < pipeline.index("ollama rm")
    assert pipeline.index("ollama rm") < pipeline.index("ollama pull qwen2.5-coder:7b")
    assert pipeline.index("ollama pull qwen2.5-coder:7b") < pipeline.index("run: python scripts/structured_worker_e2e.py")
    assert "runs-on: ubuntu-latest" in pipeline
