"""Headless task gateway endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import shutil
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse

from ouroboros.gateway._helpers import coerce_int, json_error, json_exception, request_drive_root, request_json_or, request_repo_dir
from ouroboros.headless import (
    ARTIFACTS_DIR,
    ARTIFACT_STATUS_FAILED,
    ARTIFACT_STATUS_FINALIZING,
    ARTIFACT_STATUS_PENDING,
    HEADLESS_TASKS_DIR,
    prepare_task_drive,
    task_artifacts_dir,
    write_workspace_preflight_artifact,
)
from ouroboros.contracts.task_contract import (
    attach_task_contract,
    normalize_acceptance_claims,
    normalize_allowed_resources,
    normalize_answer_protocol,
    normalize_bool,
    normalize_disabled_tools,
    normalize_resource_policy,
)
from ouroboros.outcomes import public_task_result
from ouroboros.task_results import (
    STATUS_FAILED,
    STATUS_SCHEDULED,
    list_task_results,
    load_task_result,
    validate_task_id,
    write_task_result,
)
from ouroboros.task_status import (
    FINAL_STATUSES,
    effective_task_result,
    find_child_tasks,
    load_effective_task_result,
)
from ouroboros.tool_access import path_is_relative_to, paths_overlap_casefold
from ouroboros.workspace_preflight import (
    collect_workspace_preflight,
    summarize_workspace_preflight,
)
from ouroboros.workspace_executor import normalize_executor_ref


log = logging.getLogger(__name__)

_LOG_SOURCES = (
    ("progress", ("logs", "progress.jsonl")),
    ("chat", ("logs", "chat.jsonl")),
    ("events", ("logs", "events.jsonl")),
    ("tools", ("logs", "tools.jsonl")),
    ("supervisor", ("logs", "supervisor.jsonl")),
)

_RESERVED_METADATA_KEYS = frozenset({
    "task_id",
    "parent_task_id",
    "root_task_id",
    "session_id",
    "actor_id",
    "delegation_role",
    "drive_root",
    "child_drive_root",
    "headless_child_drive_root",
    "budget_drive_root",
    "task_constraint",
    "task_contract",
    "allowed_resources",
    "deadline_at",
    "executor_ref",
    "workspace_executor",
    "project_id",
})


def _cleanup_api_admission_attempt(
    drive_root: pathlib.Path,
    task_id: str,
    admission_token: str,
    child_drive: Optional[pathlib.Path] = None,
) -> None:
    """Release one token and remove only its pre-admission task-local state."""
    from supervisor.queue import release_task_admission

    release_task_admission(task_id, admission_token)
    if child_drive is not None:
        try:
            from ouroboros.headless import remove_subagent_task_drive

            remove_subagent_task_drive(drive_root, task_id)
        except Exception:
            log.warning("Failed to clean child drive for rejected task %s", task_id, exc_info=True)
    try:
        shutil.rmtree(task_artifacts_dir(drive_root, task_id, create=False), ignore_errors=True)
    except Exception:
        log.warning("Failed to clean admission artifacts for task %s", task_id, exc_info=True)


def _external_subagent_label(body: Dict[str, Any], metadata: Dict[str, Any]) -> bool:
    role_values = [
        body.get("delegation_role"),
        metadata.get("delegation_role"),
    ]
    return any(str(value or "").strip().lower() == "subagent" for value in role_values)


def _normalize_deadline_at(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("deadline_at must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError("deadline_at must include a timezone offset or Z")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fold_contract_policies(body: Dict[str, Any], raw_metadata: Dict[str, Any], metadata: Dict[str, Any]):
    """Normalize the declarative contract policies from the request body into task
    metadata (extracted from api_tasks_create for the function-size gate; pure).
    Returns (allowed_resources, resource_policy, disabled_tools, acceptance_claims,
    error) — error is non-empty for an invalid service_teardown."""
    allowed_resources = normalize_allowed_resources(body.get("allowed_resources") or raw_metadata.get("allowed_resources") or {})
    if allowed_resources:
        metadata["allowed_resources"] = allowed_resources
    resource_policy = normalize_resource_policy(body.get("resource_policy") or raw_metadata.get("resource_policy") or {})
    if resource_policy:
        metadata["resource_policy"] = resource_policy
    disabled_tools = normalize_disabled_tools(body.get("disabled_tools") or raw_metadata.get("disabled_tools") or [])
    if disabled_tools:
        metadata["disabled_tools"] = disabled_tools
    acceptance_claims = normalize_acceptance_claims(body.get("acceptance_claims") or raw_metadata.get("acceptance_claims") or [])
    if acceptance_claims:
        metadata["acceptance_claims"] = acceptance_claims
    # v6.60.0: adapter-declared answer protocol ("" | "final_answer_line") — flows into
    # the task contract (and to subagents via the normal contract inheritance).
    answer_protocol = normalize_answer_protocol(body.get("answer_protocol") or raw_metadata.get("answer_protocol"))
    if answer_protocol:
        metadata["answer_protocol"] = answer_protocol
    service_teardown = str(body.get("service_teardown") or raw_metadata.get("service_teardown") or "").strip().lower()
    if service_teardown:
        if service_teardown not in {"stop", "keep"}:
            return allowed_resources, resource_policy, disabled_tools, acceptance_claims, "service_teardown must be 'stop' or 'keep'"
        metadata["service_teardown"] = service_teardown
    return allowed_resources, resource_policy, disabled_tools, acceptance_claims, ""


def _admission_rejection_response(
    admitted: Any,
    *,
    drive_root: pathlib.Path,
    task_id: str,
    project_id: str,
    workspace_root: Optional[pathlib.Path],
    child_drive: Optional[pathlib.Path],
    status_code: int = 409,
    detail: str = "Task was not scheduled because its admission fence is closed.",
) -> Optional[JSONResponse]:
    """Terminalize a typed queue refusal so no scheduled phantom remains."""
    if not (isinstance(admitted, dict) and admitted.get("_admission_blocked")):
        return None
    reason_code = str(admitted.get("_admission_blocked") or "admission_fence")
    if reason_code in {"duplicate_task_id", "admission_reservation_lost"}:
        return JSONResponse(
            {
                "error": "Task id is already owned by another admission attempt.",
                "task_id": task_id,
                "status": "rejected",
                "admission": {"reason_code": reason_code},
            },
            status_code=409,
        )
    admission = {
        "reason_code": reason_code,
        "project_id": str(admitted.get("_project_id") or project_id),
        "project_lifecycle": str(admitted.get("_project_lifecycle") or ""),
        "acceptance_fence_token": str(admitted.get("_acceptance_fence_token") or ""),
        "acceptance_fence_status": str(admitted.get("_acceptance_fence_status") or ""),
    }
    write_task_result(
        drive_root,
        task_id,
        STATUS_FAILED,
        reason_code=reason_code,
        admission=admission,
        artifact_status=ARTIFACT_STATUS_FAILED if workspace_root else "",
        result=detail,
        cost_usd=0.0,
    )
    if child_drive is not None:
        from ouroboros.headless import remove_subagent_task_drive

        removed = remove_subagent_task_drive(drive_root, task_id)
        write_task_result(
            drive_root,
            task_id,
            STATUS_FAILED,
            admission_cleanup={"child_drive_removed": bool(removed)},
        )
    try:
        shutil.rmtree(task_artifacts_dir(drive_root, task_id, create=False), ignore_errors=True)
    except Exception:
        log.warning("Failed to clean rejected task artifacts for %s", task_id, exc_info=True)
    return JSONResponse(
        {
            "error": detail,
            "task_id": task_id,
            "status": STATUS_FAILED,
            "admission": admission,
        },
        status_code=status_code,
    )


def _enqueue_api_task_durably(
    task: Dict[str, Any],
    *,
    drive_root: pathlib.Path,
    task_id: str,
    admission_token: str,
    result_fields: Dict[str, Any],
) -> Dict[str, Any]:
    """Atomically enqueue, snapshot, and publish the scheduled task result."""
    from supervisor import queue

    with queue._queue_lock:
        admitted = queue.enqueue_task(task)
        if isinstance(admitted, dict) and admitted.get("_admission_blocked"):
            return admitted
        if queue.persist_queue_snapshot(reason="api_task_create") is not True:
            queue.PENDING[:] = [
                row for row in queue.PENDING
                if not (
                    isinstance(row, dict)
                    and str(row.get("id") or "") == task_id
                    and str(row.get("_admission_owner_token") or "") == admission_token
                )
            ]
            queue.persist_queue_snapshot(reason="api_task_create_rollback")
            return {
                **task,
                "_admission_blocked": "queue_snapshot_persist_failed",
                "_admission_status_code": 503,
            }
        write_task_result(drive_root, task_id, STATUS_SCHEDULED, **result_fields)
        queue.release_task_admission(task_id, admission_token)
        return admitted


def _complete_api_task_admission(
    task: Dict[str, Any],
    *,
    drive_root: pathlib.Path,
    task_id: str,
    admission_token: str,
    project_id: str,
    description: str,
    allowed_resources: Dict[str, Any],
    deadline_at: str,
    workspace_root: Optional[pathlib.Path],
    workspace_mode: str,
    memory_mode: str,
    child_drive: Optional[pathlib.Path],
    artifacts: List[Dict[str, Any]],
    metadata: Dict[str, Any],
) -> JSONResponse:
    """Publish one API admission or roll back only its token-owned queue row."""
    result_fields = {
        "parent_task_id": task.get("parent_task_id"),
        "root_task_id": task.get("root_task_id"),
        "session_id": task.get("session_id"),
        "actor_id": task.get("actor_id"),
        "delegation_role": task.get("delegation_role"),
        "project_id": project_id,
        "description": description,
        "context": task.get("context"),
        "expected_output": task.get("expected_output"),
        "constraints": task.get("constraints"),
        "allowed_resources": allowed_resources,
        "deadline_at": deadline_at,
        "task_contract": task.get("task_contract"),
        "workspace_root": task.get("workspace_root"),
        "workspace_mode": workspace_mode,
        "memory_mode": memory_mode,
        "child_drive_root": str(child_drive or ""),
        "budget_drive_root": str(drive_root) if child_drive is not None else "",
        "artifacts": artifacts,
        "artifact_status": ARTIFACT_STATUS_PENDING if workspace_root else "",
        "metadata": metadata,
        "result": "Task accepted and durably scheduled.",
    }
    try:
        admitted = _enqueue_api_task_durably(
            task,
            drive_root=drive_root,
            task_id=task_id,
            admission_token=admission_token,
            result_fields=result_fields,
        )
        snapshot_failed = (
            str(admitted.get("_admission_blocked") or "")
            == "queue_snapshot_persist_failed"
        )
        rejection = _admission_rejection_response(
            admitted,
            drive_root=drive_root,
            task_id=task_id,
            project_id=project_id,
            workspace_root=workspace_root,
            child_drive=child_drive,
            status_code=503 if snapshot_failed else 409,
            detail=(
                "Task was not scheduled because its durable queue snapshot could not be written."
                if snapshot_failed
                else "Task was not scheduled because its admission fence is closed."
            ),
        )
        if rejection is not None:
            return rejection
    except Exception as exc:
        try:
            from supervisor import queue as supervisor_queue

            with supervisor_queue._queue_lock:
                supervisor_queue.PENDING[:] = [
                    row for row in supervisor_queue.PENDING
                    if not (
                        isinstance(row, dict)
                        and str(row.get("id") or "") == task_id
                        and str(row.get("_admission_owner_token") or "")
                        == admission_token
                    )
                ]
            supervisor_queue.persist_queue_snapshot(
                reason="api_task_create_failed_rollback"
            )
        except Exception:
            log.warning(
                "Failed to roll back API task %s after admission error",
                task_id,
                exc_info=True,
            )
        write_task_result(
            drive_root,
            task_id,
            "failed",
            **{
                **result_fields,
                "artifact_status": ARTIFACT_STATUS_FAILED if workspace_root else "",
                "result": f"Failed to enqueue task: {exc}",
            },
        )
        _cleanup_api_admission_attempt(
            drive_root, task_id, admission_token, child_drive
        )
        return json_exception(exc, 503)
    return JSONResponse({"ok": True, "task_id": task_id, "status": STATUS_SCHEDULED})


async def api_tasks_create(request: Request) -> JSONResponse:
    """POST /api/tasks — enqueue a managed headless task."""

    body = await request_json_or(request, {})
    if not isinstance(body, dict):
        return json_error("request body must be a JSON object", 400)
    description = str(body.get("description") or "").strip()
    if not description:
        return json_error("description is required", 400)

    ready_error = _supervisor_ready_error(request)
    if ready_error:
        return ready_error

    drive_root = request_drive_root(request)
    repo_dir = request_repo_dir(request)
    try:
        task_id = validate_task_id(body.get("task_id") or uuid.uuid4().hex[:16])
    except ValueError as exc:
        return json_error(str(exc), 400)
    if load_task_result(drive_root, task_id):
        return json_error(f"task_id already exists: {task_id}", 409)
    if (drive_root / HEADLESS_TASKS_DIR / task_id).exists() or (drive_root / ARTIFACTS_DIR / task_id).exists():
        return json_error(f"task_id already has headless state: {task_id}", 409)
    try:
        workspace_root = _resolve_workspace_root(
            body.get("workspace_root"),
            system_repo_dir=repo_dir,
            drive_root=drive_root,
        )
    except ValueError as exc:
        return json_error(str(exc), 400)
    workspace_mode = str(body.get("workspace_mode") or ("external" if workspace_root else "")).strip()
    memory_mode = str(body.get("memory_mode") or ("forked" if workspace_root else "shared")).strip().lower()
    if memory_mode not in {"forked", "empty", "shared"}:
        return json_error("memory_mode must be one of forked, empty, shared", 400)
    if workspace_root and memory_mode == "shared":
        return json_error("memory_mode=shared is not allowed for external workspaces; use forked or empty", 400)
    raw_project_id = str(body.get("project_id") or "")
    if raw_project_id:
        from ouroboros.project_facts import explicit_project_id_ok

        # Validate the UNSTRIPPED value so leading/trailing whitespace (which would
        # collapse two inputs into one store) is rejected, not silently normalized.
        if not explicit_project_id_ok(raw_project_id):
            # Fail closed: an explicit project_id must already be filesystem-clean.
            # Reject (rather than silently normalize/empty -> canonical), so two
            # inputs never collapse to one store and isolation is never defeated.
            return json_error(
                "project_id must be filesystem-safe (alphanumeric/_/-/., no spaces or slashes)", 400)
    from ouroboros.project_facts import resolve_project_id as _resolve_pid

    _task_project_id = _resolve_pid({"project_id": raw_project_id, "workspace_root": str(workspace_root or "")})
    # D5 (Option A): keep the RECORDED memory_mode exactly as requested — shared/forked/
    # empty semantics are unchanged. Isolation for a project-scoped `shared` task comes
    # from MATERIALIZING an isolated child drive (data-root isolation), NOT from mutating
    # the recorded mode. The worker uses task['drive_root'] (the child), and a pure
    # --project-id task never shows the memory_mode line, so the recorded mode stays
    # purely informational while post-task writes still land on the isolated child.
    effective_drive_mode = "forked" if (_task_project_id and memory_mode == "shared") else memory_mode
    task_type = str(body.get("type") or "task")
    if task_type in {"evolution", "review", "deep_self_review"}:
        return json_error(
            f"task type {task_type!r} is internal-only and cannot be created via the task API "
            "(use /evolve or /review); evolution additionally requires advanced/pro runtime mode",
            400,
        )
    if workspace_root and task_type != "task":
        return json_error("external workspace tasks must use type='task'", 400)
    try:
        chat_id = int(body.get("chat_id") if body.get("chat_id") is not None else 0)
        depth = int(body.get("depth") or 0)
    except (TypeError, ValueError):
        return json_error("chat_id and depth must be integers", 400)

    raw_metadata = dict(body.get("metadata") or {}) if isinstance(body.get("metadata"), dict) else {}
    if _external_subagent_label(body, raw_metadata):
        return json_error("delegation_role=subagent is only allowed through the internal schedule_subagent tool", 400)
    if str(body.get("parent_task_id") or "").strip() or str(body.get("root_task_id") or "").strip():
        return json_error("parent_task_id and root_task_id are internal lineage fields; external tasks must start as roots", 400)
    if "project_id" in raw_metadata:
        # project_id is a top-level field; silently dropping it from metadata would
        # let a caller believe isolation is active while the task runs unscoped.
        return json_error("project_id must be a top-level field, not metadata", 400)
    metadata = {str(k): v for k, v in raw_metadata.items() if str(k) not in _RESERVED_METADATA_KEYS}
    allowed_resources, resource_policy, disabled_tools, acceptance_claims, policy_error = (
        _fold_contract_policies(body, raw_metadata, metadata)
    )
    if policy_error:
        return json_error(policy_error, 400)
    if "executor_ref" in raw_metadata or "workspace_executor" in raw_metadata:
        return json_error("metadata.executor_ref/workspace_executor is reserved; pass executor_ref as a top-level task field", 400)
    if "executor_ref" in body:
        raw_executor_ref = body.get("executor_ref")
        if not isinstance(raw_executor_ref, dict) or not raw_executor_ref:
            return json_error("executor_ref must be a JSON object", 400)
        if workspace_root is None:
            return json_error("executor_ref requires an external workspace_root", 400)
        try:
            normalized_executor = normalize_executor_ref(raw_executor_ref)
        except ValueError as exc:
            return json_error(str(exc), 400)
        if normalized_executor is not None:
            for mapping in normalized_executor.mappings:
                for protected_root, label in ((repo_dir, "Ouroboros system repo"), (drive_root, "Ouroboros data drive")):
                    if paths_overlap_casefold(mapping.host_path, protected_root):
                        return json_error(f"executor_ref mapping must not overlap the {label}", 400)
            if not any(path_is_relative_to(workspace_root, mapping.host_path) for mapping in normalized_executor.mappings):
                return json_error("executor_ref mappings must cover workspace_root", 400)
            metadata["executor_ref"] = {
                "type": normalized_executor.kind,
                "id": normalized_executor.executor_id,
                "network": normalized_executor.network,
                "workspace_host_path": str(normalized_executor.mappings[0].host_path),
                "workspace_backend_path": normalized_executor.mappings[0].backend_path,
                "container_name": normalized_executor.container_name,
                "path_mappings": [
                    {"host_path": str(mapping.host_path), "backend_path": mapping.backend_path}
                    for mapping in normalized_executor.mappings
                ],
            }
    try:
        deadline_at = _normalize_deadline_at(body.get("deadline_at") or raw_metadata.get("deadline_at") or "")
    except ValueError as exc:
        return json_error(str(exc), 400)
    timeout_sec = 0.0
    try:
        timeout_sec = float(body.get("timeout_sec") or body.get("timeout") or 0)
    except (TypeError, ValueError):
        timeout_sec = 0.0
    if not deadline_at and timeout_sec > 0:
        deadline_at = datetime.fromtimestamp(time.time() + timeout_sec, timezone.utc).isoformat().replace("+00:00", "Z")
    if deadline_at:
        metadata["deadline_at"] = deadline_at
    admission_token = uuid.uuid4().hex
    from supervisor.queue import reserve_task_admission

    reservation = reserve_task_admission(
        task_id,
        admission_token,
        require_worker_pool=True,
        drive_root=drive_root,
    )
    if reservation.get("status") != "reserved":
        reason = str(reservation.get("reason") or "admission_reservation_failed")
        status_code = 503 if reason.startswith("worker_pool_") else 409
        return json_error(
            f"task admission refused: {reason}",
            status_code,
            task_id=task_id,
            reason_code=reason,
            worker_pool_disabled_reason=str(
                reservation.get("worker_pool_disabled_reason") or ""
            ),
        )
    try:
        child_drive = prepare_task_drive(
            drive_root, task_id, effective_drive_mode, project_id=_task_project_id
        )
    except Exception as exc:
        _cleanup_api_admission_attempt(drive_root, task_id, admission_token)
        return json_exception(exc, 503)
    # v6.52.0 (P1): stage attachments into the SAME drive the task will read from at
    # runtime — the child drive when forked/empty, else the shared drive (matches the
    # task['drive_root'] set at the end of this handler). The returned manifest renders
    # READY read_file(root='artifact_store', ...) lines and feeds native image blocks.
    from ouroboros.artifacts import stage_task_attachments

    effective_drive = child_drive or drive_root
    try:
        attachment_manifest = stage_task_attachments(
            effective_drive, task_id, _normalize_attachments(body.get("attachments"))
        )
    except Exception as exc:
        _cleanup_api_admission_attempt(
            drive_root, task_id, admission_token, child_drive
        )
        return json_exception(exc, 503)
    attachment_images = [m for m in attachment_manifest if m.get("is_image")]
    metadata.setdefault("session_id", str(body.get("session_id") or uuid.uuid4().hex))
    metadata.setdefault("actor_id", str(body.get("actor_id") or "cli"))
    metadata.setdefault("source", str(body.get("source") or "api_task"))
    metadata.setdefault("delegation_role", "root")
    parent_task_id = None
    root_task_id = task_id
    metadata.setdefault("task_id", task_id)
    metadata.setdefault("parent_task_id", parent_task_id or "")
    metadata.setdefault("root_task_id", root_task_id)
    artifacts: List[Dict[str, Any]] = []
    workspace_preflight_summary: Dict[str, Any] = {}
    if workspace_root:
        metadata["workspace_root"] = str(workspace_root)
        try:
            preflight = collect_workspace_preflight(workspace_root)
            workspace_preflight_summary = summarize_workspace_preflight(preflight)
            metadata["workspace_preflight"] = workspace_preflight_summary
            artifacts.append(write_workspace_preflight_artifact(drive_root, task_id, preflight))
        except Exception as exc:
            workspace_preflight_summary = {
                "schema_version": 1,
                "workspace_root": str(workspace_root),
                "error": f"{type(exc).__name__}: {exc}",
            }
            metadata["workspace_preflight"] = workspace_preflight_summary

    try:
        task_text = _compose_task_text(
            description,
            workspace_root=workspace_root,
            workspace_mode=workspace_mode,
            memory_mode=memory_mode,
            workspace_preflight=workspace_preflight_summary,
            attachments=attachment_manifest,
        )
    except Exception as exc:
        _cleanup_api_admission_attempt(
            drive_root, task_id, admission_token, child_drive
        )
        return json_exception(exc, 503)
    task = {
        "id": task_id,
        "type": task_type,
        "chat_id": chat_id,
        "text": task_text,
        "description": description,
        "context": str(body.get("context") or ""),
        "expected_output": str(body.get("expected_output") or ""),
        "constraints": str(body.get("constraints") or ""),
        "context_requires_self_body_docs": normalize_bool(body.get("context_requires_self_body_docs")),
        "allowed_resources": allowed_resources,
        "resource_policy": resource_policy,
        "disabled_tools": disabled_tools,
        "acceptance_claims": acceptance_claims,
        "deadline_at": deadline_at,
        "depth": depth,
        "parent_task_id": parent_task_id,
        "root_task_id": root_task_id,
        "session_id": metadata["session_id"],
        "actor_id": metadata["actor_id"],
        "delegation_role": metadata["delegation_role"],
        "workspace_root": str(workspace_root) if workspace_root else "",
        "workspace_mode": workspace_mode,
        "memory_mode": memory_mode,
        "project_id": _task_project_id,
        "metadata": metadata,
        # v6.52.0 (P1): the STAGED manifest (root/relpath/mime/is_image), not raw
        # host paths — relpaths resolve against task['drive_root'] at read time.
        "attachments": attachment_manifest,
        "attachment_images": attachment_images,
        # v6.52.0 (P1): record the effective drive (child when forked/empty, else the shared
        # drive) so build_user_content can resolve staged attachment IMAGES for EVERY task
        # shape — not just child-drive tasks. The child-drive block below re-affirms it.
        "drive_root": str(effective_drive),
        "_require_unique_task_id": True,
        "_require_worker_pool": True,
        "_admission_token": admission_token,
    }
    try:
        task = attach_task_contract(task)
    except Exception as exc:
        _cleanup_api_admission_attempt(
            drive_root, task_id, admission_token, child_drive
        )
        return json_exception(exc, 503)
    if child_drive is not None:
        task["drive_root"] = str(child_drive)
        task["child_drive_root"] = str(child_drive)
        task["budget_drive_root"] = str(drive_root)
        metadata["child_drive_root"] = str(child_drive)
        metadata["budget_drive_root"] = str(drive_root)
    return _complete_api_task_admission(
        task,
        drive_root=drive_root,
        task_id=task_id,
        admission_token=admission_token,
        project_id=_task_project_id,
        description=description,
        allowed_resources=allowed_resources,
        deadline_at=deadline_at,
        workspace_root=workspace_root,
        workspace_mode=workspace_mode,
        memory_mode=memory_mode,
        child_drive=child_drive,
        artifacts=artifacts,
        metadata=metadata,
    )


async def api_tasks_list(request: Request) -> JSONResponse:
    statuses = [
        item.strip()
        for item in str(request.query_params.get("status") or "").split(",")
        if item.strip()
    ]
    limit = max(1, min(coerce_int(request.query_params.get("limit"), 50), 500))
    drive_root = request_drive_root(request)
    wanted = {status.lower() for status in statuses}
    # List view is a status/cost projection: never materialize artifacts (no child
    # rebase copies, no artifact-dir scans, no disposition/sha claims) on a GET list.
    rows = [
        public_task_result(effective_task_result(drive_root, row, materialize_artifacts=False))
        for row in list_task_results(drive_root)
    ]
    if wanted:
        rows = [row for row in rows if str(row.get("status") or "").lower() in wanted]
    rows.sort(key=lambda item: str(item.get("ts") or ""), reverse=True)
    return JSONResponse({"tasks": rows[:limit], "queue": _queue_snapshot(drive_root)})


def _task_cost_breakdown_view(drive_root: pathlib.Path, result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Read-side "where did the money go" projection for a ROOT task's detail.

    Computed from the physical-attempt ledger AT READ TIME and never persisted
    into the task result — the ledger stays the single monetary authority (P7);
    the stored envelope keeps only its existing own/subtree projections.
    ``children_usd`` is subtree − own − unattributed (the subtraction every
    reader had to do by hand); ``delegated`` is a filter over the execution
    axis (subscription sessions), not a third sum. Unavailable accounting
    returns None — the field is simply absent, never a confident $0. That
    covers BOTH an unreadable ledger and a readable one that holds no
    attributable row for this subtree (empty or legacy-only): ``_summary()``
    always returns a float for ``accounted_usd``, so "no accounting happened"
    is decided on the ROW COUNTS, never on the dollar sum being 0.0."""
    task_id = str(result.get("task_id") or "")
    root_id = str(result.get("root_task_id") or "") or task_id
    # Subtree math is ledger-attributable only at the root (child rows carry
    # the ROOT's id, not every ancestor's); non-root details omit the view.
    if not task_id or root_id != task_id:
        return None
    try:
        from ouroboros.usage_accounting import usage_breakdown

        breakdown = usage_breakdown(drive_root, root_task_id=root_id)
    except Exception:
        log.debug("cost breakdown view unavailable for %s", task_id, exc_info=True)
        return None
    subtree = breakdown.get("accounted_usd")
    counts = breakdown.get("attempt_counts")
    counts = counts if isinstance(counts, dict) else {}
    # `metadata_only` is a count of AMBIGUOUS legacy calls carrying no money, so
    # it can never make a $0 measured; only priced attempt rows or subscription
    # sessions can. With neither, nothing was accounted for this subtree and the
    # view is ABSENT — the empty/legacy-ledger case that a `0.0 == measured zero`
    # reading would have published as `own 0 / children 0 / cost_final true`.
    priced_rows = sum(int(value or 0) for key, value in counts.items() if key != "metadata_only")
    sessions = int(breakdown.get("subscription_sessions") or 0)
    if subtree is None or (priced_rows <= 0 and sessions <= 0):
        return None
    own_bucket = (breakdown.get("by_task") or {}).get(task_id)
    # No rows attributed to the root itself is a MEASURED zero (all spend was
    # children's), not an unknown — unknowns ride `unknown_unmetered` below.
    own = float(own_bucket.get("accounted_usd") or 0.0) if isinstance(own_bucket, dict) else 0.0
    # Money inside this subtree that no task id claims (legacy/blank-task rows)
    # is DISCLOSED on its own axis instead of being silently folded into the
    # children's share: own + children + unattributed == subtree.
    unattributed_bucket = (breakdown.get("unattributed") or {}).get("task")
    unattributed = (
        float(unattributed_bucket.get("accounted_usd") or 0.0)
        if isinstance(unattributed_bucket, dict) else 0.0
    )
    delegated = breakdown.get("delegated") if isinstance(breakdown.get("delegated"), dict) else {}
    return {
        "own_usd": round(own, 6),
        "children_usd": round(max(0.0, float(subtree) - own - unattributed), 6),
        "unattributed_usd": round(unattributed, 6),
        "delegated_disclosed_usd": round(float(delegated.get("settled_usd") or 0.0), 6),
        "subscription_sessions": sessions,
        "unknown_unmetered": breakdown.get("unknown_unmetered"),
        "non_final_rows": breakdown.get("non_final_rows"),
        "cost_final": bool(breakdown.get("cost_final")),
        "authority": "physical_attempt_ledger",
    }


