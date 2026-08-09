"""Connection registry: identity, storage, secret separation, legacy projection."""

from __future__ import annotations

import json
import os

import pytest

from ouroboros import connections as conns


def _row(**overrides):
    row = {
        "connection_id": "openai-a",
        "provider": "openai",
        "kind": "api_key",
        "privacy": "no_training",
    }
    row.update(overrides)
    return row


# --- validation --------------------------------------------------------------


def test_parse_accepts_a_well_formed_row():
    conn = conns.parse_connection(_row(tags="prod, fast", models=["gpt-5.6"]))
    assert conn.connection_id == "openai-a"
    assert conn.tags == ("prod", "fast")
    assert conn.models == ("gpt-5.6",)
    assert conn.origin == conns.ORIGIN_REGISTRY


@pytest.mark.parametrize("bad_id", ["", "   ", "Has Spaces", "x" * 65, "!bad"])
def test_parse_rejects_unusable_identity(bad_id):
    with pytest.raises(conns.ConnectionConfigError):
        conns.parse_connection(_row(connection_id=bad_id))


def test_parse_rejects_unknown_kind_and_privacy():
    with pytest.raises(conns.ConnectionConfigError, match="kind"):
        conns.parse_connection(_row(kind="carrier-pigeon"))
    with pytest.raises(conns.ConnectionConfigError, match="privacy"):
        conns.parse_connection(_row(privacy="probably-fine"))


def test_privacy_defaults_to_unknown_not_to_safe():
    # "unknown" must be its own value: an omitted declaration is NOT permission,
    # and the sensitivity gate keys off an explicit no_training only.
    conn = conns.parse_connection({"connection_id": "x", "provider": "openai"})
    assert conn.privacy == conns.PRIVACY_UNKNOWN


def test_daily_limit_none_is_not_zero():
    # None = no limit; 0.0 = spend nothing. Collapsing them would silently either
    # uncap a capped connection or freeze an uncapped one.
    assert conns.parse_connection(_row()).daily_usd is None
    assert conns.parse_connection(_row(daily_usd=0)).daily_usd == 0.0


def test_models_empty_means_every_model_of_the_provider():
    assert conns.parse_connection(_row()).serves_model("anything")
    restricted = conns.parse_connection(_row(models=["gpt-5.6"]))
    assert restricted.serves_model("gpt-5.6")
    assert not restricted.serves_model("gpt-4")


# --- storage -----------------------------------------------------------------


def test_registry_round_trips(tmp_path):
    conns.upsert(conns.parse_connection(_row()), tmp_path)
    conns.upsert(conns.parse_connection(_row(connection_id="openai-b")), tmp_path)
    stored = conns.load_registry(tmp_path)
    assert [c.connection_id for c in stored] == ["openai-a", "openai-b"]


def test_upsert_replaces_by_identity_and_keeps_order(tmp_path):
    conns.upsert(conns.parse_connection(_row()), tmp_path)
    conns.upsert(conns.parse_connection(_row(connection_id="openai-b")), tmp_path)
    conns.upsert(conns.parse_connection(_row(label="renamed")), tmp_path)
    stored = conns.load_registry(tmp_path)
    assert [c.connection_id for c in stored] == ["openai-a", "openai-b"]
    assert stored[0].label == "renamed"


def test_catalog_file_never_contains_a_secret(tmp_path):
    conns.upsert(conns.parse_connection(_row()), tmp_path)
    conns.set_secret("openai-a", {"OPENAI_API_KEY": "sk-super-secret"}, tmp_path)
    catalog = conns.catalog_path(tmp_path).read_text(encoding="utf-8")
    assert "sk-super-secret" not in catalog
    assert "sk-super-secret" in conns.secrets_path(tmp_path).read_text(encoding="utf-8")


def test_clearing_a_secret_removes_it_rather_than_storing_a_blank(tmp_path):
    conns.upsert(conns.parse_connection(_row()), tmp_path)
    conns.set_secret("openai-a", {"OPENAI_API_KEY": "sk-x"}, tmp_path)
    conns.set_secret("openai-a", {"OPENAI_API_KEY": ""}, tmp_path)
    assert conns.load_secrets(tmp_path) == {}


def test_remove_deletes_the_row_and_its_secret(tmp_path):
    conns.upsert(conns.parse_connection(_row()), tmp_path)
    conns.set_secret("openai-a", {"OPENAI_API_KEY": "sk-x"}, tmp_path)
    conns.remove("openai-a", tmp_path)
    assert conns.load_registry(tmp_path) == ()
    assert conns.load_secrets(tmp_path) == {}


def test_a_corrupt_catalog_degrades_to_empty_instead_of_raising(tmp_path):
    path = conns.catalog_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert conns.load_registry(tmp_path) == ()


def test_one_unparsable_row_does_not_hide_the_others(tmp_path):
    path = conns.catalog_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "connections": [
            {"connection_id": "GOOD-ONE", "provider": "openai"},
            {"connection_id": "", "provider": "openai"},
            {"connection_id": "good-two", "provider": "openai"},
        ]}),
        encoding="utf-8",
    )
    assert [c.connection_id for c in conns.load_registry(tmp_path)] == ["good-one", "good-two"]


