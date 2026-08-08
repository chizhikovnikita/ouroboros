"""Connection registry: every usable route to a model as a first-class entity.

Before this module a provider had exactly ONE credential (``PROVIDER_ENV_KEYS``),
so "many OpenAI keys", "several ChatGPT accounts" or "three local endpoints" had
nowhere to live. A *connection* is one usable route — a key, a subscription
profile, or an endpoint — carrying its own limits, privacy declaration and, later,
its own measured rating.

Two files, deliberately separate:

- ``state/connections.json``         the catalog. No secrets, so it can be read,
                                     diffed and shown wholesale.
- ``state/connections_secrets.json`` the credentials, keyed by connection id.

The split is what lets the gateway serve the catalog without ever loading a
secret into a response, and it keeps the catalog out of ``settings.json``, which
is read and masked as one blob and would not survive hundreds of rows.

Legacy single-provider keys are PROJECTED as connections rather than migrated:
the settings key stays the authority for its own value, so an install with one
``OPENAI_API_KEY`` and an empty registry keeps working unchanged (BIBLE provider
independence), and nothing has to be copied to two places.
"""

from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from ouroboros.provider_models import (
    PROVIDER_CREDENTIAL_GROUPS,
    normalize_model_identity,
)


CATALOG_REL = pathlib.Path("state/connections.json")
SECRETS_REL = pathlib.Path("state/connections_secrets.json")

SCHEMA_VERSION = 1

KIND_API_KEY = "api_key"
KIND_SUBSCRIPTION = "subscription"
KIND_ENDPOINT = "endpoint"
VALID_KINDS: frozenset[str] = frozenset({KIND_API_KEY, KIND_SUBSCRIPTION, KIND_ENDPOINT})

# Privacy is DECLARED, never measured: whether a vendor trains on submitted data
# is a contractual fact, not something a probe can discover. "unknown" is its own
# value and must never be silently read as "safe" — the sensitivity gate treats
# only an explicit ``no_training`` as permission.
PRIVACY_NO_TRAINING = "no_training"
PRIVACY_TRAINS_ON_DATA = "trains_on_data"
PRIVACY_UNKNOWN = "unknown"
VALID_PRIVACY: frozenset[str] = frozenset({
    PRIVACY_NO_TRAINING, PRIVACY_TRAINS_ON_DATA, PRIVACY_UNKNOWN,
})

ORIGIN_REGISTRY = "registry"
ORIGIN_LEGACY = "legacy_settings"

# How sensitive the DATA of a piece of work is. Ordered: a higher rank may only
# travel over connections cleared for that rank. Kept deliberately two-valued —
# a gate nobody can reason about is a gate nobody sets correctly.
SENSITIVITY_PUBLIC = "public"
SENSITIVITY_SENSITIVE = "sensitive"
SENSITIVITY_RANK: dict[str, int] = {
    "": 0,
    SENSITIVITY_PUBLIC: 0,
    SENSITIVITY_SENSITIVE: 1,
}
VALID_SENSITIVITY: frozenset[str] = frozenset(SENSITIVITY_RANK)


def sensitivity_rank(value: Any) -> int:
    """Rank of a sensitivity label; an unknown label is treated as the STRICTEST.

    Fail-closed on purpose: a typo or a label from a newer version must not
    silently downgrade to "anything may carry this".
    """
    text = str(value or "").strip().lower()
    if not text:
        return 0
    return SENSITIVITY_RANK.get(text, max(SENSITIVITY_RANK.values()))


def stricter_sensitivity(left: Any, right: Any) -> str:
    """The more restrictive of two labels."""
    left_text = str(left or "").strip().lower()
    right_text = str(right or "").strip().lower()
    return left_text if sensitivity_rank(left_text) >= sensitivity_rank(right_text) else right_text


def connection_allows_sensitivity(connection: "Connection", sensitivity: Any) -> bool:
    """Whether ``connection`` may carry work of this sensitivity.

    Sensitive work requires an EXPLICIT ``no_training`` declaration. `unknown` is
    not permission: whether a vendor trains on submitted data is a contractual
    fact, and an undeclared connection is exactly the one we cannot vouch for.
    """
    if sensitivity_rank(sensitivity) <= 0:
        return True
    return connection.privacy == PRIVACY_NO_TRAINING

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
_ID_MAX_CHARS = 64