async def api_task_get(request: Request) -> JSONResponse:
    try:
        task_id = validate_task_id(request.path_params.get("task_id"))
    except ValueError as exc:
        return json_error(str(exc), 400)
    drive_root = request_drive_root(request)
    data = load_effective_task_result(drive_root, task_id)
    if not data:
        return json_error("task not found", 404)
    payload = public_task_result(data)
    breakdown_view = _task_cost_breakdown_view(drive_root, data)
    if breakdown_view is not None:
        payload["cost_breakdown"] = breakdown_view
    return JSONResponse(payload)


async def api_task_artifact(request: Request):
    try:
        task_id = validate_task_id(request.path_params.get("task_id"))
    except ValueError as exc:
        return json_error(str(exc), 400)
    name = str(request.path_params.get("name") or "").strip()
    if not name or "/" in name or "\\" in name or name in {".", ".."} or ".." in pathlib.PurePosixPath(name).parts:
        return json_error("artifact name must be a simple filename", 400)
    drive_root = request_drive_root(request)
    result = load_effective_task_result(drive_root, task_id)
    if not result:
        return json_error("task not found", 404)
    artifact = _artifact_by_name(result, name)
    if artifact is None:
        return json_error("artifact not found", 404, task_id=task_id, artifact=name)
    base = task_artifacts_dir(drive_root, task_id).resolve(strict=False)
    path = pathlib.Path(str(artifact.get("path") or "")).resolve(strict=False)
    if path.name != name:
        return json_error("artifact metadata path does not match requested name", 500)
    try:
        path.relative_to(base)
    except ValueError:
        return json_error("artifact path is outside task artifact directory", 500)
    if not path.is_file():
        return json_error("artifact file is missing", 404, task_id=task_id, artifact=name)
    return FileResponse(path)


