"""materialize_artifacts contract tests (v6.90.x P2).

`effective_task_result(..., materialize_artifacts=False)` is a "status/cost
projection only" read for hot display surfaces (history annotation,
api_tasks_list, the SSE follow loop, api_logs_tail discovery): it must skip the
entire artifact block — including the MUTATING child-artifact rebase and the
collect_task_artifact_records file scans — and the task-tree disposition hash
lookup, and must never carry sha-bearing/disposition claims. Every sha-economy
consumer keeps the True default, so a terminal child's `_child_result_sha256`
is identical no matter how many False-path reads happened in between.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from ouroboros.task_results import write_task_result
from ouroboros.task_status import find_child_tasks, load_effective_task_result


def _seed_child_drive_scenario(tmp_path):
    """Parent result referencing a terminal child drive that holds an artifact."""
    from ouroboros.artifacts import collect_task_artifact_records, copy_file_to_task_artifacts

    data = tmp_path / "data"
    child = tmp_path / "child"
    source_dir = tmp_path / "Desktop"
    source_dir.mkdir()
    source = source_dir / "report.html"
    source.write_text("<h1>child</h1>", encoding="utf-8")
    copy_file_to_task_artifacts(SimpleNamespace(drive_root=child, task_id="childart"), source, kind="user_file")
    child_artifacts = collect_task_artifact_records(child, "childart")
    write_task_result(
        child,
        "childart",
        "completed",
        result="done",
        artifacts=child_artifacts,
        artifact_status="ready",
        ts="2026-01-01T00:00:02Z",
    )
    write_task_result(
        data,
        "childart",
        "completed",
        result="done",
        child_drive_root=str(child),
        # Legacy mirrored disposition fields on disk must be stripped either way.
        child_result_disposition="integrated",
        child_result_disposition_sha256="deadbeef",
        delegation_role="subagent",
        parent_task_id="parent1",
        root_task_id="parent1",
    )
    (data / "state").mkdir(parents=True, exist_ok=True)
    (data / "state" / "queue_snapshot.json").write_text('{"pending": [], "running": []}', encoding="utf-8")
    return data, child


def test_false_read_skips_artifact_block_and_disposition(tmp_path, monkeypatch):
    data, child = _seed_child_drive_scenario(tmp_path)
    import ouroboros.artifacts as artifacts_mod

    calls = {"collect": 0, "copy": 0}
    real_collect = artifacts_mod.collect_task_artifact_records
    real_copy = artifacts_mod.copy_file_to_task_artifacts
    monkeypatch.setattr(
        artifacts_mod, "collect_task_artifact_records",
        lambda *a, **k: calls.__setitem__("collect", calls["collect"] + 1) or real_collect(*a, **k),
    )
    monkeypatch.setattr(
        artifacts_mod, "copy_file_to_task_artifacts",
        lambda *a, **k: calls.__setitem__("copy", calls["copy"] + 1) or real_copy(*a, **k),
    )

    row = load_effective_task_result(data, "childart", materialize_artifacts=False)

    assert row["status"] == "completed"
    assert row["result"] == "done"
    # No artifact machinery ran and no files were copied to the parent.
    assert calls == {"collect": 0, "copy": 0}
    assert not (data / "task_results" / "artifacts" / "childart" / "report.html").exists()
    # A False row never carries sha-bearing/disposition claims — even the legacy
    # mirrored on-disk fields are stripped.
    for field in (
        "child_result_disposition",
        "child_result_disposition_sha256",
        "child_result_disposition_reason",
        "child_result_disposition_source",
        "parent_decision_child_result_sha256",
        "terminal_child_result_snapshot",
    ):
        assert field not in row


def test_child_result_sha_stable_across_false_path_reads(tmp_path):
    from ouroboros.tools.join_ledger import _child_result_sha256

    data, child = _seed_child_drive_scenario(tmp_path)

    sha_before = _child_result_sha256(load_effective_task_result(data, "childart"))
    # Any number of projection-only reads in between must not perturb the sha
    # economy (they perform no writes at all).
    for _ in range(3):
        load_effective_task_result(data, "childart", materialize_artifacts=False)
        find_child_tasks(data, parent_task_id="parent1", root_task_id="parent1", materialize_artifacts=False)
    sha_after = _child_result_sha256(load_effective_task_result(data, "childart"))

    assert sha_before == sha_after


def test_true_default_still_materializes_child_artifacts(tmp_path):
    """The default path keeps the read-repair durability: child artifacts are
    rebased onto the parent drive (api_task_artifact depends on it)."""
    data, child = _seed_child_drive_scenario(tmp_path)

    row = load_effective_task_result(data, "childart")

    rebased = data / "task_results" / "artifacts" / "childart" / "report.html"
    assert rebased.exists()
    assert rebased.read_text(encoding="utf-8") == "<h1>child</h1>"
    assert any(
        (item.get("name") or "") == "report.html" for item in row.get("artifacts") or []
    )


def test_history_and_tasks_list_paths_do_no_artifact_work(tmp_path, monkeypatch):
    """Counter-assert: GET /api/chat/history and GET /api/tasks perform ZERO
    collect_task_artifact_records / copy_file_to_task_artifacts calls."""
    import ouroboros.artifacts as artifacts_mod
    from ouroboros.gateway.history import make_chat_history_endpoint
    from ouroboros.gateway.tasks import api_tasks_list

    data, child = _seed_child_drive_scenario(tmp_path)
    logs = data / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    (logs / "progress.jsonl").write_text(
        json.dumps({"ts": "2026-01-01T00:00:01Z", "content": "step", "task_id": "childart"}) + "\n",
        encoding="utf-8",
    )

    calls = {"collect": 0, "copy": 0}
    monkeypatch.setattr(
        artifacts_mod, "collect_task_artifact_records",
        lambda *a, **k: calls.__setitem__("collect", calls["collect"] + 1) or [],
    )
    monkeypatch.setattr(
        artifacts_mod, "copy_file_to_task_artifacts",
        lambda *a, **k: calls.__setitem__("copy", calls["copy"] + 1) or None,
    )

    history = make_chat_history_endpoint(data)
    response = asyncio.run(history(SimpleNamespace(query_params={"limit": "10"})))
    payload = json.loads(response.body.decode("utf-8"))["messages"]
    assert any(item.get("task_id") == "childart" for item in payload)

    request = SimpleNamespace(
        query_params={},
        app=SimpleNamespace(state=SimpleNamespace(drive_root=data)),
    )
    response = asyncio.run(api_tasks_list(request))
    tasks = json.loads(response.body.decode("utf-8"))["tasks"]
    assert any(task.get("task_id") == "childart" for task in tasks)

    assert calls == {"collect": 0, "copy": 0}
