"""Reviewer-slot configuration SSOT (phase 6.1).

ONE structured setting — ``OUROBOROS_REVIEWER_SLOTS`` — describes every
configured reviewer row: the commit-triad slots, the scope slots, and the one
optional advisory reviewer. Each row is::

    {"slot_id": "t_9f3a", "route": {"kind": "api_chat" | "agent_session",
                                    "target_id": "<model id | harness[=model]>"},
     "effort": "high"}

``slot_id`` is a STABLE owner-assigned identity, never an array index: a row's
receipts must keep lining up with its own history when the owner reorders or
edits rows (see ``review_substrate.slot_id_for_row`` for why a model is not an
identity either). ``target_id`` is an API model id on the ``api_chat`` kind and
an OPAQUE Claudexor route spec (``harness[=model]`` — Claudexor's own
reviewer-panel spelling, no ``::`` syntax) on ``agent_session``. Effort is a
per-row property on the existing ``EFFORT_SCALE`` — the same mechanism the
model lanes use, deliberately not a new one.

MIGRATION (D15: "старый читается, если новых нет"): when the structured key is
absent, the legacy comma-lists (``OUROBOROS_REVIEW_MODELS`` /
``OUROBOROS_SCOPE_REVIEW_MODELS``) plus the phase-5 per-row route lists are
read into rows, and the global Review / Scope Review efforts are copied into
each row. There is NO permanent double-write: once the structured key is
saved, the comma keys become a derived runtime projection
(``project_reviewer_slots_into_env``) that only exists for the surfaces that
stay on the API by owner decision (D15 — plan review, task acceptance, skill
review consume API rows only; harness delivery is commit-triad/scope/advisory
territory).

Malformed configuration RAISES: mapping a typo to ``api_chat`` would silently
spend the API money the owner configured the row to move off of, and mapping
it to ``agent_session`` would silently delegate a row the owner never
delegated (same posture as ``configured_review_routes``).
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

REVIEWER_SLOTS_ENV = "OUROBOROS_REVIEWER_SLOTS"

ROUTE_KIND_API = "api_chat"
ROUTE_KIND_SESSION = "agent_session"
_ROUTE_KINDS = (ROUTE_KIND_API, ROUTE_KIND_SESSION)

# Real limits, shown in the UI instead of promising an arbitrary number (D14).
# Pinned against their owners by tests: the triad ceiling is
# ``tools.review.MAX_MODELS``; the scope pool is the parallel-review thread
# pool width. Imported lazily there (tools.review is heavy), asserted equal in
# tests so the copies cannot drift silently.
TRIAD_SLOT_LIMIT = 10
SCOPE_SLOT_LIMIT = 4

_SLOT_ID_MAX_CHARS = 64


@dataclass(frozen=True)
class ConfiguredReviewerSlot:
    """One configured reviewer row: identity, delivery, strength."""

    slot_id: str
    kind: str  # api_chat | agent_session
    target_id: str  # API model id, or opaque ``harness[=model]`` session spec
    effort: str = ""  # "" = the surface's global default
    # The opaque per-row session spec. Structured agent_session rows carry
    # their target here; api rows and LEGACY session rows carry '' — a legacy
    # row's delivery stayed on the shared session-route key (phase-5 shape),
    # and the empty value is what keeps that fallback alive downstream.
    session_target: str = ""
    # Optional manual credential pin (Q2-в): '' = the daemon's rotation policy
    # (D28 default). Meaningful on agent_session rows only.
    profile_id: str = ""

    @property
    def is_session(self) -> bool:
        return self.kind == ROUTE_KIND_SESSION


@dataclass(frozen=True)
class AdvisorySlotConfig:
    """The ONE optional advisory reviewer (D14).

    ``enabled=False`` is a standing owner decision with a constitutional
    consequence the UI must state: every reviewed commit then records an
    AUDITED BYPASS instead of an advisory verdict (never a silent skip).
    """

    enabled: bool = True
    kind: str = "api"  # api | agent_session
    # agent_session: harness[=model] spec ('' = shared route). api: the
    # Claude-SDK model spelling — sonnet, opus[1m], claude-… — NOT an
    # OpenRouter catalog id ('' = resolve_claude_code_model() default).
    target_id: str = ""
    effort: str = "low"
    profile_id: str = ""  # optional manual credential pin (Q2-в); '' = rotation


@dataclass(frozen=True)
class ReviewerSlotConfig:
    triad: Tuple[ConfiguredReviewerSlot, ...]
    scope: Tuple[ConfiguredReviewerSlot, ...]
    advisory: AdvisorySlotConfig
    source: str  # "structured" | "legacy"


def structured_reviewer_slots_raw() -> str:
    return str(os.environ.get(REVIEWER_SLOTS_ENV, "") or "").strip()


def structured_reviewer_slots_present() -> bool:
    return bool(structured_reviewer_slots_raw())


def _valid_effort(value: Any, where: str) -> str:
    effort = str(value or "").strip().lower()
    if not effort:
        return ""
    from ouroboros.config import EFFORT_SCALE

    if effort not in EFFORT_SCALE:
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: {where} names an unknown effort {effort!r}; "
            f"valid: {', '.join(EFFORT_SCALE)}"
        )
    return effort


def _parse_slot(row: Any, where: str, seen_ids: set) -> ConfiguredReviewerSlot:
    if not isinstance(row, dict):
        raise ValueError(f"{REVIEWER_SLOTS_ENV}: {where} is not an object")
    slot_id = str(row.get("slot_id") or "").strip()
    if not slot_id or len(slot_id) > _SLOT_ID_MAX_CHARS:
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: {where} needs a stable non-empty slot_id "
            f"(≤{_SLOT_ID_MAX_CHARS} chars) — identity is never an array index"
        )
    if slot_id in seen_ids:
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: slot_id {slot_id!r} appears twice; a row's "
            "receipts can only line up with ONE history"
        )
    seen_ids.add(slot_id)
    route = row.get("route")
    if not isinstance(route, dict):
        raise ValueError(f"{REVIEWER_SLOTS_ENV}: {where} route must be an object "
                         "{kind, target_id}")
    kind = str(route.get("kind") or "").strip().lower()
    if kind not in _ROUTE_KINDS:
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: {where} names an unknown route kind {kind!r}; "
            f"valid: {', '.join(_ROUTE_KINDS)}"
        )
    target = str(route.get("target_id") or "").strip()
    if not target:
        raise ValueError(f"{REVIEWER_SLOTS_ENV}: {where} route.target_id is empty")
    if kind == ROUTE_KIND_SESSION and "::" in target:
        # api_chat targets keep the EXISTING `provider::model` direct-routing
        # spelling; the owner's "no ::" directive bans it for HARNESS routes —
        # the provider there is the harness name itself, never a string prefix.
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: {where} session target {target!r} uses '::' — "
            "a delegated row is spelled harness[=model] (owner directive: no "
            "'::' syntax on coding-agent routes)"
        )
    profile = str(route.get("profile_id") or "").strip()
    return ConfiguredReviewerSlot(
        slot_id=slot_id, kind=kind, target_id=target,
        effort=_valid_effort(row.get("effort"), where),
        session_target=target if kind == ROUTE_KIND_SESSION else "",
        profile_id=profile if kind == ROUTE_KIND_SESSION else "",
    )


def _parse_advisory(raw: Any) -> AdvisorySlotConfig:
    if raw is None:
        return AdvisorySlotConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"{REVIEWER_SLOTS_ENV}: advisory must be an object")
    route = raw.get("route")
    if route is not None and not isinstance(route, dict):
        # Same typed refusal _parse_slot gives (:150). Without it a non-dict
        # route reached `.get` on a str/list and raised AttributeError, which
        # escapes every `except ValueError` that treats this parser as the
        # typed authority — including the commit gate's fail-closed branch and
        # reviewer_slot_config_error's callers.
        raise ValueError(f"{REVIEWER_SLOTS_ENV}: advisory route must be an object "
                         "{kind, target_id}")
    route = route or {}
    kind = str(route.get("kind") or raw.get("kind") or "api").strip().lower()
    if kind in ("", "api", "api_chat"):
        kind = "api"
    elif kind != ROUTE_KIND_SESSION:
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: advisory names an unknown route kind {kind!r}; "
            "valid: api, agent_session"
        )
    target = str(route.get("target_id") or raw.get("target_id") or "").strip()
    if kind == ROUTE_KIND_SESSION and "::" in target:
        raise ValueError(f"{REVIEWER_SLOTS_ENV}: advisory session target {target!r} uses '::'")
    return AdvisorySlotConfig(
        enabled=bool(raw.get("enabled", True)),
        kind=kind,
        target_id=target,
        effort=_valid_effort(raw.get("effort"), "advisory") or "low",
        profile_id=str(route.get("profile_id") or "").strip(),
    )


def parse_reviewer_slots(raw: str) -> ReviewerSlotConfig:
    """Strict parse of the structured setting. Raises ValueError, row-precise."""
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{REVIEWER_SLOTS_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{REVIEWER_SLOTS_ENV} must be a JSON object")
    unknown = sorted(set(payload) - {"triad", "scope", "advisory"})
    if unknown:
        raise ValueError(f"{REVIEWER_SLOTS_ENV} has unknown top-level keys: {unknown}")
    seen_ids: set = set()
    groups: Dict[str, List[ConfiguredReviewerSlot]] = {}
    for group, limit in (("triad", TRIAD_SLOT_LIMIT), ("scope", SCOPE_SLOT_LIMIT)):
        rows = payload.get(group)
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise ValueError(f"{REVIEWER_SLOTS_ENV}: {group} must be an array")
        if len(rows) > limit:
            raise ValueError(
                f"{REVIEWER_SLOTS_ENV}: {group} has {len(rows)} rows; the real "
                f"limit is {limit} (shown in the UI, not negotiable here)"
            )
        groups[group] = [
            _parse_slot(row, f"{group}[{idx}]", seen_ids) for idx, row in enumerate(rows)
        ]
    if not groups["triad"]:
        raise ValueError(f"{REVIEWER_SLOTS_ENV}: triad needs at least one slot")
    if not groups["scope"]:
        raise ValueError(f"{REVIEWER_SLOTS_ENV}: scope needs at least one slot")
    return ReviewerSlotConfig(
        triad=tuple(groups["triad"]),
        scope=tuple(groups["scope"]),
        advisory=_parse_advisory(payload.get("advisory")),
        source="structured",
    )


# ---------------------------------------------------------------------------
# Legacy migration read (comma-lists + phase-5 route envs + global efforts).
# ---------------------------------------------------------------------------


def _shared_session_route_spec() -> tuple[str, str]:
    """The legacy shared session route as ``(identity, effort)``.

    Identity is ``harness[=model]`` — route identity ONLY, effort split off so
    the per-slot effort field stays the single SSOT (D1/6.3). '' when no shared
    session route is configured (a legacy row marked agent_session with no
    shared route was already undeliverable; the empty target keeps that honest
    rather than inventing a harness)."""
    from ouroboros.review_execution import review_session_route

    route = review_session_route()
    if route is None:
        return "", ""
    identity = route.route_id + (f"={route.model}" if route.model else "")
    return identity, str(route.effort or "")


def _legacy_rows(models: List[str], route_env_key: str, effort: str,
                 id_prefix: str) -> List[ConfiguredReviewerSlot]:
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import configured_review_routes, slot_id_for_row

    routes = configured_review_routes(route_env_key, len(models))
    session_target, session_effort = _shared_session_route_spec()
    rows: List[ConfiguredReviewerSlot] = []
    for idx, model in enumerate(models):
        session = routes[idx] is ReviewRouteKind.AGENT_SESSION
        if session:
            # A legacy session row delivered via the SHARED session route (a
            # harness), and its comma-list "model" was never used for delivery
            # — the session resolves its own. So its route identity is that
            # shared harness, NOT the model: writing the model into the
            # harness-shaped target_id is exactly the mapping bug the UI then
            # renders as a nonsense harness. Effort rides the field only.
            rows.append(ConfiguredReviewerSlot(
                slot_id=slot_id_for_row(idx + 1, prefix=id_prefix),
                kind=ROUTE_KIND_SESSION,
                target_id=session_target,
                session_target=session_target,
                effort=session_effort or effort,
            ))
        else:
            # An api row keeps its provider-tagged model as the route identity.
            rows.append(ConfiguredReviewerSlot(
                slot_id=slot_id_for_row(idx + 1, prefix=id_prefix),
                kind=ROUTE_KIND_API,
                target_id=str(model),
                effort=effort,
            ))
    return rows


def _legacy_config() -> ReviewerSlotConfig:
    from ouroboros.config import get_review_models, get_scope_review_models, resolve_effort
    from ouroboros.review_execution import (
        SCOPE_REVIEW_ROUTES_ENV,
        TRIAD_REVIEW_ROUTES_ENV,
    )
    from ouroboros.review_substrate import SCOPE_SLOT_ID_PREFIX, SLOT_ID_PREFIX

    triad = _legacy_rows(
        [str(m) for m in (get_review_models() or []) if str(m or "").strip()],
        TRIAD_REVIEW_ROUTES_ENV, resolve_effort("review"), SLOT_ID_PREFIX,
    )
    scope = _legacy_rows(
        [str(m) for m in (get_scope_review_models() or []) if str(m or "").strip()],
        SCOPE_REVIEW_ROUTES_ENV, resolve_effort("scope_review"), SCOPE_SLOT_ID_PREFIX,
    )
    # The legacy advisory had no standing enable switch (bypass was per-call)
    # and no per-row effort; the route token is the phase-5 env.
    raw_route = str(os.environ.get("OUROBOROS_ADVISORY_REVIEW_ROUTE", "") or "").strip().lower()
    if raw_route in ("", "api", "api_chat"):
        advisory_kind = "api"
    elif raw_route == ROUTE_KIND_SESSION:
        advisory_kind = ROUTE_KIND_SESSION
    else:
        raise ValueError(
            f"OUROBOROS_ADVISORY_REVIEW_ROUTE names an unknown advisory route "
            f"{raw_route!r}; valid: api, agent_session"
        )
    return ReviewerSlotConfig(
        triad=tuple(triad),
        scope=tuple(scope),
        advisory=AdvisorySlotConfig(enabled=True, kind=advisory_kind),
        source="legacy",
    )


def load_reviewer_slot_config() -> ReviewerSlotConfig:
    """THE loader: structured when present, legacy migration read otherwise."""
    raw = structured_reviewer_slots_raw()
    if raw:
        return parse_reviewer_slots(raw)
    return _legacy_config()


def reviewer_slot_config_error() -> str:
    """The structured config's row-precise parse error, or '' when none (#116).

    Thin facade for the surfaces that must refuse loudly instead of running on
    a silently projected default panel (plan review, skill review). Reads ONLY
    the structured raw value — a legacy-only config (comma keys, no structured
    key) always returns '' (bench constraint: benches configure legacy keys
    only and must stay unaffected). No caching: the check re-parses so a
    hot-reloaded fix is seen immediately."""
    raw = structured_reviewer_slots_raw()
    if not raw:
        return ""
    try:
        parse_reviewer_slots(raw)
    except ValueError as exc:
        return str(exc)
    return ""


# ---------------------------------------------------------------------------
# Consumer accessors.
# ---------------------------------------------------------------------------


def commit_triad_rows() -> List[ConfiguredReviewerSlot]:
    """The commit-triad rows (the ONE surface whose rows may be delegated,
    beside scope and advisory — D15)."""
    return list(load_reviewer_slot_config().triad)


def commit_scope_rows() -> List[ConfiguredReviewerSlot]:
    return list(load_reviewer_slot_config().scope)


def advisory_slot_config() -> AdvisorySlotConfig:
    return load_reviewer_slot_config().advisory


def structured_scope_review_slots() -> Optional[list]:
    """The scope ReviewSlots from the structured SSOT, or None on legacy.

    Lives here (not in the substrate) purely for module-size altitude: the
    substrate stays the owner of ReviewSlot semantics and calls this first.
    """
    if not structured_reviewer_slots_present():
        return None
    from ouroboros.config import review_model_uses_local
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewSlot

    return [
        ReviewSlot(
            slot_id=row.slot_id,
            model=row.target_id,
            effort=row.effort or row_effort(row, "scope_review"),
            role_hint="scope reviewer",
            use_local=review_model_uses_local(row.target_id),
            route=(ReviewRouteKind.AGENT_SESSION if row.is_session
                   else ReviewRouteKind.API_CHAT),
            session_target=row.session_target,
            session_profile=row.profile_id,
        )
        for row in commit_scope_rows()
    ]


def commit_triad_delivery() -> Dict[str, list]:
    """Aligned per-row delivery vectors for the commit triad, one call.

    The commit surface consumes rows as parallel lists (models for display and
    slot construction, routes for delivery, efforts/session targets/ids as row
    properties); deriving them HERE keeps the surface at its size gate and
    keeps the vectors impossible to misalign. Raises ValueError on a malformed
    configuration — the caller turns that into its typed infra block.
    """
    from ouroboros.review_execution import ReviewRouteKind

    rows = commit_triad_rows()
    return {
        "models": [row.target_id for row in rows],
        "routes": [
            ReviewRouteKind.AGENT_SESSION if row.is_session else ReviewRouteKind.API_CHAT
            for row in rows
        ],
        "efforts": [row_effort(row, "review") for row in rows],
        "session_targets": [row.session_target for row in rows],
        "session_profiles": [row.profile_id for row in rows],
        "slot_ids": [row.slot_id for row in rows],
    }


def row_effort(row: ConfiguredReviewerSlot, surface: str) -> str:
    """A row's effective effort: its own, else the surface's global default
    (the existing effort mechanism, reused per the owner's directive)."""
    if row.effort:
        return row.effort
    from ouroboros.config import resolve_effort

    return resolve_effort(surface)


# ---------------------------------------------------------------------------
# Runtime projection for the API-pinned surfaces (D15).
# ---------------------------------------------------------------------------


def api_fallback_disclosure(config: "ReviewerSlotConfig") -> Dict[str, Any]:
    """What the API-pinned surfaces (plan review, task acceptance, skill review)
    will REALLY run when a group has no api_chat row left (D4/D15).

    When every commit-triad (or scope) row is delegated, those surfaces cannot
    run on a delegated route — they stay on the API by owner decision — so they
    fall back to the shipped DEFAULT models and spend API money. That is a real
    substitution the UI must not paper over: this returns the substituted model
    list per affected group, or an empty dict when every group keeps an api row.
    """
    from ouroboros.config import SETTINGS_DEFAULTS

    out: Dict[str, Any] = {}
    if config.triad and not any(not r.is_session for r in config.triad):
        out["triad"] = str(SETTINGS_DEFAULTS["OUROBOROS_REVIEW_MODELS"]).split(",")
    if config.scope and not any(not r.is_session for r in config.scope):
        out["scope"] = str(SETTINGS_DEFAULTS["OUROBOROS_SCOPE_REVIEW_MODELS"]).split(",")
    return out


def reviewer_slot_api_fallback_warning(raw: Optional[str] = None) -> str:
    """Save-time warning for the all-delegated substitution, or '' when none.

    ``raw`` lets the save handler pass the INCOMING structured value (before it
    is stored); with none it reads the currently stored value. A malformed
    value returns '' (the strict parser reports that separately)."""
    raw = structured_reviewer_slots_raw() if raw is None else str(raw or "").strip()
    if not raw:
        return ""
    try:
        disclosure = api_fallback_disclosure(parse_reviewer_slots(raw))
    except ValueError:
        return ""
    return _fallback_warning_text(disclosure)


def _fallback_warning_text(disclosure: Dict[str, Any]) -> str:
    """The owner-facing all-delegated-fallback sentence, or '' when none."""
    if not disclosure:
        return ""
    surfaces = " and ".join(
        {"triad": "commit review", "scope": "scope review"}[g] for g in disclosure)
    models = sorted({m for row in disclosure.values() for m in row})
    return (
        f"Every {surfaces} row is delegated to a coding agent. Plan review, task "
        f"acceptance and skill review stay on the API and will fall back to the "
        f"shipped default models ({', '.join(models)}), spending API budget. "
        f"Keep at least one API reviewer row to avoid the fallback."
    )


def reviewer_slot_save_check(raw: str) -> str:
    """Validate an incoming structured value and return the fallback warning.

    Raises ValueError (row-precise) on a malformed value so the save handler
    turns it into a 400; returns the all-delegated API-fallback warning ('' when
    none) otherwise. One call keeps the handler at its size gate."""
    disclosure = api_fallback_disclosure(parse_reviewer_slots(raw))  # raises on malformed
    return _fallback_warning_text(disclosure)


def _record_api_fallback_substitution(disclosure: Dict[str, Any]) -> None:
    """Durable half of the D4 disclosure: a config projection that silently
    substituted default models leaves a record, not just a log line."""
    from ouroboros.utils import utc_now_iso, write_text_atomic

    path = _last_execution_path().parent / "reviewer_slot_api_fallback.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps({
            "ts": utc_now_iso(),
            "reason": "all_commit_rows_delegated_api_surfaces_fell_back_to_defaults",
            "substituted": disclosure,
        }, ensure_ascii=False, indent=1))
    except OSError:
        pass


