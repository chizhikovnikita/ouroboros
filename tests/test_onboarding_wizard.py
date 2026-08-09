import pathlib

import pytest

from ouroboros.onboarding_wizard import build_onboarding_html, prepare_onboarding_settings
from ouroboros.settings_setup_contract import build_setup_bootstrap, build_setup_contract


REPO = pathlib.Path(__file__).resolve().parents[1]


def _base_payload() -> dict:
    return {
        "OPENROUTER_API_KEY": "",
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "MINIMAX_API_KEY": "",
        "MINIMAX_REGION": "",
        "TOTAL_BUDGET": 10,
        "OUROBOROS_PER_TASK_COST_USD": 20,
        "OUROBOROS_REVIEW_ENFORCEMENT": "advisory",
        "LOCAL_MODEL_SOURCE": "",
        "LOCAL_MODEL_FILENAME": "",
        "LOCAL_MODEL_CONTEXT_LENGTH": 16384,
        "LOCAL_MODEL_N_GPU_LAYERS": -1,
        "LOCAL_MODEL_CHAT_FORMAT": "",
        "LOCAL_ROUTING_MODE": "cloud",
        "OUROBOROS_MODEL": "openai::gpt-5.5",
        "OUROBOROS_MODEL_HEAVY": "openai::gpt-5.5",
        "OUROBOROS_MODEL_LIGHT": "openai::gpt-5.5-mini",
        "OUROBOROS_MODEL_FALLBACKS": "openai::gpt-5.5-mini",
    }


def test_prepare_onboarding_settings_requires_runnable_config():
    prepared, error = prepare_onboarding_settings(_base_payload(), {})

    assert prepared == {}
    assert "Configure OpenRouter, OpenAI, OpenAI-compatible, Cloud.ru, MiniMax, Anthropic, or a local model" in error


def test_prepare_onboarding_settings_accepts_openai_only_setup():
    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"

    prepared, error = prepare_onboarding_settings(payload, {})

    assert error is None
    assert prepared["OPENAI_API_KEY"] == "sk-openai-1234567890"
    assert prepared["OUROBOROS_MODEL"] == "openai::gpt-5.5"
    assert prepared["TOTAL_BUDGET"] == 10.0
    assert prepared["OUROBOROS_PER_TASK_COST_USD"] == 20.0
    assert prepared["OUROBOROS_REVIEW_ENFORCEMENT"] == "advisory"
    assert prepared["OUROBOROS_MODEL_CONSCIOUSNESS"] == ""
    # Onboarding no longer manages auto-grant; the global SSOT default applies.
    assert "OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS" not in prepared


def test_settings_default_auto_grant_is_true():
    from ouroboros.config import SETTINGS_DEFAULTS

    assert SETTINGS_DEFAULTS["OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS"] == "true"


def test_prepare_onboarding_settings_preserves_existing_auto_grant_choice():
    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"

    prepared, error = prepare_onboarding_settings(
        payload,
        {"OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS": "false"},
    )

    assert error is None
    # Onboarding does not override an explicit existing choice.
    assert prepared["OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS"] == "false"


@pytest.mark.parametrize(("key", "value", "error"), [
    ("TOTAL_BUDGET", 0, "Budget must be greater than zero."),
    ("TOTAL_BUDGET", "0", "Budget must be greater than zero."),
    ("TOTAL_BUDGET", -1, "Budget must be greater than zero."),
    ("TOTAL_BUDGET", 0.005, "Budget must be at least 0.01."),
    ("TOTAL_BUDGET", "nan", "Budget must be a number."),
    ("OUROBOROS_PER_TASK_COST_USD", 0, "Per-task cost cap must be greater than zero."),
    ("OUROBOROS_PER_TASK_COST_USD", "0", "Per-task cost cap must be greater than zero."),
    ("OUROBOROS_PER_TASK_COST_USD", -1, "Per-task cost cap must be greater than zero."),
    ("OUROBOROS_PER_TASK_COST_USD", 0.005, "Per-task cost cap must be at least 0.01."),
    ("OUROBOROS_PER_TASK_COST_USD", "nan", "Per-task cost cap must be a number."),
    ("OUROBOROS_PER_TASK_COST_USD", False, "Per-task cost cap must be a number."),
])
def test_prepare_onboarding_settings_rejects_invalid_budget_values(key, value, error):
    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"
    payload[key] = value

    prepared, actual_error = prepare_onboarding_settings(payload, {})

    assert prepared == {}
    assert actual_error == error