def _record_cascade_incident(task_id: str, kind: str, detail: str = "") -> None:
    """Durably record a cascade outcome the owner must be able to see.

    The client already holds ``ok:true`` and the card only resolves on a real
    ``task_done``, so a cascade that RAISED or that cancelled NOTHING is a silent
    lie unless it is recorded. Both land on the supervisor's own drive root (the
    same log every other cancel artifact uses) and are pushed to the live owner
    surfaces when a bridge exists.
    """
    row = {"type": kind, "task_id": task_id}
    if detail:
        row["error"] = detail
    try:
        from supervisor import queue as supervisor_queue
        from ouroboros.utils import append_jsonl, utc_now_iso

        append_jsonl(
            pathlib.Path(supervisor_queue.DRIVE_ROOT) / "logs" / "supervisor.jsonl",
            {"ts": utc_now_iso(), **row},
        )
    except Exception:
        log.debug("Failed to persist cascade cancel incident for %s", task_id, exc_info=True)
    try:
        from supervisor.message_bus import try_get_bridge

        bridge = try_get_bridge()
        if bridge is not None:
            from ouroboros.utils import utc_now_iso

            bridge.push_log({"ts": utc_now_iso(), **row})
    except Exception:
        log.debug("Failed to surface cascade cancel incident for %s", task_id, exc_info=True)


