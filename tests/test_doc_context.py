"""Tests for the low/max reference-doc layout in the always-on agent context.

Doc matrix (D-ARCH unification, owner 2026-08-08):
  ARCHITECTURE follows the OWNER MODE alone: full in max for EVERY task class
       (self-body, project tasks with or without a folder, evolution, external
       surfaces); navigation map in low. It is Ouroboros's capability/tools/
       access map — never dropped per-task in max.
  DEVELOPMENT is MODE-INDEPENDENT per task class, keyed by D-DEV (owner
       2026-08-08) on the ACTIVE REPO BINDING — the handbook loads exactly when
       the work targets Ouroboros's own body, a path fact and never a guess from
       message text (P5). On-demand pointer for the EXTERNAL-SURFACE class (a
       bound workspace incl. an auto-provisioned genesis tree, a subagent, an
       api/cli/scheduled surface); full for everything still bound to the system
       repo — including a direct-chat turn in a PROJECT ROOM, which binds no
       workspace. Project MEMBERSHIP is deliberately NOT the signal. Explicit
       context_requires_development / context_requires_self_body_docs win.
  README/CHECKLISTS: on-demand pointer in all modes.
SYSTEM + BIBLE are tier-0 and always full.
"""

import os
import pathlib
import tempfile

# Unique sentinel placed inside the ARCHITECTURE body so we can prove the full
# body is inlined (max) vs replaced by a structure-only nav map (low).
_ARCH_BODY_SENTINEL = "ARCH_BODY_SENTINEL_XYZ"