def test_prepare_onboarding_settings_accepts_cloudru_only_setup():
    payload = _base_payload()
    payload["CLOUDRU_FOUNDATION_MODELS_API_KEY"] = "cloudru-key-1234567890"
    payload["OUROBOROS_MODEL"] = "cloudru::zai-org/GLM-4.7"
    payload["OUROBOROS_MODEL_HEAVY"] = "cloudru::zai-org/GLM-4.7"
    payload["OUROBOROS_MODEL_LIGHT"] = "cloudru::zai-org/GLM-4.7"
    payload["OUROBOROS_MODEL_FALLBACKS"] = "cloudru::zai-org/GLM-4.7"

    prepared, error = prepare_onboarding_settings(payload, {})

    assert error is None
    assert prepared["CLOUDRU_FOUNDATION_MODELS_API_KEY"] == "cloudru-key-1234567890"
    assert prepared["OUROBOROS_MODEL"] == "cloudru::zai-org/GLM-4.7"


def test_prepare_onboarding_settings_accepts_minimax_only_setup():
    payload = _base_payload()
    payload.update({
        "MINIMAX_API_KEY": "minimax-key-1234567890",
        "MINIMAX_REGION": "CN_ZH",
        "OUROBOROS_MODEL": "minimax::MiniMax-M3",
        "OUROBOROS_MODEL_HEAVY": "minimax::MiniMax-M3",
        "OUROBOROS_MODEL_LIGHT": "minimax::MiniMax-M2.7",
        "OUROBOROS_MODEL_FALLBACKS": "minimax::MiniMax-M2.7",
    })

    prepared, error = prepare_onboarding_settings(payload, {})

    assert error is None
    assert prepared["MINIMAX_API_KEY"] == "minimax-key-1234567890"
    assert prepared["MINIMAX_REGION"] == "cn_zh"
    assert prepared["OUROBOROS_MODEL"] == "minimax::MiniMax-M3"
    assert prepared["OUROBOROS_MODEL_LIGHT"] == "minimax::MiniMax-M2.7"


def test_prepare_onboarding_settings_rejects_unknown_minimax_region():
    payload = _base_payload()
    payload["MINIMAX_API_KEY"] = "minimax-key-1234567890"
    payload["MINIMAX_REGION"] = "unknown"

    prepared, error = prepare_onboarding_settings(payload, {})

    assert prepared == {}
    assert error == "MiniMax Region must be global_en or cn_zh."


def test_prepare_onboarding_settings_accepts_anthropic_only_setup():
    payload = _base_payload()
    payload["ANTHROPIC_API_KEY"] = "sk-ant-1234567890"
    payload["OUROBOROS_MODEL"] = "anthropic::claude-opus-4-6"
    payload["OUROBOROS_MODEL_HEAVY"] = "anthropic::claude-opus-4-6"
    payload["OUROBOROS_MODEL_LIGHT"] = "anthropic::claude-sonnet-4-6"
    payload["OUROBOROS_MODEL_FALLBACKS"] = "anthropic::claude-sonnet-4-6"

    prepared, error = prepare_onboarding_settings(payload, {})

    assert error is None
    assert prepared["ANTHROPIC_API_KEY"] == "sk-ant-1234567890"
    assert prepared["OUROBOROS_MODEL"] == "anthropic::claude-opus-4-6"


