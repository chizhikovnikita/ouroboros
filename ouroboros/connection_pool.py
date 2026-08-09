"""Connection selection: which pooled route carries the next send.

Stage 2 of the pool. The registry (``ouroboros.connections``) says what exists;
this module decides which one to use for one call, spreads load across the pool,
and takes a failing connection out of rotation for a while.

Two design points worth knowing before changing anything here:

**In-flight is derived, not stored.** Workers are separate PROCESSES, so an
in-memory counter of "busy" connections would be wrong the moment a second worker
starts. The usage ledger already appends a ``reserved`` row under a cross-process
lock and moves it to a terminal state when the attempt settles — so the number of
reserved-but-not-terminal rows for a connection IS its in-flight count. Inventing
a second concurrency store would create a second truth that can disagree with the
money ledger.

**No opinion means no change.** When the pool has nothing to say — an empty
registry, one legacy-projected key, an unknown provider — selection returns None
and the caller keeps the environment-derived target byte-identical to what it
used before the pool existed. That is what keeps a single-provider install
working (BIBLE provider independence).
"""

from __future__ import annotations

import logging
import os
import pathlib
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from ouroboros import connections as registry
from ouroboros.provider_models import PROVIDER_CREDENTIAL_GROUPS, provider_for_model


log = logging.getLogger(__name__)

# How long a connection stays out of rotation after a failure. Rate limits and
# quota exhaustion resolve on their own clock, so the penalty is time-based
# rather than a permanent disable an owner would have to undo by hand.
DEFAULT_COOLDOWN_SEC = 60.0
AUTH_COOLDOWN_SEC = 900.0

# Share of calls that ignore the rating entirely. Without it the pool would stop
# sampling its other members and could not learn that the current favourite has
# degraded — or that a newly added connection is good.
EXPLORATION_SHARE = 0.15
# Success rate is a noisy estimate; connections within this band of the best are
# treated as equally good so a hair's difference does not starve one.
RATING_TIE_BAND = 0.05

_COOLDOWN_LOCK = threading.Lock()
# connection_id -> unix time when it may be tried again. Process-local ON PURPOSE:
# a cooldown is a hint that decays, not durable state worth a disk write on every
# provider hiccup. A sibling worker re-learns it from its own first failure.
_COOLDOWN_UNTIL: Dict[str, float] = {}


class NoPermittedConnection(RuntimeError):
    """Sensitive work has no connection cleared to carry it.

    Raised rather than returning None, and that distinction is the whole gate:
    None means "the pool has no opinion, use the environment credential", which
    for sensitive work would hand the data to whatever key happens to be in the
    environment — exactly the provider the gate exists to avoid.
    """


@dataclass(frozen=True)
class Selection:
    """One chosen connection plus the credential material it carries."""

    connection: registry.Connection
    credentials: Mapping[str, str]

    @property
    def connection_id(self) -> str:
        return self.connection.connection_id


def _now() -> float:
    return time.time()


def note_failure(connection_id: str, *, kind: str = "error") -> None:
    """Take a connection out of rotation for a while after a failed send.

    ``kind`` separates a transient provider hiccup from a credential that is
    simply wrong: a bad key will keep failing, so it earns a much longer rest
    than a rate limit that clears in a minute.
    """
    connection_id = str(connection_id or "")
    if not connection_id:
        return
    penalty = AUTH_COOLDOWN_SEC if str(kind) == "auth" else DEFAULT_COOLDOWN_SEC
    with _COOLDOWN_LOCK:
        _COOLDOWN_UNTIL[connection_id] = _now() + penalty
    log.info("connection %s cooling down for %.0fs (%s)", connection_id, penalty, kind)


def note_success(connection_id: str) -> None:
    """Clear any cooldown after a send succeeds on this connection."""
    connection_id = str(connection_id or "")
    if not connection_id:
        return
    with _COOLDOWN_LOCK:
        _COOLDOWN_UNTIL.pop(connection_id, None)