class ConnectionConfigError(ValueError):
    """Raised when a stored or submitted connection row is not usable."""


def _model_forms(model: str) -> frozenset[str]:
    """Every spelling one model id can legitimately appear as.

    Provider filtering already happened before this is consulted, so reducing to
    the bare name cannot leak a match across providers.
    """
    text = str(model or "").strip()
    if not text:
        return frozenset()
    forms = {text, normalize_model_identity(text)}
    for form in tuple(forms):
        for separator in ("::", "/"):
            if separator in form:
                forms.add(form.split(separator, 1)[1])
    return frozenset(part for part in forms if part)


@dataclass(frozen=True)
class Connection:
    """One usable route to models.

    ``connection_id`` is a stable owner-visible identity, never an array index:
    a row's measured history and its budget attribution can only line up with one
    identity, so reordering the catalog must not re-point them.
    """

    connection_id: str
    kind: str
    provider: str
    label: str = ""
    enabled: bool = True
    tags: Tuple[str, ...] = ()
    base_url: str = ""
    # Provider-specific non-secret companions (region, scope, ...). The keys that
    # matter per provider are already enumerated in PROVIDER_CREDENTIAL_GROUPS.
    extras: Mapping[str, str] = field(default_factory=dict)
    # Empty = this connection serves whatever its provider serves. A non-empty
    # list restricts it to named models.
    models: Tuple[str, ...] = ()
    privacy: str = PRIVACY_UNKNOWN
    # 0 = unbounded. Enforced by the pool (stage 2), stored here.
    max_concurrent: int = 0
    daily_usd: Optional[float] = None
    origin: str = ORIGIN_REGISTRY

    def serves_model(self, model: str) -> bool:
        """Whether this connection is allowed to carry ``model``.

        The allowlist is matched on model IDENTITY, not on the exact spelling: the
        same model legitimately appears as ``openai::gpt-x``, ``openai/gpt-x`` and
        bare ``gpt-x`` depending on the surface the owner copied it from, and an
        allowlist that silently missed a spelling would quietly take the
        connection out of rotation.
        """
        if not self.models:
            return True
        wanted = _model_forms(model)
        return any(_model_forms(allowed) & wanted for allowed in self.models)

    def to_public_dict(self) -> Dict[str, Any]:
        """Catalog row for the gateway. Carries no secret by construction."""
        return {
            "connection_id": self.connection_id,
            "kind": self.kind,
            "provider": self.provider,
            "label": self.label,
            "enabled": self.enabled,
            "tags": list(self.tags),
            "base_url": self.base_url,
            "extras": dict(self.extras),
            "models": list(self.models),
            "privacy": self.privacy,
            "max_concurrent": self.max_concurrent,
            "daily_usd": self.daily_usd,
            "origin": self.origin,
        }


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _validate_id(raw: Any, *, where: str) -> str:
    connection_id = _text(raw).lower()
    if not connection_id:
        raise ConnectionConfigError(f"{where}: connection_id must not be empty")
    if len(connection_id) > _ID_MAX_CHARS:
        raise ConnectionConfigError(
            f"{where}: connection_id is longer than {_ID_MAX_CHARS} characters"
        )
    if not _ID_RE.match(connection_id):
        raise ConnectionConfigError(
            f"{where}: connection_id {connection_id!r} may use only a-z, 0-9, '_', '.', ':' and '-'"
        )
    return connection_id


def _validate_choice(raw: Any, valid: Iterable[str], *, where: str, what: str, default: str) -> str:
    value = _text(raw).lower() or default
    if value not in set(valid):
        raise ConnectionConfigError(
            f"{where}: unknown {what} {value!r}; valid: {', '.join(sorted(valid))}"
        )
    return value


def _string_tuple(raw: Any) -> Tuple[str, ...]:
    if raw is None or raw == "":
        return ()
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [_text(part) for part in raw]
    else:
        return ()
    # Order-preserving dedupe: the owner's order is meaningful for display.
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


def _positive_int(raw: Any, *, where: str, what: str) -> int:
    if raw in (None, ""):
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ConnectionConfigError(f"{where}: {what} must be a whole number") from exc
    if value < 0:
        raise ConnectionConfigError(f"{where}: {what} must not be negative")
    return value


def _optional_money(raw: Any, *, where: str, what: str) -> Optional[float]:
    """None means NO LIMIT — deliberately distinct from 0.0, which means 'spend nothing'."""
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ConnectionConfigError(f"{where}: {what} must be a number") from exc
    if value < 0:
        raise ConnectionConfigError(f"{where}: {what} must not be negative")
    return value


