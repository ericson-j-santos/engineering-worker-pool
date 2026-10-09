import json
from pathlib import Path
import importlib.util

import pytest
from app import local_code_worker as worker


def _harness():
    path = Path(__file__).resolve().parents[1] / "scripts/local_worker_e2e.py"
    spec = importlib.util.spec_from_file_location("local_worker_readback_harness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def _completed_views():
    task = {"task_id": "task-1", "state": "completed", "produced_sha": "b" * 40}
    work = {"work_id": "work-1", "task_id": "task-1", "task": dict(task),
            "evidence": {"state": "verified", "validation_sha": "b" * 40,
                         "independent_readback": True, "positive_control": True,
                         "negative_control": True}}
    snapshot = {"tasks": [], "queue": {"completed": 1, "queued": 0, "running": 0},
                "quarantine_count": 0}
    return work, task, snapshot


def test_completed_snapshot_uses_summary_and_individual_task_readback():
    harness = _harness()
    work, task, snapshot = _completed_views()
    for _ in range(2):
        harness.validate_completed_pool(work, task, snapshot, "task-1", "work-1", "b" * 40)
    assert snapshot["tasks"] == []  # real store contract excludes completed rows


@pytest.mark.parametrize("defect", [
    "missing_completed", "duplicate_completed", "unfinished_task", "wrong_task_sha",
    "wrong_work_sha", "missing_evidence", "wrong_task_id", "wrong_work_id",
    "lease_exposed", "negative_control_missing", "quarantined", "boolean_count",
])
def test_completed_readback_rejects_partial_duplicate_and_stale_evidence(defect):
    harness = _harness()
    work, task, snapshot = _completed_views()
    if defect == "missing_completed":
        snapshot["queue"]["completed"] = 0
    elif defect == "duplicate_completed":
        snapshot["queue"]["completed"] = 2
    elif defect == "unfinished_task":
        snapshot["tasks"] = [{"task_id": "task-2", "state": "queued"}]
        snapshot["queue"]["queued"] = 1
    elif defect == "wrong_task_sha":
        task["produced_sha"] = "c" * 40
    elif defect == "wrong_work_sha":
        work["task"]["produced_sha"] = "c" * 40
    elif defect == "missing_evidence":
        work["evidence"]["state"] = "pending"
    elif defect == "wrong_task_id":
        task["task_id"] = "other"
    elif defect == "wrong_work_id":
        work["work_id"] = "other"
    elif defect == "lease_exposed":
        task["lease_token"] = "PRIVATE_VALUE"
    elif defect == "negative_control_missing":
        work["evidence"]["negative_control"] = False
    elif defect == "quarantined":
        snapshot["quarantine_count"] = 1
    else:
        snapshot["queue"]["completed"] = True
    with pytest.raises(worker.WorkerBlocked, match="final_pool_readback"):
        harness.validate_completed_pool(work, task, snapshot, "task-1", "work-1", "b" * 40)