def project_reviewer_slots_into_env() -> None:
    """Project the structured config into the legacy env keys, at env-apply time.

    Plan review, task acceptance and skill review stay on the API by owner
    decision (D15) and keep reading ``get_review_models()``; when the owner's
    triad mixes in delegated rows, those surfaces must see ONLY the API rows.
    This is a runtime DERIVATION, not a second write: settings.json holds the
    structured key alone, and a stale comma value there is overwritten here
    rather than winning silently.

    Also owns the historical default-if-empty floor for both comma keys (moved
    verbatim from ``apply_settings_to_env`` so the tail behavior is one place).

    A malformed structured value is logged loudly and left UNPROJECTED —
    env-apply runs at server startup, where raising would take the whole app
    down with it; the review surfaces themselves re-parse strictly and BLOCK
    with the precise error instead.
    """
    from ouroboros.config import SETTINGS_DEFAULTS

    raw = structured_reviewer_slots_raw()
    if raw:
        try:
            config = parse_reviewer_slots(raw)
        except ValueError:
            import logging

            logging.getLogger(__name__).error(
                "%s is malformed; legacy env keys left unprojected — review "
                "surfaces will block with the precise parse error",
                REVIEWER_SLOTS_ENV, exc_info=True,
            )
        else:
            api_triad = [r.target_id for r in config.triad if not r.is_session]
            api_scope = [r.target_id for r in config.scope if not r.is_session]
            if api_triad:
                os.environ["OUROBOROS_REVIEW_MODELS"] = ",".join(api_triad)
            else:
                # An all-delegated triad leaves the API surfaces (D15) on the
                # shipped defaults rather than on zero reviewers.
                os.environ.pop("OUROBOROS_REVIEW_MODELS", None)
            if api_scope:
                os.environ["OUROBOROS_SCOPE_REVIEW_MODELS"] = ",".join(api_scope)
            else:
                os.environ.pop("OUROBOROS_SCOPE_REVIEW_MODELS", None)
                os.environ.pop("OUROBOROS_SCOPE_REVIEW_MODEL", None)
            # D4: the substitution about to happen at the floor below is not
            # silent — it lands loudly in the log and a durable record. The
            # save-time warning (reviewer_slot_api_fallback_warning) is the
            # third disclosure surface the owner actually reads.
            disclosure = api_fallback_disclosure(config)
            if disclosure:
                import logging

                logging.getLogger(__name__).warning(
                    "reviewer slots: every %s row is delegated; the API-pinned "
                    "review surfaces fall back to shipped default models %s and "
                    "spend API budget",
                    " and ".join(disclosure), disclosure,
                )
                _record_api_fallback_substitution(disclosure)
    if not os.environ.get("OUROBOROS_REVIEW_MODELS"):
        os.environ["OUROBOROS_REVIEW_MODELS"] = str(SETTINGS_DEFAULTS["OUROBOROS_REVIEW_MODELS"])
    if not os.environ.get("OUROBOROS_SCOPE_REVIEW_MODELS") and not os.environ.get("OUROBOROS_SCOPE_REVIEW_MODEL"):
        os.environ["OUROBOROS_SCOPE_REVIEW_MODELS"] = str(SETTINGS_DEFAULTS["OUROBOROS_SCOPE_REVIEW_MODELS"])