def test_prepare_onboarding_settings_rejects_local_only_cloud_routing():
    payload = _base_payload()
    payload["LOCAL_MODEL_SOURCE"] = "Qwen/Qwen2.5-7B-Instruct-GGUF"
    payload["LOCAL_MODEL_FILENAME"] = "qwen2.5-7b-instruct-q3_k_m.gguf"
    payload["LOCAL_ROUTING_MODE"] = "cloud"

    prepared, error = prepare_onboarding_settings(payload, {})

    assert prepared == {}
    assert error == "Local-only setups must route at least one model to the local runtime."


def test_prepare_onboarding_settings_rejects_consciousness_only_local_routing():
    payload = _base_payload()
    payload["LOCAL_MODEL_SOURCE"] = "Qwen/Qwen2.5-7B-Instruct-GGUF"
    payload["LOCAL_MODEL_FILENAME"] = "qwen2.5-7b-instruct-q3_k_m.gguf"
    payload["LOCAL_ROUTING_MODE"] = "cloud"
    payload["USE_LOCAL_CONSCIOUSNESS"] = True

    prepared, error = prepare_onboarding_settings(payload, {})

    assert prepared == {}
    assert error == "Local-only setups must route at least one model to the local runtime."


def test_prepare_onboarding_settings_sets_all_local_routes():
    payload = _base_payload()
    payload["LOCAL_MODEL_SOURCE"] = "Qwen/Qwen2.5-7B-Instruct-GGUF"
    payload["LOCAL_MODEL_FILENAME"] = "qwen2.5-7b-instruct-q3_k_m.gguf"
    payload["LOCAL_ROUTING_MODE"] = "all"

    prepared, error = prepare_onboarding_settings(payload, {})

    assert error is None
    assert prepared["USE_LOCAL_MAIN"] is True
    assert prepared["USE_LOCAL_HEAVY"] is True
    assert prepared["USE_LOCAL_LIGHT"] is True
    assert prepared["USE_LOCAL_FALLBACK"] is True


def test_prepare_onboarding_settings_preserves_non_wizard_provider_fields():
    """The wizard only edits fields it actually exposes. Settings fields
    that live in ``settings_ui.js`` but not in the wizard (``OPENAI_BASE_URL``,
    ``CLOUDRU_FOUNDATION_MODELS_BASE_URL``) must survive re-running onboarding.
    ``OPENAI_COMPATIBLE_*`` are now wizard-managed and come from the payload."""
    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"
    payload["OPENAI_COMPATIBLE_BASE_URL"] = "https://compat.example/v1"
    payload["OPENAI_COMPATIBLE_API_KEY"] = "compat-secret-xyz"
    current = {
        "OPENAI_BASE_URL": "https://legacy.example/v1",
        "CLOUDRU_FOUNDATION_MODELS_BASE_URL": "https://cloud.example/v1",
    }

    prepared, error = prepare_onboarding_settings(payload, current)

    assert error is None
    # Non-wizard fields are preserved from current settings.
    assert prepared["OPENAI_BASE_URL"] == "https://legacy.example/v1"
    assert prepared["CLOUDRU_FOUNDATION_MODELS_BASE_URL"] == "https://cloud.example/v1"
    # Compatible fields come from the wizard payload.
    assert prepared["OPENAI_COMPATIBLE_BASE_URL"] == "https://compat.example/v1"
    assert prepared["OPENAI_COMPATIBLE_API_KEY"] == "compat-secret-xyz"


def test_prepare_onboarding_settings_accepts_openai_compatible_setup():
    """An OpenAI-compatible base URL alone (no key) is a valid remote provider."""
    payload = _base_payload()
    payload["OPENAI_COMPATIBLE_BASE_URL"] = "http://localhost:11434/v1"
    payload["OUROBOROS_MODEL"] = "openai-compatible::llama3"
    payload["OUROBOROS_MODEL_HEAVY"] = "openai-compatible::llama3"
    payload["OUROBOROS_MODEL_LIGHT"] = "openai-compatible::llama3"
    payload["OUROBOROS_MODEL_FALLBACKS"] = "openai-compatible::llama3"

    prepared, error = prepare_onboarding_settings(payload, {})

    assert error is None
    assert prepared["OPENAI_COMPATIBLE_BASE_URL"] == "http://localhost:11434/v1"
    assert prepared["OPENAI_COMPATIBLE_API_KEY"] == ""
    assert prepared["OUROBOROS_MODEL"] == "openai-compatible::llama3"


