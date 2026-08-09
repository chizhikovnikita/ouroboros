"""Shared multi-review substrate.

This module is the common cognitive primitive for migrated review surfaces and
the contract target for remaining legacy immune-system reviews. Slot identity is
separate from model identity, so duplicate model IDs are valid independent
reviewer slots.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import pathlib
import queue
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List

log = logging.getLogger("review_substrate")

from ouroboros.config import get_review_models, review_model_uses_local
from ouroboros.llm import LLMClient
from ouroboros.observability import new_call_id, persist_call, redact_projection
from ouroboros.provider_models import provider_for_model
# Everything below the seam. Re-exported here because the substrate is the
# historical import site for the api_chat prompt renderers; `review_execution`
# owns them now and must never import this module back.
from ouroboros.review_execution import (  # noqa: F401  (compat re-exports)
    AgentSessionReviewExecutor,
    ApiChatReviewExecutor,
    ReviewAssignment,
    ReviewAttemptResult,
    ReviewRouteKind,
    ReviewRouteUnavailable,
    ReviewSlotExecutor,
    _execute_slot_attempt,
    _messages_char_count,
    _render_prompt,
    _render_prompt_parts,
    _request_messages,
    _review_route_executor,
    assert_cache_breakpoint_cap,
    configured_review_routes,
)
# Reviewer-output JSON extraction lives in ONE place beside the array
# extractor it falls back to (the fenced-object and verdict parsers were
# split across two modules for no reason).
from ouroboros.triad_review import parse_review_findings
from ouroboros.usage_accounting import (
    UsageAccountingError,
    UsageScope,
    current_usage_scope,
    physical_attempt_limit,
    usage_scope,
)
from ouroboros.utils import sanitize_tool_result_for_log, truncate_review_artifact


def review_repo_dirs_for(ctx: Any) -> tuple[pathlib.Path, pathlib.Path]:
    """Return validated ``(governance, subject)`` roots for plan/scope review."""
    from ouroboros.tools.registry import active_repo_dir_for

    workspace_raw = getattr(ctx, "workspace_root", None)
    workspace = pathlib.Path(workspace_raw) if isinstance(workspace_raw, (str, pathlib.Path)) else None
    if workspace is not None and not str(getattr(ctx, "workspace_mode", "") or "").strip():
        raise ValueError("workspace_root is set without workspace_mode")
    system_raw = getattr(ctx, "system_repo_dir", None)
    system = pathlib.Path(system_raw) if isinstance(system_raw, (str, pathlib.Path)) else None
    governance = (system or pathlib.Path(getattr(ctx, "repo_dir"))).resolve(strict=False)
    subject = pathlib.Path(active_repo_dir_for(ctx)).resolve(strict=False)
    if not governance.is_dir() or not subject.is_dir():
        raise ValueError(f"unavailable governance/subject root: {governance} / {subject}")
    return governance, subject


@dataclass(frozen=True)
class ReviewSlot:
    slot_id: str
    model: str
    effort: str = "medium"
    timeout_sec: float = 300
    max_tokens: int = 16_384
    temperature: float | None = None
    role_hint: str = ""
    use_local: bool = False
    # Delivery route for this slot. ``use_local`` above is the existing
    # precedent for a per-slot transport hint; ``route`` is the general axis.
    route: ReviewRouteKind = ReviewRouteKind.API_CHAT
    # agent_session rows only: THIS row's opaque ``harness[=model]`` target
    # (6.1 — every slot is independently harness-or-API). Empty falls back to
    # the shared session-route key, which is the whole legacy behavior.
    session_target: str = ""
    # Optional manual credential pin (Q2-в); '' = the daemon's rotation (D28).
    session_profile: str = ""


@dataclass
class ReviewRequest:
    surface: str
    goal: str
    scope: str = ""
    subject: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)
    evidence_refs: List[Dict[str, Any]] = field(default_factory=list)
    checklist: str = ""
    policy: Dict[str, Any] = field(default_factory=dict)
    task_id: str = ""
    messages: List[Dict[str, Any]] = field(default_factory=list)
    call_type: str = ""
    max_tokens: int | None = None
    temperature: float | None = None
    no_proxy: bool = False
    # Session delivery (agent_session route only; the api_chat route never reads
    # either). ``session_root`` is the repository root the reviewer session runs
    # in; ``session_task`` is the surface's compact route-owned task text — the
    # SAME task/criteria the api pack carries, minus the assembled evidence,
    # because a delegated reviewer retrieves context with its own tools (D12).
    session_root: str = ""
    session_task: str = ""


@dataclass
class ReviewActorRecord:
    slot_id: str
    model: str
    status: str
    raw_text: str = ""
    parsed: Any = None
    # Per-actor parsed verdict (PASS/FAIL/DEGRADED/UNKNOWN). Carried here so the
    # objective axis can aggregate outcome_tier from only the actors that
    # CONTRIBUTED to a quorum PASS, instead of re-deriving the verdict downstream.
    signal: str = ""
    error: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    prompt_ref: Dict[str, Any] = field(default_factory=dict)
    response_ref: Dict[str, Any] = field(default_factory=dict)
    duration_sec: float = 0.0
    # Compact typed truth for task-result/event projection.  Raw model output
    # remains in the existing private audit record; these fields prevent UI
    # consumers from conflating a transport failure, malformed JSON, and a
    # valid semantic DEGRADED verdict.
    transport_status: str = ""
    parse_status: str = ""
    semantic_verdict: str = ""
    provider: str = ""
    actor_role: str = ""
    coverage: Dict[str, Any] = field(default_factory=dict)
    # Participation is independent of agreement with the aggregate: every
    # contract-valid PASS/FAIL response counts, while enforcement_impact says
    # whether that participant supports completion or vetoes it.
    quorum_contribution: bool = False
    reason: str = ""
    enforcement_impact: str = ""


@dataclass
class ReviewRunResult:
    request: Dict[str, Any]
    actors: List[Dict[str, Any]]
    parsed_findings: List[Dict[str, Any]]
    aggregate_signal: str
    degraded: bool = False
    degraded_reasons: List[str] = field(default_factory=list)
    # Bible P3: a single configured reviewer is honored but the lost cross-model
    # diversity is recorded LOUDLY and DURABLY here (centralized for every surface
    # that runs through ReviewCoordinator — acceptance, etc. — so a one-slot review
    # can never quietly look like an ordinary multi-reviewer PASS).
    single_reviewer_no_diversity: bool = False
    panel_id: str = ""


# Thin ReviewProfile hardness levels (Bible P3 DRY): the behavior is carried by
# request.policy; these name the three surfaces so callers/reviewers describe
# hardness consistently without a parallel pipeline.
HARDNESS_ADVISORY_VISIBLE = "advisory_visible"  # fed back as a compact capsule, never blocks
HARDNESS_LABEL_ONLY = "label_only"              # recorded on the objective axis, not shown
HARDNESS_HARD_GATE = "hard_gate"                # blocking commit/scope immune gate (unchanged)

# Tier vocabulary SSOT lives in outcomes.py; reuse it so a future tier rename
# cannot silently desync the capsule from the objective axis.
from ouroboros.outcomes import OUTCOME_TIER_BEST_EFFORT, OUTCOME_TIER_BLOCKED, OUTCOME_TIER_SOLVED

_TIER_ORDER = {OUTCOME_TIER_SOLVED: 0, OUTCOME_TIER_BEST_EFFORT: 1, OUTCOME_TIER_BLOCKED: 2}


def _transport_error_status(error: Any) -> str:
    """Classify transport failures without depending on a non-empty message."""
    error_type = type(error).__name__ if isinstance(error, BaseException) else ""
    error_text = str(error or "")
    if (
        isinstance(error, TimeoutError)
        or "timeout" in error_type.casefold()
        or "timeout" in error_text.casefold()
        or "timed out" in error_text.casefold()
    ):
        return "timeout"
    return "provider_transport_error"


def _public_review_reason(value: Any) -> str:
    """Redact model-controlled reason text before publishing it in full.

    v6.70.0 honesty change (owner decision): reviewer rationale is a cognitive
    artifact (BIBLE P1 — multi-model review outputs must not fall back to
    generic transport truncation). The former 500/800-char caps destroyed the
    only owner-reachable copy of the reasoning (task_results carried the same
    truncated projection and the full observability blobs were unreferenced),
    so the projection now publishes the COMPLETE redacted text; secrets are
    still masked by redact_projection."""
    text = str(value or "")
    if not text:
        return ""
    return str(redact_projection(text).value)


def _review_actor_projection(actor: Any, surface: str) -> Dict[str, Any]:
    row = actor if isinstance(actor, dict) else asdict(actor)
    parsed = row.get("parsed") if isinstance(row.get("parsed"), (dict, list)) else None
    usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    explicit_parse = str(row.get("parse_status") or "")
    semantic = str(row.get("semantic_verdict") or "").upper()
    if not semantic and isinstance(parsed, dict):
        semantic = str(parsed.get("verdict") or parsed.get("status") or "").upper()
    if not semantic:
        semantic = str(row.get("signal") or "").upper()
    valid = (
        explicit_parse != "malformed"
        and parsed is not None
        and semantic in {"PASS", "FAIL", "DEGRADED"}
    )
    error = str(row.get("error") or "")
    transport = str(row.get("transport_status") or "")
    if not transport:
        transport = (
            "success" if str(row.get("status") or "") in {"ok", "empty"}
            else _transport_error_status(error)
        )
    criteria = parsed.get("criteria_used") if isinstance(parsed, dict) else []
    criteria = criteria if isinstance(criteria, list) else []
    if isinstance(parsed, dict):
        parsed_findings = parsed.get("findings")
    elif isinstance(parsed, list):
        parsed_findings = parsed
    else:
        parsed_findings = []
    parsed_findings = (
        [item for item in parsed_findings if isinstance(item, dict)]
        if isinstance(parsed_findings, list)
        else []
    )
    reason = str(row.get("reason") or "")
    if not reason and isinstance(parsed, dict):
        reason = str(parsed.get("summary") or parsed.get("reason") or "")
    if not reason and isinstance(parsed, list):
        for item in parsed_findings:
            reason = str(
                item.get("summary")
                or item.get("reason")
                or item.get("evidence")
                or item.get("item")
                or item.get("recommendation")
                or ""
            )
            if reason:
                break
    reason = reason or error or ("Reviewer response was malformed or absent." if not valid else "")
    model = str(usage.get("resolved_model") or row.get("model") or "")
    provider = str(usage.get("provider") or row.get("provider") or "")
    if not provider:
        provider = provider_for_model(model) if model else "unknown"
    outcome_tier = (
        str(parsed.get("outcome_tier") or "").strip().lower()
        if isinstance(parsed, dict)
        else ""
    )
    if outcome_tier not in {
        OUTCOME_TIER_SOLVED, OUTCOME_TIER_BEST_EFFORT, OUTCOME_TIER_BLOCKED,
    }:
        outcome_tier = ""
    dialogue_vote = (
        str(parsed.get("dialogue_status") or "").strip().lower()
        if isinstance(parsed, dict)
        else ""
    )
    if dialogue_vote not in DIALOGUE_STATUS_VALUES:
        dialogue_vote = ""
    return {
        "slot_id": str(row.get("slot_id") or ""), "model": model, "provider": provider,
        "actor_role": str(row.get("actor_role") or f"{surface} reviewer"),
        "transport_status": transport,
        "parse_status": explicit_parse or ("valid" if valid else "malformed"),
        "semantic_verdict": semantic if valid else "",
        "outcome_tier": outcome_tier if valid else "",
        "dialogue_status": dialogue_vote if valid else "",
        "coverage": {
            "criteria_total": len(criteria),
            "findings": len(parsed_findings),
        },
        "quorum_contribution": bool(row.get("quorum_contribution")),
        "reason": _public_review_reason(reason),
        "enforcement_impact": str(row.get("enforcement_impact") or "abstains"),
        # Forensic pointer to the full raw reviewer response in the private
        # observability store (durable-copy reachability; never the raw text,
        # never absolute host paths — exported task records must not leak the
        # install layout). persist_call() nests the content hashes inside
        # redacted_projection_ref/manifest_ref; project them flat.
        "response_ref": _response_ref_projection(row.get("response_ref")),
    }


def _response_ref_projection(ref: Any) -> Dict[str, str]:
    if not isinstance(ref, dict):
        return {}
    out: Dict[str, str] = {}
    if ref.get("call_id"):
        out["call_id"] = str(ref["call_id"])
    projection_ref = ref.get("redacted_projection_ref")
    if isinstance(projection_ref, dict) and projection_ref.get("sha256"):
        out["sha256"] = str(projection_ref["sha256"])
    elif ref.get("sha256"):
        out["sha256"] = str(ref["sha256"])
    manifest_ref = ref.get("manifest_ref")
    if isinstance(manifest_ref, dict) and manifest_ref.get("sha256"):
        out["manifest_sha256"] = str(manifest_ref["sha256"])
    return out


def _review_enforcement_impact(run: Dict[str, Any]) -> str:
    if str(run.get("enforcement_impact") or ""):
        return str(run["enforcement_impact"])
    request = run.get("request") if isinstance(run.get("request"), dict) else {}
    hardness = str((request.get("policy") or {}).get("hardness") or "")
    signal = str(run.get("aggregate_signal") or "").upper()
    if str(run.get("authority") or "") == "agent_advisory" or hardness == HARDNESS_ADVISORY_VISIBLE:
        return "advisory"
    if signal == "PASS":
        return "allows_completion"
    return "blocks_completion" if signal == "FAIL" and hardness == HARDNESS_HARD_GATE else "degrades_completion"


def _review_panel_id(request: ReviewRequest, actors: List[ReviewActorRecord]) -> str:
    seed = {
        "surface": request.surface,
        "task_id": request.task_id,
        "actors": [
            [actor.slot_id, actor.model, actor.response_ref]
            for actor in actors
        ],
    }
    digest = hashlib.sha256(
        json.dumps(seed, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    return f"panel_{digest[:16]}"


def build_review_binding(
    *,
    candidate: str,
    evidence: Dict[str, Any],
    fence_token_or_state: Any,
) -> Dict[str, Any]:
    """Build the exact host-panel identity without introducing another ledger."""
    from ouroboros.review_evidence import task_acceptance_evidence_revision

    candidate_hash = hashlib.sha256(str(candidate or "").encode("utf-8")).hexdigest()
    evidence_revision = task_acceptance_evidence_revision(evidence)
    fence_value = (
        json.dumps(fence_token_or_state, sort_keys=True, separators=(",", ":"), default=str)
        if isinstance(fence_token_or_state, (dict, list, tuple))
        else str(fence_token_or_state or "direct_context")
    )
    fence_hash = hashlib.sha256(fence_value.encode("utf-8")).hexdigest()
    binding_payload = {
        "candidate_hash": candidate_hash,
        "evidence_revision": evidence_revision,
        "fence_hash": fence_hash,
    }
    binding_hash = hashlib.sha256(
        json.dumps(binding_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        **binding_payload,
        "binding_hash": binding_hash,
        "panel_id": f"panel_{binding_hash[:16]}",
    }


def compact_review_projection(review_runs: Any) -> Dict[str, Any]:
    """Project existing audit runs without copying raw prompts or responses."""
    panels: List[Dict[str, Any]] = []
    for index, raw_run in enumerate(review_runs or []):
        if not isinstance(raw_run, dict):
            continue
        request = raw_run.get("request") if isinstance(raw_run.get("request"), dict) else {}
        surface = str(request.get("surface") or "review")
        actors = [_review_actor_projection(actor, surface) for actor in (raw_run.get("actors") or []) if isinstance(actor, dict)]
        policy = request.get("policy") if isinstance(request.get("policy"), dict) else {}
        min_successful = max(1, int(policy.get("min_successful_slots") or 1))
        contributing = sum(1 for actor in actors if actor["quorum_contribution"])
        transport_statuses = [actor["transport_status"] for actor in actors]
        transport = (
            "success" if transport_statuses and all(s == "success" for s in transport_statuses)
            else ("partial" if "success" in transport_statuses else (
                "timeout" if transport_statuses and all(s == "timeout" for s in transport_statuses)
                else "provider_transport_error"
            ))
        )
        reasons = raw_run.get("degraded_reasons") if isinstance(raw_run.get("degraded_reasons"), list) else []
        panel: Dict[str, Any] = {
            "panel_id": str(raw_run.get("panel_id") or f"panel_{index + 1}"),
            "surface": surface,
            "authority": str(raw_run.get("authority") or "unspecified"),
            "aggregate_signal": str(raw_run.get("aggregate_signal") or "UNKNOWN").upper(),
            "transport_status": str(raw_run.get("transport_status") or transport),
            "parse_status": str(raw_run.get("parse_status") or (
                "valid" if actors and all(a["parse_status"] == "valid" for a in actors) else "malformed"
            )),
            "coverage": {
                "actors_configured": len(actors),
                "transport_success": sum(1 for actor in actors if actor["transport_status"] == "success"),
                "parse_valid": sum(1 for actor in actors if actor["parse_status"] == "valid"),
                "quorum_contributing": contributing,
            },
            "quorum": {"required": min_successful, "contributed": contributing, "configured": len(actors)},
            # v6.74.0 (A6): the fallback reason is the structured panel_reason
            # reducer — it names the real blocker (tier + finding / degraded
            # causes) instead of an opaque aggregate label. An explicitly
            # recorded reason still wins.
            "reason": _public_review_reason(
                str(raw_run.get("reason") or "; ".join(str(item) for item in reasons)
                    or panel_reason(raw_run)),
            ),
            "enforcement_impact": _review_enforcement_impact(raw_run),
            "actors": actors,
            "superseded": bool(raw_run.get("superseded_by_revision")),
        }
        if raw_run.get("single_reviewer_no_diversity"):
            panel["single_reviewer_no_diversity"] = True
        if isinstance(raw_run.get("dialogue"), dict):
            panel["dialogue"] = raw_run.get("dialogue")
        for key in (
            "candidate_hash", "evidence_revision", "fence_hash", "binding_hash",
        ):
            if raw_run.get(key) not in (None, ""):
                panel[key] = str(raw_run.get(key))
        panels.append(panel)
    return {"panels": panels}


_CRITERION_STATUSES = frozenset({"supported", "missing", "partial", "rejected"})


def _criteria_have_supported_evidence(criteria: Any) -> bool:
    return bool(isinstance(criteria, list) and criteria and all(
        isinstance(item, dict)
        and bool(str(item.get("criterion") or "").strip())
        and str(item.get("status") or "").strip().lower() == "supported"
        and bool(item.get("evidence_refs"))
        for item in criteria
    ))


def _criteria_shape_valid(criteria: Any, tier: str) -> bool:
    """Shape + tier coherence for a reviewer's criteria_used (v6.71.1).

    SHAPE: a non-empty list of {criterion, status ∈ enum}, and every 'supported'
    criterion names evidence_refs. COHERENCE: 'solved' still requires ALL criteria
    'supported' with refs — the release-clean bar (task_acceptance_is_clean) is
    unchanged; a non-solved tier (best_effort / blocked_with_evidence) may honestly
    carry partial/missing/rejected criteria. This lets an honest PASS that marks one
    criterion 'partial' contribute as a valid NON-clean vote instead of being demoted
    to parse_status=malformed — the old all-must-be-'supported' gate (the prompt itself
    offers 'partial') silently starved the honest-partial path and fueled acceptance
    loops (BIBLE P2/P3; the FAIL-veto and clean-solved contracts are untouched)."""
    if not (isinstance(criteria, list) and criteria):
        return False
    for item in criteria:
        if not isinstance(item, dict):
            return False
        if not str(item.get("criterion") or "").strip():
            return False
        status = str(item.get("status") or "").strip().lower()
        if status not in _CRITERION_STATUSES:
            return False
        if status == "supported" and not item.get("evidence_refs"):
            return False
    if str(tier or "").strip().lower() == OUTCOME_TIER_SOLVED:
        return _criteria_have_supported_evidence(criteria)
    return True


def _contributing_actors(result: ReviewRunResult) -> List[Dict[str, Any]]:
    """Actors whose verdict CONTRIBUTED to the aggregate, so a parse-degraded or
    non-responsive slot cannot inject a tier / coach / finding into a clean quorum
    result (Bible P3: one degraded slot must not poison the aggregate — the exact
    class the split-participation gate was built to avoid). For aggregate PASS only
    PASS actors speak; for FAIL only FAIL actors; for a DEGRADED/UNKNOWN aggregate
    only the cleanly-parsed PASS/FAIL actors may speak (never the degraded ones)."""
    actors = [a for a in (getattr(result, "actors", None) or []) if isinstance(a, dict)]
    agg = str(getattr(result, "aggregate_signal", "") or "").upper()
    if agg in ("PASS", "FAIL"):
        return [a for a in actors if str(a.get("signal", "")).upper() == agg]
    return [a for a in actors if str(a.get("signal", "")).upper() in ("PASS", "FAIL")]


def aggregate_outcome_tier(result: ReviewRunResult) -> str:
    """Worst-tier-wins across the actors that CONTRIBUTED to the aggregate verdict."""
    worst, worst_rank = "", -1
    for actor in _contributing_actors(result):
        parsed = actor.get("parsed") if isinstance(actor, dict) else None
        tier = str((parsed or {}).get("outcome_tier") or "").strip().lower() if isinstance(parsed, dict) else ""
        rank = _TIER_ORDER.get(tier, -1)
        if rank > worst_rank:
            worst_rank, worst = rank, tier
    return worst


def task_acceptance_is_clean(result: Any) -> bool:
    """Whether a task-acceptance verdict satisfies the release-clean contract.

    The evidence condition is UNCONDITIONAL (D-Q5 deleted the constant-true
    ``require_criterion_evidence`` knob — the v6.60.0 dead-key precedent), and a
    'supported' criterion counts only when ≥1 of its ``evidence_refs`` RESOLVED
    against the packet (host annotation stamped at panel time; absent on
    historical rows — forward-only). Both demote ONLY this clean bit onto the
    existing non-clean rails; parse validity/quorum/verdicts untouched (v6.71.1)."""
    if str(getattr(result, "aggregate_signal", "") or "").upper() != "PASS" or bool(getattr(result, "degraded", False)):
        return False
    contributing = _contributing_actors(result)
    if not contributing:
        return False
    for actor in contributing:
        parsed = actor.get("parsed") if isinstance(actor, dict) else None
        if not isinstance(parsed, dict) or str(parsed.get("outcome_tier") or "").lower() != OUTCOME_TIER_SOLVED:
            return False
        if not _criteria_have_supported_evidence(parsed.get("criteria_used")):
            return False
        if any(isinstance(r, dict) and not r.get("supported_evidence_resolves")
               for r in (actor.get("criteria_refs_unresolved") or [])):
            return False
    return True


# v6.74.0 (A5): reviewer-authored dialogue status. The reviewer — not a host
# counter or hash — judges whether the acceptance dialogue is still actionable.
DIALOGUE_CONTINUE = "continue_actionable"
DIALOGUE_UNREACHABLE = "unreachable_here"
DIALOGUE_STABLE_DISAGREEMENT = "stable_disagreement"
DIALOGUE_STATUS_VALUES = (DIALOGUE_CONTINUE, DIALOGUE_UNREACHABLE, DIALOGUE_STABLE_DISAGREEMENT)


def _contract_valid_actors(result: Any) -> List[Dict[str, Any]]:
    """Actors with a DELIBERATE, CONTRACT-VALID reviewer object: parsed dict,
    recognizable verdict, parse_status not "malformed". Wider than
    ``_contributing_actors`` (deliberate DEGRADED keeps its vote, sol #3) but a
    contract-DEMOTED/garbage response never votes terminal (commit triad #1)."""
    out: List[Dict[str, Any]] = []
    for actor in (getattr(result, "actors", None) or []):
        row = actor if isinstance(actor, dict) else asdict(actor)
        parsed = row.get("parsed")
        if str(row.get("parse_status") or "") == "malformed":
            continue
        if isinstance(parsed, dict) and str(
            parsed.get("verdict") or parsed.get("status") or ""
        ).strip().upper() in {"PASS", "FAIL", "DEGRADED"}:
            out.append(row)
    return out


def aggregate_dialogue_status(result: Any, *, quorum: int) -> Dict[str, Any]:
    """Pure reducer over the reviewers' typed ``dialogue_status`` votes (A5, P5):
    the host validates the enum, applies the caller's quorum, and transports the
    result. Precedence: any continue vote from a QUORUM-CONTRIBUTING actor keeps
    the loop; else a quorum of terminal votes terminates. Missing/invalid votes
    default to ``continue_actionable`` (fail-safe, backward-compatible).
    Returns ``{"status", "votes"}`` with the full distribution for audit."""
    contributing = {str(a.get("slot_id", "")) for a in _contributing_actors(result)}
    votes: Dict[str, List[str]] = {}
    for row in _contract_valid_actors(result):
        parsed = row.get("parsed") if isinstance(row.get("parsed"), dict) else {}
        vote = str(parsed.get("dialogue_status") or "").strip().lower()
        if vote not in DIALOGUE_STATUS_VALUES:
            vote = DIALOGUE_CONTINUE
        votes.setdefault(vote, []).append(str(row.get("slot_id", "")))
    continue_slots = votes.get(DIALOGUE_CONTINUE, [])
    unreachable = votes.get(DIALOGUE_UNREACHABLE, [])
    disagreement = votes.get(DIALOGUE_STABLE_DISAGREEMENT, [])
    terminal = unreachable + disagreement
    if any(slot in contributing for slot in continue_slots):
        status = DIALOGUE_CONTINUE
    elif len(terminal) >= max(1, int(quorum)):
        status = (
            DIALOGUE_UNREACHABLE
            if len(unreachable) >= len(disagreement)
            else DIALOGUE_STABLE_DISAGREEMENT
        )
    else:
        status = DIALOGUE_CONTINUE
    return {"status": status, "votes": votes}


def _unresolved_evidence_ref_labels(run: Any) -> List[str]:
    """The D-Q5 refs a contributing actor cited that did NOT resolve, as
    ``ref (basis)`` labels (or the panel-wide ``host_resolution_unavailable``).

    Pure read of the deciding detail already recorded on the actor rows, so
    ``panel_reason`` can name the REAL blocker: on a D-Q5 demotion every criterion
    IS marked supported with refs, and the criteria-support line would describe a
    condition that is already satisfied."""
    from ouroboros.review_evidence_refs import NON_RESOLVING_BASIS_KINDS

    labels: List[str] = []
    for actor in _contributing_actors(run):
        for row in (actor.get("criteria_refs_unresolved") or []):
            if not isinstance(row, dict) or row.get("supported_evidence_resolves"):
                continue
            if str(row.get("resolution_status") or ""):
                labels.append(str(row["resolution_status"]))
                continue
            for ref in (row.get("refs") or []):
                if not isinstance(ref, dict):
                    continue
                basis = str(ref.get("resolved_as") or "")
                if basis and basis not in NON_RESOLVING_BASIS_KINDS:
                    continue  # this one resolved; it is not the blocker
                labels.append(f"{str(ref.get('ref') or '?')[:80]} ({basis or 'no packet entry'})")
    return list(dict.fromkeys(labels))


def panel_reason(run: Any) -> str:
    """One honest reason line naming the REAL blocker (v6.74.0, A6); shared by
    the capsule header, the compact projection fallback, and progress lines.
    Accepts a ``ReviewRunResult`` or its dict/namespace record."""
    from types import SimpleNamespace

    if isinstance(run, dict):
        run = SimpleNamespace(**run)
    aggregate = str(getattr(run, "aggregate_signal", "") or "UNKNOWN").upper()
    tier = aggregate_outcome_tier(run)
    if aggregate == "PASS":
        if task_acceptance_is_clean(run):
            return "clean acceptance"
        # A D-Q5 demotion leaves every criterion marked supported WITH refs, so the
        # criteria-support line below would name a condition that is already
        # satisfied. Name the ref that actually decided instead.
        unresolved = _unresolved_evidence_ref_labels(run)
        if unresolved:
            more = f" (+{len(unresolved) - 3} more)" if len(unresolved) > 3 else ""
            return (
                f"tier={tier or 'unclassified'} — cited evidence does not resolve "
                f"against the packet: {', '.join(unresolved[:3])}{more}"
            )
        return (
            f"tier={tier or 'unclassified'} — a PASS is not release-clean until "
            "every criterion is supported"
        )
    if aggregate == "FAIL":
        fail_slots = {
            str(actor.get("slot_id", ""))
            for actor in _contributing_actors(run)
            if str(actor.get("signal", "")).upper() == "FAIL"
        }
        named = ""
        for actor in _contributing_actors(run):
            if str(actor.get("signal", "")).upper() != "FAIL":
                continue
            parsed = actor.get("parsed") if isinstance(actor.get("parsed"), dict) else {}
            for finding in (parsed.get("findings") or []):
                if isinstance(finding, dict):
                    named = str(finding.get("item") or finding.get("recommendation") or "").strip()
                    if named:
                        break
            if not named:
                named = str(parsed.get("summary") or "").strip()
            if named:
                break
        if not named:
            # Coordinator-flattened findings (slot_id-stamped) from FAIL slots.
            for finding in (getattr(run, "parsed_findings", None) or []):
                if isinstance(finding, dict) and str(finding.get("slot_id", "")) in fail_slots:
                    named = str(finding.get("item") or finding.get("recommendation") or "").strip()
                    if named:
                        break
        if named:
            compact = truncate_review_artifact(" ".join(named.split()), limit=300)
            return f"tier={tier or 'unclassified'} — {compact}"
        return f"tier={tier or 'unclassified'} — reviewer FAIL without a named finding"
    reasons = [str(r) for r in (getattr(run, "degraded_reasons", None) or []) if str(r)]
    if len(reasons) > 4:
        return "; ".join(reasons[:4]) + f" ⚠️ OMISSION NOTE: +{len(reasons) - 4} more causes in the run record"
    return "; ".join(reasons) or "no valid reviewer quorum"


def dissent_findings(result: ReviewRunResult, *, limit: int = 1) -> List[str]:
    """Compact dissent bullets from NON-contributing minority reviewers (v6.54.4).

    A cleanly-parsed reviewer whose verdict differs from the aggregate AND who
    carries a CONCRETE recommendation/alternative contributes ONE verbatim
    "[DISSENT — slot N]: ..." line. Not a veto — the aggregate stands; this ends
    the class where an aggregate-PASS silently discarded a minority FAIL whose
    concrete recommendation was correct (GAIA 3cef3a44). A DELIBERATE minority
    DEGRADED — the reviewer's own parsed verdict (the prompt's "cannot judge →
    return DEGRADED and explain" branch, which is exactly what the 3cef3a44
    reviewer returned) — may dissent too, but only on the strength of a concrete
    findings[].recommendation. Parse-fail placeholders (parsed=None),
    contract-demoted PASSes (their parsed verdict stays PASS — they agree with
    the aggregate), and coach-only DEGRADED stay excluded (no clean dissenting
    signal). ONE bullet by design (plan decision #13) — the first concrete
    dissenter speaks."""
    agg = str(getattr(result, "aggregate_signal", "") or "").upper()
    contributing_ids = {str(a.get("slot_id", "")) for a in _contributing_actors(result)}
    out: List[str] = []
    for actor in (getattr(result, "actors", None) or []):
        if not isinstance(actor, dict) or len(out) >= limit:
            continue
        slot_id = str(actor.get("slot_id", ""))
        signal = str(actor.get("signal", "")).upper()
        if slot_id in contributing_ids or signal == agg:
            continue
        parsed = actor.get("parsed") if isinstance(actor.get("parsed"), dict) else {}
        deliberate_degraded = (
            signal == "DEGRADED"
            and str(parsed.get("verdict") or "").strip().upper() == "DEGRADED"
        )
        if signal not in ("PASS", "FAIL") and not deliberate_degraded:
            continue
        recommendation = ""
        for finding in (parsed.get("findings") or []):
            if isinstance(finding, dict):
                recommendation = str(finding.get("recommendation") or "").strip()
                if recommendation:
                    break
        if not recommendation and not deliberate_degraded:
            recommendation = str(parsed.get("completion_coach") or "").strip()
        if not recommendation:
            continue  # a bare contrary verdict with no concrete alternative is noise
        compact = " ".join(recommendation.split())
        if len(compact) > 300:
            compact = compact[:300].rstrip() + "…"
        out.append(f"[DISSENT — {slot_id} said {signal}]: check this before finalizing — {compact}")
    return out


def build_improvement_capsule(
    result: ReviewRunResult,
    *,
    rails_line: str = "",
    open_obligations: List[Dict[str, Any]] | None = None,
) -> str:
    """Compact, anti-derailment "Final improvement note" fed back to the agent:
    the actual verdict + tier + real blocker (v6.74.0, A1 — today only the tier
    label printed), the concrete open obligation ids, one pre-rendered rails
    line (money/time/rounds/passes headroom, assembled by the caller from the
    real sources), exact-deduplicated actionable findings, and one
    completion_coach. Returns "" when there is nothing actionable. The full
    ReviewRunResult stays on the objective axis / trace; the agent sees only this
    capsule, so it does not rewrite its deliverable into a meta-essay about the
    review (the failure mode that made the host-forced path label-only).

    Tier, coach, and bullets are drawn ONLY from the actors that contributed to the
    aggregate verdict, so a single parse-degraded slot cannot inject a blocking note
    into an otherwise-clean quorum PASS."""
    aggregate_signal = str(getattr(result, "aggregate_signal", "") or "").upper()
    contributing = _contributing_actors(result)
    # A semantic DEGRADED verdict abstains from quorum, but a concrete finding is
    # still an owner-approved correction rail for the required+blocking re-drive.
    # Transport/parse placeholders and contract-demoted PASS/FAIL actors remain
    # excluded: only an explicitly parsed verdict=DEGRADED may supply ANY capsule
    # content (tier, coach, or finding) when the aggregate itself is DEGRADED.
    deliberate_degraded = [
        actor
        for actor in (getattr(result, "actors", None) or [])
        if (
            aggregate_signal == "DEGRADED"
            and isinstance(actor, dict)
            and str(actor.get("signal") or "").upper() == "DEGRADED"
            and isinstance(actor.get("parsed"), dict)
            and str(actor["parsed"].get("verdict") or "").strip().upper() == "DEGRADED"
        )
    ]
    eligible_actors = deliberate_degraded if aggregate_signal == "DEGRADED" else contributing
    eligible_slots = {str(actor.get("slot_id", "")) for actor in eligible_actors}
    tier = ""
    tier_rank = -1
    for actor in eligible_actors:
        parsed = actor.get("parsed") if isinstance(actor, dict) else None
        actor_tier = (
            str(parsed.get("outcome_tier") or "").strip().lower()
            if isinstance(parsed, dict)
            else ""
        )
        actor_rank = _TIER_ORDER.get(actor_tier, -1)
        if actor_rank > tier_rank:
            tier_rank, tier = actor_rank, actor_tier
    coach = ""
    for actor in eligible_actors:
        parsed = actor.get("parsed") if isinstance(actor, dict) else None
        if isinstance(parsed, dict) and not coach:
            coach = str(parsed.get("completion_coach") or "").strip()
        if coach:
            break
    bullets: List[str] = []
    seen_bullets: set[str] = set()
    for finding in (getattr(result, "parsed_findings", None) or []):
        if not isinstance(finding, dict):
            continue
        # Only findings from a contributing actor may surface in the capsule.
        if str(finding.get("slot_id", "")) not in eligible_slots:
            continue
        text = str(finding.get("recommendation") or finding.get("item") or "").strip()
        # Exact normalized deduplication only.  Do not introduce semantic
        # clustering or another findings authority for the improvement loop.
        dedup_key = " ".join(text.split())
        if text and dedup_key not in seen_bullets:
            seen_bullets.add(dedup_key)
            bullets.append(text)
    # A SOLVED review carries a (contract-required) completion_coach, but a coach
    # alone must NOT force a revise round on an already-solved deliverable — that
    # would re-loop EVERY clean required review. The capsule is actionable only
    # when there are real findings to act on OR the tier itself is incomplete
    # (best_effort/blocked). The coach is then included as the next step.
    dissent = dissent_findings(result)
    # A coach alone stays non-actionable for a clean SOLVED PASS, but it is the
    # bounded correction rail for a contributing FAIL.  The coordinator admits a
    # task-acceptance FAIL only when this function can return such a rail.
    actionable = (
        bool(bullets)
        or bool(dissent)
        or (
            aggregate_signal == "FAIL"
            and bool(coach)
        )
        or tier in (OUTCOME_TIER_BEST_EFFORT, OUTCOME_TIER_BLOCKED)
    )
    if not actionable:
        return ""
    # Lead with the actual outcome (A1): verdict + tier + the real blocker, so
    # the agent sees WHAT failed instead of a bare ledger label.
    header = f"[Final improvement note] Review verdict: {aggregate_signal or 'UNKNOWN'}"
    if tier:
        header += f" (tier: {tier})"
    header += f" — {panel_reason(result)}."
    lines = [header]
    open_ids = [
        str(o.get("id"))
        for o in (open_obligations or [])
        if isinstance(o, dict) and o.get("id")
    ]
    if open_ids:
        lines.append(
            f"Open blocking obligation(s) ({len(open_ids)}): " + ", ".join(open_ids) + "."
        )
    if rails_line:
        lines.append(f"Remaining headroom — {rails_line}.")
    # Dissent rides ON TOP of the capsule (v6.54.4): same anti-derailment frame,
    # never a veto — a minority reviewer with a concrete recommendation is a
    # "check this before finalizing" pointer, not a re-litigation of the verdict.
    lines += dissent
    lines += [f"- {b}" for b in bullets]
    if coach:
        lines.append(f"Highest-value next step: {coach}")
    lines.append(
        # The three real moves (A1): the old "revise only if it genuinely
        # improves the result; otherwise produce your normal final answer" tail
        # was the measured cause of the do-nothing resubmit loop (SWE 1b311217:
        # 7 passes, zero tool calls). The anti-derailment guards stay verbatim.
        "Three real moves are available: (1) FIX — change the work/answer so the next panel is "
        "clean; (2) REBUT — file obligation_dispositions (rejected + your reason) via the "
        "task_acceptance_review tool for findings you can show are wrong; the reviewer "
        "adjudicates the argument; (3) DECLARE UNREACHABLE — dispose an obligation as "
        "unsatisfiable in this environment (rejected + the concrete gap), and the reviewer "
        "judges reachability. Resubmitting the same answer with none of these moves changes "
        "nothing. "
        "Do not mention this review or the reviewer unless the user asked. "
        "The assessment tier above is an internal ledger label — never emit an internal ledger "
        "identifier as the deliverable itself."
    )
    return "\n".join(lines)


# Identity prefixes for the configured reviewer surfaces. A surface that fans
# rows out registers its prefix here rather than spelling one inline, so
# ``slot_id_for_row`` stays the only place a row id is built.
SLOT_ID_PREFIX = "slot"
SCOPE_SLOT_ID_PREFIX = "scope_slot"
PLAN_SLOT_ID_PREFIX = "plan_slot"


def slot_id_for_row(index: int, *, prefix: str = SLOT_ID_PREFIX) -> str:
    """Identity of the ``index``-th (1-based) configured reviewer row.

    The single mint for reviewer-slot identity, and the reason this module's
    contract says slot identity is separate from model identity. Naming a row
    after its own model instead collides two rows that share a model (a supported
    configuration — ``get_scope_review_models`` preserves duplicates on purpose),
    collides two model spellings that sanitize alike (``openai::gpt-5`` and
    ``openai/gpt/5``), and moves a row's identity the moment the owner edits its
    model, so the row's receipts stop lining up with its own history. The model,
    the route and the effort are PROPERTIES of a row, never its name.
    """
    return f"{prefix}_{int(index)}"


def reviewer_slots(
    models: List[str] | None = None,
    *,
    effort: str = "medium",
    role_hint: str = "",
    id_prefix: str = SLOT_ID_PREFIX,
    route_env_key: str = "",
) -> List[ReviewSlot]:
    """The configured reviewer rows, each carrying its DELIVERY route.

    ``route_env_key`` names the surface's per-row route list (plan 5.1): the
    commit triad and scope pass theirs, so a row can be an api_chat call or a
    delegated agent session. Surfaces that stay on the API by owner decision
    (task acceptance, plan review — D15) pass NOTHING, which pins every row to
    ``api_chat`` explicitly rather than by accident.
    """
    raw_models = models if models is not None else get_review_models()
    named = [str(model) for model in (raw_models or []) if str(model or "").strip()]
    routes = configured_review_routes(route_env_key, len(named)) if route_env_key else [
        ReviewRouteKind.API_CHAT
    ] * len(named)
    return [
        ReviewSlot(slot_id=slot_id_for_row(idx + 1, prefix=id_prefix), model=model, effort=effort,
                   role_hint=role_hint, use_local=review_model_uses_local(model),
                   route=routes[idx])
        for idx, model in enumerate(named)
    ]


def scope_reviewer_slots(
    models: List[str] | None = None, *, effort: str | None = None,
) -> List[ReviewSlot]:
    """The configured scope-reviewer rows — the single owner of scope-slot identity.

    Both scope surfaces read their ids from here: the substrate call that produces
    the durable prompt/response refs, and the actor records the commit attempt
    persists. One row therefore carries exactly one identity instead of two that
    disagree. Each row also carries its configured delivery route (D14: every
    scope slot is independently harness-or-API).

    With no explicit ``models`` the rows come from the reviewer-slot SSOT
    (6.1): stable owner ids, per-row route/target/effort. An explicit list
    keeps the historical positional behavior for callers that rebuild one row.

    An omitted ``effort`` resolves to the configured scope-review effort: the
    legacy path used to take this parameter's old literal default instead,
    silently running the BLOCKING reviewer below configured strength (the
    downgrade class the owner forbade).
    """
    if effort is None:
        from ouroboros.config import resolve_effort

        effort = resolve_effort("scope_review")
    if models is None:
        from ouroboros.reviewer_slot_config import structured_scope_review_slots

        structured = structured_scope_review_slots()
        if structured is not None:
            return structured
        # Resolved at call time so the configured list stays the live authority.
        from ouroboros.config import get_scope_review_models

        models = get_scope_review_models()
    from ouroboros.review_execution import SCOPE_REVIEW_ROUTES_ENV

    return reviewer_slots(
        models, effort=effort, role_hint="scope reviewer", id_prefix=SCOPE_SLOT_ID_PREFIX,
        route_env_key=SCOPE_REVIEW_ROUTES_ENV,
    )


class ReviewCoordinator:
    def __init__(
        self,
        *,
        llm: LLMClient | None = None,
        drive_root: pathlib.Path | None = None,
        usage_ctx: Any = None,
    ):
        self.llm = llm or LLMClient()
        if drive_root is not None:
            self.drive_root = pathlib.Path(drive_root)
        else:
            # ISO-DRIP: the default is the ABSOLUTE config SSOT, never the old
            # cwd-relative "../data" — with any cwd under a repo/ that spelling
            # names the LIVE data root's sibling, so default-constructed
            # coordinators dripped synthetic review records into live
            # observability (and on trees with the absolute-root guard they
            # silently LOST the records instead: persist_call refuses relative
            # roots and the coordinator swallows it into empty refs). Same
            # resolution order the review surfaces already use
            # (review_drive_root: ctx → DATA_DIR); read late off the module so
            # test isolation and runtime rebinding are honored.
            from ouroboros import config

            self.drive_root = pathlib.Path(config.DATA_DIR)
        self.usage_ctx = usage_ctx

    def run(self, request: ReviewRequest, slots: List[ReviewSlot]) -> ReviewRunResult:
        if not slots:
            return ReviewRunResult(
                request=asdict(request),
                actors=[],
                parsed_findings=[],
                aggregate_signal="DEGRADED",
                degraded=True,
                degraded_reasons=["no_review_slots"],
                panel_id=_review_panel_id(request, []),
            )

        result_queue: "queue.Queue[ReviewActorRecord]" = queue.Queue()
        started_slots: List[ReviewSlot] = []
        base_scope = current_usage_scope() or UsageScope()
        usage_meta = (
            getattr(self.usage_ctx, "task_metadata", {})
            if self.usage_ctx is not None
            else {}
        )
        if not isinstance(usage_meta, dict):
            usage_meta = {}
        task_id = str(request.task_id or base_scope.task_id or "")
        root_task_id = str(
            usage_meta.get("root_task_id") or base_scope.root_task_id or task_id
        )
        budget_root = (
            usage_meta.get("budget_drive_root")
            or getattr(self.usage_ctx, "budget_drive_root", "")
            or base_scope.drive_root
            or self.drive_root
        )
        if base_scope.global_limit_usd is not None:
            global_limit = base_scope.global_limit_usd
        else:
            try:
                configured_global_limit = float(os.environ.get("TOTAL_BUDGET", "0") or 0)
                global_limit = configured_global_limit if configured_global_limit > 0 else None
            except (TypeError, ValueError):
                global_limit = None
        if base_scope.root_limit_usd is not None:
            root_limit = base_scope.root_limit_usd
        else:
            try:
                configured_root_limit = float(
                    os.environ.get("OUROBOROS_PER_TASK_COST_USD", "0") or 0
                )
                root_limit = configured_root_limit if configured_root_limit > 0 else None
            except (TypeError, ValueError):
                root_limit = None
        review_usage_scope = UsageScope(
            drive_root=budget_root,
            task_id=task_id,
            root_task_id=root_task_id,
            parent_task_id=str(usage_meta.get("parent_task_id") or base_scope.parent_task_id or ""),
            category=f"{request.surface}_review",
            source="review_substrate",
            global_limit_usd=global_limit,
            root_limit_usd=root_limit,
        )

        def _start_slot(slot: ReviewSlot) -> None:
            started_slots.append(slot)

            def _worker() -> None:
                try:
                    with usage_scope(review_usage_scope):
                        result_queue.put(self._run_slot(request, slot))
                except Exception as exc:
                    result_queue.put(self._error_actor(request, slot, f"{type(exc).__name__}: {exc}"))

            thread = threading.Thread(
                target=_worker,
                name=f"ouroboros-review-{request.surface}-{slot.slot_id}",
                daemon=True,
            )
            thread.start()

        for slot in slots:
            _start_slot(slot)

        actors: List[ReviewActorRecord] = []
        slot_timeout = max(0.001, max(float(slot.timeout_sec or 1) for slot in slots))
        deadline = time.monotonic() + slot_timeout
        while len(actors) < len(slots):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                actors.append(result_queue.get(timeout=remaining))
            except queue.Empty:
                break

        seen = {actor.slot_id for actor in actors}
        started_ids = {slot.slot_id for slot in started_slots}
        for slot in slots:
            if slot.slot_id not in seen:
                if slot.slot_id in started_ids:
                    actors.append(self._error_actor(request, slot, f"Timeout after {slot.timeout_sec:g}s"))
                else:
                    actors.append(self._error_actor(request, slot, "Not started before reviewer timeout budget expired"))
        slot_order = {slot.slot_id: idx for idx, slot in enumerate(slots)}
        slots_by_id = {slot.slot_id: slot for slot in slots}
        actors.sort(key=lambda actor: slot_order.get(actor.slot_id, len(slot_order)))
        try:
            # «Выполняется как» (D22): beside each saved row the UI shows what
            # the row REALLY ran as last time. Disclosure only; best-effort.
            from ouroboros.reviewer_slot_config import record_reviewer_slot_executions

            record_reviewer_slot_executions(request.surface, actors, slots_by_id)
        except Exception:
            log.debug("reviewer-slot last-execution write failed", exc_info=True)

        all_findings: List[Dict[str, Any]] = []
        # Split participation faults (a slot errored / timed out / returned empty)
        # from parse-degraded (a slot produced a DEGRADED verdict or unparseable
        # text). Only a participation fault fail-closes: a single Markdown/non-JSON
        # slot must NOT poison a clean quorum PASS (the old `degraded_reasons` gate
        # over-degraded honest 2-of-3 PASS reviews).
        actor_errors: List[str] = []
        parse_degraded: List[str] = []
        fail_count = 0
        pass_count = 0
        # When tier classification is required, the contract is ENFORCED before an
        # actor contributes to quorum. A tier-less PASS is non-responsive. A task-
        # acceptance FAIL contributes only when it carries a bounded correction rail;
        # a bare veto must not terminalize Required+Blocking with nothing to improve.
        classify_tier = bool(
            request.surface == "task_acceptance"
            and (request.policy or {}).get("classify_outcome_tier")
        )
        _valid_tiers = {"solved", "best_effort", "blocked_with_evidence"}
        # A SOLVED task-acceptance PASS need not carry a tier-up coach. Commit/scope
        # use distinct surfaces and retain their own hard-gate semantics.
        is_advisory = (
            request.surface == "task_acceptance"
            or str((request.policy or {}).get("hardness") or "") == HARDNESS_ADVISORY_VISIBLE
        )
        for actor in actors:
            if actor.status == "error":
                actor_errors.append(f"{actor.slot_id}:{actor.error}")
            elif actor.status != "ok":
                actor_errors.append(f"{actor.slot_id}:{actor.status}")
            parsed, findings, signal = parse_review_findings(actor.raw_text)
            actor.parsed = parsed
            actor.signal = signal
            slot = slots_by_id.get(actor.slot_id)
            actor.actor_role = (
                str(getattr(slot, "role_hint", "") or "").strip()
                or f"{request.surface} reviewer"
            )
            truth = _review_actor_projection(actor, request.surface)
            for key in (
                "model", "transport_status", "parse_status", "semantic_verdict", "provider",
                "coverage", "reason",
            ):
                setattr(actor, key, truth[key])
            all_findings.extend({**item, "slot_id": actor.slot_id, "model": actor.model} for item in findings)
            # The required-tier contract needs BOTH a valid outcome_tier AND a
            # non-empty completion_coach (both are required JSON keys); a PASS
            # missing either is non-responsive to the contract.
            _tier = str(parsed.get("outcome_tier") or "").strip().lower() if isinstance(parsed, dict) else ""
            _criteria = parsed.get("criteria_used") if isinstance(parsed, dict) else None
            _criteria_ok = _criteria_shape_valid(_criteria, _tier)
            contract_ok = (
                _tier in _valid_tiers
                and (
                    bool(str((parsed or {}).get("completion_coach") or "").strip())
                    # Advisory carve-out: a SOLVED deliverable has no tier-up step, so an
                    # empty coach must NOT demote it to DEGRADED.
                    or (is_advisory and _tier == "solved")
                )
                # Criteria shape rides the tier contract (its knob was constant-true, deleted).
                and _criteria_ok
            )
            if signal == "FAIL":
                # A task-acceptance FAIL is authoritative only when it obeys the
                # tier contract and carries a bounded correction rail.  A bare
                # veto cannot terminalize Required+Blocking with nothing the
                # agent can improve; keep the raw FAIL in parsed for forensics,
                # but make the actor abstain exactly like other contract failures.
                _has_concrete_finding = any(
                    isinstance(item, dict)
                    and bool(str(item.get("recommendation") or item.get("item") or "").strip())
                    for item in findings
                )
                _parsed_obj = parsed if isinstance(parsed, dict) else {}
                _has_correction_rail = (
                    bool(str(_parsed_obj.get("completion_coach") or "").strip())
                    or _has_concrete_finding
                    or _tier in {"best_effort", "blocked_with_evidence"}
                )
                if classify_tier and (
                    _tier not in _valid_tiers or not _has_correction_rail
                ):
                    parse_degraded.append(
                        f"{actor.slot_id}:fail_missing_tier_or_correction_rail"
                    )
                    actor.signal = "DEGRADED"
                    actor.parse_status = "malformed"
                    actor.semantic_verdict = ""
                    actor.reason = (
                        "Reviewer response violated the required outcome-tier or "
                        "correction-rail contract."
                    )
                else:
                    fail_count += 1
            elif signal == "PASS" and classify_tier and not contract_ok:
                parse_degraded.append(
                    f"{actor.slot_id}:missing_tier_coach_or_criterion_evidence"
                )
                # A contract-degraded PASS did NOT contribute to quorum, so its
                # recorded signal must be non-contributing too — else _contributing_
                # actors (and the objective-axis tier collector) would still let it
                # inject a tier/coach/finding (e.g. a PASS carrying a blocked tier +
                # empty coach) into the clean quorum capsule. Demote to DEGRADED;
                # the raw verdict stays in actor.parsed for forensics.
                actor.signal = "DEGRADED"
                actor.parse_status = "malformed"
                actor.semantic_verdict = ""
                actor.reason = (
                    "Reviewer response violated the required outcome-tier, coach, "
                    "or criterion-evidence contract."
                )
            elif signal == "PASS":
                pass_count += 1
            elif signal == "DEGRADED":
                parse_degraded.append(f"{actor.slot_id}:degraded")
        min_successful = max(1, int((request.policy or {}).get("min_successful_slots") or 1))
        fail_closed_on_errors = bool((request.policy or {}).get("fail_closed_on_errors"))
        degraded_reasons = actor_errors + parse_degraded
        # Task acceptance is conservative: any valid contributing FAIL vetoes.
        # DEGRADED/parse-failed actors abstain, while PASS still needs the adaptive
        # quorum supplied by the caller.  Commit/scope semantics remain unchanged.
        fail_threshold = 1
        if fail_count >= fail_threshold:
            aggregate = "FAIL"
        elif pass_count >= min_successful and not (
            fail_closed_on_errors and actor_errors and request.surface != "task_acceptance"
        ):
            aggregate = "PASS"
        else:
            aggregate = "DEGRADED"
            # Honest flag: DEGRADED must always carry a reason. Insufficient quorum
            # is itself the reason.
            if not degraded_reasons:
                degraded_reasons.append(
                    f"quorum_not_met: pass_count={pass_count} < min_successful={min_successful}"
                )
        # Bible P3 (centralized): a single configured slot is honored but the lost
        # cross-model diversity is recorded loudly + durably on EVERY surface that
        # runs through the coordinator, independent of the verdict (does NOT flip
        # the aggregate — block-vs-advisory still follows the caller's enforcement).
        # v6.74.0 (A6): the diversity note is an ORTHOGONAL LABEL — the typed
        # ``single_reviewer_no_diversity`` field below plus the projection label —
        # not a degraded_reason, so the panel ``reason`` names the real blocker
        # instead of leading every one-slot verdict with a diversity footnote.
        single_reviewer = len(slots) == 1
        participating_ids = {
            actor.slot_id
            for actor in actors
            if str(actor.signal or "").upper() in {"PASS", "FAIL"}
        }
        for actor in actors:
            actor.quorum_contribution = actor.slot_id in participating_ids
            if not actor.quorum_contribution:
                actor.enforcement_impact = "abstains"
            elif str(actor.signal or "").upper() == "FAIL":
                actor.enforcement_impact = "veto"
            else:
                actor.enforcement_impact = "supports_pass"
        return ReviewRunResult(
            request=asdict(request),
            actors=[asdict(actor) for actor in actors],
            parsed_findings=all_findings,
            # `degraded` tracks the aggregate so the review axis (which also reads
            # this flag) does not mark a quorum PASS as degraded over a single
            # parse-degraded slot.
            aggregate_signal=aggregate,
            degraded=(aggregate == "DEGRADED"),
            degraded_reasons=degraded_reasons,
            single_reviewer_no_diversity=single_reviewer,
            panel_id=_review_panel_id(request, actors),
        )

    def _custody_drive_root(self) -> pathlib.Path:
        """Where a DELEGATED slot's custody rows live: the canonical (budget)
        drive when the usage context names one, else this coordinator's drive.
        Data handed to the seam once, so the api_chat route never pays for it."""
        ctx = self.usage_ctx
        if ctx is not None and getattr(ctx, "drive_root", None):
            try:
                from ouroboros.delegate_custody import custody_root

                return custody_root(ctx)
            except Exception:
                log.debug("custody root resolution failed; using coordinator drive", exc_info=True)
        return self.drive_root

    def _error_actor(self, request: ReviewRequest, slot: ReviewSlot, error: str) -> ReviewActorRecord:
        call_id = new_call_id(f"review_{request.surface}_{slot.slot_id}_error")
        base_call_type = request.call_type or f"{request.surface}_review"
        assignment = ReviewAssignment(
            request=request, slot=slot, call_id=call_id, call_type=base_call_type,
            custody_root=self._custody_drive_root(),
        )
        # The synthetic prompt record is the route's own projection too: a slot
        # that never started must not build a pack its route would never send.
        # Best-effort by construction — this is the last-resort record for a slot
        # that already failed, so a route refusal (or an unrenderable prompt)
        # must degrade the record, never re-raise inside the failure path.
        try:
            prompt_projection = _review_route_executor(assignment, llm=self.llm).prompt_payload()
        except Exception:
            prompt_projection = {}
        prompt_ref: Dict[str, Any] = {}
        response_ref: Dict[str, Any] = {}
        try:
            prompt_ref = persist_call(
                self.drive_root,
                task_id=request.task_id or "review",
                call_id=f"{call_id}_prompt",
                call_type=f"{base_call_type}_prompt",
                payload={"request": asdict(request), "slot": asdict(slot), **prompt_projection},
                manifest={"surface": request.surface, "slot_id": slot.slot_id, "model": slot.model, "synthetic": True},
            )
        except Exception:
            prompt_ref = {}
        try:
            response_ref = persist_call(
                self.drive_root,
                task_id=request.task_id or "review",
                call_id=f"{call_id}_error",
                call_type=f"{base_call_type}_error",
                payload={"error": sanitize_tool_result_for_log(error)},
                manifest={"surface": request.surface, "slot_id": slot.slot_id, "model": slot.model, "status": "error", "synthetic": True},
            )
        except Exception:
            response_ref = {}
        return ReviewActorRecord(
            slot_id=slot.slot_id,
            model=slot.model,
            status="error",
            error=sanitize_tool_result_for_log(error),
            prompt_ref=prompt_ref,
            response_ref=response_ref,
        )

    def _run_slot(self, request: ReviewRequest, slot: ReviewSlot) -> ReviewActorRecord:
        call_id = new_call_id(f"review_{request.surface}_{slot.slot_id}")
        base_call_type = request.call_type or f"{request.surface}_review"
        assignment = ReviewAssignment(
            request=request, slot=slot, call_id=call_id, call_type=base_call_type,
            custody_root=self._custody_drive_root(),
        )
        # Transport is chosen once, here, through the seam; the prompt itself is
        # rendered by the route (lazily) rather than by this method, so a route
        # that does not send an API pack never assembles one.
        executor = _review_route_executor(assignment, llm=self.llm)
        prompt_projection = executor.prompt_payload()
        prompt_ref: Dict[str, Any] = {}
        response_ref: Dict[str, Any] = {}
        start = time.time()
        try:
            prompt_ref = persist_call(
                self.drive_root,
                task_id=request.task_id or "review",
                call_id=f"{call_id}_prompt",
                call_type=f"{base_call_type}_prompt",
                payload={"request": asdict(request), "slot": asdict(slot), **prompt_projection},
                manifest={"surface": request.surface, "slot_id": slot.slot_id, "model": slot.model},
            )
        except Exception:
            prompt_ref = {}
        if request.surface == "task_acceptance" and request.evidence.get("__immutable_core_overflow__"):
            raw_text = json.dumps({
                "verdict": "DEGRADED",
                "findings": [],
                "summary": (
                    "Immutable owner requirements do not fit the acceptance evidence "
                    "budget; no requirement was silently truncated."
                ),
            })
            try:
                response_ref = persist_call(
                    self.drive_root,
                    task_id=request.task_id or "review",
                    call_id=f"{call_id}_response",
                    call_type=f"{base_call_type}_response",
                    payload={"message": {"content": raw_text}, "usage": {}},
                    manifest={
                        "surface": request.surface, "slot_id": slot.slot_id,
                        "model": slot.model, "status": "degraded_core_overflow",
                        "physical_attempts": 0,
                    },
                )
            except Exception:
                response_ref = {}
            return ReviewActorRecord(
                slot_id=slot.slot_id,
                model=slot.model,
                status="ok",
                raw_text=raw_text,
                prompt_ref=prompt_ref,
                response_ref=response_ref,
                duration_sec=round(time.time() - start, 3),
            )
        try:
            p3_actor = request.surface in {"multi_model_review", "scope_review"}
            acceptance_actor = request.surface == "task_acceptance"
            actor_attempts = 2 if (p3_actor or acceptance_actor) else 1
            # Acceptance and P3 share the same two-physical-send rail. The
            # documented contract ("one substantive call and at most two
            # physical attempts total — same-route transport retry or
            # extraction/format repair") historically retried only empty/errored
            # responses; a MALFORMED non-empty acceptance response burned the
            # actor as DEGRADED without using its second permitted send. The
            # prompt, slot, and model never change on the repair resend.
            attempt_rail = (
                physical_attempt_limit(2)
                if acceptance_actor or p3_actor
                else contextlib.nullcontext()
            )
            with attempt_rail:
                _prior_msg, _prior_usage, _prior_text = None, None, ""
                for actor_attempt in range(actor_attempts):
                    try:
                        # The one seam. A null/non-object provider message comes
                        # back as empty raw_text: retry once on P3, then preserve
                        # the fail-closed empty actor.
                        attempt = _execute_slot_attempt(
                            assignment, llm=self.llm, executor=executor,
                        )
                        msg, usage, raw_text = attempt.message, attempt.usage, attempt.raw_text
                    except UsageAccountingError:
                        # Budget/ledger/physical-rail failures are not transport
                        # transients and must remain fail-closed without another
                        # send — but when the RAIL blocks the format-repair resend
                        # (the first send burned both physical attempts on an
                        # internal transport retry), keep the malformed first
                        # answer as forensics instead of degrading to a bare error.
                        if _prior_text:
                            msg, usage, raw_text = _prior_msg, _prior_usage, _prior_text
                            break
                        raise
                    except Exception:
                        if actor_attempt + 1 < actor_attempts:
                            continue
                        if _prior_text:
                            # The repair RESEND failed (transport, timeout): keep the
                            # malformed-but-substantive first answer as forensics.
                            msg, usage, raw_text = _prior_msg, _prior_usage, _prior_text
                            break
                        raise
                    if raw_text.strip():
                        if (
                            acceptance_actor
                            and actor_attempt + 1 < actor_attempts
                            and parse_review_findings(raw_text)[0] is None
                        ):
                            _prior_msg, _prior_usage, _prior_text = msg, usage, raw_text
                            try:
                                # P1 forensics: a successful repair must not make the
                                # malformed first answer unreconstructible.
                                persist_call(
                                    self.drive_root,
                                    task_id=request.task_id or "review",
                                    call_id=f"{call_id}_attempt1_response",
                                    call_type=f"{base_call_type}_attempt1_response",
                                    payload={"message": msg, "usage": usage},
                                    manifest={"surface": request.surface, "slot_id": slot.slot_id, "model": slot.model, "repair_attempt": 1},
                                )
                            except Exception:
                                log.warning(
                                    "Failed to persist malformed first acceptance attempt for %s/%s — the repair resend will overwrite it",
                                    request.surface, slot.slot_id, exc_info=True,
                                )
                            continue  # extraction/format repair: one same-route resend
                        break
                    if _prior_text:
                        # Empty repair resend: keep the substantive malformed first
                        # answer instead of degrading to an empty actor.
                        msg, usage, raw_text = _prior_msg, _prior_usage, _prior_text
                        break
                    if actor_attempt + 1 >= actor_attempts:
                        break
            self._emit_usage(request, slot, usage, prompt_chars=executor.prompt_chars())
            try:
                response_ref = persist_call(
                    self.drive_root,
                    task_id=request.task_id or "review",
                    call_id=f"{call_id}_response",
                    call_type=f"{base_call_type}_response",
                    payload={"message": msg, "usage": usage},
                    manifest={"surface": request.surface, "slot_id": slot.slot_id, "model": slot.model},
                )
            except Exception:
                response_ref = {}
            return ReviewActorRecord(
                slot_id=slot.slot_id,
                model=slot.model,
                status="ok" if raw_text.strip() else "empty",
                raw_text=raw_text,
                usage=usage,
                prompt_ref=prompt_ref,
                response_ref=response_ref,
                duration_sec=round(time.time() - start, 3),
            )
        except Exception as exc:
            error_msg = truncate_review_artifact(str(exc), limit=4000)
            try:
                response_ref = persist_call(
                    self.drive_root,
                    task_id=request.task_id or "review",
                    call_id=f"{call_id}_error",
                    call_type=f"{base_call_type}_error",
                    payload={
                        "error_type": type(exc).__name__,
                        "error": sanitize_tool_result_for_log(error_msg),
                    },
                    manifest={"surface": request.surface, "slot_id": slot.slot_id, "model": slot.model, "status": "error"},
                )
            except Exception:
                response_ref = {}
            return ReviewActorRecord(
                slot_id=slot.slot_id,
                model=slot.model,
                status="error",
                error=sanitize_tool_result_for_log(error_msg),
                transport_status=_transport_error_status(exc),
                prompt_ref=prompt_ref,
                response_ref=response_ref,
                duration_sec=round(time.time() - start, 3),
            )

    def _emit_usage(
        self,
        request: ReviewRequest,
        slot: ReviewSlot,
        usage: Dict[str, Any],
        *,
        prompt_chars: int = 0,
    ) -> None:
        if self.usage_ctx is None:
            return
        try:
            from ouroboros.tools.review_helpers import emit_review_usage

            emit_review_usage(
                self.usage_ctx,
                model=slot.model,
                usage=usage,
                source=f"review_substrate:{request.surface}",
                prompt_chars=prompt_chars,
                extra={"surface": request.surface, "slot_id": slot.slot_id},
            )
        except Exception:
            pass


def run_review_request(
    request: ReviewRequest,
    *,
    slots: List[ReviewSlot] | None = None,
    drive_root: pathlib.Path | None = None,
    llm: LLMClient | None = None,
    usage_ctx: Any = None,
) -> ReviewRunResult:
    coordinator = ReviewCoordinator(llm=llm, drive_root=drive_root, usage_ctx=usage_ctx)
    result = coordinator.run(request, reviewer_slots(role_hint=request.surface) if slots is None else slots)
    if request.surface == "task_acceptance":
        # D-Q5 annotation-only pass: feeds the clean bit + disclosure, never parse
        # validity/quorum/verdicts. Called UNGUARDED on purpose — the annotator is
        # total and fail-CLOSED (a resolver failure stamps the non-clean row), and
        # `review_evidence` already built this packet, so swallowing an error here
        # could only turn "the host never checked the refs" into a clean PASS.
        from ouroboros.review_evidence import annotate_criteria_evidence_resolution

        annotate_criteria_evidence_resolution(result.actors, request.evidence)
    return result