# ---------------------------------------------------------------------------
# «Выполняется как» (D22): the last EFFECTIVE execution per slot.
#
# The UI projection of capability_delta — beside each SAVED row, what the row
# REALLY ran as last time (route, model, effort, verdict method, any deltas).
# Disclosure, never enforcement: nothing reads this back into routing.
# ---------------------------------------------------------------------------

LAST_EXECUTION_FILENAME = "reviewer_slot_last_execution.json"
_LAST_EXECUTION_CAP = 64  # slots are ≤ 10+4+1; the cap only bounds junk growth


def _last_execution_path() -> "pathlib.Path":
    import pathlib

    from ouroboros.config import DATA_DIR

    return pathlib.Path(DATA_DIR) / "state" / LAST_EXECUTION_FILENAME


# `run_parallel_review` runs the triad and the scope surfaces CONCURRENTLY, in two
# threads of one process, and each finishes by folding its own rows into this one
# file. `write_text_atomic` makes the write untearable but cannot make the
# read-modify-write around it atomic: both threads read the same "before", and the
# slower one wrote its rows over the faster one's. The surface that vanished was
# whichever finished first — so the panel silently lost a whole row's «Выполняется
# как» line. In-process lock only: the concurrency is threads, not processes.
_LAST_EXECUTION_LOCK = threading.Lock()


