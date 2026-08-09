"""Ouroboros-owned Claudexor daemon (D30).

Ouroboros runs its OWN ``claudexord`` under a data-plane config dir
(``CLAUDEXOR_CONFIG_DIR`` — the override IS the complete relocatable root:
config, credential profiles, secrets, daemon token, socket, runs). The
operator's personal ``~/.claudexor`` state is never read, never imported and
never touched: coexisting daemons per config dir are the engine's own
first-class seam, and the owner's existing logins stay the owner's (accounts
for the Ouroboros home are logged in fresh, through the daemon's own login
jobs).

Lifecycle follows the local-model-server template:

* spawn through ``process_custody`` (session scope) so a dead server
  generation's daemon is reaped by fingerprint, never by command-line class;
* ATTACH-IF-ALIVE: a live daemon already serving our config dir (a previous
  generation's, custody-pending) is attached to, not duplicated — the engine
  refuses a second daemon on the same socket anyway;
* OWN-ONLY-IF-SELF-STARTED: ``stop`` terminates only a process THIS manager
  spawned. An attached daemon — ours-by-home or anyone else's — is never
  killed here; foreign daemons are not ours to stop, and the custody reaper
  already owns cross-generation cleanup for self-spawned ones.

Zero auth logic lives here or anywhere in Ouroboros: login jobs, device-code
custody, verification and rotation are the daemon's own product surface,
reached through the ``/v2`` control API (``gateways/claudexor.py``).
"""

from __future__ import annotations

import logging
import os
import pathlib
import subprocess
import threading
import time
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

_OWNED_DIR_NAME = "claudexor"
_SPAWN_WAIT_SEC = 20.0
_SPAWN_POLL_SEC = 0.25
def owned_config_dir() -> pathlib.Path:
    """The data-plane root the owned daemon lives under."""
    from ouroboros.config import DATA_DIR

    return pathlib.Path(DATA_DIR) / _OWNED_DIR_NAME


def owned_descriptor_path() -> pathlib.Path:
    return owned_config_dir() / "daemon" / "control-api.json"


def owned_daemon_provisioned() -> bool:
    """Has the owner ever provisioned the owned daemon? (descriptor exists)

    This is the D30 cutover predicate: default daemon discovery prefers the
    owned home exactly from the moment this is True, and the moment is an
    owner action (first login/connect), never a silent boot-time switch.
    """
    try:
        return owned_descriptor_path().is_file()
    except OSError:
        return False


OWNERSHIP_MARKER = "ouroboros-owned.json"


def ownership_marker_path() -> pathlib.Path:
    return owned_config_dir() / OWNERSHIP_MARKER