def test_prepare_onboarding_settings_accepts_empty_heavy_and_light():
    """Role-model (v6.39): only Main is required; empty Heavy/Light fall back to Main, so
    the owner is not forced to fill every slot (mirrors the relaxed JS validateModelsStep
    and the live desktop launcher path)."""
    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"
    payload["OUROBOROS_MODEL"] = "openai::gpt-5.5"
    payload["OUROBOROS_MODEL_HEAVY"] = ""
    payload["OUROBOROS_MODEL_LIGHT"] = ""

    prepared, error = prepare_onboarding_settings(payload, {})

    assert error is None
    assert prepared["OUROBOROS_MODEL"] == "openai::gpt-5.5"


def test_prepare_onboarding_settings_still_requires_main_model():
    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"
    payload["OUROBOROS_MODEL"] = ""

    prepared, error = prepare_onboarding_settings(payload, {})

    assert prepared == {}
    assert "Main model" in error


def test_prepare_onboarding_settings_rejects_openai_compatible_key_without_base_url():
    payload = _base_payload()
    payload["OPENAI_COMPATIBLE_API_KEY"] = "compat-secret-xyz"

    prepared, error = prepare_onboarding_settings(payload, {})

    assert prepared == {}
    assert "Configure OpenRouter, OpenAI, OpenAI-compatible, Cloud.ru, MiniMax, Anthropic, or a local model" in error


def test_onboarding_frontend_uses_base_url_first_compatible_validation():
    source = (REPO / "web/modules/onboarding_wizard.js").read_text(encoding="utf-8")

    assert "!['OPENAI_COMPATIBLE_API_KEY', 'MINIMAX_REGION'].includes(field.settingKey)" in source
    assert "const hasRemote = keyValues.some(([, value]) => value);" not in source


def test_build_onboarding_html_contains_multistep_markers():
    html = build_onboarding_html({})

    assert '"contract": {' in html
    assert '"providerFields": [' in html
    assert 'const STEP_ORDER = bootstrap.stepOrder || (SETUP_CONTRACT.steps || []).map' in html
    assert "Add your access" in html
    assert "Keys + local" in html
    assert "Choose models" in html
    assert "model slots" in html
    assert "Choose review mode" in html
    assert "Set your budget" in html
    assert "Local model settings" in html
    assert "openai::gpt-5.6-terra" in html
    assert "openai::gpt-5.6-luna" in html
    assert "anthropic::claude-sonnet-5" in html
    assert "minimax::MiniMax-M3" in html
    assert "minimax::MiniMax-M2.7" in html
    assert "OPENAI_BASE_URL: ''" not in html
    assert "OPENAI_COMPATIBLE_API_KEY: ''" not in html
    assert "OPENAI_COMPATIBLE_BASE_URL: ''" not in html
    assert "CLOUDRU_FOUNDATION_MODELS_BASE_URL: ''" not in html


def test_build_onboarding_html_accepts_web_host_mode():
    html = build_onboarding_html({}, host_mode="web")

    assert '"hostMode": "web"' in html
    assert '"supportsLocalRuntimeControls": true' in html
    assert "@media (max-width: 720px)" in html
    assert "scroll-snap-type: x proximity;" in html


def test_build_onboarding_html_adapts_to_multi_provider_access():
    html = build_onboarding_html({})

    assert "function detectProviderProfile()" in html
    assert "function activeProviderProfile()" in html
    assert "function profileLabel(profile)" in html
    assert "function nextButtonShouldBeDisabled()" in html
    assert "function syncCurrentStepActionState()" in html
    assert "return 'direct-multi';" in html
    assert "PROVIDER_FIELDS.map((field) => [field.settingKey, trim(state[field.stateKey])])" in html
    assert "MODEL_SLOTS.map((slot) => [slot.settingKey, trim(state[slot.stateKey])])" in html
    assert "LOCAL_ROUTING_MODE: trim(state.localSource) ? (trim(state.localRoutingMode) || 'cloud') : 'cloud'" in html


