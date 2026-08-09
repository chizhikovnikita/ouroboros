"""Task-local context-fit projections for the ordinary Main model path.

This module is deliberately data-only around the existing context builder and
capability-evidence SSOT.  It does not own routing, provider retries, or global
context-mode state; callers supply the captured context core and exact-route
resolver so ``ouroboros.context`` remains the public compatibility surface.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pathlib
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

from ouroboros.context_layout import reference_doc_sections
from ouroboros.utils import estimate_tokens, iter_jsonl_objects

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContextFitProjection:
    """One deterministic Low/Max rendering of a shared immutable context core."""

    mode: str
    system_content_json: str
    estimated_tokens: int
    calibrated_tokens: int
    calibration_ratio: float
    fits_known_window: Optional[bool]

    def system_message(self) -> Dict[str, Any]:
        return {"role": "system", "content": json.loads(self.system_content_json)}


@dataclass(frozen=True)
class ContextFitPlan:
    """Task-local context fit decision for the ordinary Main agent path.

    The two projections are rendered from the same captured core.  The plan is
    deliberately data-only: it neither owns provider routing nor changes the
    owner-selected global context mode.  P3 review calls do not use this path.
    """

    core_sha256: str
    preferred_mode: str
    initial_mode: str
    model: str
    provider: str
    route_fp: str
    # Named for ``capability_evidence.CapabilityEvidence``, not paraphrased: the plan
    # is an evidence-shaped record, so ``is_known`` applies to it DIRECTLY instead of
    # each reader restating the three-clause freshness boolean (the wire keys stay
    # ``evidence_status`` / ``evidence_stale``).
    status: str
    stale: bool
    window_tokens: int
    output_reserve_tokens: int
    user_content_json: str
    max_projection: ContextFitProjection
    low_projection: ContextFitProjection

    def projection(self, mode: str) -> ContextFitProjection:
        return self.low_projection if str(mode or "").lower() == "low" else self.max_projection

    def messages_for(self, mode: str) -> List[Dict[str, Any]]:
        projection = self.projection(mode)
        return [
            projection.system_message(),
            {"role": "user", "content": json.loads(self.user_content_json)},
        ]

    def reproject_transcript(
        self,
        messages: List[Dict[str, Any]],
        mode: str,
    ) -> List[Dict[str, Any]]:
        """Replace only the captured system view; preserve every dialogue/tool turn."""
        if not messages:
            return self.messages_for(mode)
        rebuilt = list(messages)
        if str(rebuilt[0].get("role") or "") == "system":
            rebuilt[0] = self.projection(mode).system_message()
        else:
            rebuilt.insert(0, self.projection(mode).system_message())
        return rebuilt

    def projected_tokens_with_tools(
        self,
        mode: str,
        tools: Optional[List[Dict[str, Any]]],
    ) -> int:
        """Calibrated physical prompt projection after schemas are available."""
        projection = self.projection(mode)
        return int(
            (projection.estimated_tokens + tool_schema_tokens(tools))
            * projection.calibration_ratio
        )

    def initial_mode_with_tools(self, tools: Optional[List[Dict[str, Any]]]) -> str:
        from ouroboros.capability_evidence import is_known

        if self.preferred_mode != "max" or not is_known(self, require_fresh=True):
            return self.initial_mode
        projected = self.projected_tokens_with_tools("max", tools)
        return (
            "low"
            if projected + self.output_reserve_tokens > self.window_tokens
            else self.initial_mode
        )


@dataclass(frozen=True)
class ContextCore:
    """Single captured context source rendered into deterministic projections."""

    base_prompt: str
    bible_md: str
    architecture_md: str
    development_md: str
    semi_stable_text: str
    dynamic_text: str
    user_content_json: str
    docs_need_development: bool


def _render_context_system_content(
    env: Any,
    core: ContextCore,
    *,
    mode: str,
) -> List[Dict[str, Any]]:
    # D-ARCH (owner, 2026-08-08): the reference-doc form follows the RENDERED
    # mode directly — ARCHITECTURE is full in max for every task class and the
    # nav map in low; DEVELOPMENT inclusion is the caller's mode-independent
    # decision carried on the core (the former per-task force_low_docs lever is
    # gone: workspace binding no longer shapes the docs).
    static_parts = [core.base_prompt, "## BIBLE.md\n\n" + core.bible_md]
    static_parts.extend(
        reference_doc_sections(
            env,
            context_mode=mode,
            include_development=core.docs_need_development,
            architecture_text=core.architecture_md,
            development_text=core.development_md,
        )
    )
    # Stable governance/policy is first; mutable task evidence is last.  This is
    # the cache-friendly ordering recommended by both supported cache routes.
    return [
        {
            "type": "text",
            "text": "\n\n".join(static_parts),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": core.semi_stable_text,
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": core.dynamic_text},
    ]


def tool_schema_tokens(tools: Optional[List[Dict[str, Any]]]) -> int:
    """chars/4 estimate of the TOOL-SCHEMA segment of a prompt.

    One seam for every consumer that has to account for the schemas: they are sent
    on the wire beside ``messages``, so any measure built from the transcript alone
    silently omits them (~148K chars / ~37K tokens on the submarine traces — enough
    to make an emergency-compaction trigger fire a whole tool envelope late).
    """
    if not tools:
        return 0
    return estimate_tokens(json.dumps(tools, ensure_ascii=False, sort_keys=True, default=str))


def estimate_context_prompt_tokens(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """Existing chars/4 estimate, including tools and a bounded image proxy."""
    from ouroboros.context_budget import IMAGE_BLOCK_CHAR_EQUIVALENT

    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    total += estimate_tokens(str(block))
                elif str(block.get("type") or "") in {"image", "image_url"}:
                    total += max(1, IMAGE_BLOCK_CHAR_EQUIVALENT // 4)
                else:
                    total += estimate_tokens(str(block.get("text", "")))
            total += 6
        else:
            total += estimate_tokens(str(content)) + 6
        if msg.get("tool_calls"):
            total += estimate_tokens(
                json.dumps(msg["tool_calls"], ensure_ascii=False, default=str)
            )
    total += tool_schema_tokens(tools)
    return max(0, int(total))


def main_loop_token_density(drive_root: Any, model: str) -> float:
    """MAIN-LOOP calibrated token density: neutral 1.0 cold, measured supersedes.

    The baseline `_route_calibration_ratio` starts from, exposed as its own SSOT so
    the emergency-compaction necessity trigger and the fit projections share ONE
    policy. DELIBERATELY NOT ``capability_evidence.resolve_token_density`` — that is
    the review-pack COLD-CONSERVATIVE value, which on an empty observation store
    (every fresh install and isolated benchmark server) would silently narrow the
    main loop's horizon on a guess (the v6.80.0 → v6.81.0 oscillation; BIBLE P1).
    Only a MEASURED density for this exact model identity may raise it above 1.0.
    """
    baseline = 1.0
    try:
        from ouroboros.capability_evidence import get_token_density
        from ouroboros.provider_models import normalize_model_identity

        measured = get_token_density(drive_root, normalize_model_identity(str(model or "")))
        if measured > 0:
            baseline = measured
    except Exception:
        log.debug("Measured token density unavailable", exc_info=True)
    return baseline


def _route_calibration_ratio(
    drive_root: pathlib.Path,
    route_fp: str,
    model: str,
) -> float:
    """Measured model baseline plus successful exact-route observations.

    DELIBERATELY NOT the cold-conservative review-pack density (v6.80.0): with an
    empty observation store — every fresh install and EVERY isolated benchmark server
    — a cold cross-model density would silently demote ``initial_mode`` from Max to
    Low for the main loop. Unknown routes deliberately try Max (BIBLE P1: the horizon
    is not narrowed by a guess). Only MEASURED knowledge about this exact model, plus
    exact-route observations, may raise the baseline above 1.0.

    What actually happens on a genuine overflow (verified, not assumed): the round
    FAILS. ``loop_llm_call`` classifies it as ``context_overflow`` with
    ``retry_same_request=False``, marks the usage ``execution_status="infra_failed" /
    reason_code="llm_api_error"``, and emits the one-time
    ``context_overflow_suggest_low`` owner hint — it does NOT rebuild anything. The
    ONE-SHOT recovery lives one level up in ``loop.py``: after that failed round, a
    confirmed ``context_overflow`` on a ``preferred_mode="max"`` plan for the same
    model persists a forensic pre-retry checkpoint and reprojects the transcript into
    task-local Low exactly once (guarded by ``_context_fit_low_retry_used``), then
    retries. So the residual cost of the neutral cold baseline is bounded to ONE failed
    round per task, and only when the very first prompt already exceeds the window on a
    fresh evidence store; the first successful send records this model's density, after
    which the projection is measured rather than guessed.
    """
    ratios = [float(main_loop_token_density(drive_root, model))]
    try:
        events_path = pathlib.Path(drive_root) / "logs" / "events.jsonl"
        for event in iter_jsonl_objects(
            events_path,
            max_entries=200,
            tail_bytes=2_000_000,
        ):
            if event.get("type") != "llm_round":
                continue
            if str(event.get("context_route_fp") or "") != str(route_fp or ""):
                continue
            estimated = int(event.get("estimated_prompt_tokens") or 0)
            actual = int(event.get("prompt_tokens") or 0)
            if estimated > 0 and actual > 0:
                ratio = actual / estimated
                if 0.5 <= ratio <= 4.0:
                    ratios.append(ratio)
    except Exception:
        log.debug("Failed to read route token calibration", exc_info=True)
    return max(ratios)


def resolve_context_fit_route(
    task: Dict[str, Any],
    *,
    allow_fetch: bool,
) -> Tuple[Dict[str, Any], Any]:
    """Resolve one exact route through the existing settings/evidence SSOT."""
    from ouroboros.capability_evidence import probe
    from ouroboros.config import DATA_DIR
    from ouroboros.gateway.settings import _active_main_route, _owner_read_settings_raw

    settings = _owner_read_settings_raw()
    model = str(task.get("model") or "").strip()
    local_override = task.get("use_local_model")
    route = _active_main_route(
        settings,
        model_override=model,
        use_local_override=(
            bool(local_override) if local_override is not None else None
        ),
    )
    evidence = probe(
        DATA_DIR,
        provider=route["provider"],
        model=route["model"],
        base_url=route["base_url"],
        use_local=route["use_local"],
        allow_fetch=allow_fetch,
    )
    return route, evidence


def _failed_route_evidence(task: Dict[str, Any]) -> Tuple[Dict[str, Any], Any]:
    from ouroboros.capability_evidence import route_fingerprint
    from ouroboros.gateway.settings import _active_main_route, _owner_read_settings_raw

    route = _active_main_route(
        _owner_read_settings_raw(),
        model_override=str(task.get("model") or ""),
        use_local_override=(
            bool(task.get("use_local_model"))
            if task.get("use_local_model") is not None
            else None
        ),
    )
    evidence = SimpleNamespace(
        route_fp=route_fingerprint(
            provider=route["provider"],
            base_url=route["base_url"],
            model=route["model"],
        ),
        status="failed",
        stale=True,
        window_tokens=0,
    )
    return route, evidence


def build_context_fit_plan(
    env: Any,
    core: ContextCore,
    task: Dict[str, Any],
    *,
    preferred_mode: str,
    route_resolver: Callable[..., Tuple[Dict[str, Any], Any]],
) -> ContextFitPlan:
    """Deterministically project one captured core into ordinary-task Max and Low."""
    preferred = str(preferred_mode or "max").strip().lower()
    if preferred not in {"low", "max"}:
        preferred = "max"

    meta = task.get("task_metadata") if isinstance(task.get("task_metadata"), dict) else {}
    is_subagent = str(
        task.get("delegation_role") or meta.get("delegation_role") or ""
    ).strip().lower() == "subagent"
    try:
        route, evidence = route_resolver(task, allow_fetch=not is_subagent)
    except Exception:
        log.debug("Context-fit route evidence unavailable; preserving Max", exc_info=True)
        route, evidence = _failed_route_evidence(task)

    user_content = json.loads(core.user_content_json)
    # Keep the fit projection tied to the physical dispatch contract instead of
    # duplicating its output reservation.  The lazy import avoids coupling the
    # data-only fit representation to the high-level model loop.
    from ouroboros.capability_evidence import is_known
    from ouroboros.loop_llm_call import MAIN_LOOP_MAX_TOKENS

    output_reserve = MAIN_LOOP_MAX_TOKENS
    ratio = _route_calibration_ratio(
        pathlib.Path(env.drive_root),
        str(evidence.route_fp or ""),
        str(route["model"] or ""),
    )
    known_window = is_known(evidence, require_fresh=True)

    def _projection(mode: str) -> ContextFitProjection:
        system_content = _render_context_system_content(env, core, mode=mode)
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]
        estimated = estimate_context_prompt_tokens(messages)
        calibrated = int(estimated * ratio)
        fits = (
            calibrated + output_reserve <= int(evidence.window_tokens or 0)
            if known_window
            else None
        )
        return ContextFitProjection(
            mode=mode,
            system_content_json=json.dumps(
                system_content,
                ensure_ascii=False,
                sort_keys=True,
            ),
            estimated_tokens=estimated,
            calibrated_tokens=calibrated,
            calibration_ratio=ratio,
            fits_known_window=fits,
        )

    max_projection = _projection("max")
    low_projection = _projection("low")
    # Unknown routes deliberately try Max.  Only positive exact-route evidence
    # that the captured Max projection cannot fit selects Low before dispatch.
    initial_mode = preferred
    if preferred == "max" and max_projection.fits_known_window is False:
        initial_mode = "low"

    core_payload = json.dumps(
        {
            "base_prompt": core.base_prompt,
            "bible_md": core.bible_md,
            "architecture_md": core.architecture_md,
            "development_md": core.development_md,
            "semi_stable_text": core.semi_stable_text,
            "dynamic_text": core.dynamic_text,
            "user_content": user_content,
            "docs_need_development": core.docs_need_development,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return ContextFitPlan(
        core_sha256=hashlib.sha256(core_payload.encode("utf-8")).hexdigest(),
        preferred_mode=preferred,
        initial_mode=initial_mode,
        model=str(route["model"] or ""),
        provider=str(route["provider"] or ""),
        route_fp=str(evidence.route_fp or ""),
        status=str(evidence.status or ""),
        stale=bool(evidence.stale),
        window_tokens=int(evidence.window_tokens or 0),
        output_reserve_tokens=output_reserve,
        user_content_json=core.user_content_json,
        max_projection=max_projection,
        low_projection=low_projection,
    )