def _run_cascade_cancel(task_id: str) -> bool:
    """Subtree cancel for the HTTP cascade path, AWAITED by its caller.

    Returns True when the subtree is settled — cancelled, or already terminal
    (the benign completion-wins race) — and False when the teardown failed or
    refused while the tree is STILL live, which the endpoint reports rather than
    answering ok:true for a cancellation that did not happen. Failures stay
    durable and owner-visible as incidents either way.
    """
    try:
        from supervisor.queue import cancel_task_by_id

        if not cancel_task_by_id(task_id, cascade=True):
            # A cascade that cancelled nothing is only an incident when the subtree
            # is STILL live: the ordinary completion-wins race (the task reached
            # terminal between the pre-check and this call) is the benign case the
            # UI already handles, and reporting it would train the owner to ignore
            # the real "refused to cancel" signal.
            from supervisor.task_lifecycle import task_subtree_is_live

            if task_subtree_is_live(task_id):
                _record_cascade_incident(task_id, "task_cancel_cascade_noop")
                return False
        return True
    except Exception as exc:
        log.warning("Cascade cancel failed for %s", task_id, exc_info=True)
        _record_cascade_incident(task_id, "task_cancel_cascade_error", repr(exc))
        return False


# Sentinel telling "the body did not parse" apart from a legitimate JSON null.
_NO_BODY = object()