def parse_connection(row: Any, *, where: str = "connection") -> Connection:
    """Validate one catalog row into a Connection, or raise."""
    if not isinstance(row, dict):
        raise ConnectionConfigError(f"{where}: row is not an object")

    connection_id = _validate_id(row.get("connection_id"), where=where)
    provider = _text(row.get("provider")).lower()
    if not provider:
        raise ConnectionConfigError(f"{where}: provider must not be empty")

    extras_raw = row.get("extras")
    extras: Dict[str, str] = {}
    if isinstance(extras_raw, dict):
        extras = {_text(k): _text(v) for k, v in extras_raw.items() if _text(k)}
    elif extras_raw not in (None, ""):
        raise ConnectionConfigError(f"{where}: extras must be an object")

    return Connection(
        connection_id=connection_id,
        kind=_validate_choice(row.get("kind"), VALID_KINDS, where=where, what="kind", default=KIND_API_KEY),
        provider=provider,
        label=_text(row.get("label")),
        enabled=bool(row.get("enabled", True)),
        tags=_string_tuple(row.get("tags")),
        base_url=_text(row.get("base_url")),
        extras=extras,
        models=_string_tuple(row.get("models")),
        privacy=_validate_choice(
            row.get("privacy"), VALID_PRIVACY, where=where, what="privacy", default=PRIVACY_UNKNOWN
        ),
        max_concurrent=_positive_int(row.get("max_concurrent"), where=where, what="max_concurrent"),
        daily_usd=_optional_money(row.get("daily_usd"), where=where, what="daily_usd"),
        origin=ORIGIN_REGISTRY,
    )


# --- storage -----------------------------------------------------------------


def _drive_root(root: pathlib.Path | str | None = None) -> pathlib.Path:
    if root is not None:
        return pathlib.Path(root)
    from ouroboros.config import DATA_DIR

    return pathlib.Path(DATA_DIR)


def catalog_path(root: pathlib.Path | str | None = None) -> pathlib.Path:
    return _drive_root(root) / CATALOG_REL


def secrets_path(root: pathlib.Path | str | None = None) -> pathlib.Path:
    return _drive_root(root) / SECRETS_REL


def load_registry(root: pathlib.Path | str | None = None) -> Tuple[Connection, ...]:
    """Read the stored catalog. An unreadable file yields an EMPTY registry.

    Empty is the safe answer: the legacy projection below still supplies whatever
    single-provider credentials are configured, so a corrupt catalog degrades the
    pool to today's behavior instead of taking the runtime offline.
    """
    from ouroboros.utils import read_json_dict

    payload = read_json_dict(catalog_path(root))
    if not payload:
        return ()
    rows = payload.get("connections")
    if not isinstance(rows, list):
        return ()

    out: list[Connection] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        try:
            parsed = parse_connection(row, where=f"connections[{index}]")
        except ConnectionConfigError:
            # One bad row must not hide the rest; it is skipped, not fatal.
            continue
        if parsed.connection_id in seen:
            continue
        seen.add(parsed.connection_id)
        out.append(parsed)
    return tuple(out)


def save_registry(connections: Sequence[Connection], root: pathlib.Path | str | None = None) -> None:
    """Persist the catalog atomically. Rejects duplicate ids before writing."""
    from ouroboros.utils import atomic_write_json

    seen: set[str] = set()
    for conn in connections:
        if conn.connection_id in seen:
            raise ConnectionConfigError(
                f"connection_id {conn.connection_id!r} appears twice; identity must be unique"
            )
        seen.add(conn.connection_id)

    path = catalog_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "connections": [conn.to_public_dict() for conn in connections],
        },
        trailing_newline=True,
    )


def load_secrets(root: pathlib.Path | str | None = None) -> Dict[str, Dict[str, str]]:
    """Read the credential store. Never include this in a gateway response."""
    from ouroboros.utils import read_json_dict

    payload = read_json_dict(secrets_path(root)) or {}
    out: Dict[str, Dict[str, str]] = {}
    for connection_id, values in payload.items():
        if isinstance(values, dict):
            out[str(connection_id)] = {str(k): str(v) for k, v in values.items()}
    return out