_AUTH_FAILURE_MARKERS = ("401", "403", "invalid api key", "unauthorized", "authentication")


def note_attempt_outcome(connection_id: str, exc: BaseException | None) -> None:
    """Record how one physical send ended, classifying the failure for rotation.

    Called from the accounting path, which owns money rather than routing — the
    classification therefore lives here. Best-effort by design: routing health is
    a hint, and failing an otherwise good accounting path over it would trade a
    real result for a heuristic.
    """
    connection_id = str(connection_id or "")
    if not connection_id:
        return
    try:
        if exc is None:
            note_success(connection_id)
            return
        text = f"{type(exc).__name__}: {exc}".lower()
        kind = "auth" if any(marker in text for marker in _AUTH_FAILURE_MARKERS) else "error"
        note_failure(connection_id, kind=kind)
    except Exception:
        log.debug("connection health note skipped", exc_info=True)


def cooling_down(connection_id: str) -> bool:
    with _COOLDOWN_LOCK:
        until = _COOLDOWN_UNTIL.get(str(connection_id or ""), 0.0)
    return until > _now()


def reset_cooldowns() -> None:
    """Test helper: forget every cooldown."""
    with _COOLDOWN_LOCK:
        _COOLDOWN_UNTIL.clear()


def in_flight_counts(root: pathlib.Path | str | None = None) -> Dict[str, int]:
    """Reserved-but-not-terminal attempts per connection, read from the ledger.

    This is the cross-process load signal. A ledger that cannot be read yields an
    EMPTY map rather than an exception: not knowing the load must degrade to
    "spread by chance", never to "cannot route".
    """
    try:
        from ouroboros.usage_ledger import _drive_root, _read_records_locked, _locked

        drive_root = _drive_root(root)
        with _locked(drive_root):
            records = _read_records_locked(drive_root)
    except Exception:
        log.debug("in-flight counts unavailable; routing without load awareness", exc_info=True)
        return {}

    states: Dict[str, Tuple[str, str]] = {}
    for row in records:
        attempt_id = str(row.get("attempt_id") or "")
        if not attempt_id:
            continue
        state = str(row.get("state") or "")
        # Later rows supersede earlier ones for the same attempt; the connection
        # id is only written on the first (reserved) row.
        previous = states.get(attempt_id)
        connection_id = str(row.get("connection_id") or "") or (previous[1] if previous else "")
        states[attempt_id] = (state, connection_id)

    counts: Dict[str, int] = {}
    for state, connection_id in states.values():
        if not connection_id:
            continue
        if state in ("reserved", "dispatched"):
            counts[connection_id] = counts.get(connection_id, 0) + 1
    return counts


def _credentials_for(connection: registry.Connection, root) -> Dict[str, str]:
    return dict(registry.secret_for(connection, root))


def _at_concurrency_limit(connection: registry.Connection, counts: Mapping[str, int]) -> bool:
    if connection.max_concurrent <= 0:
        return False
    return counts.get(connection.connection_id, 0) >= connection.max_concurrent


def _prefer_better_rated(
    pool: Sequence[registry.Connection], root: pathlib.Path | str | None
) -> list:
    """Narrow to the best-rated connections, keeping an exploration share.

    Pure exploitation would starve every connection that has not proven itself,
    including the ones an owner just added — and a pool that never tries its other
    members cannot notice the favourite degrading. So a slice of calls deliberately
    ignores the ranking.
    """
    ranked = list(pool)
    if len(ranked) < 2:
        return ranked
    try:
        from ouroboros.connection_rating import rank_value, collect_stats

        if random.random() < EXPLORATION_SHARE:
            return ranked
        from ouroboros.connection_rating import quality_by_connection

        stats = collect_stats(root)
        quality = quality_by_connection(root)

        def _value(conn):
            return rank_value(stats.get(conn.connection_id), quality=quality.get(conn.connection_id))

        best = max(_value(conn) for conn in ranked)
        # Everything within the band counts as "as good as the best": success rate
        # is a noisy estimate and a hair's difference is not a reason to starve one.
        top = [conn for conn in ranked if _value(conn) >= best - RATING_TIE_BAND]
        return top or ranked
    except Exception:
        log.debug("rating unavailable; routing without it", exc_info=True)
        return ranked