def _make_env_and_memory(tmpdir: pathlib.Path):
    from ouroboros.agent import Env
    from ouroboros.memory import Memory

    repo_dir = tmpdir / "repo"
    drive_root = tmpdir / "drive"
    repo_dir.mkdir(parents=True, exist_ok=True)
    drive_root.mkdir(parents=True, exist_ok=True)
    for subdir in ["state", "memory", "memory/knowledge", "logs"]:
        (drive_root / subdir).mkdir(parents=True, exist_ok=True)
    (repo_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (repo_dir / "docs").mkdir(parents=True, exist_ok=True)
    (repo_dir / "prompts" / "SYSTEM.md").write_text("You are Ouroboros.", encoding="utf-8")
    (repo_dir / "BIBLE.md").write_text("# Principle 0: Agency", encoding="utf-8")
    (repo_dir / "docs" / "ARCHITECTURE.md").write_text(
        "# Ouroboros v5.5.0 — Architecture\n\n## Section A\n\n"
        + _ARCH_BODY_SENTINEL
        + " alpha\n\n## Section B\n\nbeta\n",
        encoding="utf-8",
    )
    (repo_dir / "docs" / "DEVELOPMENT.md").write_text("# DEVELOPMENT.md — Dev Guide", encoding="utf-8")
    (repo_dir / "README.md").write_text('[![Version 5.5.0](https://img.shields.io/badge/version-5.5.0-green.svg)](VERSION)', encoding="utf-8")
    (repo_dir / "docs" / "CHECKLISTS.md").write_text("## Repo Commit Checklist\n| # | item |", encoding="utf-8")
    (drive_root / "state" / "state.json").write_text('{"spent_usd": 0}', encoding="utf-8")
    (drive_root / "memory" / "scratchpad.md").write_text("test scratchpad", encoding="utf-8")
    (drive_root / "memory" / "identity.md").write_text("I am Ouroboros.", encoding="utf-8")
    env = Env(repo_dir=repo_dir, drive_root=drive_root)
    memory = Memory(drive_root=drive_root, repo_dir=repo_dir)
    return env, memory


def _build_system_text(task_overrides=None, *, context_mode="max"):
    from ouroboros.context import build_llm_messages
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    env, memory = _make_env_and_memory(tmpdir)
    task = {"id": "test-1", "type": "task", "text": "hello"}
    if task_overrides:
        task.update(task_overrides)
    prev = os.environ.get("OUROBOROS_CONTEXT_MODE")
    os.environ["OUROBOROS_CONTEXT_MODE"] = context_mode
    try:
        messages, _ = build_llm_messages(env=env, memory=memory, task=task)
    finally:
        if prev is None:
            os.environ.pop("OUROBOROS_CONTEXT_MODE", None)
        else:
            os.environ["OUROBOROS_CONTEXT_MODE"] = prev
    content = messages[0]["content"]
    return " ".join(block.get("text", "") for block in content if isinstance(block, dict))


def test_max_mode_inlines_architecture_and_development_in_full():
    text = _build_system_text(context_mode="max")
    assert "## ARCHITECTURE.md" in text
    assert _ARCH_BODY_SENTINEL in text  # full body inlined
    assert "navigation map" not in text
    assert "## DEVELOPMENT.md" in text


def test_max_mode_external_workspace_keeps_arch_full_but_drops_development():
    """D-ARCH: ARCHITECTURE is full-resident in max for EVERY class, including
    the external-surface class; DEVELOPMENT (the self-engineering handbook)
    is the on-demand pointer there — external work targets OTHER codebases."""
    from ouroboros.contracts.task_contract import build_task_contract

    external = _build_system_text(
        {
            "workspace_root": "/tmp/example-workspace",
            "workspace_mode": "external",
            "actor_id": "cli",
            "metadata": {"source": "cli"},
        },
        context_mode="max",
    )
    assert "## ARCHITECTURE.md" in external
    assert _ARCH_BODY_SENTINEL in external  # capability map stays resident
    assert "navigation map" not in external
    assert "## DEVELOPMENT.md" not in external  # handbook is the pointer
    assert "DEVELOPMENT.md" in external  # ...but visibly named (P1)

    self_body = _build_system_text(
        {
            "workspace_root": "/tmp/example-workspace",
            "workspace_mode": "external",
            "actor_id": "cli",
            "metadata": {"source": "cli"},
            "context_requires_self_body_docs": True,
        },
        context_mode="max",
    )
    assert _ARCH_BODY_SENTINEL in self_body
    assert "## DEVELOPMENT.md" in self_body  # explicit self-body keeps DEV full

    contract = build_task_contract({
        "id": "task-docs",
        "context_requires_self_body_docs": "true",
        "metadata": {"source": "api_task"},
    })

    assert contract["context_requires_self_body_docs"] is True

    contract_false = build_task_contract({
        "id": "task-docs-false",
        "context_requires_self_body_docs": "false",
        "metadata": {"source": "api_task"},
    })
    assert contract_false["context_requires_self_body_docs"] is False


def test_low_mode_external_workspace_gets_nav_arch_and_dev_pointer():
    external = _build_system_text(
        {
            "workspace_root": "/tmp/example-workspace",
            "workspace_mode": "external",
            "actor_id": "cli",
            "metadata": {"source": "cli"},
        },
        context_mode="low",
    )
    assert "navigation map" in external
    assert _ARCH_BODY_SENTINEL not in external
    assert "## DEVELOPMENT.md" not in external


def test_max_mode_evolution_task_keeps_arch_and_development_full():
    """D-ARCH (owner, 2026-08-08): evolution gets the FULL capability map in
    max ('и эволюции тоже её класть нужно') — the v6.30.0 nav-map downgrade is
    removed — and keeps the engineering handbook inline."""
    text = _build_system_text({"type": "evolution"}, context_mode="max")
    assert "## ARCHITECTURE.md" in text
    assert _ARCH_BODY_SENTINEL in text
    assert "navigation map" not in text
    assert "## DEVELOPMENT.md" in text  # handbook still full

    # Deep self-review keeps the full self-body docs (unchanged).
    review_text = _build_system_text({"type": "deep_self_review"}, context_mode="max")
    assert _ARCH_BODY_SENTINEL in review_text

    # In low mode evolution stays on the cheap form: nav ARCH + full DEV.
    low_text = _build_system_text({"type": "evolution"}, context_mode="low")
    assert "navigation map" in low_text
    assert _ARCH_BODY_SENTINEL not in low_text
    assert "## DEVELOPMENT.md" in low_text


def test_development_keys_on_the_repo_binding_not_project_membership():
    """D-DEV (owner 2026-08-08): the handbook loads iff the work targets
    Ouroboros's own body, and the structural signal is the ACTIVE REPO BINDING —
    NOT `project_id`. ARCHITECTURE is unaffected and follows the owner mode alone
    (D-ARCH): full in max for every class, navigation map in low."""
    # A project task with a BOUND workspace (this is what a promoted project task
    # is since Q10=A auto-provisions a genesis tree) works on ANOTHER codebase.
    folder_max = _build_system_text(
        {"project_id": "proj_sub", "workspace_root": "/tmp/proj-tree", "workspace_mode": "external"},
        context_mode="max",
    )
    assert _ARCH_BODY_SENTINEL in folder_max  # ARCH full in max, always
    assert "## DEVELOPMENT.md" not in folder_max
    assert "DEVELOPMENT.md" in folder_max  # named in the on-demand pointer

    # A direct-chat turn in a PROJECT ROOM binds no workspace: still Ouroboros's
    # own body, so it KEEPS the handbook. This is the case the project_id-keyed
    # draft got wrong.
    room_chat_max = _build_system_text(
        {"project_id": "proj_sub", "_is_direct_chat": True}, context_mode="max"
    )
    assert _ARCH_BODY_SENTINEL in room_chat_max
    assert "## DEVELOPMENT.md" in room_chat_max
    room_chat_low = _build_system_text(
        {"project_id": "proj_sub", "_is_direct_chat": True}, context_mode="low"
    )
    assert "navigation map" in room_chat_low
    assert _ARCH_BODY_SENTINEL not in room_chat_low
    assert "## DEVELOPMENT.md" in room_chat_low

    # workspace="none" binds nothing -> the task is not external -> keeps it.
    opt_out = _build_system_text(
        {"project_id": "proj_sub", "workspace": "none"}, context_mode="max"
    )
    assert "## DEVELOPMENT.md" in opt_out

    # Evolution / self-body keep it through the self-body branch even in a room.
    evolution = _build_system_text(
        {"project_id": "proj_sub", "type": "evolution"}, context_mode="max"
    )
    assert "## DEVELOPMENT.md" in evolution

    # Explicit per-task overrides still win in BOTH directions.
    explicit_dev = _build_system_text(
        {
            "project_id": "proj_sub", "workspace_root": "/tmp/proj-tree",
            "workspace_mode": "external", "context_requires_development": True,
        },
        context_mode="max",
    )
    assert "## DEVELOPMENT.md" in explicit_dev
    explicit_self_body = _build_system_text(
        {
            "project_id": "proj_sub", "workspace_root": "/tmp/proj-tree",
            "workspace_mode": "external", "context_requires_self_body_docs": True,
        },
        context_mode="max",
    )
    assert "## DEVELOPMENT.md" in explicit_self_body
    explicit_off = _build_system_text(
        {"project_id": "proj_sub", "context_requires_development": False}, context_mode="max"
    )
    assert "## DEVELOPMENT.md" not in explicit_off


def test_readme_and_checklists_are_on_demand_pointer_in_both_modes():
    for mode in ("max", "low"):
        text = _build_system_text(context_mode=mode)
        assert "Reference docs available on demand" in text
        # Named in the pointer (visible, never silently dropped) but not inlined.
        assert "README.md" in text
        assert "CHECKLISTS.md" in text


def test_low_mode_architecture_is_navigation_map_not_full_body():
    text = _build_system_text(context_mode="low")
    assert "navigation map" in text
    assert "Section A" in text and "Section B" in text  # headings present
    assert _ARCH_BODY_SENTINEL not in text  # full body NOT inlined in low


def test_low_mode_development_full_for_direct_chat_tasks_unless_explicitly_disabled():
    code_text = _build_system_text({"type": "task"}, context_mode="low")
    assert "## DEVELOPMENT.md" in code_text  # code / self-mod task → full

    chat_text = _build_system_text({"_is_direct_chat": True}, context_mode="low")
    assert "## DEVELOPMENT.md" in chat_text  # chat can still be code / self-mod work

    pure_chat_text = _build_system_text(
        {"_is_direct_chat": True, "context_requires_development": False},
        context_mode="low",
    )
    assert "## DEVELOPMENT.md" not in pure_chat_text
    assert "DEVELOPMENT.md" in pure_chat_text  # but named in the on-demand pointer


# --- v6.64: exact-route probe-on-first-use, with no invented 200K fallback ---

def test_maybe_downgrade_threads_allow_fetch_and_stays_max_when_confirmed():
    from types import SimpleNamespace
    from unittest.mock import patch
    import ouroboros.context as context_mod
    from ouroboros.loop import _maybe_downgrade_max_unconfirmed

    seen = {}

    def _fake_route(task, *, allow_fetch=False):
        seen["allow_fetch"] = allow_fetch
        return {}, SimpleNamespace(status="confirmed", stale=False, window_tokens=1_000_000)

    with patch.object(context_mod, "_context_fit_route", _fake_route):
        out = _maybe_downgrade_max_unconfirmed("max", False, "z-ai/glm-5.2", allow_fetch=True)
    assert out == "max"  # confirmed -> stays max (lazy probe succeeded)
    assert seen["allow_fetch"] is True  # allow_fetch threaded through

    # Missing evidence is unknown, not a silent 200K route: try Max once.
    with patch.object(
        context_mod,
        "_context_fit_route",
        lambda *_a, **_kw: ({}, SimpleNamespace(status="unprobeable", stale=False, window_tokens=0)),
    ):
        assert _maybe_downgrade_max_unconfirmed("max", False, "m", allow_fetch=True) == "max"