def read_ownership_marker() -> Dict[str, Any]:
    """The durable claim that THIS data plane provisioned the home ({} = none)."""
    import json

    try:
        raw = json.loads(ownership_marker_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def verify_owned_home() -> str:
    """'' when the home is OURS to manage; a typed reason otherwise.

    Two independent facts, both required before any restart may CLAIM the
    home: the config dir sits under OUR data plane, and the ownership marker
    (when present) names the same data plane. A marker naming a different
    data plane is a FOREIGN home — disclosed, never adopted, never killed.
    A missing marker on our own data-plane path is fine: it is written at
    provision time, and pre-marker homes are ours by construction of the path.
    """
    from ouroboros.config import DATA_DIR

    config_dir = owned_config_dir()
    data_dir = pathlib.Path(DATA_DIR).resolve()
    try:
        config_dir.resolve().relative_to(data_dir)
    except ValueError:
        return f"config dir {config_dir} is outside the data plane {data_dir}"
    marker = read_ownership_marker()
    marked = str(marker.get("data_dir") or "")
    if marked and pathlib.Path(marked).resolve() != data_dir:
        return (f"ownership marker names a different data plane ({marked}); "
                "this home is not ours to manage")
    return ""


def _write_ownership_marker() -> None:
    import json

    from ouroboros.config import DATA_DIR
    from ouroboros.utils import utc_now_iso, write_text_atomic

    try:
        ownership_marker_path().parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(ownership_marker_path(), json.dumps({
            "owner": "ouroboros",
            "data_dir": str(pathlib.Path(DATA_DIR).resolve()),
            "provisioned_at": utc_now_iso(),
        }, ensure_ascii=False, indent=1))
    except OSError:
        log.warning("ownership marker write failed", exc_info=True)


def resolve_claudexord() -> str:
    """Compatibility view of the old single-binary resolver."""
    from ouroboros.claudexor_runtime import resolve_external_claudexord

    return resolve_external_claudexord()


def attach_login_command(job_id: str) -> str:
    """The copy-paste fallback card's command (D30): run by the USER in the
    user's own terminal, outside the Ouroboros UI. There is no in-app terminal
    and there will not be one."""
    return (
        f"CLAUDEXOR_CONFIG_DIR={owned_config_dir()} "
        f"claudexor setup attach {str(job_id)}"
    )


class OwnedClaudexorDaemon:
    """Supervisor for the one Ouroboros-owned daemon (module singleton)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._last_error = ""
        self._engine_version = ""
        self._engine_build_sha = ""

    # -- state ------------------------------------------------------------

    def _classify_liveness(self) -> tuple:
        """(endpoint_or_None, state, detail) — the ONE liveness probe.

        The bearer token in OUR descriptor is the identity proof: each home's
        daemon mints its own random token, so an AUTHENTICATED handshake can
        only succeed against the daemon serving our home. An auth refusal
        (401/403) therefore means something ELSE answered on the descriptor's
        stale port — a foreign daemon, alive, not ours: disclosed, never
        killed, never adopted. Every other failure is a dead/unreachable
        daemon: the ordinary stale case a restart heals.
        """
        from ouroboros.gateways.claudexor import (
            ClaudexorGateway,
            ClaudexorUnavailable,
            discover_daemon_at,
        )

        if not owned_daemon_provisioned():
            self._engine_version = ""
            self._engine_build_sha = ""
            return None, "not_provisioned", ""
        try:
            endpoint = discover_daemon_at(owned_config_dir())
        except ClaudexorUnavailable as exc:
            return None, "stale", f"{exc.code}: {exc}"
        try:
            with ClaudexorGateway(endpoint) as gateway:
                handshake = gateway.handshake()
                self._engine_version = gateway.engine_version
                engine = handshake.get("engine") if isinstance(handshake.get("engine"), dict) else {}
                self._engine_build_sha = str(engine.get("sha") or "")
            return endpoint, "running", ""
        except ClaudexorUnavailable as exc:
            self._engine_version = ""
            self._engine_build_sha = ""
            status = int(getattr(exc, "status_code", 0) or 0)
            if status in (401, 403):
                return None, "foreign_daemon", (
                    f"{exc.code}: a live daemon answered on the owned home's "
                    "descriptor port but REFUSED our home's token — a foreign "
                    "daemon recycled the port. It is not ours: disclosed, not "
                    "killed; a restart of OUR daemon rewrites the descriptor."
                )
            return None, "stale", f"{exc.code}: {exc}"

    def _alive_endpoint(self) -> Optional[Any]:
        """Endpoint of a LIVE daemon on our home, or None. Never spawns."""
        endpoint, state, detail = self._classify_liveness()
        if detail:
            self._last_error = detail
        return endpoint

    def status_dict(self) -> Dict[str, Any]:
        """UI status projection. Read-only: never spawns."""
        endpoint, state, detail = self._classify_liveness()
        if detail:
            self._last_error = detail
        ownership_problem = verify_owned_home() if state != "not_provisioned" else ""
        from ouroboros.claudexor_runtime import get_runtime_manager

        runtime_manager = get_runtime_manager()
        runtime = runtime_manager.status(
            running=state == "running",
            engine_version=self._engine_version,
            engine_build_sha=self._engine_build_sha,
        )
        command = runtime_manager.resolve_command()
        return {
            "state": state,
            "config_dir": str(owned_config_dir()),
            "engine_version": self._engine_version,
            "engine_build_sha": self._engine_build_sha,
            "self_started": bool(self._proc is not None and self._proc.poll() is None),
            "binary": command[0] if command else None,
            "runtime": runtime,
            "last_error": self._last_error or None,
            # Typed foreign-home disclosure ('' = ours): a marker naming another
            # data plane means we display, and manage, NOTHING here.
            "ownership_problem": ownership_problem or None,
        }

    # -- lifecycle ----------------------------------------------------------

    def ensure_running(self) -> Any:
        """Attach to a live owned daemon, or (re)start one; returns its endpoint.

        The stale lifecycle, minimal and honest: verify liveness by an
        AUTHENTICATED handshake; a dead daemon whose home carries OUR ownership
        marker is restarted under the same supervision and reconciled (fresh
        discovery + handshake against the rewritten descriptor); a live daemon
        that refuses our token is FOREIGN — disclosed in the typed state, never
        killed, and never a reason not to restart OUR OWN dead daemon, whose
        socket is free by definition. A home whose marker names another data
        plane is refused outright: restarting there would be adoption.

        Raises ClaudexorUnavailable (typed) when the binary is missing, the
        home is not ours, or the spawned daemon never published a live
        descriptor.
        """
        from ouroboros.gateways.claudexor import ClaudexorUnavailable

        with self._lock:
            endpoint, state, detail = self._classify_liveness()
            # NEVER ADOPT: before claiming the home (restart OR first spawn),
            # prove it is ours — under our data plane, marker (if any) naming
            # our data plane. A foreign marker is a typed refusal, not a kill.
            if endpoint is None:
                ownership_problem = verify_owned_home()
                if ownership_problem:
                    raise ClaudexorUnavailable("foreign_daemon_home", ownership_problem)
            if state == "foreign_daemon" and detail:
                # A live foreign daemon sits on our STALE descriptor port. Our
                # own daemon is dead (it would hold that port otherwise), so
                # restarting ours is legitimate: the fresh spawn binds a new
                # ephemeral port and rewrites the descriptor. The foreign one
                # is left untouched and the fact is disclosed, not silenced.
                log.warning("owned-daemon restart proceeding past a foreign "
                            "responder on the stale port: %s", detail)
            from ouroboros.claudexor_runtime import ClaudexorRuntimeError, get_runtime_manager

            runtime_manager = get_runtime_manager()
            if endpoint is not None:
                pin = getattr(runtime_manager, "pin", None)
                if (
                    pin is not None
                    and self._engine_version == getattr(pin, "version", None)
                    and self._engine_build_sha == getattr(pin, "build_sha", None)
                ):
                    # The live, authenticated daemon already serves the exact
                    # pinned identity. Never touch its directory here: a broken
                    # on-disk copy of the SAME target would otherwise trigger a
                    # repair that swaps the serving tree under the running
                    # process. Disk repair happens at the next natural start
                    # through the ordinary ensure path (owner decision 2A:
                    # side-by-side, current work is never touched).
                    return endpoint
            try:
                command = runtime_manager.ensure()
            except ClaudexorRuntimeError as exc:
                if endpoint is not None:
                    log.warning(
                        "managed runtime ensure failed while the owned daemon remains live: %s", exc
                    )
                    return endpoint
                raise ClaudexorUnavailable(exc.code, str(exc)) from exc
            if endpoint is not None:
                # A newer managed tree may have been staged above, but a live
                # daemon is never hot-swapped. The next natural start selects it.
                return endpoint
            config_dir = owned_config_dir()
            config_dir.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ)
            env["CLAUDEXOR_CONFIG_DIR"] = str(config_dir)
            # Loopback-only ephemeral port is the engine default; explicitly
            # scrub any operator-level overrides that would cross homes.
            for crossing in ("CLAUDEXOR_DAEMON_SOCK", "CLAUDEXOR_CONTROL_PORT"):
                env.pop(crossing, None)
            command_bin = pathlib.Path(command[0]).parent
            if command_bin.is_dir():
                # Windows materializes os.environ with its native "Path" key; a
                # plain dict lookup of "PATH" misses it and would hand the child
                # a PATH holding only the Node bin dir (the engine then reports
                # git_missing). Prepend onto whichever key the host actually has.
                path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
                # An EMPTY PATH component means the CURRENT WORKING DIRECTORY on
                # POSIX. A host with no PATH (a scrubbed service manager, a bare
                # container unit) would otherwise leave a trailing empty entry
                # here and make CWD an executable search root for a long-lived
                # daemon that shells out to tools of its own. Drop every empty
                # component; order is otherwise preserved exactly.
                inherited = str(env.get(path_key, "") or "")
                composed = [str(command_bin), *inherited.split(os.pathsep)]
                env[path_key] = os.pathsep.join(part for part in composed if part)
            runtime = get_runtime_manager().status()
            log_path = config_dir / "daemon.log"
            from ouroboros.config import DATA_DIR
            from ouroboros.process_custody import spawn_supervised

            log.info("Spawning owned claudexord under %s from %s", config_dir, runtime.get("source") or "external")
            with open(log_path, "ab") as sink:
                self._proc = spawn_supervised(
                    command,
                    drive_root=pathlib.Path(DATA_DIR),
                    purpose="claudexor_daemon",
                    scope="session",
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=sink,
                    stderr=sink,
                )
            _write_ownership_marker()
            deadline = time.monotonic() + _SPAWN_WAIT_SEC
            while time.monotonic() < deadline:
                if self._proc.poll() is not None:
                    break
                # RECONCILE: fresh discovery + AUTHENTICATED handshake against
                # the descriptor the new daemon just wrote — the same identity
                # proof attach uses, so a restart never claims a port it does
                # not hold.
                endpoint = self._alive_endpoint()
                if endpoint is not None:
                    self._last_error = ""
                    self._enable_rotation(endpoint)
                    return endpoint
                time.sleep(_SPAWN_POLL_SEC)
            # Two Ouroboros processes can race only on first provisioning: the
            # winner publishes the owned endpoint and the losing Claudexor
            # child exits after observing the same writer lease. Reconcile one
            # final time after child exit before reporting a false spawn
            # failure. An exited loser is not a process this manager owns.
            endpoint = self._alive_endpoint()
            if endpoint is not None:
                if self._proc.poll() is not None:
                    self._proc = None
                self._last_error = ""
                self._enable_rotation(endpoint)
                return endpoint
            tail = ""
            try:
                tail = log_path.read_bytes()[-500:].decode("utf-8", errors="replace")
            except OSError:
                pass
            # OUR OWN child, and it never became a daemon we can reach: leaving it
            # alive orphans a process holding this config dir, and leaving the handle
            # set makes the NEXT ensure_running spawn a second one beside it. Killed
            # here rather than in `stop()`, which by contract only ever terminates a
            # daemon we successfully started.
            self._terminate_child()
            raise ClaudexorUnavailable(
                "daemon_spawn_failed",
                "the owned claudexord did not publish a live control descriptor "
                f"within {_SPAWN_WAIT_SEC:.0f}s"
                + (f"; log tail: {tail}" if tail else ""),
            )

    def _terminate_child(self) -> None:
        """Stop and forget the child this manager spawned. Caller holds the lock."""
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        from ouroboros.platform_layer import kill_process_group_id, process_group_id

        try:
            pgid = process_group_id(proc.pid)
            if pgid:
                kill_process_group_id(pgid)
            else:
                proc.terminate()
        except Exception:
            proc.terminate()

    def _enable_rotation(self, endpoint: Any) -> None:
        """D28 at provisioning: ONE settings patch turns profile auto-rotation
        on for every discovered harness (the engine default is fail). Config,
        not code — the daemon owns the rotation engine; best-effort because a
        patch failure must not eat the login that provisioned the daemon."""
        try:
            from ouroboros.gateways.claudexor import ClaudexorGateway

            with ClaudexorGateway(endpoint) as gateway:
                gateway.handshake()
                harness_ids = [
                    str(row.get("id") or "")
                    for row in gateway.agent_capabilities().get("harnesses") or []
                    if isinstance(row, dict) and row.get("id")
                ]
                if harness_ids:
                    gateway.patch_settings({
                        "harnesses": {hid: {"profileLimitAction": "rotate"} for hid in harness_ids},
                    })
        except Exception:
            log.warning("rotation enablement patch failed (D28); the daemon keeps "
                        "its own default until the next provisioning", exc_info=True)

    def stop(self) -> bool:
        """Terminate ONLY a self-started daemon; attached daemons are left alone."""
        with self._lock:
            live = self._proc is not None and self._proc.poll() is None
            self._terminate_child()
            return live


_MANAGER: Optional[OwnedClaudexorDaemon] = None
_MANAGER_LOCK = threading.Lock()


def get_owned_daemon() -> OwnedClaudexorDaemon:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = OwnedClaudexorDaemon()
        return _MANAGER


def ensure_owned_gateway() -> Any:
    """Return an authenticated gateway to the lazily ensured owned daemon.

    This is the explicit start/probe seam. The gateway transport itself stays
    pure I/O; callers own ``close()`` (or use it as a context manager). Daemon
    stop semantics are unchanged: only ``get_owned_daemon().stop()`` may stop a
    process this manager spawned, and it never kills an attached process.
    """
    from ouroboros.gateways.claudexor import ClaudexorGateway

    endpoint = get_owned_daemon().ensure_running()
    gateway = ClaudexorGateway(endpoint)
    try:
        gateway.handshake()
    except Exception:
        gateway.close()
        raise
    return gateway


__all__ = [
    "OwnedClaudexorDaemon",
    "ownership_marker_path",
    "read_ownership_marker",
    "verify_owned_home",
    "attach_login_command",
    "ensure_owned_gateway",
    "get_owned_daemon",
    "owned_config_dir",
    "owned_daemon_provisioned",
    "owned_descriptor_path",
    "resolve_claudexord",
]