def active_sensitivity() -> str:
    """Data sensitivity bound to this execution context, if any."""
    try:
        from ouroboros.usage_accounting import current_usage_scope

        scope = current_usage_scope()
        return str(getattr(scope, "data_sensitivity", "") or "") if scope is not None else ""
    except Exception:
        # Fail CLOSED: if the ambient sensitivity cannot be read, assume the work
        # is sensitive rather than assuming it is safe to send anywhere.
        log.warning("data sensitivity unreadable; treating this work as sensitive", exc_info=True)
        return registry.SENSITIVITY_SENSITIVE


def candidates(
    model: str,
    root: pathlib.Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    sensitivity: Optional[str] = None,
) -> Tuple[registry.Connection, ...]:
    """Connections eligible to carry ``model`` right now.

    The privacy gate is applied HERE, in candidate selection, and nowhere else:
    a connection that may train on the data is simply absent from the list for
    sensitive work, however cheap or fast or well-rated it is. A gate expressed as
    a ranking penalty or as prompt text is one a cheap provider can outweigh.
    """
    usable = registry.usable_connections(model, root, env=env)

    level = active_sensitivity() if sensitivity is None else sensitivity
    if registry.sensitivity_rank(level) > 0:
        permitted = tuple(
            conn for conn in usable if registry.connection_allows_sensitivity(conn, level)
        )
        if not permitted:
            # Deliberately NOT falling back to the full set. Everything else in
            # this module degrades toward "route anyway", because failing a send
            # is worse than a slow one — but here the failure mode is sending the
            # owner's sensitive data to a vendor that trains on it, which no
            # amount of availability justifies.
            log.warning(
                "no connection cleared for %s work on %s; refusing to route", level, model
            )
            return ()
        usable = permitted

    usable = _most_specific(usable)

    ready = tuple(conn for conn in usable if not cooling_down(conn.connection_id))
    # Every candidate resting is not the same as having none: rather than fail the
    # send, fall back to the full permitted set and let the provider decide.
    return ready or usable


def _most_specific(pool: Sequence[registry.Connection]) -> Tuple[registry.Connection, ...]:
    """Narrow to the connections that NAME this model, when any do.

    Two gateways can share one transport lane while selling different catalogs, and
    an empty model list means "whatever this provider serves" — a default, not a
    claim. So when one connection lists the model outright and another only matches
    by default, the declaration wins: an owner who wrote the model down was telling
    the pool something, and load-balancing across the silent one just buys a 403.

    Ordering is preserved so the caller's rating and load rules still decide within
    the narrowed set.
    """
    declared = tuple(conn for conn in pool if conn.models)
    return declared or tuple(pool)