def test_setup_contract_groups_rarely_used_providers():
    """The provider-field ``group`` column drives the onboarding layout:
    Cloud.ru and both OpenAI-compatible fields collapse under "More options"
    while OpenRouter stays pinned at index 0 of the always-visible group."""
    contract = build_setup_contract("web")
    fields = contract["providerFields"]

    assert fields[0]["settingKey"] == "OPENROUTER_API_KEY"
    assert {field["settingKey"]: field["group"] for field in fields} == {
        "OPENROUTER_API_KEY": "primary",
        "OPENAI_API_KEY": "primary",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY": "more",
        "MINIMAX_API_KEY": "more",
        "MINIMAX_REGION": "more",
        "ANTHROPIC_API_KEY": "primary",
        "OPENAI_COMPATIBLE_BASE_URL": "more",
        "OPENAI_COMPATIBLE_API_KEY": "more",
    }


def test_build_onboarding_html_collapses_rarely_used_providers():
    html = build_onboarding_html({})

    # Second access-step collapse hosting the "more" provider group.
    assert 'data-collapse="more-providers"' in html
    assert 'data-collapse="local-model"' in html
    assert "More options" in html
    assert "hasMoreProviderValue()" in html
    # Both collapses bind through scoped data-collapse selectors; the old
    # singular selector only ever reached the FIRST details element.
    assert "root.querySelector('.wizard-collapse')" not in html
    assert "moreProvidersOpen" in html


def test_setup_contract_has_no_secret_values():
    contract = build_setup_contract("web")
    text = repr(contract)
    budget_fields = {field["settingKey"]: field for field in contract["budgetFields"]}

    assert contract["hostMode"] == "web"
    assert "providerFields" in contract
    assert budget_fields["TOTAL_BUDGET"]["settingsInputId"] == "s-total-budget"
    assert budget_fields["TOTAL_BUDGET"]["min"] == "0.01"
    assert budget_fields["TOTAL_BUDGET"]["step"] == "any"
    assert budget_fields["OUROBOROS_PER_TASK_COST_USD"]["settingsInputId"] == "s-settings-per-task-cost"
    assert "settingsInputId" in contract["providerFields"][0]
    assert "OPENROUTER_API_KEY" in text
    assert "sk-or-v1-super-secret" not in text
    assert "sk-ant-super-secret" not in text
    suggestions = build_setup_bootstrap({}, "web")["modelSuggestions"]
    assert "anthropic/claude-sonnet-5" in suggestions
    assert "anthropic::claude-sonnet-5" in suggestions
    assert "minimax::MiniMax-M3" in suggestions
    assert "minimax::MiniMax-M2.7" in suggestions

    configured_value = "minimax-hidden-value"
    bootstrap = build_setup_bootstrap({"MINIMAX_API_KEY": configured_value}, "web")
    initial = bootstrap["initialState"]
    assert initial["providerProfile"] == "minimax"
    assert initial["mainModel"] == "minimax::MiniMax-M3"
    assert initial["lightModel"] == "minimax::MiniMax-M2.7"


def test_api_settings_exposes_setup_contract_without_secrets(tmp_path):
    from unittest.mock import patch

    import server as srv
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    secret = "sk-or-v1-super-secret-token"
    patches = [
        patch.object(srv, "load_settings", return_value={"OPENROUTER_API_KEY": secret}),
        patch.object(srv, "apply_runtime_provider_defaults", lambda settings: (dict(settings), False, [])),
        patch("ouroboros.server_auth.get_configured_network_password", return_value=""),
    ]
    for item in patches:
        item.start()
    try:
        app = Starlette(routes=[Route("/api/settings", endpoint=srv.api_settings_get, methods=["GET"])])
        app.state.drive_root = tmp_path
        with TestClient(app) as client:
            response = client.get("/api/settings")
        assert response.status_code == 200
        assert secret not in response.text
        contract = response.json()["_meta"]["setup_contract"]
        assert contract["providerFields"][0]["settingKey"] == "OPENROUTER_API_KEY"
        assert secret not in repr(contract)
    finally:
        for item in patches:
            item.stop()


