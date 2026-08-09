"""Settings save budget hot-reload regression tests."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


def _settings_client(monkeypatch, tmp_path, current: dict):
    import server as srv
    import ouroboros.gateway.settings as gateway_settings

    monkeypatch.setattr(srv, "load_settings", lambda: dict(current))

    def fake_save_settings(settings, *args, **kwargs):
        current.clear()
        current.update(settings)

    monkeypatch.setattr(srv, "save_settings", fake_save_settings)
    monkeypatch.setattr(gateway_settings, "_owner_write_settings", fake_save_settings)
    monkeypatch.setattr(srv, "_apply_settings_to_env", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "_start_supervisor_if_needed", lambda *_a, **_k: False)
    monkeypatch.setattr(srv, "apply_runtime_provider_defaults", lambda s: (dict(s), False, []))
    monkeypatch.setattr(srv, "_mcp_reconfigure_startup", lambda *_a, **_k: None, raising=False)

    app = Starlette(routes=[Route("/api/settings", endpoint=srv.api_settings_post, methods=["POST"])])
    app.state.drive_root = tmp_path / "drive"
    app.state.repo_dir = tmp_path / "repo"
    return TestClient(app)


def test_settings_post_updates_budget_limits_and_per_task_threshold(monkeypatch, tmp_path):
    import supervisor.message_bus as bus_mod
    import supervisor.state as state_mod

    from ouroboros.config import SETTINGS_DEFAULTS as _defaults
    current = dict(_defaults)
    current["TOTAL_BUDGET"] = 10.0
    monkeypatch.setattr(state_mod, "TOTAL_BUDGET_LIMIT", 10.0)
    monkeypatch.setattr(bus_mod, "TOTAL_BUDGET_LIMIT", 10.0)

    client = _settings_client(monkeypatch, tmp_path, current)

    resp = client.post("/api/settings", json={"TOTAL_BUDGET": 25.0})

    assert resp.status_code == 200, resp.text
    assert resp.json().get("immediate_changed") is True
    assert state_mod.TOTAL_BUDGET_LIMIT == 25.0
    assert bus_mod.TOTAL_BUDGET_LIMIT == 25.0

    resp = client.post("/api/settings", json={"OUROBOROS_PER_TASK_COST_USD": "7.5"})

    assert resp.status_code == 200, resp.text
    assert resp.json().get("immediate_changed") is not True
    assert resp.json().get("next_task_changed") is True
    assert current["OUROBOROS_PER_TASK_COST_USD"] == 7.5

    invalid_cases = [
        ({"TOTAL_BUDGET": 0}, "greater than zero"),
        ({"TOTAL_BUDGET": 0.005}, "at least 0.01"),
        (["TOTAL_BUDGET", 25], "JSON body must be an object."),
        ({"OUROBOROS_PER_TASK_COST_USD": "nan"}, "must be a number"),
        ({"OUROBOROS_PER_TASK_COST_USD": "0.005"}, "at least 0.01"),
        ({"TOTAL_BUDGET": True}, "must be a number"),
    ]
    clean_budget_state = dict(current)
    clean_budget_state["TOTAL_BUDGET"] = 10.0
    clean_budget_state["OUROBOROS_PER_TASK_COST_USD"] = 20.0
    for payload, error in invalid_cases:
        current.clear()
        current.update(clean_budget_state)
        resp = client.post("/api/settings", json=payload)

        assert resp.status_code == 400
        assert error in resp.json()["error"]
        assert current["TOTAL_BUDGET"] == 10.0
        assert current["OUROBOROS_PER_TASK_COST_USD"] == 20.0


def test_settings_post_rejects_malformed_evolution_cadence(monkeypatch, tmp_path):
    """A direct API client must not be able to persist a malformed post-task evolution
    cadence (e.g. every_n:0) — backend half of the strict every_n validation contract."""

    key = "OUROBOROS_POST_TASK_EVOLUTION_CADENCE"
    from ouroboros.config import SETTINGS_DEFAULTS as _defaults
    current = dict(_defaults)
    current[key] = "llm"
    client = _settings_client(monkeypatch, tmp_path, current)

    for good in ("off", "llm", "every_n:1", "every_n:25"):
        resp = client.post("/api/settings", json={key: good})
        assert resp.status_code == 200, (good, resp.text)
        assert current[key] == good

    current[key] = "llm"
    for bad in ("every_n:0", "every_n:-1", "every_n:", "every_nonsense", "daily"):
        resp = client.post("/api/settings", json={key: bad})
        assert resp.status_code == 400, (bad, resp.text)
        assert "every_n:<positive int>" in resp.json()["error"]
        assert current[key] == "llm", bad  # not persisted


def test_settings_post_validates_and_applies_update_channel(monkeypatch, tmp_path):
    from ouroboros.config import SETTINGS_DEFAULTS as _defaults

    key = "OUROBOROS_UPDATE_CHANNEL"
    current = dict(_defaults)
    client = _settings_client(monkeypatch, tmp_path, current)

    for value in ("stable", "qa", "development"):
        resp = client.post("/api/settings", json={key: value.upper()})
        assert resp.status_code == 200, (value, resp.text)
        assert bool(resp.json().get("immediate_changed")) is (value != "stable")
        assert current[key] == value

    previous = current[key]
    for value in ("", "nightly", None):
        resp = client.post("/api/settings", json={key: value})
        assert resp.status_code == 400, (value, resp.text)
        assert "stable, qa, development" in resp.json()["error"]
        assert current[key] == previous


def test_settings_post_auto_downgrades_max_on_sub1m_route_change(monkeypatch, tmp_path):
    """v6.33.0 WS11 (owner decision): changing the model while Max is on stays
    FRICTION-FREE — the model change SUCCEEDS and, if the new route can't be
    confirmed ≥1M, context mode AUTO-DOWNGRADES to Low with a plain notice (not a
    409 that blocks the save)."""
    import ouroboros.capability_evidence as ce
    import ouroboros.config as cfg
    from ouroboros.config import SETTINGS_DEFAULTS as _defaults

    current = dict(_defaults)
    current["OUROBOROS_CONTEXT_MODE"] = "max"
    current["OUROBOROS_MODEL"] = "openai/gpt-5.5"
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE", "max")
    # Owned by monkeypatch so the auto-downgrade's direct os.environ write is undone
    # (the sibling scope-gate test below does the same). "" is the UNKNOWN baseline.
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE_AUTO_LOW", "")
    # Isolate the capability-evidence store so a cached confirmed/acked window from
    # another test cannot leak in (the probe reads config.DATA_DIR, not tmp_path).
    (tmp_path / "evidence-store").mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "evidence-store")
    # New route carries no >=1M evidence (unprobeable) and is REACHABLE — mock both the
    # metadata window AND the generative probe (v6.46.0) so no real network call is made;
    # a reachable-but-unprobeable route must auto-downgrade (a transport FAILURE is the
    # sibling 503 test, kept distinct).
    monkeypatch.setattr(ce, "_provider_metadata_window", lambda *a, **k: 0)
    monkeypatch.setattr(ce, "_generative_probe_window", lambda *a, **k: (0, ce.STATUS_UNPROBEABLE, "reachable-no-evidence"))

    client = _settings_client(monkeypatch, tmp_path, current)
    resp = client.post("/api/settings", json={"OUROBOROS_MODEL": "anthropic/claude-opus-4-8"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("context_mode_downgraded") is True
    assert body.get("context_mode") == "low"
    assert body.get("notice")  # plain-language explanation
    # The model change SUCCEEDED; context mode dropped to Low.
    assert current["OUROBOROS_MODEL"] == "anthropic/claude-opus-4-8"
    assert current["OUROBOROS_CONTEXT_MODE"] == "low"


def test_absent_auto_low_flag_still_leaves_scope_review_on(monkeypatch, tmp_path):
    """BIBLE P3 (v6.80.0): the derived auto-low flag is TRI-STATE and fail-CLOSED.

    A settings.json written before v6.80.0 — and any env allowlist that forwards the
    context mode without the derived flag — carries `low` with NO `..._AUTO_LOW` key.
    Reading that ABSENCE as "the owner chose low" would switch the immune scope gate
    off for every subsequent commit with no owner ever selecting it, which is exactly
    what an agent-reachable `{"OUROBOROS_MODEL": ...}` POST can produce. Absence must
    resolve to owner-`max`; only an explicit stored `false` is an owner declaration.
    """
    import json
    import os

    import ouroboros.config as cfg
    from ouroboros.tools import scope_review as sr

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"OUROBOROS_CONTEXT_MODE": "low"}), encoding="utf-8",
    )
    monkeypatch.setattr(cfg, "SETTINGS_PATH", settings_path)
    # Isolate the process env: apply_settings_to_env writes os.environ directly.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for _key in ("OUROBOROS_CONTEXT_MODE", "OUROBOROS_CONTEXT_MODE_AUTO_LOW"):
        os.environ.pop(_key, None)

    loaded = cfg.load_settings()
    assert loaded["OUROBOROS_CONTEXT_MODE"] == "low"
    assert loaded["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] == "", "absent stays UNKNOWN, not false"

    # A forwarded/system `true` is SEMANTICALLY identical to absence — neither is an owner
    # declaration — and benchmark launchers forward it deliberately alongside the mode, so
    # projection leaves it alone when the file is silent (it must not clobber what a launcher
    # forwarded). What has to hold is the RESOLVED tri-state, not the key's presence.
    os.environ["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] = "true"
    cfg.apply_settings_to_env(loaded)
    assert not cfg._owner_declared_low(os.environ.get("OUROBOROS_CONTEXT_MODE_AUTO_LOW"))
    assert cfg.get_context_mode() == "low"           # context sizing narrows...
    assert cfg.get_owner_context_mode() == "max"     # ...the P3 scope gate does not
    assert sr._scope_review_skipped_in_low_context() is False

    # The direction that DOES matter is still cleared fail-closed: env may not author the
    # owner-declared-low claim over a file that does not carry it.
    os.environ["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] = "false"
    cfg.apply_settings_to_env(loaded)
    assert "OUROBOROS_CONTEXT_MODE_AUTO_LOW" not in os.environ
    assert cfg.get_owner_context_mode() == "max"
    assert sr._scope_review_skipped_in_low_context() is False

    # An explicit owner declaration (only api_owner_context_mode writes one) does turn
    # the same effective mode into a declared scope-review skip.
    os.environ["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] = "false"
    assert cfg.get_owner_context_mode() == "low"
    assert sr._scope_review_skipped_in_low_context() is True


def test_derived_auto_low_is_exposed_so_the_owner_can_confirm_low(monkeypatch):
    """An auto-downgraded `low` must be CLEARABLE from the owner controls.

    Both owner paths short-circuit on `next === current` and `/api/state` exposed only
    the EFFECTIVE mode, so an install auto-downgraded to `low` — the very class for
    which CHECKLISTS #21 names `low` context mode as the replacement for the removed
    degraded advisory scope review — could never send the POST that clears the derived
    flag: scope review kept resolving a sub-floor window and blocking every commit.
    """
    import pathlib

    import ouroboros.config as cfg
    from ouroboros.gateway.contracts import StateResponse

    repo = pathlib.Path(__file__).resolve().parent.parent

    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE", "low")
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE_AUTO_LOW", "true")
    assert cfg.get_owner_context_mode() != cfg.get_context_mode(), "derived low"
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE_AUTO_LOW", "false")
    assert cfg.get_owner_context_mode() == cfg.get_context_mode(), "owner-declared low"

    assert "context_mode_auto_low" in StateResponse.__annotations__
    state_src = (repo / "ouroboros" / "gateway" / "state.py").read_text(encoding="utf-8")
    assert '"context_mode_auto_low": get_owner_context_mode() != get_context_mode()' in state_src

    # Both owner controls must POST a re-picked Low while the flag is set (the endpoint
    # is idempotent and clears it); the chat toggle reads the flag from /api/state and
    # Settings from its own payload.
    guard = "if (next === current && !(next === 'low' && derivedLow))"
    for module, source in (
        ("chat.js", "contextModeBtn.dataset.contextModeAutoLow === 'true'"),
        ("settings.js", "currentSettings?.OUROBOROS_CONTEXT_MODE_AUTO_LOW"),
    ):
        src = (repo / "web" / "modules" / module).read_text(encoding="utf-8")
        assert source in src, module
        assert guard in src, module
    chat_src = (repo / "web" / "modules" / "chat.js").read_text(encoding="utf-8")
    assert "data.context_mode_auto_low" in chat_src, "the toggle must consume /api/state"


def test_settings_auto_downgrade_narrows_context_but_keeps_scope_review_on(monkeypatch, tmp_path):
    """BIBLE P3 (v6.80.0): the agent cannot switch scope review off for its own commits.

    Since the context mode also decides scope-review applicability, the friction-free
    Max->Low auto-downgrade on the AGENT-REACHABLE `/api/settings` path (a plain
    `{"OUROBOROS_MODEL": ...}` POST names neither the context key nor settings.json, so
    no self-lowering guard sees it) must narrow context SIZING only: the owner-selected
    mode still reads `max` and scope review still runs.
    """
    import ouroboros.capability_evidence as ce
    import ouroboros.config as cfg
    from ouroboros.config import SETTINGS_DEFAULTS as _defaults
    from ouroboros.tools import scope_review as sr

    current = dict(_defaults)
    current["OUROBOROS_CONTEXT_MODE"] = "max"
    current["OUROBOROS_MODEL"] = "openai/gpt-5.5"
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE", "max")
    # Owned by monkeypatch so the endpoint's direct os.environ write is undone.
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE_AUTO_LOW", "false")
    (tmp_path / "evidence-store").mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "evidence-store")
    # Reachable route with NO >=1M evidence -> genuine confirmation, not probe_failed.
    monkeypatch.setattr(ce, "_provider_metadata_window", lambda *a, **k: 128_000)
    monkeypatch.setattr(
        ce, "_generative_probe_window",
        lambda *a, **k: (128_000, ce.STATUS_CONFIRMED, "128k-window"),
    )

    client = _settings_client(monkeypatch, tmp_path, current)
    resp = client.post("/api/settings", json={"OUROBOROS_MODEL": "openai/gpt-4o-mini"})

    assert resp.status_code == 200, resp.text
    assert resp.json().get("context_mode_downgraded") is True
    # The EFFECTIVE mode narrowed (context sizing behaves exactly as before)...
    assert current["OUROBOROS_CONTEXT_MODE"] == "low"
    assert cfg.get_context_mode() == "low"
    # ...but the OWNER-selected mode is untouched, in settings and in env.
    assert current["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] == "true"
    assert cfg.get_owner_context_mode() == "max"

    # ...so the P3 scope gate is still ON: the reviewer is assembled and called.
    assert sr._scope_review_skipped_in_low_context() is False

    called: list = []

    class _Ctx:
        repo_dir = str(tmp_path)
        task_id = "auto-low-scope-still-on"
        pending_events: list = []

        def drive_logs(self):
            return tmp_path

    monkeypatch.setattr(sr, "_build_scope_prompt", lambda *a, **k: called.append(1) or ("p", None))
    monkeypatch.setattr(sr, "_call_scope_llm", lambda *a, **k: called.append(1) or ("", None, ""))
    result = sr.run_scope_review(_Ctx(), "test commit", scope_model="anthropic/claude-fable-5")
    assert called, "an auto-downgraded effective mode must not skip scope review"
    assert result.status != "skipped_low_context_mode"


def test_auto_low_flag_cannot_be_cleared_by_a_generic_save(monkeypatch, tmp_path):
    """BIBLE P3: the agent cannot switch its own scope gate off through save_settings.

    Since v6.80.0 the derived flag is an AUTHORITY BIT: with the effective mode already
    `low`, clearing it makes get_owner_context_mode report `low` and
    _scope_review_skipped_in_low_context() disable the P3 gate. The gateway merge guard
    only covers the HTTP payload, so without a CONFIG-layer ratchet an agent subprocess
    could load settings, flip that one key, save, and inherit a scope-review skip on the
    next start. This test drives the REAL save_settings() and the real reload, because a
    test against an internal helper would stay green while the hole stayed open.
    """
    import json
    import os

    import pytest

    import ouroboros.config as cfg
    from ouroboros.gateway.settings import _owner_write_settings
    from ouroboros.tools import scope_review as sr

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "OUROBOROS_CONTEXT_MODE": "low",
        "OUROBOROS_CONTEXT_MODE_AUTO_LOW": "true",  # SYSTEM auto-downgrade, not the owner
    }), encoding="utf-8")
    monkeypatch.setattr(cfg, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "_BOOT_RUNTIME_MODE", None)
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for _key in ("OUROBOROS_CONTEXT_MODE", "OUROBOROS_CONTEXT_MODE_AUTO_LOW",
                 "OUROBOROS_RUNTIME_MODE", cfg.BOOT_RUNTIME_MODE_ENV_KEY):
        os.environ.pop(_key, None)

    # A generic caller: load the real settings, flip ONLY the derived flag, save.
    loaded = cfg.load_settings()
    assert loaded["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] == "true"
    loaded["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] = "false"
    with pytest.raises(PermissionError, match="OUROBOROS_CONTEXT_MODE_AUTO_LOW"):
        cfg.save_settings(loaded)

    # Nothing reached the file, so the reload path still leaves the P3 gate ON.
    reloaded = cfg.load_settings()
    assert reloaded["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] == "true"
    cfg.apply_settings_to_env(reloaded)
    assert cfg.get_context_mode() == "low"          # context sizing stays narrowed...
    assert cfg.get_owner_context_mode() == "max"    # ...the owner horizon does not move
    assert sr._scope_review_skipped_in_low_context() is False

    # UNKNOWN (a pre-v6.80.0 file with no flag at all) -> false is refused too: absence
    # is not an owner declaration, so authoring one is still the owner's move.
    settings_path.write_text(json.dumps({"OUROBOROS_CONTEXT_MODE": "low"}), encoding="utf-8")
    unknown = cfg.load_settings()
    unknown["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] = "false"
    with pytest.raises(PermissionError, match="unknown"):
        cfg.save_settings(unknown)

    # The SYSTEM direction is untouched: the auto-downgrade may still set the flag.
    system = cfg.load_settings()
    system["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] = "true"
    cfg.save_settings(system)
    assert cfg.load_settings()["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] == "true"

    # And the OWNER path (the same allow_context_lowering authorisation the dedicated
    # endpoint holds) does clear it — the exception stays reachable to its owner.
    owner = cfg.load_settings()
    owner["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] = "false"
    _owner_write_settings(owner, allow_context_lowering=True)
    final = cfg.load_settings()
    assert final["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] == "false"
    cfg.apply_settings_to_env(final)
    assert cfg.get_owner_context_mode() == "low"
    assert sr._scope_review_skipped_in_low_context() is True


def test_env_inherited_auto_low_does_not_break_ordinary_settings_saves(monkeypatch, tmp_path):
    """The derived auto-low flag is DISK-authored: load_settings must not overlay env.

    PRODUCTION regression, not test hygiene. The ratchet reads the previous value with
    `_settings_file_value` — DISK-only for every key it guards, because env is
    inherited and freely rewritten by agent subprocesses. But `load_settings` used to
    overlay env onto any key ABSENT from settings.json, so a process whose env declared
    owner-low over a file that carried no such key got `false` handed to it, and the very
    next ordinary save was refused: `POST /api/settings` (`_owner_write_settings` with
    allow_context_lowering=False when no auto-downgrade fired) and `_set_tool_timeout`
    both 500 on a `PermissionError` nobody authored. That divergence is REACHABLE: the
    benchmark env allowlists forward this flag (server_runner, harbor_installed_agent,
    run_clb) while `build_isolated_settings` only copies it when the LIVE settings file
    already carries it — so an isolated benchmark server hits exactly this state.

    The failure direction is what makes the fix safe. The overlay only ever applied to a
    DISK-ABSENT key (a value already stored on disk short-circuits it), so the sole
    behavioural change is `unknown -> false` becoming `unknown -> unknown`. UNKNOWN
    resolves FAIL-CLOSED, so the P3 scope gate stays ON: this can cause more review than
    intended, never less. A genuine owner declaration STORED on disk is untouched.
    """
    import json
    import os

    import ouroboros.config as cfg
    from ouroboros.tools import scope_review as sr

    settings_path = tmp_path / "settings.json"
    # Pre-v6.80.0 / isolated-benchmark shape: `low` with NO derived flag stored.
    settings_path.write_text(json.dumps({"OUROBOROS_CONTEXT_MODE": "low"}), encoding="utf-8")
    monkeypatch.setattr(cfg, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "_BOOT_RUNTIME_MODE", None)
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for _key in ("OUROBOROS_CONTEXT_MODE", "OUROBOROS_RUNTIME_MODE", cfg.BOOT_RUNTIME_MODE_ENV_KEY):
        os.environ.pop(_key, None)
    # The env — and ONLY the env — declares owner-low (inherited, forwarded, exported).
    os.environ["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] = "false"

    loaded = cfg.load_settings()
    assert loaded["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] == "", "env must not author the derived flag"
    cfg.save_settings(loaded)  # the PermissionError 500 this regression pins down

    # Fail-CLOSED after the save: the stale env value is cleared and the gate stays ON.
    cfg.apply_settings_to_env(cfg.load_settings())
    assert "OUROBOROS_CONTEXT_MODE_AUTO_LOW" not in os.environ
    assert cfg.get_context_mode() == "low"           # context sizing still narrows...
    assert cfg.get_owner_context_mode() == "max"     # ...the P3 gate does not switch off
    assert sr._scope_review_skipped_in_low_context() is False

    # The other direction is NOT weakened: a declaration actually STORED on disk (only
    # api_owner_context_mode writes one) is still honoured, env overlay or not.
    settings_path.write_text(json.dumps({
        "OUROBOROS_CONTEXT_MODE": "low",
        "OUROBOROS_CONTEXT_MODE_AUTO_LOW": "false",
    }), encoding="utf-8")
    stored = cfg.load_settings()
    assert stored["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] == "false"
    cfg.apply_settings_to_env(stored)
    assert cfg.get_owner_context_mode() == "low"
    assert sr._scope_review_skipped_in_low_context() is True


def test_settings_post_errors_on_max_route_change_when_provider_unreachable(monkeypatch, tmp_path):
    """v6.33.0 WS11 P4 (owner decision): a genuine NO-CONNECTION during the
    max-mode probe is an ERROR (503), NOT a silent downgrade — and the model is
    NOT saved (distinct from a sub-1M/unprobeable route, which auto-downgrades)."""
    import ouroboros.capability_evidence as ce
    import ouroboros.config as cfg
    from ouroboros.config import SETTINGS_DEFAULTS as _defaults

    current = dict(_defaults)
    current["OUROBOROS_CONTEXT_MODE"] = "max"
    current["OUROBOROS_MODEL"] = "openrouter/gpt-5.5"
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE", "max")
    (tmp_path / "evidence-store").mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "evidence-store")
    # Provider unreachable: no metadata window AND a transport failure.
    monkeypatch.setattr(ce, "_provider_metadata_window", lambda *a, **k: 0)
    monkeypatch.setattr(ce, "_metadata_fetch_transport_failed", lambda *a, **k: True)

    client = _settings_client(monkeypatch, tmp_path, current)
    resp = client.post("/api/settings", json={"OUROBOROS_MODEL": "openrouter/other-model"})

    assert resp.status_code == 503, resp.text
    assert "connection" in resp.json().get("error", "").lower()
    # The model was NOT saved — the error path returns before persistence.
    assert current["OUROBOROS_MODEL"] == "openrouter/gpt-5.5"
    assert current["OUROBOROS_CONTEXT_MODE"] == "max"


def test_max_context_auto_downgrade_writes_typed_attribution_event(tmp_path, monkeypatch):
    """W3 adjacent (e): the system-initiated Max->Low narrowing (model change onto
    an unverified route) used to leave ZERO rows in events.jsonl — the submarine
    forensics mis-attributed it to the owner. It now writes a typed
    context_mode_auto_downgraded event with actor=system_auto_low."""
    import json

    from ouroboros.gateway import settings as gw_settings

    monkeypatch.setattr(gw_settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        gw_settings, "_max_context_block",
        lambda *_a, **_k: {"error": "confirmed window 500K < 1M"},
    )
    monkeypatch.setattr(
        gw_settings, "_active_main_route",
        lambda s, **_k: {
            "provider": "openrouter",
            "model": str(s.get("OUROBOROS_MODEL") or ""),
            "base_url": "",
            "use_local": False,
        },
    )
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE", "max")
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE_AUTO_LOW", "false")

    current = {"OUROBOROS_MODEL": "x-ai/grok-4.5"}
    old = {"OUROBOROS_MODEL": "anthropic/claude-opus-5"}
    notice, probe_error = gw_settings._apply_max_context_auto_downgrade(current, old)

    assert probe_error is None
    assert "Context mode switched to Low" in str(notice)
    assert current["OUROBOROS_CONTEXT_MODE"] == "low"
    assert current["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] == "true"

    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    events = [r for r in rows if r.get("type") == "context_mode_auto_downgraded"]
    assert len(events) == 1
    ev = events[0]
    assert ev["actor"] == "system_auto_low"
    assert (ev["from_mode"], ev["to_mode"]) == ("max", "low")
    assert ev["model"] == "x-ai/grok-4.5"
    assert "500K" in ev["reason"]