def select(
    model: str,
    root: pathlib.Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    exclude: Sequence[str] = (),
    sensitivity: Optional[str] = None,
) -> Optional[Selection]:
    """Choose a connection for ``model``, or None when the pool has no opinion.

    None is a first-class answer: the caller then keeps its environment-derived
    target exactly as before, which is how an install with no registry keeps
    behaving identically.
    """
    level = active_sensitivity() if sensitivity is None else sensitivity
    sensitive = registry.sensitivity_rank(level) > 0

    try:
        pool = candidates(model, root, env=env, sensitivity=level)
    except NoPermittedConnection:
        raise
    except Exception:
        if sensitive:
            # The environment fallback is not available to sensitive work: an
            # unreadable registry must stop the send, not silently widen it.
            raise NoPermittedConnection(
                f"cannot establish which connections may carry {level} work for {model}"
            )
        log.warning("connection selection failed; falling back to environment credentials", exc_info=True)
        return None

    excluded = {str(item) for item in exclude}
    pool = tuple(conn for conn in pool if conn.connection_id not in excluded)
    if not pool:
        if sensitive:
            raise NoPermittedConnection(
                f"no connection is cleared to carry {level} work for {model}"
            )
        return None

    # A single legacy-projected connection is exactly today's behavior; letting it
    # fall through keeps the environment path untouched instead of rebuilding an
    # identical target through the overlay. Sensitive work never takes this shortcut
    # — the environment credential carries no privacy declaration at all.
    if not sensitive and len(pool) == 1 and pool[0].origin == registry.ORIGIN_LEGACY:
        return None

    counts = in_flight_counts(root)
    open_slots = [conn for conn in pool if not _at_concurrency_limit(conn, counts)]
    if not open_slots:
        # Everything is at its declared ceiling. Refusing here would fail a send
        # the provider might well accept, so the least-loaded one still goes.
        open_slots = list(pool)

    open_slots = _prefer_better_rated(open_slots, root)

    fewest = min(counts.get(conn.connection_id, 0) for conn in open_slots)
    least_loaded = [conn for conn in open_slots if counts.get(conn.connection_id, 0) == fewest]
    # Ties are broken at random rather than by list order: a stable order would
    # send every idle-pool call to the same connection and defeat the spreading.
    chosen = random.choice(least_loaded)

    credentials = _credentials_for(chosen, root)
    if not credentials and chosen.kind != registry.KIND_ENDPOINT:
        if sensitive:
            raise NoPermittedConnection(
                f"the only connection cleared for {level} work on {model} has no credential"
            )
        return None
    return Selection(connection=chosen, credentials=credentials)


def _primary_credential_key(provider: str) -> str:
    keys = PROVIDER_CREDENTIAL_GROUPS.get(provider, ())
    return keys[0] if keys else ""


def apply_to_target(target: Dict[str, Any], model: str, root: pathlib.Path | str | None = None) -> Dict[str, Any]:
    """Overlay a pooled connection onto an environment-derived provider target.

    The provider-specific dict is built by ``llm._resolve_remote_target`` exactly
    as before — it encodes real provider knowledge (headers, capability flags,
    GigaChat's non-OpenAI transport). This only replaces the credential and
    endpoint fields and stamps the chosen identity, so when the pool has no
    opinion the target is returned untouched.
    """
    if os.environ.get("OUROBOROS_DISABLE_CONNECTION_POOL", "").strip().lower() in ("1", "true", "yes"):
        # An availability switch must not double as a privacy bypass: turning the
        # pool off may not turn a sensitivity gate off with it.
        if registry.sensitivity_rank(active_sensitivity()) > 0:
            raise NoPermittedConnection(
                "the connection pool is disabled, so no connection can be cleared for sensitive work"
            )
        return target

    provider = str(target.get("provider") or provider_for_model(model))
    try:
        selection = select(model, root)
    except NoPermittedConnection:
        # Never downgraded to "use the environment key": that is precisely the
        # send this refusal exists to prevent.
        raise
    except Exception:
        log.warning("connection overlay skipped", exc_info=True)
        return target
    if selection is None or selection.connection.provider != provider:
        return target

    updated = dict(target)
    updated["connection_id"] = selection.connection_id

    primary = _primary_credential_key(provider)
    if primary and selection.credentials.get(primary):
        updated["api_key"] = selection.credentials[primary]
    if selection.connection.base_url:
        updated["base_url"] = selection.connection.base_url

    # Provider companions the connection declares (region, scope, password, ...)
    # ride in `extras`; only keys this provider actually reads are honored.
    known = set(PROVIDER_CREDENTIAL_GROUPS.get(provider, ()))
    for key, value in selection.connection.extras.items():
        if key in known and value:
            updated.setdefault("extras", {})
            updated["extras"][key] = value
    for key, value in selection.credentials.items():
        if key == primary or key not in known:
            continue
        if provider == "gigachat" and key == "GIGACHAT_PASSWORD" and value:
            updated["password"] = value

    return updated