def test_build_onboarding_html_includes_claude_runtime_cta_and_host_transports():
    desktop_html = build_onboarding_html({}, host_mode="desktop")
    web_html = build_onboarding_html({}, host_mode="web")

    assert "Claude Runtime" in desktop_html or "Claude runtime" in desktop_html
    assert "Skip for now" in desktop_html
    assert "window.pywebview.api.claude_code_status" in desktop_html
    assert "window.pywebview.api.install_claude_code" in desktop_html
    assert "/api/claude-code/status" in web_html
    assert "/api/claude-code/install" in web_html


def _launcher_has_onboarding_bridge() -> bool:
    launcher = REPO / "launcher.py"
    if not launcher.exists():
        return False
    source = launcher.read_text(encoding="utf-8")
    return all(marker in source for marker in (
        "has_startup_ready_provider(settings)",
        "prepare_onboarding_settings(data, settings)",
        'build_onboarding_html(settings, host_mode="desktop")',
        "def claude_code_status(self) -> dict:",
        "def install_claude_code(self) -> dict:",
    ))

_LAUNCHER_HAS_ONBOARDING_BRIDGE = _launcher_has_onboarding_bridge()

@pytest.mark.skipif(
    not _LAUNCHER_HAS_ONBOARDING_BRIDGE,
    reason="launcher.py does not contain onboarding bridge (may be an older bundle or post-refactor version)",
)
def test_launcher_uses_shared_onboarding_and_claude_cli_bridge():
    source = (REPO / "launcher.py").read_text(encoding="utf-8")

    assert "has_startup_ready_provider(settings)" in source
    assert "prepare_onboarding_settings(data, settings)" in source
    assert 'build_onboarding_html(settings, host_mode="desktop")' in source
    assert "def claude_code_status(self) -> dict:" in source
    assert "def install_claude_code(self) -> dict:" in source


def test_web_style_contains_onboarding_overlay_shell():
    style = (REPO / "web" / "style.css").read_text(encoding="utf-8")

    assert ".onboarding-overlay {" in style
    assert ".onboarding-frame {" in style
    assert ".onboarding-overlay-backdrop {" in style
    assert ".onboarding-restart-card {" in style


def test_onboarding_overlay_surfaces_restart_required_message():
    source = (REPO / "web" / "modules" / "onboarding_overlay.js").read_text(encoding="utf-8")

    assert "showRestartRequiredOverlay" in source
    assert "event.data.restart_required" in source
    assert "Continue in current mode" in source


# --- v6.82.0 rev.3-2: the wizard save authors safety "light" for NEW installs ---


def _fresh_settings_path(monkeypatch, tmp_path):
    from ouroboros import config as cfg

    monkeypatch.setattr(cfg, "SETTINGS_PATH", tmp_path / "settings.json")
    return cfg


def test_fresh_install_is_eligible_for_light_but_the_shared_validator_never_authors_it(
    monkeypatch, tmp_path,
):
    """A fresh install (no settings file) is ELIGIBLE for the new-install "light"
    coverage, but only the DESKTOP launcher authors it: the shared validator is also
    the web/Docker path, which posts through the non-owner generic /api/settings.
    SETTINGS_DEFAULTS itself stays "full" (rev.3-2)."""
    from ouroboros.config import SETTINGS_DEFAULTS
    from ouroboros.settings_setup_contract import wizard_authors_safety_light

    assert SETTINGS_DEFAULTS["OUROBOROS_SAFETY_MODE"] == "full"
    _fresh_settings_path(monkeypatch, tmp_path)  # no file on disk
    assert wizard_authors_safety_light() is True

    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"
    prepared, error = prepare_onboarding_settings(payload, {"OUROBOROS_SAFETY_MODE": "full"})

    assert error is None
    # The validator passes the CURRENT value through untouched — no authorship here.
    assert prepared["OUROBOROS_SAFETY_MODE"] == "full"