async def api_task_cancel(request: Request) -> JSONResponse:
    try:
        task_id = validate_task_id(request.path_params.get("task_id"))
    except ValueError as exc:
        return json_error(str(exc), 400)
    # Optional JSON body {"cascade": true} (v6.82): cancel the task AND its
    # atomically-snapshotted live subtree, answering only once that teardown has
    # finished. An absent/empty body keeps today's single-task behavior
    # byte-identical for headless callers (the CLI posts {}).
    # An ABSENT body keeps the legacy single-task path; a body that is PRESENT but
    # unparseable (or not a JSON object) is a client error. Collapsing the two would
    # answer a malformed cascade request by quietly cancelling only the root and
    # leaving its descendants running.
    raw_body = (await request.body()) or b""
    if raw_body.strip():
        body = await request_json_or(request, _NO_BODY)
        if body is _NO_BODY or not isinstance(body, dict):
            return json_error("request body must be a JSON object", 400, task_id=task_id)
    else:
        body = {}
    # STRICT boolean (DEVELOPMENT.md): a string "false" must never select the
    # destructive subtree path, and a non-boolean value is a client error rather
    # than a silent single-task cancel.
    raw_cascade = body.get("cascade")
    if raw_cascade is not None and not isinstance(raw_cascade, bool):
        return json_error("cascade must be a boolean", 400, task_id=task_id)
    cascade = raw_cascade is True
    if not cascade:
        try:
            from supervisor.queue import (
                CANCEL_CANCELLED, CANCEL_FAILED, cancel_task_custody,
            )

            # The TYPED outcome, not a boolean: a task whose worker refused to die
            # is neither cancelled nor absent, and answering 404 for it would tell
            # the caller the task is gone while it keeps running.
            outcome = await asyncio.to_thread(cancel_task_custody, task_id)
        except Exception as exc:
            return json_exception(exc, 503)
        if outcome == CANCEL_FAILED:
            return json_error(
                "cancellation did not settle; the task is still live",
                503, task_id=task_id,
            )
        if outcome != CANCEL_CANCELLED:
            # LEGACY CONTRACT preserved: the plain path has always answered 404 for
            # an INACTIVE task, and one that already settled on its own is exactly
            # that — the typed outcome must not silently widen the envelope.
            return json_error("task not found or not active", 404, task_id=task_id)
        return JSONResponse({"ok": True, "task_id": task_id})
    # Cascade path: ONE synchronous transaction. The caller is answered only once
    # the subtree is actually torn down, which is what makes the whole
    # split-transaction family (durable pre-acknowledgement latch, partial-latch
    # taxonomy, ownership handed to a background teardown, rollbacks that could
    # withdraw a concurrent cascade's fences) unnecessary rather than merely
    # guarded. The cost is honest and bounded: a large tree makes the caller wait
    # for the worker kills and joins it asked for. Off the event loop; process
    # kills and joins deliberately happen outside the supervisor queue lock. Repeats are
    # idempotent (the per-task cancel finalizes-on-miss) and a fully-cancelled tree
    # is no longer live, so it answers 404 like any other inactive task.
    try:
        from supervisor.task_lifecycle import task_subtree_is_live

        if not await asyncio.to_thread(task_subtree_is_live, task_id):
            return json_error("task not found or not active", 404, task_id=task_id)
        settled = await asyncio.to_thread(_run_cascade_cancel, task_id)
        if not settled:
            # The teardown refused or failed while the subtree is STILL live: an
            # ok:true here would report a cancellation that did not happen.
            return json_error(
                "subtree cancellation did not settle; the tree is still live",
                503, task_id=task_id,
            )
    except Exception as exc:
        return json_exception(exc, 503)
    return JSONResponse({"ok": True, "task_id": task_id, "cascade": True})


async def api_task_resume(request: Request) -> JSONResponse:
    """Resume only a replay-safe task paused before its first model dispatch."""
    try:
        task_id = validate_task_id(request.path_params.get("task_id"))
    except ValueError as exc:
        return json_error(str(exc), 400)
    try:
        from supervisor.queue import resume_budget_paused_task

        result = resume_budget_paused_task(task_id)
    except Exception as exc:
        return json_exception(exc, 503)
    if result.get("ok"):
        return JSONResponse(result)
    error = str(result.get("error") or "resume_refused")
    status = 409 if error in {
        "task_not_budget_paused", "replay_unsafe", "root_budget_fence_missing",
    } else 404
    return json_error(error, status, task_id=task_id, **({"action": result["action"]} if result.get("action") else {}))