def _assert_owner_only(path: pathlib.Path) -> None:
    """Raise unless ``path`` is unreadable by group and other.

    Deliberately NOT best-effort. A credential file that silently stayed
    world-readable is exactly the failure nobody notices, so refusing to publish
    the file is the safe direction. POSIX only: on Windows the mode bits are not
    the access-control mechanism (ACLs are), and enforcing them there would fail
    the write for no security gain.
    """
    if os.name != "posix":
        return
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ConnectionConfigError(
            f"refusing to store credentials at {path}: mode {mode:04o} is readable beyond the owner"
        )


def save_secrets(secrets: Mapping[str, Mapping[str, str]], root: pathlib.Path | str | None = None) -> None:
    """Persist credentials, owner-only from the moment the bytes exist.

    Hand-rolled rather than reusing ``utils.atomic_write_json``: that helper
    creates its temp sibling with the process umask (typically 0644) and only then
    renames it into place, so the SECRET-BEARING temp file — and, until a
    follow-up chmod, the final file — is briefly world-readable. Here the file
    descriptor is opened 0600 before a single byte is written, and the mode is
    verified before the rename publishes it.
    """
    import json
    import uuid

    path = secrets_path(root)
    # 0o700 applies only when this call creates the directory; an existing shared
    # state/ directory is left alone on purpose — the file mode is the guarantee,
    # and silently re-permissioning a directory other modules own is not this
    # function's business.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    payload = {
        str(cid): {str(k): str(v) for k, v in values.items()}
        for cid, values in secrets.items()
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # A restrictive umask can only tighten 0o600; a permissive one cannot
        # loosen it. Verify anyway — this is the last point before publication.
        _assert_owner_only(tmp)
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def secret_for(connection: Connection, root: pathlib.Path | str | None = None) -> Dict[str, str]:
    """Credential material for one connection.

    A legacy-projected connection reads its live settings/env value instead of the
    secret store, so the settings key stays the single authority for its own value
    and nothing is duplicated into a second place that could drift.
    """
    if connection.origin == ORIGIN_LEGACY:
        keys = PROVIDER_CREDENTIAL_GROUPS.get(connection.provider, ())
        return {key: os.environ.get(key, "") for key in keys if os.environ.get(key, "")}
    return dict(load_secrets(root).get(connection.connection_id, {}))


def has_credential(connection: Connection, root: pathlib.Path | str | None = None) -> bool:
    """Whether this connection currently has usable credential material."""
    if connection.kind == KIND_ENDPOINT and connection.base_url:
        # A local/compatible endpoint may legitimately need no key at all.
        return True
    return any(str(value).strip() for value in secret_for(connection, root).values())


# --- legacy projection -------------------------------------------------------


def legacy_connection_id(provider: str) -> str:
    return f"legacy:{provider}"


# Which settings key means "this provider is configured". Derived from
# PROVIDER_CREDENTIAL_GROUPS rather than PROVIDER_ENV_KEYS: that narrower table
# covers only the five single-key providers, so projecting from it made the
# openai-compatible and GigaChat lanes invisible — including, for an install whose
# ONLY configured provider is a custom endpoint, every connection it has.
_LEGACY_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "openrouter": ("OPENROUTER_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "minimax": ("MINIMAX_API_KEY",),
    "cloudru": ("CLOUDRU_FOUNDATION_MODELS_API_KEY",),
    # Either auth route counts as configured.
    "gigachat": ("GIGACHAT_CREDENTIALS", "GIGACHAT_PASSWORD"),
    # A custom endpoint may authenticate with its own key or reuse the legacy
    # OPENAI_* pair, exactly as llm._resolve_remote_target does.
    "openai-compatible": ("OPENAI_COMPATIBLE_API_KEY", "OPENAI_COMPATIBLE_BASE_URL"),
}

# Where a projected connection reads its endpoint from, when it has one.
_LEGACY_BASE_URL_KEYS: dict[str, tuple[str, ...]] = {
    "cloudru": ("CLOUDRU_FOUNDATION_MODELS_BASE_URL",),
    "gigachat": ("GIGACHAT_BASE_URL",),
    "openai-compatible": ("OPENAI_COMPATIBLE_BASE_URL", "OPENAI_BASE_URL"),
}