def record_reviewer_slot_executions(surface: str, actors: Any, slots_by_id: Dict[str, Any]) -> None:
    """Record each actor's last effective execution (best-effort, atomic).

    Written into the CANONICAL data plane (not the review drive): this is UI
    state beside the saved settings, not per-task forensics — those live in
    the durable actor records already.
    """
    from ouroboros.utils import utc_now_iso, write_text_atomic

    path = _last_execution_path()
    with _LAST_EXECUTION_LOCK:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        for actor in actors or []:
            slot = slots_by_id.get(getattr(actor, "slot_id", ""))
            if slot is None:
                continue
            usage = dict(getattr(actor, "usage", {}) or {})
            route_kind = str(getattr(getattr(slot, "route", None), "value", "") or "api_chat")
            delegated_route = str(usage.get("delegated_route") or "")
            session = route_kind == "agent_session" or bool(delegated_route)
            effective: Dict[str, Any] = {
                # For a session the harness resolves route/model on its side; for
                # api_chat what was sent is what ran.
                "route": (f"agent_session:{delegated_route}" if delegated_route
                          else route_kind),
                # APPLIED honesty: a session whose telemetry disclosed no resolved
                # model shows ABSENCE — the requested model must never be dressed
                # up as the applied one. An api row's sent model IS its applied one.
                "model": (str(usage.get("resolved_model") or "") if session
                          else str(getattr(slot, "model", "") or "")),
                # No "effort": no APPLIED effort exists anywhere upstream (no
                # applied/resolved effort in any receipt or telemetry), so the only
                # value available is the REQUESTED one — already recorded below. Echoing
                # it here dressed the request up as the applied value, the exact thing
                # the model rule above forbids.
                "verdict_method": str(usage.get("verdict_method") or ""),
            }
            # D29 applied account/access, verbatim from the engine receipt; absent
            # keys mean the telemetry predates the receipt — shown as absence.
            if usage.get("applied_profile"):
                effective["profile_id"] = str(usage["applied_profile"])
            if usage.get("applied_access"):
                effective["access"] = str(usage["applied_access"])
            data[str(actor.slot_id)] = {
                "ts": utc_now_iso(),
                "surface": str(surface or ""),
                "requested": {
                    "route_kind": route_kind,
                    "model": str(getattr(slot, "model", "") or ""),
                    "effort": str(getattr(slot, "effort", "") or ""),
                    "session_target": str(getattr(slot, "session_target", "") or ""),
                    "profile_id": str(getattr(slot, "session_profile", "") or ""),
                },
                "effective": effective,
                "capability_delta": usage.get("capability_delta") or [],
                "status": str(getattr(actor, "status", "") or ""),
            }
        if len(data) > _LAST_EXECUTION_CAP:
            ordered = sorted(data.items(), key=lambda kv: str(kv[1].get("ts") or ""))
            data = dict(ordered[-_LAST_EXECUTION_CAP:])
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=1))


def reviewer_slot_last_executions() -> Dict[str, Any]:
    """Read the projection ('' shape on any read problem — disclosure only)."""
    try:
        data = json.loads(_last_execution_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


__all__ = [
    "REVIEWER_SLOTS_ENV",
    "ROUTE_KIND_API",
    "ROUTE_KIND_SESSION",
    "SCOPE_SLOT_LIMIT",
    "TRIAD_SLOT_LIMIT",
    "AdvisorySlotConfig",
    "ConfiguredReviewerSlot",
    "ReviewerSlotConfig",
    "advisory_slot_config",
    "api_fallback_disclosure",
    "commit_scope_rows",
    "commit_triad_rows",
    "reviewer_slot_api_fallback_warning",
    "load_reviewer_slot_config",
    "parse_reviewer_slots",
    "reviewer_slot_config_error",
    "project_reviewer_slots_into_env",
    "record_reviewer_slot_executions",
    "reviewer_slot_last_executions",
    "row_effort",
    "structured_reviewer_slots_present",
    "structured_scope_review_slots",
    "structured_reviewer_slots_raw",
]