async def api_task_events(request: Request) -> StreamingResponse:
    try:
        task_id = validate_task_id(request.path_params.get("task_id"))
    except ValueError as exc:
        message = str(exc)
        async def _bad_id():
            yield _sse({"type": "error", "error": message, "seq": 1}, event_id=1)
        return StreamingResponse(_bad_id(), media_type="text/event-stream", status_code=400)
    cursor = max(0, coerce_int(request.query_params.get("cursor"), 0))
    wait_sec = max(0, min(coerce_int(request.query_params.get("wait"), 30), 120))
    drive_root = request_drive_root(request)
    if not load_task_result(drive_root, task_id):
        async def _missing():
            yield _sse({"type": "error", "error": "task not found", "task_id": task_id, "seq": 1}, event_id=1)
        return StreamingResponse(_missing(), media_type="text/event-stream", status_code=404)

    async def _stream():
        # Initial replay = one full archive-aware merge (identical to a fresh
        # iter_task_events call, so the client's cross-reconnect `cursor` keeps
        # addressing the same positions — the CLI contract, ouroboros/cli.py
        # _watch_task). The follow phase then reads only bytes APPENDED to each
        # discovered log per tick; new rows are emitted incrementally with
        # monotonic in-stream seq only while they all sort strictly after the
        # emitted tail, otherwise one full re-merge resumes emission from the
        # cursor (at-least-once across those boundaries — pre-existing property,
        # disclosed in ARCHITECTURE.md).
        nonlocal cursor
        deadline = time.time() + wait_sec
        follower = _TaskEventFollower(drive_root, task_id)
        emitted_final = False
        tail_key = None
        need_full = True
        while True:
            refreshed = False
            advanced = False
            if need_full:
                rows = await asyncio.to_thread(follower.full_merge)
                pending = [row for row in rows if int(row.get("seq") or 0) > cursor]
                if rows:
                    tail_key = _event_sort_key(rows[-1])
                need_full = False
                refreshed = True  # full_merge reloaded the result projection
            else:
                new_rows, advanced = await asyncio.to_thread(follower.poll)
                interleaved = bool(new_rows) and tail_key is not None and _event_sort_key(new_rows[0]) <= tail_key
                if interleaved or follower.filter_grew:
                    # New rows interleave with already-emitted history, or a new
                    # child id joined the lineage filter (rows matching only via
                    # subagent_task_id may sit in already-consumed bytes): ONE
                    # full re-merge, resume emission from the cursor.
                    rows = await asyncio.to_thread(follower.full_merge)
                    pending = [row for row in rows if int(row.get("seq") or 0) > cursor]
                    if rows:
                        tail_key = _event_sort_key(rows[-1])
                    refreshed = True
                else:
                    pending = []
                    for row in new_rows:
                        row["seq"] = cursor + len(pending) + 1
                        pending.append(row)
                    if pending:
                        tail_key = _event_sort_key(pending[-1])
            for event in pending:
                cursor = int(event.get("seq") or cursor)
                if str(event.get("type") or "") == "task_result":
                    data = event.get("data") if isinstance(event.get("data"), dict) else {}
                    if str(data.get("status") or "").lower() in FINAL_STATUSES:
                        if not emitted_final:
                            # ONE materializing read at terminal emission (P2
                            # review, fix 5): the merged rows are status/cost
                            # projections, but watching a task to completion
                            # must still deliver the artifact-bearing terminal
                            # payload (and run its read-repair rebase) exactly
                            # once per stream.
                            full = await asyncio.to_thread(
                                load_effective_task_result, drive_root, task_id
                            )
                            if full:
                                event["data"] = public_task_result(full)
                        emitted_final = True
                yield _sse(event, event_id=cursor)
            # Recompute the terminal projection only when something moved: log
            # offsets advanced, new roots joined, or the queue snapshot changed.
            if not refreshed and (advanced or follower.queue_snapshot_changed()):
                suppress_before = follower.suppress_task_done
                await asyncio.to_thread(follower.refresh_result)
                if follower.suppress_task_done != suppress_before:
                    # The task_done suppression window opened/closed: which rows
                    # exist in the merge changed, so re-merge before continuing.
                    need_full = True
                    continue
            if follower.result_is_final():
                if not emitted_final:
                    result = public_task_result(load_effective_task_result(drive_root, task_id))
                    if result:
                        final_event = {
                            "source": "task_result",
                            "line": 0,
                            "ts": str(result.get("ts") or ""),
                            "type": "task_result",
                            "task_id": task_id,
                            "data": result,
                            "seq": cursor + 1,
                        }
                        cursor = int(final_event["seq"])
                        yield _sse(final_event, event_id=cursor)
                break
            if time.time() >= deadline:
                yield ": heartbeat\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(_stream(), media_type="text/event-stream")


# Live logs that the supervisor rotates into archive/<prefix>_<ts>.jsonl
# (supervisor/state.rotate_jsonl_log_if_needed); the other sources never rotate.
_ROTATED_LOG_PREFIXES = {"progress": "progress", "chat": "chat"}


def _event_sort_key(item: Dict[str, Any]) -> tuple:
    return (str(item.get("ts") or ""), str(item.get("source") or ""), int(item.get("line") or 0))


def _compact_ts_stamp(ts: str) -> str:
    """ISO-ish timestamp -> archive-stamp form (YYYYMMDDTHHMMSS), or "" if unusable."""
    stamp = ts.strip().replace("-", "").replace(":", "")
    return stamp[:15] if len(stamp) >= 15 and stamp[8:9] == "T" else ""


def _archive_stamp_predates(name: str, prefix: str, floor: str) -> bool:
    """True when ``<prefix>_<stamp>[_N].jsonl`` was rotated strictly before ``floor``."""
    stamp = name[len(prefix) + 1:].split(".", 1)[0].split("_", 1)[0]
    return len(stamp) == 15 and stamp < floor


def _read_live_jsonl_entries(path: pathlib.Path, offset: int) -> tuple[List[Dict[str, Any]], int, Optional[int]]:
    """Parse COMPLETE JSONL lines from byte ``offset``; returns (entries, new_offset, ino).

    A torn final line (a concurrent append caught mid-write) is left unconsumed so
    the next read starts exactly at its first byte — unlike a naive full read, no
    row is ever half-parsed and then skipped forever."""
    try:
        with path.open("rb") as handle:
            stat = os.fstat(handle.fileno())
            if offset:
                handle.seek(offset)
            data = handle.read()
    except OSError:
        return [], offset, None
    cut = data.rfind(b"\n")
    if cut < 0:
        return [], offset, stat.st_ino
    chunk = data[: cut + 1]
    entries: List[Dict[str, Any]] = []
    for raw in chunk.splitlines():
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries, offset + len(chunk), stat.st_ino