def test_save_registry_rejects_duplicate_identity(tmp_path):
    row = conns.parse_connection(_row())
    with pytest.raises(conns.ConnectionConfigError, match="twice"):
        conns.save_registry([row, row], tmp_path)


# --- legacy projection / provider independence -------------------------------


def test_legacy_key_is_projected_as_a_connection():
    projected = conns.legacy_connections({"OPENAI_API_KEY": "sk-x"})
    assert [c.connection_id for c in projected] == ["legacy:openai"]
    assert projected[0].origin == conns.ORIGIN_LEGACY


def test_provider_independence_one_key_and_an_empty_registry_still_routes(tmp_path, monkeypatch):
    # The whole point of projecting rather than migrating: an install that never
    # touches the new UI keeps working exactly as before.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    usable = conns.usable_connections("openai::gpt-5.6", tmp_path)
    assert [c.connection_id for c in usable] == ["legacy:openai"]


def test_a_registry_row_is_never_shadowed_by_the_legacy_projection(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    conns.upsert(conns.parse_connection(_row(connection_id="legacy:openai")), tmp_path)
    rows = [c for c in conns.all_connections(tmp_path) if c.connection_id == "legacy:openai"]
    assert len(rows) == 1
    assert rows[0].origin == conns.ORIGIN_REGISTRY


def test_a_legacy_row_cannot_be_written_into_the_catalog(tmp_path):
    legacy = conns.legacy_connections({"OPENAI_API_KEY": "sk-x"})[0]
    with pytest.raises(conns.ConnectionConfigError, match="cannot be stored"):
        conns.upsert(legacy, tmp_path)


def test_legacy_secret_is_read_live_from_settings_not_duplicated(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-first")
    legacy = conns.legacy_connections()[0]
    assert conns.secret_for(legacy, tmp_path)["OPENAI_API_KEY"] == "sk-first"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-rotated")
    assert conns.secret_for(legacy, tmp_path)["OPENAI_API_KEY"] == "sk-rotated"
    assert conns.load_secrets(tmp_path) == {}


# --- selection filtering -----------------------------------------------------


def test_usable_connections_skips_disabled_and_uncredentialed(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    conns.upsert(conns.parse_connection(_row(connection_id="with-key")), tmp_path)
    conns.set_secret("with-key", {"OPENAI_API_KEY": "sk-x"}, tmp_path)
    conns.upsert(conns.parse_connection(_row(connection_id="no-key")), tmp_path)
    conns.upsert(conns.parse_connection(_row(connection_id="off", enabled=False)), tmp_path)
    conns.set_secret("off", {"OPENAI_API_KEY": "sk-y"}, tmp_path)

    usable = conns.usable_connections("openai::gpt-5.6", tmp_path)
    assert [c.connection_id for c in usable] == ["with-key"]


def test_usable_connections_respects_a_model_allowlist(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    conns.upsert(conns.parse_connection(_row(connection_id="only-mini", models=["gpt-mini"])), tmp_path)
    conns.set_secret("only-mini", {"OPENAI_API_KEY": "sk-x"}, tmp_path)
    assert conns.usable_connections("openai::gpt-5.6", tmp_path) == ()
    assert len(conns.usable_connections("openai::gpt-mini", tmp_path)) == 1


@pytest.mark.parametrize("allowed", ["gpt-mini", "openai::gpt-mini", "openai/gpt-mini"])
@pytest.mark.parametrize("asked", ["gpt-mini", "openai::gpt-mini", "openai/gpt-mini"])
def test_the_model_allowlist_matches_identity_not_spelling(allowed, asked):
    # The same model is copied from different surfaces in different spellings; an
    # allowlist that missed one would silently drop the connection from rotation.
    conn = conns.parse_connection(_row(models=[allowed]))
    assert conn.serves_model(asked)


def test_the_model_allowlist_still_excludes_a_different_model():
    conn = conns.parse_connection(_row(models=["openai::gpt-mini"]))
    assert not conn.serves_model("openai::gpt-large")


def test_an_endpoint_connection_needs_no_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    conns.upsert(
        conns.parse_connection({
            "connection_id": "local-1",
            "provider": "openai-compatible",
            "kind": "endpoint",
            "base_url": "http://127.0.0.1:8080/v1",
        }),
        tmp_path,
    )
    usable = conns.usable_connections("openai-compatible::llama", tmp_path)
    assert [c.connection_id for c in usable] == ["local-1"]


# --- credential file permissions ---------------------------------------------


def _mode(path):
    return path.stat().st_mode & 0o777


@pytest.mark.skipif(os.name != "posix", reason="mode bits are not the ACL mechanism off POSIX")
def test_credential_file_is_owner_only(tmp_path):
    conns.set_secret("openai-a", {"OPENAI_API_KEY": "sk-x"}, tmp_path)
    assert _mode(conns.secrets_path(tmp_path)) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="mode bits are not the ACL mechanism off POSIX")
def test_rewriting_tightens_a_previously_permissive_file(tmp_path):
    # A file left readable by an older build must not stay readable after a write.
    conns.set_secret("openai-a", {"OPENAI_API_KEY": "sk-x"}, tmp_path)
    path = conns.secrets_path(tmp_path)
    path.chmod(0o644)
    conns.set_secret("openai-b", {"OPENAI_API_KEY": "sk-y"}, tmp_path)
    assert _mode(path) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="mode bits are not the ACL mechanism off POSIX")
def test_no_world_readable_window_exists_during_the_write(tmp_path, monkeypatch):
    """The temp file that briefly holds the secret must already be owner-only.

    This is the actual defect class: a helper that writes with the umask and
    chmods afterwards leaves the SECRET-BEARING temp file readable in between.
    """
    seen: list[int] = []
    real_replace = os.replace

    def _spy(src, dst):
        seen.append(os.stat(src).st_mode & 0o777)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy)
    conns.set_secret("openai-a", {"OPENAI_API_KEY": "sk-x"}, tmp_path)
    assert seen and all(mode & 0o077 == 0 for mode in seen), f"temp file modes: {[oct(m) for m in seen]}"


@pytest.mark.skipif(os.name != "posix", reason="mode bits are not the ACL mechanism off POSIX")
def test_a_permissive_result_refuses_to_publish_instead_of_passing_silently(tmp_path):
    probe = tmp_path / "probe.json"
    probe.write_text("{}", encoding="utf-8")
    probe.chmod(0o644)
    with pytest.raises(conns.ConnectionConfigError, match="readable beyond the owner"):
        conns._assert_owner_only(probe)


# --- legacy projection must cover EVERY provider lane -------------------------


@pytest.mark.parametrize("env,expected", [
    ({"OPENROUTER_API_KEY": "sk-x"}, "legacy:openrouter"),
    ({"OPENAI_API_KEY": "sk-x"}, "legacy:openai"),
    ({"ANTHROPIC_API_KEY": "sk-x"}, "legacy:anthropic"),
    ({"MINIMAX_API_KEY": "sk-x"}, "legacy:minimax"),
    ({"CLOUDRU_FOUNDATION_MODELS_API_KEY": "sk-x"}, "legacy:cloudru"),
    ({"GIGACHAT_CREDENTIALS": "sk-x"}, "legacy:gigachat"),
    ({"OPENAI_COMPATIBLE_API_KEY": "sk-x"}, "legacy:openai-compatible"),
    ({"OPENAI_COMPATIBLE_BASE_URL": "http://host/v1"}, "legacy:openai-compatible"),
])
def test_every_provider_lane_is_projected(env, expected):
    # Projecting only the five single-key providers made a custom-endpoint install
    # show an EMPTY pool while it was routing traffic perfectly well.
    assert expected in {c.connection_id for c in conns.legacy_connections(env)}


def test_a_custom_endpoint_projection_carries_its_base_url():
    projected = conns.legacy_connections({
        "OPENAI_COMPATIBLE_API_KEY": "sk-x",
        "OPENAI_COMPATIBLE_BASE_URL": "https://api.example/v1",
    })[0]
    assert projected.base_url == "https://api.example/v1"
    assert projected.kind == conns.KIND_ENDPOINT


def test_a_compatible_lane_on_the_legacy_openai_pair_is_still_seen():
    projected = conns.legacy_connections({
        "OPENAI_COMPATIBLE_BASE_URL": "https://api.example/v1",
        "OPENAI_API_KEY": "sk-legacy",
    })
    assert {"legacy:openai", "legacy:openai-compatible"} <= {c.connection_id for c in projected}


def test_nothing_configured_projects_nothing():
    assert conns.legacy_connections({}) == ()


def test_a_pasted_endpoint_url_is_rejected_as_a_provider():
    """The closerouter incident: a URL in `provider` made a silently inert row.

    Routing matches this field against `provider_for_model`, and the credential key
    is derived from it, so an unrecognized lane both fails to route and stores the
    secret under a name no transport reads — while the UI showed it as configured.
    """
    with pytest.raises(conns.ConnectionConfigError) as excinfo:
        conns.parse_connection({
            "connection_id": "key1",
            "provider": "https://closerouter.dev/",
            "base_url": "https://api.closerouter.dev/v1",
        })
    message = str(excinfo.value)
    assert "unknown provider" in message
    # The error has to teach the fix, not just refuse.
    assert "base_url" in message and "openai-compatible" in message


def test_every_known_transport_lane_is_accepted_as_a_provider():
    from ouroboros.provider_models import PROVIDER_CREDENTIAL_GROUPS

    for provider in PROVIDER_CREDENTIAL_GROUPS:
        parsed = conns.parse_connection({"connection_id": "c1", "provider": provider})
        assert parsed.provider == provider


def test_an_invalid_provider_row_is_skipped_without_hiding_the_healthy_ones(tmp_path):
    path = conns.catalog_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"connections": [
        {"connection_id": "broken", "provider": "https://example.dev/"},
        {"connection_id": "good", "provider": "openai-compatible", "base_url": "https://x/v1"},
    ]}), encoding="utf-8")

    loaded = conns.load_registry(tmp_path)
    assert [c.connection_id for c in loaded] == ["good"]
