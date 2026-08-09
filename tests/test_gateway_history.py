from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from ouroboros.gateway.history import make_chat_history_endpoint


def test_chat_history_preserves_subagent_lane_group_metadata(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    (logs / "progress.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-06-05T00:00:00Z",
                "content": "subagent queued",
                "task_id": "child1",
                "subagent_event": "scheduled",
                "model_lane": "review",
                "requested_model_lane": "review",
                "effective_model_lane": "review",
                "model": "review-a",
                "task_group_id": "group1",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    endpoint = make_chat_history_endpoint(tmp_path)
    response = asyncio.run(endpoint(SimpleNamespace(query_params={"limit": "10"})))
    payload = json.loads(response.body.decode("utf-8"))["messages"]

    rec = next(item for item in payload if item.get("task_id") == "child1")
    assert rec["model_lane"] == "review"
    assert rec["requested_model_lane"] == "review"
    assert rec["effective_model_lane"] == "review"
    assert rec["model"] == "review-a"
    assert rec["task_group_id"] == "group1"


def test_chat_history_replays_delivered_document_row(tmp_path):
    """A persisted document chat row is replayed as a msg_type=document record so
    the frontend rebuilds the file bubble on reload from the durable URL."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-07-09T00:00:00Z",
                "direction": "out",
                "chat_id": 1,
                "user_id": 7,
                "text": "quarterly numbers",
                "type": "document",
                "filename": "report.pdf",
                "mime": "application/pdf",
                "download_url": "/api/files/download?path=Desktop/report.pdf",
                "caption": "quarterly numbers",
                "task_id": "t-doc",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (logs / "progress.jsonl").write_text("", encoding="utf-8")

    endpoint = make_chat_history_endpoint(tmp_path)
    response = asyncio.run(endpoint(SimpleNamespace(query_params={"limit": "10"})))
    payload = json.loads(response.body.decode("utf-8"))["messages"]

    rec = next(item for item in payload if item.get("msg_type") == "document")
    assert rec["role"] == "assistant"
    assert rec["filename"] == "report.pdf"
    assert rec["mime"] == "application/pdf"
    assert rec["download_url"] == "/api/files/download?path=Desktop/report.pdf"
    assert rec["caption"] == "quarterly numbers"


def test_chat_history_backfills_from_rotated_archive(tmp_path):
    """The live chat.jsonl is rotated to archive/chat_<ts>.jsonl at ~800KB. History
    replay must backfill from the most recent archive(s) so a rotation does not
    silently erase the visible conversation — including delivered file bubbles —
    that scrolled just before it (BIBLE P1: no silent loss)."""
    logs = tmp_path / "logs"
    logs.mkdir()
    archive = tmp_path / "archive"
    archive.mkdir()

    # Older conversation + a delivered document, now rotated into the archive.
    (archive / "chat_20260709T165729.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-07-09T16:00:00Z",
                "direction": "in",
                "chat_id": 1,
                "user_id": 1,
                "text": "older message before the rotation",
            }
        )
        + "\n"
        + json.dumps(
            {
                "ts": "2026-07-09T16:05:00Z",
                "direction": "out",
                "chat_id": 1,
                "user_id": 7,
                "text": "here is the old pdf",
                "type": "document",
                "filename": "archived_report.pdf",
                "mime": "application/pdf",
                "download_url": "/api/files/download?path=Desktop/archived_report.pdf",
                "caption": "here is the old pdf",
                "task_id": "t-old-doc",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # Small live file written after the rotation.
    (logs / "chat.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-07-09T17:29:00Z",
                "direction": "in",
                "chat_id": 1,
                "user_id": 1,
                "text": "newest live message",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (logs / "progress.jsonl").write_text("", encoding="utf-8")

    endpoint = make_chat_history_endpoint(tmp_path)
    response = asyncio.run(endpoint(SimpleNamespace(query_params={"limit": "50"})))
    payload = json.loads(response.body.decode("utf-8"))["messages"]

    texts = [item.get("text", "") for item in payload]
    # The archived human message survives the rotation.
    assert "older message before the rotation" in texts
    assert "newest live message" in texts
    # The archived delivered-document row is replayed as a document bubble.
    doc = next(item for item in payload if item.get("msg_type") == "document")
    assert doc["filename"] == "archived_report.pdf"
    assert doc["download_url"] == "/api/files/download?path=Desktop/archived_report.pdf"
    # Chronological reassembly: archived rows precede the newer live row.
    assert texts.index("older message before the rotation") < texts.index("newest live message")


def test_progress_history_backfills_from_rotated_archive(tmp_path):
    """progress.jsonl now rotates to archive/progress_<ts>.jsonl like chat. History
    replay must backfill rotated progress rows (newest-first, until the n_progress
    quota) so a rotation does not silently erase live task cards (BIBLE P1)."""
    logs = tmp_path / "logs"
    logs.mkdir()
    archive = tmp_path / "archive"
    archive.mkdir()

    (archive / "progress_20260808T010000.jsonl").write_text(
        json.dumps({
            "ts": "2026-08-08T00:30:00Z",
            "content": "archived step",
            "task_id": "rotated-task",
        }) + "\n",
        encoding="utf-8",
    )
    (logs / "progress.jsonl").write_text(
        json.dumps({
            "ts": "2026-08-08T01:30:00Z",
            "content": "live step",
            "task_id": "live-task",
        }) + "\n",
        encoding="utf-8",
    )
    (logs / "chat.jsonl").write_text("", encoding="utf-8")

    endpoint = make_chat_history_endpoint(tmp_path)
    response = asyncio.run(endpoint(SimpleNamespace(query_params={"n_progress": "10"})))
    payload = json.loads(response.body.decode("utf-8"))["messages"]

    texts = [item.get("text", "") for item in payload]
    assert "archived step" in texts
    assert "live step" in texts
    # Chronological reassembly: archived rows precede the newer live row.
    assert texts.index("archived step") < texts.index("live step")


def test_progress_archive_not_read_when_live_satisfies_quota(tmp_path):
    """Archive backfill stops once the live window already satisfies the filtered
    quota — the rotated segments are not touched on the common path."""
    logs = tmp_path / "logs"
    logs.mkdir()
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "progress_20260808T010000.jsonl").write_text(
        json.dumps({"ts": "2026-08-08T00:30:00Z", "content": "old archived", "task_id": "t-old"}) + "\n",
        encoding="utf-8",
    )
    (logs / "progress.jsonl").write_text(
        "\n".join(
            json.dumps({"ts": f"2026-08-08T01:00:{i:02d}Z", "content": f"live-{i}", "task_id": "t1"})
            for i in range(5)
        ) + "\n",
        encoding="utf-8",
    )
    (logs / "chat.jsonl").write_text("", encoding="utf-8")

    endpoint = make_chat_history_endpoint(tmp_path)
    response = asyncio.run(endpoint(SimpleNamespace(query_params={"n_progress": "3"})))
    payload = json.loads(response.body.decode("utf-8"))["messages"]

    texts = [item.get("text", "") for item in payload]
    assert "old archived" not in texts  # quota satisfied by the live window
    assert texts == ["live-2", "live-3", "live-4"]


def test_history_annotation_after_quota_resolves_in_window_card(tmp_path):
    """Post-quota annotation behavior change (v6.90.x P2): a terminal task whose
    truth-anchor rows fell OUTSIDE the emitted window (here: its task_summary chat
    row evicted by the n_human quota) still resolves its surviving in-window
    progress card with the full terminal truth. Pre-change, the summary row seen
    during the full parse suppressed the progress-row anchor and the truth was
    applied only to rows the quota then dropped."""
    logs = tmp_path / "logs"
    logs.mkdir()
    chat_rows = [
        json.dumps({
            "ts": "2026-08-08T00:00:00Z",
            "direction": "system",
            "type": "task_summary",
            "task_id": "windowed",
            "chat_id": 1,
            "text": "Task windowed finished.",
            "tool_calls": 1,
            "rounds": 1,
        })
    ]
    # Newer human rows push the summary row out of a small n_human window.
    chat_rows += [
        json.dumps({
            "ts": f"2026-08-08T00:10:{i:02d}Z",
            "direction": "in" if i % 2 else "out",
            "chat_id": 1,
            "text": f"newer human {i}",
        })
        for i in range(4)
    ]
    (logs / "chat.jsonl").write_text("\n".join(chat_rows) + "\n", encoding="utf-8")
    (logs / "progress.jsonl").write_text(
        json.dumps({
            "ts": "2026-08-08T00:05:00Z",
            "content": "still visible progress",
            "task_id": "windowed",
        }) + "\n",
        encoding="utf-8",
    )
    results = tmp_path / "task_results"
    results.mkdir()
    (results / "windowed.json").write_text(
        json.dumps({
            "task_id": "windowed",
            "status": "completed",
            "cost_usd": 0.42,
            "cost_final": True,
            "reason_code": "",
        }),
        encoding="utf-8",
    )

    endpoint = make_chat_history_endpoint(tmp_path)
    response = asyncio.run(
        endpoint(SimpleNamespace(query_params={"n_human": "2", "n_progress": "10"}))
    )
    payload = json.loads(response.body.decode("utf-8"))["messages"]

    assert not any(item.get("system_type") == "task_summary" for item in payload)
    rec = next(item for item in payload if item.get("task_id") == "windowed")
    assert rec["task_terminal_status"] == "completed"
    # Full terminal truth (cost) landed on the in-window progress anchor.
    assert rec["cost_usd"] == 0.42
    assert rec["cost_final"] is True


def test_chat_history_backfill_quota_is_thread_aware(tmp_path):
    """Regression for the v6.58.5 review finding: the archive-backfill human-row
    quota must be counted with the SAME thread filter used at render time. A
    project-thread request whose LIVE file already holds `want` unrelated
    main-chat rows must still read the archive so rotated PROJECT rows/documents
    are recovered (they used to be skipped because the quota counted every live
    human row before the thread filter)."""
    from ouroboros import projects_registry
    from ouroboros.contracts.chat_id_policy import project_chat_id

    # A registered project so its chat_id classifies as a project thread.
    projects_registry.create_project(tmp_path, "proj_demo", name="Demo")
    pc = project_chat_id("proj_demo")

    logs = tmp_path / "logs"
    logs.mkdir()
    archive = tmp_path / "archive"
    archive.mkdir()

    # Rotated archive holds a PROJECT-thread delivered document.
    (archive / "chat_20260709T150000.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-07-09T14:00:00Z",
                "direction": "out",
                "chat_id": pc,
                "user_id": 7,
                "text": "project pdf",
                "type": "document",
                "filename": "project_report.pdf",
                "mime": "application/pdf",
                "download_url": "/api/files/download?path=Desktop/project_report.pdf",
                "caption": "project pdf",
                "task_id": "t-proj-doc",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # Live file: only UNRELATED main-chat rows (chat_id defaults to 1).
    (logs / "chat.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "ts": f"2026-07-09T17:0{i}:00Z",
                    "direction": "in" if i % 2 else "out",
                    "chat_id": 1,
                    "user_id": 1,
                    "text": f"main chat row {i}",
                }
            )
            for i in range(4)
        )
        + "\n",
        encoding="utf-8",
    )
    (logs / "progress.jsonl").write_text("", encoding="utf-8")

    endpoint = make_chat_history_endpoint(tmp_path)
    # want=2 (< the 4 unrelated live rows): old quota would stop before reading
    # the archive; thread-aware quota reads it because 0 live rows match `pc`.
    response = asyncio.run(
        endpoint(SimpleNamespace(query_params={"chat_id": str(pc), "n_human": "2"}))
    )
    payload = json.loads(response.body.decode("utf-8"))["messages"]

    doc = next(item for item in payload if item.get("msg_type") == "document")
    assert doc["filename"] == "project_report.pdf"
    # And unrelated main-chat rows do NOT leak into the project thread.
    assert not any(item.get("text", "").startswith("main chat row") for item in payload)


def test_chat_history_preserves_subagent_accept_markers(tmp_path):
    """WS8 accept/count markers must survive chat-history replay (gateway contract)."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    (logs / "progress.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-06-08T00:00:00Z",
                "content": "subagent queued",
                "task_id": "child2",
                "subagent_event": "scheduled",
                "accepted": True,
                "active_subagent_count": 3,
                "max_active_subagents": 6,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    endpoint = make_chat_history_endpoint(tmp_path)
    response = asyncio.run(endpoint(SimpleNamespace(query_params={"limit": "10"})))
    payload = json.loads(response.body.decode("utf-8"))["messages"]

    rec = next(item for item in payload if item.get("task_id") == "child2")
    assert rec["accepted"] is True
    assert rec["active_subagent_count"] == 3
    assert rec["max_active_subagents"] == 6


def test_chat_history_preserves_subagent_reconciliation_metadata(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    (logs / "progress.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-06-27T00:00:00Z",
                "content": "subagent queued behind active cap",
                "task_id": "child3",
                "subagent_event": "scheduled",
                "queued_behind_active_cap": True,
                "required_capabilities": ["shell", "vcs"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    endpoint = make_chat_history_endpoint(tmp_path)
    response = asyncio.run(endpoint(SimpleNamespace(query_params={"limit": "10"})))
    payload = json.loads(response.body.decode("utf-8"))["messages"]

    rec = next(item for item in payload if item.get("task_id") == "child3")
    assert rec["queued_behind_active_cap"] is True
    assert rec["required_capabilities"] == ["shell", "vcs"]


def test_chat_history_task_summary_row_passes_flat_cost_fields_through(tmp_path):
    """v6.82 P1: agent_task_pipeline writes the pre-synthesis cost snapshot onto
    the task_summary chat row; history replay must pass those flat fields
    through so a reload still shows the card's cost (no result file present)."""
    logs = tmp_path / "logs"
    logs.mkdir()
    row_cost = {
        "cost_accounting_status": "available",
        "cost_final": False,
        "cost_usd_with_children": 1.234567,
        "cost_with_children_partial": True,
        "reserved_usd": 0.25,
        "unresolved_upper_bound_usd": 0.5,
        "unknown_unmetered": 0,
    }
    (logs / "chat.jsonl").write_text(
        json.dumps({
            "ts": "2026-07-29T00:00:00Z",
            "direction": "system",
            "type": "task_summary",
            "task_id": "cost-summary",
            "chat_id": 1,
            "text": "Task cost-summary finished.",
            "tool_calls": 2,
            "rounds": 3,
            **row_cost,
        }) + "\n",
        encoding="utf-8",
    )
    (logs / "progress.jsonl").write_text("", encoding="utf-8")

    endpoint = make_chat_history_endpoint(tmp_path)
    response = asyncio.run(endpoint(SimpleNamespace(query_params={"limit": "10"})))
    payload = json.loads(response.body.decode("utf-8"))["messages"]

    rec = next(item for item in payload if item.get("task_id") == "cost-summary")
    for field, expected in row_cost.items():
        assert rec.get(field) == expected
    # The snapshot honestly lacks cost_usd — replay must not fabricate it.
    assert "cost_usd" not in rec


def test_chat_history_attaches_terminal_cost_truth_from_task_result(tmp_path):
    """v6.82 P1: a terminal task_results/<id>.json carries the final cost truth;
    it is attached to the surviving progress anchor on replay."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    (logs / "progress.jsonl").write_text(
        json.dumps({
            "ts": "2026-07-29T00:00:00Z",
            "content": "working on it",
            "task_id": "cost-terminal",
            "chat_id": 1,
        }) + "\n",
        encoding="utf-8",
    )
    results = tmp_path / "task_results"
    results.mkdir()
    (results / "cost-terminal.json").write_text(
        json.dumps({
            "task_id": "cost-terminal",
            "status": "completed",
            "cost_usd": 1.5,
            "cost_accounting_status": "available",
            "cost_final": True,
            "cost_usd_with_children": 2.75,
            "cost_with_children_partial": False,
        }),
        encoding="utf-8",
    )

    endpoint = make_chat_history_endpoint(tmp_path)
    response = asyncio.run(endpoint(SimpleNamespace(query_params={"limit": "10"})))
    payload = json.loads(response.body.decode("utf-8"))["messages"]

    rec = next(item for item in payload if item.get("task_id") == "cost-terminal")
    assert rec["task_terminal_status"] == "completed"
    assert rec["cost_usd"] == 1.5
    assert rec["cost_final"] is True
    assert rec["cost_usd_with_children"] == 2.75
    assert rec["cost_with_children_partial"] is False
    assert rec["cost_accounting_status"] == "available"


def test_chat_history_terminal_cost_truth_overrides_row_embedded_snapshot(tmp_path):
    """v6.82 P1 precedence: the persisted task result's cost fields OVERRIDE the
    row-embedded (pre-synthesis, non-final) task_summary snapshot on replay."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text(
        json.dumps({
            "ts": "2026-07-29T00:00:00Z",
            "direction": "system",
            "type": "task_summary",
            "task_id": "cost-override",
            "chat_id": 1,
            "text": "Task cost-override finished.",
            "tool_calls": 1,
            "rounds": 2,
            "cost_accounting_status": "available",
            "cost_final": False,
            "cost_usd_with_children": 1.0,
            "cost_with_children_partial": True,
        }) + "\n",
        encoding="utf-8",
    )
    (logs / "progress.jsonl").write_text("", encoding="utf-8")
    results = tmp_path / "task_results"
    results.mkdir()
    (results / "cost-override.json").write_text(
        json.dumps({
            "task_id": "cost-override",
            "status": "completed",
            "cost_usd": 0.9,
            "cost_accounting_status": "available",
            "cost_final": True,
            "cost_usd_with_children": 2.5,
            "cost_with_children_partial": False,
        }),
        encoding="utf-8",
    )

    endpoint = make_chat_history_endpoint(tmp_path)
    response = asyncio.run(endpoint(SimpleNamespace(query_params={"limit": "10"})))
    payload = json.loads(response.body.decode("utf-8"))["messages"]

    rec = next(item for item in payload if item.get("task_id") == "cost-override")
    assert rec["cost_final"] is True
    assert rec["cost_usd_with_children"] == 2.5
    assert rec["cost_with_children_partial"] is False
    assert rec["cost_usd"] == 0.9


def test_chat_history_preserves_nullable_cost_status_and_bounds(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    cost_meta = {
        "cost_usd": None,
        "cost_accounting_status": "unavailable",
        "cost_accounting_error": "ledger_unavailable",
        "cost_final": False,
        "cost_usd_with_children": None,
        "cost_with_children_partial": True,
        "reserved_usd": None,
        "unresolved_upper_bound_usd": None,
        "unknown_unmetered": None,
    }
    (logs / "progress.jsonl").write_text(
        json.dumps({
            "ts": "2026-07-14T00:00:00Z",
            "content": "terminal accounting status",
            "task_id": "cost-unavailable",
            **cost_meta,
        }) + "\n",
        encoding="utf-8",
    )

    endpoint = make_chat_history_endpoint(tmp_path)
    response = asyncio.run(endpoint(SimpleNamespace(query_params={"limit": "10"})))
    payload = json.loads(response.body.decode("utf-8"))["messages"]

    rec = next(item for item in payload if item.get("task_id") == "cost-unavailable")
    for field, expected in cost_meta.items():
        assert field in rec
        assert rec[field] == expected


def test_chat_history_preserves_cancelable_marker(tmp_path):
    """v6.82 (P5): the supervisor's host-attested `cancelable` progress-meta marker
    must survive history replay, or a reloaded live root card would lose its
    "Cancel run" action while the task is still running."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    (logs / "progress.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-07-29T00:00:00Z",
                "content": "Scheduled task root1: do the thing",
                "task_id": "root1",
                "is_progress": True,
                "cancelable": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    endpoint = make_chat_history_endpoint(tmp_path)
    response = asyncio.run(endpoint(SimpleNamespace(query_params={"limit": "10"})))
    payload = json.loads(response.body.decode("utf-8"))["messages"]

    rec = next(item for item in payload if item.get("task_id") == "root1")
    assert rec["cancelable"] is True