class _TaskEventFollower:
    """Byte-offset follow state for one ``/api/tasks/{id}/events`` stream.

    ``full_merge`` performs the complete archive-aware scan (also serving as the
    public ``iter_task_events``) while rebuilding per-(root, source) chain state:
    consumed archive names, the live-file byte offset/inode, and the running
    parsed-line count that keeps (ts, source, line) ordering identical between
    incremental reads and a re-merge. ``poll`` then reads only appended bytes,
    re-discovers late-spawned child roots every tick (their logs join at offset
    0), and heals a mid-stream rotation by reading the newest archive's suffix
    beyond the old offset before continuing on the new live file at offset 0.
    All effective-result reads here are status/cost projections
    (``materialize_artifacts=False``) — the SSE loop must never copy artifacts
    or make disposition/sha claims on a 0.5s tick. The single sanctioned
    exception lives in the stream's emit loop, not here: the terminal
    ``task_result`` emission performs one materializing read (see
    ``api_task_events``)."""

    def __init__(self, drive_root: pathlib.Path, task_id: str) -> None:
        self.drive_root = pathlib.Path(drive_root)
        self.task_id = task_id
        self.task_filter_ids = {task_id}
        self.roots: List[pathlib.Path] = []
        self.logs: Dict[tuple, Dict[str, Any]] = {}
        self.result: Dict[str, Any] = {}
        self.suppress_task_done = False
        self.filter_grew = False
        self._queue_snapshot_mtime: Any = None
        # Archive floor (P2 review, fix 4): the RAW result's ts is the first
        # write's timestamp (creation/admission — no production writer passes an
        # explicit ts), so an archive whose rotation stamp predates it cannot
        # contain this task's rows. Empty floor = no bound (fail open).
        raw = load_task_result(self.drive_root, task_id) or {}
        self._created_floor = _compact_ts_stamp(str(raw.get("created_at") or raw.get("ts") or ""))

    def refresh_result(self) -> None:
        self.result = load_effective_task_result(
            self.drive_root, self.task_id, materialize_artifacts=False
        )
        self.suppress_task_done = _is_workspace_result(self.result) and str(
            self.result.get("artifact_status") or ""
        ).lower() in {ARTIFACT_STATUS_PENDING, ARTIFACT_STATUS_FINALIZING}

    def result_is_final(self) -> bool:
        return str(self.result.get("status") or "").lower() in FINAL_STATUSES

    def queue_snapshot_changed(self) -> bool:
        try:
            mtime = (self.drive_root / "state" / "queue_snapshot.json").stat().st_mtime_ns
        except OSError:
            mtime = None
        changed = mtime != self._queue_snapshot_mtime
        self._queue_snapshot_mtime = mtime
        return changed

    def _discover_roots(self) -> bool:
        """Refresh roots + lineage filter ids; True when something new appeared."""
        changed = False
        candidates = [self.drive_root]
        child = str(
            self.result.get("child_drive_root")
            or self.result.get("headless_child_drive_root")
            or ""
        ).strip()
        if child:
            candidates.append(pathlib.Path(child))
        for child_row in find_child_tasks(
            self.drive_root,
            parent_task_id=self.task_id,
            root_task_id=self.task_id,
            materialize_artifacts=False,
        ):
            child_id = str(child_row.get("task_id") or child_row.get("id") or "").strip()
            if child_id and child_id not in self.task_filter_ids:
                self.task_filter_ids.add(child_id)
                changed = True
                # A new FILTER ID over already-consumed bytes is lossy: rows
                # matching only via subagent_task_id were filtered out when
                # those bytes were read, so only a full re-merge recovers them
                # (new ROOTS are fine — their logs join at offset 0). The
                # stream checks this flag after every poll; full_merge resets it.
                self.filter_grew = True
            child_root = str(
                child_row.get("child_drive_root")
                or child_row.get("headless_child_drive_root")
                or ""
            ).strip()
            if child_root:
                candidates.append(pathlib.Path(child_root))
        for path in candidates:
            if path not in self.roots:
                self.roots.append(path)
                changed = True
        return changed

    def _log_state(self, root: pathlib.Path, source: str) -> Dict[str, Any]:
        key = (str(root), source)
        state = self.logs.get(key)
        if state is None:
            state = {"archives": [], "offset": 0, "ino": None, "lines": 0}
            self.logs[key] = state
        return state

    def _read_chain_delta(self, root: pathlib.Path, source: str, parts: tuple) -> List[Dict[str, Any]]:
        """Entries appended to one (root, source) chain since the recorded state.

        Fresh state (a late-discovered log) naturally degenerates to reading the
        whole chain: every archive is "new" and the live offset is 0."""
        state = self._log_state(root, source)
        live = root.joinpath(*parts)
        prefix = _ROTATED_LOG_PREFIXES.get(source)
        entries: List[Dict[str, Any]] = []
        if prefix:
            try:
                archive_paths = sorted(
                    (root / "archive").glob(f"{prefix}_*.jsonl"), key=lambda p: p.name
                )
            except OSError:
                archive_paths = []
            if self._created_floor:
                # An archive rotated before the watched task existed cannot
                # contain its rows (an archive's rows predate its rotation
                # stamp), so skip it: bounds the per-tick/merge archive work to
                # the task's lifetime instead of O(system age). Removes no
                # matching rows and touches no cursor positions by construction.
                archive_paths = [
                    path for path in archive_paths
                    if not _archive_stamp_predates(path.name, prefix, self._created_floor)
                ]
            known = set(state["archives"])
            new_archives = [p for p in archive_paths if p.name not in known]
            if new_archives:
                # Rotation: the previous live content now lives in the newest
                # archive(s). Read the first new archive beyond the consumed live
                # offset (or the offset stashed when the inode flip was observed
                # before the archive became visible), the rest fully, then
                # continue on the new live file from 0.
                had_stash = "rotated_offset" in state
                start = state.pop("rotated_offset", state["offset"])
                for index, path in enumerate(new_archives):
                    got, _, _ = _read_live_jsonl_entries(path, start if index == 0 else 0)
                    entries.extend(got)
                    state["archives"].append(path.name)
                if not (had_stash and len(new_archives) == 1):
                    # No stash: offset/ino still describe the OLD live file (now
                    # the archive), so restart on the new live file from 0.
                    # With a consumed stash and exactly one new archive, the
                    # recorded offset/ino already track the NEW live file the
                    # follower partially consumed on the stash tick — resetting
                    # to 0 would re-emit those rows (P2 review, fix 2).
                    state["offset"] = 0
                    state["ino"] = None
        try:
            live_stat = live.stat()
        except OSError:
            return entries
        if (state["ino"] is not None and live_stat.st_ino != state["ino"]) or (
            live_stat.st_size < state["offset"]
        ):
            # Live file replaced/shrank but its archive is not visible yet: stash
            # the consumed offset for the archive suffix and restart on the new
            # live file. (Any resulting duplicate rows sort at-or-before the
            # emitted tail, which forces a full re-merge — the honest fallback.)
            if prefix and "rotated_offset" not in state:
                state["rotated_offset"] = state["offset"]
            state["offset"] = 0
        got, new_offset, ino = _read_live_jsonl_entries(live, state["offset"])
        state["offset"], state["ino"] = new_offset, ino
        entries.extend(got)
        return entries

    def _entries_to_rows(
        self, root: pathlib.Path, source: str, entries: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        state = self._log_state(root, source)
        rows: List[Dict[str, Any]] = []
        for entry in entries:
            state["lines"] += 1
            entry_task = str(entry.get("task_id") or "")
            entry_subagent = str(entry.get("subagent_task_id") or "")
            entry_parent = str(entry.get("parent_task_id") or "")
            entry_root = str(entry.get("root_task_id") or "")
            if (
                entry_task not in self.task_filter_ids
                and entry_subagent not in self.task_filter_ids
                and entry_parent != self.task_id
                and entry_root != self.task_id
            ):
                continue
            event = _event_from_log_entry(source, state["lines"], entry, root)
            if self.suppress_task_done and event.get("type") == "task_done":
                continue
            rows.append(event)
        return rows

    def full_merge(self) -> List[Dict[str, Any]]:
        """Full archive-aware merge; rebuilds ALL follow state from scratch."""
        self.logs = {}
        self.roots = []
        self.task_filter_ids = {self.task_id}
        self.refresh_result()
        self.queue_snapshot_changed()
        self._discover_roots()
        rows: List[Dict[str, Any]] = []
        for root in self.roots:
            for source, parts in _LOG_SOURCES:
                entries = self._read_chain_delta(root, source, parts)
                rows.extend(self._entries_to_rows(root, source, entries))
        if self.result:
            rows.append({
                "source": "task_result",
                "line": 0,
                "ts": str(self.result.get("ts") or ""),
                "type": "task_result",
                "task_id": self.task_id,
                "data": public_task_result(self.result),
            })
        rows.sort(key=_event_sort_key)
        for idx, row in enumerate(rows, 1):
            row["seq"] = idx
        self.filter_grew = False  # the merge above read every consumed byte anew
        return rows

    def poll(self) -> tuple[List[Dict[str, Any]], bool]:
        """One follow tick: (new rows sorted by (ts, source, line), advanced?)."""
        advanced = self._discover_roots()
        rows: List[Dict[str, Any]] = []
        for root in list(self.roots):
            for source, parts in _LOG_SOURCES:
                entries = self._read_chain_delta(root, source, parts)
                if entries:
                    advanced = True
                    rows.extend(self._entries_to_rows(root, source, entries))
        rows.sort(key=_event_sort_key)
        return rows, advanced


def iter_task_events(drive_root: pathlib.Path, task_id: str) -> List[Dict[str, Any]]:
    """Return synthesized replayable events for a task from existing logs.

    Archive-aware (v6.90.x P2): each rotated log's ``archive/<prefix>_*.jsonl``
    chain is read oldest-first before the live file, so a rotation never erases
    replay history. Also the SSE initial-replay/re-merge path."""
    return _TaskEventFollower(drive_root, task_id).full_merge()


def _event_from_log_entry(source: str, line_no: int, entry: Dict[str, Any], root: pathlib.Path) -> Dict[str, Any]:
    event_type = str(entry.get("type") or source)
    if source == "progress":
        event_type = "progress"
    elif source == "chat":
        event_type = "message"
    elif source == "tools":
        event_type = "tool_call"
    data = dict(entry)
    data = public_task_result(
        data,
        include_outcome_axes=any(key in data for key in ("status", "outcome_axes", "result_status", "loop_outcome")),
    )
    return {
        "source": source,
        "line": line_no,
        "ts": str(entry.get("ts") or ""),
        "type": event_type,
        "task_id": str(entry.get("task_id") or ""),
        "root": str(root),
        "data": data,
    }


def _sse(event: Dict[str, Any], *, event_id: int) -> str:
    payload = json.dumps(event, ensure_ascii=False)
    return f"id: {event_id}\nevent: task_event\ndata: {payload}\n\n"


def _resolve_workspace_root(
    value: Any,
    *,
    system_repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
) -> Optional[pathlib.Path]:
    """Delegates to the admission SSOT (v6.58.0): the gateway and the promote path
    validate a workspace root through ONE function (workspace_admission), so the two
    surfaces can never drift. WorkspaceRootError subclasses ValueError, so existing
    `except ValueError` call sites keep working unchanged."""
    from ouroboros.workspace_admission import validate_workspace_root

    return validate_workspace_root(value, system_repo_dir=system_repo_dir, drive_root=drive_root)


def _normalize_attachments(value: Any) -> List[Dict[str, str]]:
    if not value:
        return []
    if not isinstance(value, list):
        return []
    out: List[Dict[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            path = str(item.get("path") or "").strip()
            label = str(item.get("label") or item.get("display_name") or pathlib.Path(path).name).strip()
        else:
            path = str(item or "").strip()
            label = pathlib.Path(path).name
        if path:
            out.append({"path": path, "label": label})
    return out


def _compose_task_text(
    description: str,
    *,
    workspace_root: Optional[pathlib.Path],
    workspace_mode: str,
    memory_mode: str,
    workspace_preflight: Dict[str, Any],
    attachments: Any,
) -> str:
    parts = [description]
    if workspace_root is not None:
        from ouroboros.workspace_admission import compose_workspace_block

        # SSOT block (v6.58.0): the same [HEADLESS_WORKSPACE] guidance the promote
        # path embeds, so the two admission surfaces render identical context.
        workspace_lines = compose_workspace_block(
            workspace_root=workspace_root,
            workspace_mode=workspace_mode,
            memory_mode=memory_mode,
            workspace_preflight=workspace_preflight,
        )
        if "[HEADLESS_WORKSPACE]" in description and "[END_HEADLESS_WORKSPACE]" in description:
            marker = "[END_HEADLESS_WORKSPACE]"
            idx = description.rfind(marker)
            parts = [description[:idx].rstrip(), "\n", workspace_lines, description[idx:]]
        else:
            parts.append(f"\n\n[HEADLESS_WORKSPACE]\n{workspace_lines}[END_HEADLESS_WORKSPACE]")
    rendered = _render_attachment_lines(attachments)
    if rendered:
        parts.append(f"\n\n[ATTACHMENTS]\n{rendered}\n[END_ATTACHMENTS]")
    return "".join(parts)


def _render_attachment_lines(attachments: Any) -> str:
    """Render READY attachment lines from a staged manifest.

    v6.52.0 (P1): each line is a ready-to-use read_file call against the canonical
    artifact_store root — NEVER a bare absolute host path. ``attachments`` is the
    manifest returned by ``stage_task_attachments`` (entries with root/relpath/mime/
    is_image)."""
    if not isinstance(attachments, list):
        return ""
    lines: List[str] = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        relpath = str(item.get("relpath") or "").strip()
        root = str(item.get("root") or "artifact_store").strip() or "artifact_store"
        label = str(item.get("label") or pathlib.Path(relpath).name).strip()
        if not relpath:
            continue
        kind = "image" if item.get("is_image") else (str(item.get("mime") or "").strip() or "file")
        # v6.54.3: also surface the REAL staged path for process tools — scripts
        # (openpyxl, audio, ffmpeg) open files by OS path, and omitting it made
        # models GUESS wrong absolute paths that tripped light-mode path guards.
        # The staged path lives inside this task's own artifact_store, so both
        # forms address the same file.
        abs_path = str(item.get("abs_path") or "").strip()
        script_hint = f" | script/process path: {abs_path}" if abs_path else ""
        lines.append(
            f"- {label} ({kind}): read_file(root='{root}', path='{relpath}'){script_hint}"
        )
    return "\n".join(lines)


def _is_workspace_result(result: Dict[str, Any]) -> bool:
    return bool(str(result.get("workspace_root") or "").strip() or str(result.get("workspace_mode") or "").strip())


def _artifact_by_name(result: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    for artifact in result.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        if str(artifact.get("name") or pathlib.Path(str(artifact.get("path") or "")).name) == name:
            return artifact
    return None


def _queue_snapshot(drive_root: pathlib.Path) -> Dict[str, Any]:
    path = pathlib.Path(drive_root) / "state" / "queue_snapshot.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _supervisor_ready_error(request: Request) -> Optional[JSONResponse]:
    state = getattr(request.app, "state", None)
    ready_event = getattr(state, "supervisor_ready_event", None) if state is not None else None
    if ready_event is not None and not ready_event.is_set():
        return json_error("supervisor is still starting", 503)
    try:
        from supervisor.workers import worker_pool_admission_state

        pool_state = worker_pool_admission_state()
        if ready_event is not None and not pool_state["available"]:
            return json_error(
                "supervisor worker pool is unavailable",
                503,
                reason_code="worker_pool_unavailable",
                worker_pool_disabled_reason=str(pool_state.get("disabled_reason") or ""),
            )
    except Exception as exc:
        if ready_event is not None:
            return json_error(
                "supervisor worker-pool state is unavailable",
                503,
                reason_code="worker_pool_state_unavailable",
                detail=f"{type(exc).__name__}: {exc}",
            )
    return None


__all__ = [
    "api_task_artifact",
    "api_task_cancel",
    "api_task_resume",
    "api_task_events",
    "api_task_get",
    "api_tasks_create",
    "api_tasks_list",
    "iter_task_events",
]