def legacy_connections(env: Mapping[str, str] | None = None) -> Tuple[Connection, ...]:
    """Project today's single-credential-per-provider settings as connections.

    These are not written to the catalog: the settings key remains the authority
    for its own value. That keeps a one-provider install working with an empty
    registry, which is the provider-independence invariant.
    """
    source = env if env is not None else os.environ
    out: list[Connection] = []
    for provider, keys in _LEGACY_PRIMARY_KEYS.items():
        if not any(str(source.get(key, "") or "").strip() for key in keys):
            continue
        base_url = ""
        for key in _LEGACY_BASE_URL_KEYS.get(provider, ()):
            candidate = str(source.get(key, "") or "").strip()
            if candidate:
                base_url = candidate
                break
        out.append(
            Connection(
                connection_id=legacy_connection_id(provider),
                kind=KIND_ENDPOINT if provider == "openai-compatible" and base_url else KIND_API_KEY,
                provider=provider,
                label=f"{provider} (settings key)",
                enabled=True,
                base_url=base_url,
                privacy=PRIVACY_UNKNOWN,
                origin=ORIGIN_LEGACY,
            )
        )
    return tuple(out)


def all_connections(
    root: pathlib.Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    include_legacy: bool = True,
) -> Tuple[Connection, ...]:
    """Registry rows plus the legacy projection.

    A registry row wins over the legacy projection for the same id, so an owner
    who names a connection explicitly is never shadowed by the derived one.
    """
    registry = load_registry(root)
    if not include_legacy:
        return registry
    known = {conn.connection_id for conn in registry}
    extra = tuple(conn for conn in legacy_connections(env) if conn.connection_id not in known)
    return registry + extra


def usable_connections(
    model: str,
    root: pathlib.Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Tuple[Connection, ...]:
    """Enabled, credentialed connections that may carry ``model``.

    Provider matching is the caller's contract: ``model`` is expected to already be
    provider-qualified (``openai::gpt-...``) or an OpenRouter-style id, resolved
    through ``provider_models.provider_for_model``.
    """
    from ouroboros.provider_models import provider_for_model

    provider = provider_for_model(model)
    out: list[Connection] = []
    for conn in all_connections(root, env=env):
        if not conn.enabled or conn.provider != provider:
            continue
        if not conn.serves_model(model):
            continue
        if not has_credential(conn, root):
            continue
        out.append(conn)
    return tuple(out)


def upsert(connection: Connection, root: pathlib.Path | str | None = None) -> Tuple[Connection, ...]:
    """Add or replace one catalog row, returning the new registry."""
    if connection.origin == ORIGIN_LEGACY:
        raise ConnectionConfigError(
            "a legacy-projected connection is derived from settings and cannot be stored; "
            "edit its provider key in Settings instead"
        )
    current = list(load_registry(root))
    for index, existing in enumerate(current):
        if existing.connection_id == connection.connection_id:
            current[index] = connection
            break
    else:
        current.append(connection)
    save_registry(current, root)
    return tuple(current)


def remove(connection_id: str, root: pathlib.Path | str | None = None) -> Tuple[Connection, ...]:
    """Delete a catalog row and its stored secret."""
    wanted = _text(connection_id).lower()
    current = [conn for conn in load_registry(root) if conn.connection_id != wanted]
    save_registry(current, root)

    secrets = load_secrets(root)
    if wanted in secrets:
        secrets.pop(wanted, None)
        save_secrets(secrets, root)
    return tuple(current)


def set_secret(
    connection_id: str,
    values: Mapping[str, str],
    root: pathlib.Path | str | None = None,
) -> None:
    """Store credential material for one connection.

    An empty value REMOVES that field rather than storing a blank, so "cleared"
    and "set to empty string" cannot be confused downstream.
    """
    wanted = _text(connection_id).lower()
    secrets = load_secrets(root)
    row = dict(secrets.get(wanted, {}))
    for key, value in values.items():
        text = str(value or "").strip()
        if text:
            row[str(key)] = text
        else:
            row.pop(str(key), None)
    if row:
        secrets[wanted] = row
    else:
        secrets.pop(wanted, None)
    save_secrets(secrets, root)


def set_enabled(
    connection_id: str, enabled: bool, root: pathlib.Path | str | None = None
) -> Tuple[Connection, ...]:
    wanted = _text(connection_id).lower()
    current = list(load_registry(root))
    for index, existing in enumerate(current):
        if existing.connection_id == wanted:
            current[index] = replace(existing, enabled=bool(enabled))
            save_registry(current, root)
            break
    return tuple(current)