def test_wizard_save_respects_explicitly_stored_safety_mode(monkeypatch, tmp_path):
    """An install whose settings file explicitly carries a safety mode keeps it —
    re-running the wizard never lowers (or raises) an explicit owner choice."""
    import json

    cfg = _fresh_settings_path(monkeypatch, tmp_path)
    cfg.SETTINGS_PATH.write_text(json.dumps({"OUROBOROS_SAFETY_MODE": "full"}), encoding="utf-8")

    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"
    prepared, error = prepare_onboarding_settings(payload, {"OUROBOROS_SAFETY_MODE": "full"})

    assert error is None
    assert prepared["OUROBOROS_SAFETY_MODE"] == "full"


def test_wizard_authored_light_persists_and_generic_save_still_refuses(monkeypatch, tmp_path):
    """End-to-end persist seam: the launcher's wizard save names the key as authored
    and is allowed past the full->light ratchet; a plain (non-authored) save of the
    same lowering keeps raising PermissionError (the ratchet is untouched)."""
    import json

    from ouroboros import config as cfg

    monkeypatch.setattr(cfg, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.delenv("OUROBOROS_SAFETY_MODE", raising=False)

    with pytest.raises(PermissionError, match="OUROBOROS_SAFETY_MODE lowering refused"):
        cfg.save_settings({"OUROBOROS_SAFETY_MODE": "light", "TOTAL_BUDGET": 10})

    cfg.save_settings(
        {"OUROBOROS_SAFETY_MODE": "light", "TOTAL_BUDGET": 10},
        onboarding_safety_default=True,
    )
    stored = json.loads(cfg.SETTINGS_PATH.read_text(encoding="utf-8"))
    assert stored["OUROBOROS_SAFETY_MODE"] == "light"

    # The narrow flag authorizes EXACTLY the fresh-install light authorship: once a
    # settings file exists it cannot lower again, and it can never authorize "off".
    with pytest.raises(PermissionError, match="OUROBOROS_SAFETY_MODE lowering refused"):
        cfg.save_settings(
            {"OUROBOROS_SAFETY_MODE": "off", "TOTAL_BUDGET": 10},
            onboarding_safety_default=True,
        )
    cfg.SETTINGS_PATH.unlink()
    with pytest.raises(PermissionError, match="OUROBOROS_SAFETY_MODE lowering refused"):
        cfg.save_settings(
            {"OUROBOROS_SAFETY_MODE": "off", "TOTAL_BUDGET": 10},
            onboarding_safety_default=True,
        )


def test_web_onboarding_host_never_authors_light(monkeypatch, tmp_path):
    """The web/Docker wizard posts the SAME payload through generic /api/settings,
    which drops the owner-only safety key — so a fresh web setup keeps Full. Pinned
    because the shared validator (used by both hosts) must never author it."""
    from ouroboros.gateway import settings as gw_settings

    _fresh_settings_path(monkeypatch, tmp_path)
    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"
    prepared, error = prepare_onboarding_settings(payload, {"OUROBOROS_SAFETY_MODE": "full"})
    assert error is None
    assert prepared["OUROBOROS_SAFETY_MODE"] == "full"
    # And the generic settings surface (the web wizard's save path) drops the key.
    source = pathlib.Path(gw_settings.__file__).read_text(encoding="utf-8")
    assert '"OUROBOROS_SAFETY_MODE",' in source


def test_settings_save_refuses_lock_timeout_without_deleting_the_owner_lock(
    monkeypatch, tmp_path,
):
    """A contending writer keeps both its lock and the prior settings bytes."""
    from ouroboros import config as cfg

    settings_path = tmp_path / "settings.json"
    settings_path.write_text('{"TOTAL_BUDGET": 7}', encoding="utf-8")
    lock_path = tmp_path / "settings.json.lock"
    lock_path.write_text("other-writer", encoding="utf-8")
    monkeypatch.setattr(cfg, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "_acquire_settings_lock", lambda timeout=2.0: None)

    with pytest.raises(TimeoutError, match="settings lock"):
        cfg.save_settings({"TOTAL_BUDGET": 99})

    assert settings_path.read_text(encoding="utf-8") == '{"TOTAL_BUDGET": 7}'
    assert lock_path.read_text(encoding="utf-8") == "other-writer"


def test_launcher_binds_the_settings_writer_the_wizard_callback_calls():
    """The desktop wizard save callback calls `save_settings(...)` directly; pin that
    the name is BOUND in launcher's namespace (a NameError there would break every
    fresh desktop onboarding, and launcher.py is outside most deterministic gates)."""
    import launcher

    assert callable(getattr(launcher, "save_settings", None))
    source = pathlib.Path(launcher.__file__).read_text(encoding="utf-8")
    assert "onboarding_safety_default=wizard_authors_safety" in source


def test_wizard_rejects_a_newly_typed_short_key():
    """The length check still guards a credential the owner actually authored."""
    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"
    payload["OPENAI_COMPATIBLE_API_KEY"] = "ollama"

    prepared, error = prepare_onboarding_settings(payload, {})

    assert prepared == {}
    assert error == "OpenAI-compatible API key looks too short."


def test_wizard_is_not_deadlocked_by_a_short_key_already_on_disk():
    """A stored too-short key must not veto the save that replaces it.

    build_initial_setup_state prefills every provider field from disk, so an
    untouched field posts the stored value back. Rejecting it discards the WHOLE
    payload — including the replacement typed in the same form — which makes the
    offending value the one value the wizard can never overwrite.

    The stored fixture deliberately carries ONLY the short key: a stored
    OPENAI_COMPATIBLE_BASE_URL would make has_startup_ready_provider() true and
    the wizard would never open for this install in the first place.
    """
    stored = {
        "OPENAI_COMPATIBLE_API_KEY": "ollama",
    }

    # 1. An unrelated change saves even though the short key rides along untouched.
    payload = _base_payload()
    payload["OPENAI_COMPATIBLE_API_KEY"] = "ollama"
    payload["OPENAI_COMPATIBLE_BASE_URL"] = "http://127.0.0.1:4000/v1"
    payload["OPENAI_API_KEY"] = "sk-openai-1234567890"

    prepared, error = prepare_onboarding_settings(payload, stored)

    assert error is None
    assert prepared["OPENAI_COMPATIBLE_API_KEY"] == "ollama"

    # 2. The owner can replace the short key with a real one.
    payload["OPENAI_COMPATIBLE_API_KEY"] = "sk-replacement-key-1234"

    prepared, error = prepare_onboarding_settings(payload, stored)

    assert error is None
    assert prepared["OPENAI_COMPATIBLE_API_KEY"] == "sk-replacement-key-1234"


def test_wizard_still_rejects_shortening_a_stored_key():
    """Editing a stored key DOWN to a too-short value is authorship, not a prefill."""
    stored = {"OPENAI_API_KEY": "sk-openai-1234567890"}
    payload = _base_payload()
    payload["OPENAI_API_KEY"] = "sk-short"

    prepared, error = prepare_onboarding_settings(payload, stored)

    assert prepared == {}
    assert error == "OpenAI API key looks too short."


def test_onboarding_frontend_exempts_unchanged_prefilled_keys_from_length_check():
    """The client-side mirror of the length check carries the same authorship rule.

    validateProvidersStep() runs the identical <10 rule against state prefilled
    from disk and blocks Next/Save BEFORE the payload reaches the server, so the
    server-side exemption alone leaves the wizard deadlocked. The JS must skip
    the length check for a value equal to its INITIAL_STATE prefill — and only
    for that value, so a newly typed short key is still rejected client-side.
    """
    source = (REPO / "web/modules/onboarding_wizard.js").read_text(encoding="utf-8")

    assert "value.length < 10 && value !== trim(INITIAL_STATE[field.stateKey])" in source
