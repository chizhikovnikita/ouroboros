"""Task pacing SSOT (v6.54.4): ONE urgency system for a task's time budget.

Absorbs the milestone CONTENT logic that lived inline in ``loop.py`` (deadline
50/25/10% TIME BUDGET notes and the v6.53.0 intrinsic no-deadline pacing) and
adds the acceptance-review budget layer: the finalization reserve, a budget
snapshot, and the improvement-pass gates driven by ``task_contract.budget_profile``
(``improvement_policy`` fixed | adaptive | until_deadline).

Design contract (owner-decided, sprint v6.55):
- Pacing notes fire only on milestone triggers, never per round (prompt-cache
  friendly), their wording is TASK-NEUTRAL, and note identification is by the
  checkpoint metadata — never a regex strip of transcript text.
- The gates are ADVISORY inputs to the host's review machinery; the model's own
  finalization stays P5 judgment. Forced-finalization escape hatches bypass the
  obligation gate unconditionally — a deadline never hangs on review passes.
- ``loop.py`` keeps only transport (message append + checkpoint emit); every
  threshold, text, and time computation lives here.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from ouroboros.config import (
    get_acceptance_max_improvement_passes,
    get_acceptance_reserve_pct,
    get_acceptance_review_est_sec,
    get_finalization_grace_sec,
    get_pacing_interval_sec,
)
from ouroboros.contracts.task_contract import answer_protocol_active, normalize_budget_profile
from ouroboros.deadline_utils import parse_deadline_ts, utc_now
from ouroboros.utils import append_jsonl, iter_jsonl_objects, utc_now_iso


_ACCEPTANCE_REVIEW_RESERVE_FLOOR_SEC = 200.0
_ACCEPTANCE_REVIEW_EWMA_ALPHA = 0.5
_ACCEPTANCE_TIMING_EVENT = "task_acceptance_review_timing"
log = logging.getLogger(__name__)


def _protocol_marker_phrases(ctx: Any) -> bool:
    """v6.60.0: the `FINAL ANSWER:` marker PHRASES in pacing notes are protocol-gated
    (contract ``answer_protocol="final_answer_line"``); the milestones themselves
    fire regardless. One SSOT gate shared with the loop nudges/context instruction."""
    try:
        return answer_protocol_active(ctx)
    except Exception:
        return False


@dataclass(frozen=True)
class PacingNote:
    """One milestone note: the user-turn text + its checkpoint event payload."""

    text: str
    checkpoint: Dict[str, Any]


@dataclass(frozen=True)
class BudgetSnapshot:
    """The task's time-budget facts at one moment (all seconds).

    ``has_deadline=False`` disables the time axis entirely: gates then bound
    improvement passes by COUNT only (default 1 = the historical behavior)."""

    has_deadline: bool
    total_sec: float = 0.0
    elapsed_sec: float = 0.0
    remaining_sec: float = 0.0
    reserve_sec: float = 0.0

    @property
    def inside_reserve(self) -> bool:
        return self.has_deadline and self.remaining_sec <= self.reserve_sec

    @property
    def spendable_sec(self) -> float:
        """Time available ABOVE the finalization reserve."""
        if not self.has_deadline:
            return float("inf")
        return self.remaining_sec - self.reserve_sec


def resolve_budget_profile(ctx: Any) -> Dict[str, Any]:
    """The task's normalized budget_profile (from task_contract; absent -> defaults)."""
    contract = getattr(ctx, "task_contract", None)
    if not isinstance(contract, dict):
        meta = getattr(ctx, "task_metadata", {})
        contract = meta.get("task_contract") if isinstance(meta, dict) else None
    profile = contract.get("budget_profile") if isinstance(contract, dict) else None
    if isinstance(profile, dict):
        legacy_keys = []
        if str(profile.get("improvement_policy") or "").strip().lower() == "until_deadline":
            legacy_keys.append("until_deadline")
        # Normalization materializes this field as ``None`` for every task.
        # Only a supplied value is a deprecated alias; defaults stay quiet.
        if profile.get("stall_rounds_threshold") is not None:
            legacy_keys.append("stall_rounds_threshold")
        if legacy_keys and not getattr(ctx, "_acceptance_pacing_deprecation_emitted", False):
            try:
                append_jsonl(
                    pathlib.Path(getattr(ctx, "drive_root")) / "logs" / "events.jsonl",
                    {
                        "ts": utc_now_iso(),
                        "type": "deprecated_task_pacing_alias",
                        "task_id": str(getattr(ctx, "task_id", "") or ""),
                        "aliases": legacy_keys,
                        "removal": "next_major",
                    },
                )
                ctx._acceptance_pacing_deprecation_emitted = True
            except Exception:
                log.warning(
                    "Failed to persist deprecated task-pacing aliases %s",
                    legacy_keys,
                    exc_info=True,
                )
    return normalize_budget_profile(profile)


def acceptance_review_estimate_sec(ctx: Any, *, passes_done: int = 0) -> float:
    """Return the review-time reservation reconstructed from existing events.

    The first review reserves 200 seconds.  Later reviews use
    ``max(200, 1.5 * EWMA)`` with alpha 0.5.  The existing
    ``OUROBOROS_ACCEPTANCE_REVIEW_EST_SEC`` value remains the initial estimate
    and a floor; no additional timing database is introduced.
    """
    configured = max(
        _ACCEPTANCE_REVIEW_RESERVE_FLOOR_SEC,
        float(get_acceptance_review_est_sec()),
    )
    if passes_done <= 0:
        return configured
    try:
        events_path = acceptance_timing_events_path(ctx)
    except (TypeError, OSError, ValueError):
        return configured
    ewma: Optional[float] = None
    for event in iter_jsonl_objects(
        events_path, max_entries=4000, tail_bytes=8_000_000,
    ):
        if str(event.get("type") or "") != _ACCEPTANCE_TIMING_EVENT:
            continue
        try:
            duration = float(event.get("duration_sec") or 0.0)
        except (TypeError, ValueError):
            continue
        if duration <= 0.0:
            continue
        ewma = duration if ewma is None else (
            _ACCEPTANCE_REVIEW_EWMA_ALPHA * duration
            + (1.0 - _ACCEPTANCE_REVIEW_EWMA_ALPHA) * ewma
        )
    if ewma is None:
        return configured
    return max(configured, 1.5 * ewma)


def acceptance_timing_events_path(ctx: Any) -> pathlib.Path:
    """Return the canonical event stream shared across split task drives."""
    metadata = getattr(ctx, "task_metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    root = pathlib.Path(str(
        metadata.get("budget_drive_root")
        or getattr(ctx, "budget_drive_root", "")
        or getattr(ctx, "drive_root")
    ))
    return root / "logs" / "events.jsonl"


def _reserve_sec(total_sec: float, profile: Dict[str, Any]) -> float:
    """Finalization reserve = max(grace window, reserve_pct × total)."""
    grace = float(get_finalization_grace_sec())
    pct = profile.get("reserve_finalization_pct")
    if pct is None:
        pct = get_acceptance_reserve_pct()
    return max(grace, total_sec * (float(pct) / 100.0))


def effective_finalization_reserve_sec(ctx: Any) -> float:
    """The EMIT window before a real deadline (v6.55.0): the plain finalization
    grace — the time needed to emit one best-effort answer / let a network tool
    return cleanly. Consumed by the local deadline-finalize trigger and the
    network-tool deadline clamp.

    This is deliberately the small GRACE window, NOT the percentage reserve
    (``BudgetSnapshot.reserve_sec`` = max(grace, pct×total)). The pct reserve is
    an acceptance-REVIEW gate concept (don't START a review you cannot finish);
    applying it to the finalize trigger amputated the tail of every long task —
    a 6h ProgramBench task force-finalized ~54 min early on the 15% profile
    (adversarial review r1). The finalize path fires just before the kill, so it
    needs only the emit window; the review gates keep the pct reserve via the
    snapshot. Restores the pre-sprint deadline-local behavior (monotonicity)."""
    return float(get_finalization_grace_sec())


def build_budget_snapshot(ctx: Any, *, profile: Optional[Dict[str, Any]] = None) -> BudgetSnapshot:
    """Snapshot the task's time budget from task_metadata deadline facts."""
    meta = getattr(ctx, "task_metadata", {})
    if not isinstance(meta, dict):
        return BudgetSnapshot(has_deadline=False)
    deadline = parse_deadline_ts(meta.get("deadline_at"))
    if deadline is None:
        return BudgetSnapshot(has_deadline=False)
    created = parse_deadline_ts(meta.get("created_at") or meta.get("started_at"))
    if created is None:
        created = getattr(ctx, "_time_budget_started_at", None)
        if created is None:
            # Latch the fallback anchor exactly like the note path does (fable-5
            # cumulative review F4): without the latch every metadata-poor
            # snapshot re-anchors total to "now" and the pct reserve silently
            # degrades toward the bare grace floor over the task's life.
            created = utc_now()
            try:
                ctx._time_budget_started_at = created
            except Exception:
                pass
    now = utc_now()
    total = max(1.0, (deadline - created).total_seconds())
    elapsed = max(0.0, (now - created).total_seconds())
    remaining = (deadline - now).total_seconds()
    reserve = _reserve_sec(total, profile if profile is not None else resolve_budget_profile(ctx))
    return BudgetSnapshot(
        has_deadline=True,
        total_sec=total,
        elapsed_sec=elapsed,
        remaining_sec=remaining,
        reserve_sec=reserve,
    )


def review_launch_allowed(
    snapshot: BudgetSnapshot, *, estimated_sec: Optional[float] = None,
) -> Tuple[bool, str]:
    """Gate 1: run an acceptance review only when it fits ABOVE the reserve.

    Historically a review could start two minutes before the deadline and kill
    the task; skipping inside the reserve is a strict improvement. No deadline →
    always allowed (the pass counter is the only axis)."""
    if not snapshot.has_deadline:
        return True, ""
    estimate = (
        float(estimated_sec)
        if estimated_sec is not None
        else max(_ACCEPTANCE_REVIEW_RESERVE_FLOOR_SEC, float(get_acceptance_review_est_sec()))
    )
    if snapshot.spendable_sec > estimate:
        return True, ""
    return False, "review_skipped_deadline_reserve"


def effective_max_improvement_passes(
    profile: Dict[str, Any], *, has_deadline: bool = True,
    required_blocking: bool = False,
) -> Optional[int]:
    """The COUNT axis for improvement passes.

    An explicit task-local cap always binds. Required+Blocking without one is
    intentionally unbounded by a *local count* (deadline and global lifecycle
    rails still apply). Other legacy policies retain their historical default."""
    cap = profile.get("max_improvement_passes")
    # An explicit task-local cap is authoritative under every policy, including
    # the one-minor ``until_deadline`` compatibility alias.
    if cap is not None:
        return max(0, int(cap))
    # Required+Blocking has no implicit local count cap.  Deadline, global task
    # rails and an explicitly supplied cap still stop it.
    if required_blocking:
        return None
    if profile.get("improvement_policy") == "until_deadline" and has_deadline:
        return None
    if cap is None:
        cap = get_acceptance_max_improvement_passes()
    return max(0, int(cap))


def improvement_pass_allowed(
    snapshot: BudgetSnapshot,
    passes_done: int,
    profile: Dict[str, Any],
    *,
    required_blocking: bool = False,
    estimated_sec: Optional[float] = None,
) -> Tuple[bool, str]:
    """Gate 2: one more improvement/obligation pass?

    An explicit count cap and the deadline/reserve rail are independent. For
    Required+Blocking with no explicit cap, only real system rails bound the
    loop; ``adaptive`` stops early once the spendable window can no longer fit a
    review comfortably (2× the estimate)."""
    cap = effective_max_improvement_passes(
        profile,
        has_deadline=snapshot.has_deadline,
        required_blocking=required_blocking,
    )
    if cap is not None and passes_done >= cap:
        return False, "improvement_passes_exhausted"
    if not snapshot.has_deadline:
        return True, ""
    est = (
        float(estimated_sec)
        if estimated_sec is not None
        else max(_ACCEPTANCE_REVIEW_RESERVE_FLOOR_SEC, float(get_acceptance_review_est_sec()))
    )
    needed = est * 2.0 if profile.get("improvement_policy") == "adaptive" else est
    if snapshot.spendable_sec > needed:
        return True, ""
    return False, "improvement_window_inside_reserve"


# ---------------------------------------------------------------------------
# Milestone note content (moved from loop.py; loop keeps only transport).

_TIME_BUDGET_THRESHOLDS = ((0.50, "50%"), (0.25, "25%"), (0.10, "10%"))

# Cost axis (v6.56.0): thresholds are module constants like the time axis —
# deliberately NOT settings keys (owner decision; the per-task knob is the
# contract's budget_profile.cost_hard_stop_pct, not a global).
_COST_BUDGET_THRESHOLDS = ((0.50, "50%"), (0.25, "25%"), (0.10, "10%"))
_COST_WRAPUP_SPENT_FRACTION = 0.80
# The historical in-task hard stop: half the budget remaining at task start.
_DEFAULT_COST_HARD_STOP_PCT = 50

# Typed cost-ceiling states (v6.91): ``None`` is deliberately NOT overloaded to
# mean both "unlimited" and "exhausted" — a $0.50 bench root cap under a $3
# planning margin must soft-land, never run uncapped.
COST_CEILING_DISABLED = "disabled"
COST_CEILING_ACTIVE = "active"
COST_CEILING_EXHAUSTED_SOFT_LAND = "exhausted_soft_land"
COST_CEILING_UNKNOWN = "unknown"

# Planning margin subtracted from the root cap before the graceful in-task stop:
# an ABSOLUTE emit-window's worth of money (~2 forced-wrap-up call reservation
# bounds), NEVER a percentage of the cap (a pct reserve amputated ~54 min from a
# 6h task — v6.54.4 adversarial r1, see effective_finalization_reserve_sec) and
# NOT ledger-held — the ledger fence still binds at the full cap; the margin
# only pulls the graceful stop earlier so the wrap-up call fits under the fence.
_WRAPUP_CALL_RESERVATION_BOUND_USD = 1.50
COST_PLANNING_MARGIN_USD = max(1.0, 2.0 * _WRAPUP_CALL_RESERVATION_BOUND_USD)


# Deciding-spend basis vocabulary (v6.91). The tree-accounted number is the
# authority whenever a root cap exists (the ledger fence counts the TREE); when
# it is momentarily unavailable the own-cost number still decides — but the
# substitution is DISCLOSED, never silent (BIBLE P1: represent the gap). Without
# a root cap there is no tree fence at all, so own cost is complete, not a
# fallback — the three states are kept distinct for exactly that reason.
SPEND_BASIS_TREE = "tree_accounted"
SPEND_BASIS_OWN_TREE_UNKNOWN = "own_fallback_tree_unknown"
SPEND_BASIS_OWN_NO_TREE_CAP = "own_only_no_tree_cap"


def resolve_deciding_spend(
    *,
    tree_cost_usd: Optional[float],
    task_cost_usd: Optional[float],
    root_cap_usd: Optional[float],
) -> Tuple[Optional[float], str]:
    """The spend that decides a cost surface, plus its DISCLOSED basis (SSOT).

    Shared by the loop's ceiling check and the milestone note so the stop and
    the nudge can never disagree about which number they are reading. Unknown
    spend stays None end-to-end — it is never coerced to $0."""
    if tree_cost_usd is not None:
        return float(tree_cost_usd), SPEND_BASIS_TREE
    deciding = None if task_cost_usd is None else float(task_cost_usd)
    if root_cap_usd is not None:
        return deciding, SPEND_BASIS_OWN_TREE_UNKNOWN
    return deciding, SPEND_BASIS_OWN_NO_TREE_CAP


@dataclass(frozen=True)
class CostCeiling:
    """Typed in-task cost-stop state, resolved ONCE at loop start.

    ``state``:
    - ``disabled``: no in-task stop — explicit ``cost_hard_stop_pct=0`` (bench
      contract, e.g. SWE-Pro) or no finite budget on either axis (e.g. GAIA);
      the whole cost axis stays silent.
    - ``active``: ``ceiling_usd`` is a strictly-positive graceful-stop point =
      min(pct-of-global-remaining, root_cap − planning margin).
    - ``exhausted_soft_land``: the root cap leaves no room above the planning
      margin — the loop must enter its graceful best-effort wrap-up
      immediately; it must NEVER run uncapped.
    - ``unknown``: resolution inputs errored; the axis stays silent but the
      gap is represented, never filled in (BIBLE P1)."""

    state: str
    ceiling_usd: Optional[float] = None
    root_cap_usd: Optional[float] = None
    planning_margin_usd: Optional[float] = None
    basis: str = ""


def resolve_cost_ceiling(
    budget_remaining_start_usd: Optional[float],
    profile: Dict[str, Any],
    *,
    root_cap_usd: Optional[float] = None,
) -> CostCeiling:
    """The in-task cost stop, computed ONCE at loop start (typed; v6.91).

    The GLOBAL component keeps the historical semantics: ``cost_hard_stop_pct``
    None -> 50% of the global remaining at task start; 0 -> the whole in-task
    stop is disabled (bench contract). The ROOT component is the per-task tree
    cap (``OUROBOROS_PER_TASK_COST_USD`` -> ``UsageScope.root_limit_usd``, the
    SAME value the ledger fence enforces) minus the ABSOLUTE planning margin —
    deliberately NOT pct-scaled (pct applies to the global axis only; scaling
    the owner's chosen cap would silently halve it). The ceiling is
    min(available components); NEVER a computed $0 — a root cap at or below the
    margin resolves to ``exhausted_soft_land`` instead.

    Stated plainly rather than implied: the ``room <= 0`` bail is the owner's
    "$0 ceiling" rule EXACTLY, no wider. A cap just ABOVE the margin therefore
    yields a real but tiny ceiling — ``root_cap_usd = COST_PLANNING_MARGIN_USD
    + 0.01`` gives ``ceiling_usd == 0.01``, which the first round's spend
    crosses — so a positive ``ceiling_usd`` is not by itself a promise of
    working room. Both numbers are disclosed on the carrier (``ceiling_usd``,
    ``root_cap_usd``, ``planning_margin_usd``) and printed in the stop text, so
    a reader sees the tiny ceiling instead of inferring a healthy one. Widening
    the bail into a minimum-room FLOOR would move caps the owner deliberately
    allows into immediate soft-land; that is an owner call, not a code one."""
    try:
        pct = profile.get("cost_hard_stop_pct")
        if pct is None:
            pct = _DEFAULT_COST_HARD_STOP_PCT
        pct = max(0, min(100, int(pct)))
        if pct == 0:
            return CostCeiling(
                state=COST_CEILING_DISABLED,
                root_cap_usd=(
                    float(root_cap_usd)
                    if root_cap_usd is not None and float(root_cap_usd) > 0
                    else None
                ),
                basis="cost_hard_stop_pct_zero",
            )
        components: list[float] = []
        basis_parts: list[str] = []
        if budget_remaining_start_usd is not None and float(budget_remaining_start_usd) > 0:
            components.append(float(budget_remaining_start_usd) * (pct / 100.0))
            basis_parts.append("global_pct")
        margin: Optional[float] = None
        cap: Optional[float] = None
        if root_cap_usd is not None and float(root_cap_usd) > 0:
            cap = float(root_cap_usd)
            margin = COST_PLANNING_MARGIN_USD
            room = cap - margin
            if room <= 0:
                return CostCeiling(
                    state=COST_CEILING_EXHAUSTED_SOFT_LAND,
                    root_cap_usd=cap,
                    planning_margin_usd=margin,
                    basis="root_cap_at_or_below_planning_margin",
                )
            components.append(room)
            basis_parts.append("root_cap_minus_margin")
        if not components:
            return CostCeiling(state=COST_CEILING_DISABLED, basis="no_finite_budget")
        return CostCeiling(
            state=COST_CEILING_ACTIVE,
            # Min of strictly-positive components, so never a computed $0 — but a
            # cap just above the margin makes this legitimately TINY (cap $3.01 ->
            # $0.01), which is a stop-after-the-first-spend ceiling, not headroom.
            # Documented in the docstring and pinned in test_budget_limits.
            ceiling_usd=min(components),
            root_cap_usd=cap,
            planning_margin_usd=margin,
            basis="min(" + ", ".join(basis_parts) + ")",
        )
    except Exception:
        log.warning("Cost ceiling resolution failed; axis stays silent", exc_info=True)
        return CostCeiling(state=COST_CEILING_UNKNOWN, basis="resolve_error")


def _cost_checkpoint(
    kind: str,
    *,
    deciding: float,
    task_cost: Optional[float],
    base: float,
    hard_stop: bool,
    spend_basis: str,
    **extra: Any,
) -> Dict[str, Any]:
    """The cost checkpoint payload, with ONE meaning per key name.

    ``task_cost_usd`` has meant THIS task's own accumulated cost since v6.56.0
    and still does — ``loop.py``'s ``_acceptance_loop_rails`` publishes the same
    name with the same meaning, and the rails line renders it as "$X spent this
    task". v6.91 briefly published the tree-accounted DECIDING number under that
    name, so the same key silently changed axis across a version boundary and
    every historical log reader would have been quietly re-pointed. The deciding
    number now rides its own honest name instead, and both are always present so
    no reader has to infer which axis a value came from (``spend_basis`` says
    which one the crossing used)."""
    return {
        "checkpoint_kind": kind,
        **extra,
        "deciding_spend_usd": round(float(deciding), 4),
        "task_cost_usd": round(float(task_cost), 4) if task_cost is not None else None,
        "base_usd": round(float(base), 4),
        "hard_stop": hard_stop,
        # Always present: a reader must be able to tell a tree number from an
        # own-cost stand-in without inferring it from a missing key.
        "spend_basis": spend_basis,
    }


def build_cost_budget_note(
    ctx: Any,
    *,
    start_remaining_usd: Optional[float],
    cost_ceiling_usd: Optional[float],
    task_cost: Optional[float],
    tree_cost_usd: Optional[float] = None,
    root_cap_usd: Optional[float] = None,
) -> Optional[PacingNote]:
    """Cost milestone note at 50/25/10% of the in-task cost budget remaining,
    plus a one-shot wrap-up note at ~80% spent. Fires only on crossings (never
    per round — prompt-cache friendly), latched on ctx like the time axis.

    The reference base is the hard-stop ceiling when one exists, else the
    budget remaining at task start (the informational base for
    ``cost_hard_stop_pct=0`` runs). ``start_remaining_usd`` None (no finite
    budget) keeps the axis silent. ADVISORY only — the hard stop itself lives
    in the loop's budget gate, not here (P5).

    ``tree_cost_usd`` (v6.91) is the root subtree's ledger-accounted spend
    (settled + reserved + unresolved holds, subagents included) — when known it
    is the DECIDING spend, because the ledger fence counts the tree, not this
    task's own calls (waves died at tree $84-94 while own showed $41-49).
    ``task_cost`` (own accumulated cost) stays the diagnostic line. Unknown tree
    spend falls back to own cost — never coerced to $0, and never SILENTLY
    substituted: under a ``root_cap_usd`` the fallback is a lower bound and the
    note says so (basis vocabulary in ``resolve_deciding_spend``)."""
    base = cost_ceiling_usd if cost_ceiling_usd is not None else start_remaining_usd
    deciding, spend_basis = resolve_deciding_spend(
        tree_cost_usd=tree_cost_usd, task_cost_usd=task_cost, root_cap_usd=root_cap_usd,
    )
    if base is None or base <= 0 or deciding is None:
        return None
    tree_basis = spend_basis == SPEND_BASIS_TREE
    spent_fraction = max(0.0, float(deciding)) / base
    fraction_remaining = max(0.0, 1.0 - spent_fraction)
    seen = getattr(ctx, "_cost_budget_milestones_seen", None)
    if not isinstance(seen, set):
        seen = set()
        ctx._cost_budget_milestones_seen = seen
    crossed = [(value, label) for value, label in _COST_BUDGET_THRESHOLDS if fraction_remaining <= value]
    unseen_crossed = [(value, label) for value, label in crossed if label not in seen]
    hard_stop = cost_ceiling_usd is not None
    base_kind = "in-task cost ceiling" if hard_stop else "start-of-task budget snapshot (no in-task cost stop)"
    if tree_basis:
        own_text = f"; own calls ~${task_cost:.2f}" if task_cost is not None else ""
        spent_line = (
            f"Spent this task tree: ~${deciding:.2f} "
            f"(ledger-accounted incl. in-flight holds, subagents included{own_text})"
        )
    elif spend_basis == SPEND_BASIS_OWN_TREE_UNKNOWN:
        spent_line = (
            f"Spent this task: ~${deciding:.2f} (OWN calls only — the tree-accounted "
            "total is unavailable right now, so subagent spend is NOT included; treat "
            "this as a lower bound against the tree cap)"
        )
    else:
        spent_line = f"Spent this task: ~${deciding:.2f}"
    if unseen_crossed:
        selected_label = unseen_crossed[-1][1]  # thresholds are coarse→fine
        for _value, label in crossed:
            seen.add(label)
        _late = spent_fraction >= _COST_WRAPUP_SPENT_FRACTION
        if _late:
            # The tightest milestones already carry the convergence call; a
            # separate wrap-up note right after would be pure duplication.
            ctx._cost_wrapup_seen = True
        # v6.74.4: a fast-spending workspace task can hit its FIRST cost note
        # already past the wrap-up fraction — that suppressed the wrap-up (the
        # only cost text with the tree sentence), so the late milestone itself
        # must carry it (commit triad r2, sol advisory).
        _tree_tail = (
            _TREE_FLUSH_SENTENCE if _late and _workspace_delivery(ctx) else ""
        )
        text = (
            f"[COST BUDGET — {selected_label} remaining crossed]\n"
            f"{spent_line} | Remaining: ~${max(0.0, base - deciding):.2f} "
            f"of ~${base:.2f} ({base_kind})\n"
            "Use this as planning context, not as a command to stop. Prefer the shortest path "
            "to a verifiable result; if a passing artifact or service already exists, prefer "
            "preserving and verifying it over speculative improvements." + _tree_tail
        )
        checkpoint = _cost_checkpoint(
            "cost_budget_milestone", deciding=deciding, task_cost=task_cost,
            base=base, hard_stop=hard_stop, spend_basis=spend_basis,
            milestone=selected_label,
        )
        return PacingNote(text=text, checkpoint=checkpoint)
    if spent_fraction >= _COST_WRAPUP_SPENT_FRACTION and not getattr(ctx, "_cost_wrapup_seen", False):
        ctx._cost_wrapup_seen = True
        # v6.60.0: the marker PHRASE is protocol-gated (the milestone itself is not).
        _marker_tail = (
            " If the task expects a short answer, record your current best "
            "with a `FINAL ANSWER:` line so it stays salvageable."
            if _protocol_marker_phrases(ctx) else ""
        )
        _tree_tail = _TREE_FLUSH_SENTENCE if _workspace_delivery(ctx) else ""
        if tree_basis:
            _tree_amount = f"tree-accounted ~${deciding:.2f} of ~${base:.2f}"
        elif spend_basis == SPEND_BASIS_OWN_TREE_UNKNOWN:
            _tree_amount = (
                f"~${deciding:.2f} of ~${base:.2f}, counting OWN calls only — the "
                "tree-accounted total is unavailable right now, so this is a lower bound"
            )
        else:
            _tree_amount = f"~${deciding:.2f} of ~${base:.2f}"
        text = (
            f"[COST BUDGET — wrap-up]\n"
            f"~{spent_fraction * 100:.0f}% of the {base_kind} is spent "
            f"({_tree_amount}).\n"
            "Start converging: prefer completing and verifying the current best path over "
            "opening new ones." + _tree_tail + _marker_tail
        )
        checkpoint = _cost_checkpoint(
            "cost_budget_wrapup", deciding=deciding, task_cost=task_cost,
            base=base, hard_stop=hard_stop, spend_basis=spend_basis,
        )
        return PacingNote(text=text, checkpoint=checkpoint)
    return None


# v6.74.4: one commit-neutral tree sentence shared by every pacing axis that
# can end a workspace task (time flush, cost wrap-up) — commit-neutral because
# it reaches acting self_worktree subagents, where git commits are blocked and
# a moved HEAD fails patch capture closed.
_TREE_FLUSH_SENTENCE = (
    " Your working tree ships as-is: leave it in a verified, building "
    "state — revert unverified edits rather than leaving them in the tree."
)


def _workspace_delivery(ctx: Any) -> bool:
    """True when the task's deliverable is a workspace tree. Prefers the
    canonical ``ToolContext.is_workspace_mode()`` authority (registry.py);
    falls back to the raw attribute for lightweight test contexts."""
    probe = getattr(ctx, "is_workspace_mode", None)
    if callable(probe):
        try:
            return bool(probe())
        except Exception:
            return False
    return bool(getattr(ctx, "workspace_root", None))


def build_time_budget_note(
    ctx: Any,
    *,
    round_idx: int = 0,
    accumulated_usage: Optional[Dict[str, Any]] = None,
    tree_cost_provider: Optional[Any] = None,
) -> Optional[PacingNote]:
    """Deadline-aware milestone note at 50/25/10% remaining, never per-round.

    With no deadline_at (headless/benchmark runs), falls back to intrinsic
    self-pacing. Both are ADVISORY — the model judges when to finalize; neither
    is a deterministic stop gate (P5). Milestone state rides ctx attributes so a
    note fires at most once per threshold."""
    meta = getattr(ctx, "task_metadata", {})
    if not isinstance(meta, dict):
        return None
    created = parse_deadline_ts(meta.get("created_at") or meta.get("started_at"))
    if created is None:
        created = getattr(ctx, "_time_budget_started_at", None)
        if created is None:
            created = utc_now()
            ctx._time_budget_started_at = created
    now = utc_now()
    deadline = parse_deadline_ts(meta.get("deadline_at"))
    if deadline is None:
        return build_intrinsic_pacing_note(
            ctx, created=created, now=now, round_idx=round_idx, accumulated_usage=accumulated_usage,
            tree_cost_provider=tree_cost_provider,
        )
    total = max(1.0, (deadline - created).total_seconds())
    remaining = (deadline - now).total_seconds()
    fraction_remaining = 0.0 if remaining <= 0 else remaining / total
    seen = getattr(ctx, "_time_budget_milestones_seen", None)
    if not isinstance(seen, set):
        seen = set()
        ctx._time_budget_milestones_seen = seen
    # Fire the TIGHTEST crossed milestone, not the coarsest: a task starting
    # already past 50% remaining must announce the real urgency immediately.
    crossed = [(value, label) for value, label in _TIME_BUDGET_THRESHOLDS if fraction_remaining <= value]
    unseen_crossed = [(value, label) for value, label in crossed if label not in seen]
    if not unseen_crossed:
        return None
    selected_label = unseen_crossed[-1][1]  # thresholds are coarse→fine
    for _value, label in crossed:
        seen.add(label)
    elapsed = max(0.0, (now - created).total_seconds())
    remaining_clamped = max(0.0, remaining)
    deadline_text = deadline.isoformat().replace("+00:00", "Z")
    # M4 deadline-flush at the tightest milestone: prompt for a salvageable,
    # grounded deliverable before the hard cutoff. Prompt-only; forced
    # finalization is untouched.
    _marker_flush = (
        " If the task expects a short answer, ALSO end your response with a single line, "
        "exactly: FINAL ANSWER: <answer> — so a salvageable answer is captured before the cutoff."
        if _protocol_marker_phrases(ctx) else ""
    )
    # v6.74.4 (time axis of the freeze directive): for workspace deliverables
    # the tree ships as-is, so the flush must also protect the TREE state.
    _tree_flush = _TREE_FLUSH_SENTENCE if _workspace_delivery(ctx) else ""
    flush_clause = (
        " You are near the hard cutoff: WRITE your best current deliverable now "
        "(write_file/edit_text) and run ONE cheap verify_and_record on it, so a "
        "salvageable, grounded result is in place before the deadline."
        + _tree_flush + _marker_flush
        if selected_label == "10%" else ""
    )
    text = (
        f"[TIME BUDGET — {selected_label} remaining crossed]\n"
        f"Elapsed: ~{elapsed/60:.1f} min | Remaining: ~{remaining_clamped/60:.1f} min | "
        f"Deadline: {deadline_text}\n"
        "Use this as planning context, not as a command to stop. If a passing artifact "
        "or service already exists, prefer preserving and verifying it over speculative "
        "improvements. If not, focus on the shortest path to a verifiable result."
        + flush_clause
    )
    return PacingNote(text=text, checkpoint={
        "checkpoint_kind": "time_budget_milestone",
        "milestone": selected_label,
        "elapsed_sec": round(elapsed, 3),
        "remaining_sec": round(remaining_clamped, 3),
        "deadline_at": deadline_text,
    })


def build_intrinsic_pacing_note(
    ctx: Any,
    *,
    created,
    now,
    round_idx: int,
    accumulated_usage: Optional[Dict[str, Any]],
    tree_cost_provider: Optional[Any] = None,
) -> Optional[PacingNote]:
    """No deadline: surface the agent's OWN elapsed / rounds / cost periodically.

    ADVISORY only — awareness so the one mind can choose to wrap up; deliberately
    no deterministic time/round/cost stop (finalization stays P5 judgment).

    ``tree_cost_provider`` (v6.91): a zero-arg callable returning the root
    subtree's accounting snapshot (``{"accounted_usd", "root_limit_usd"}`` or
    None). Called ONLY when the note actually fires (a rare, already
    cache-breaking surface — never per round), so a fresh ledger read at most
    once per pacing interval keeps the number honest after long child waits.
    Unknown stays "unknown", never $0."""
    interval = get_pacing_interval_sec()
    if interval <= 0:
        return None
    elapsed = max(0.0, (now - created).total_seconds())
    bucket = int(elapsed // interval)
    if bucket <= 0:
        return None
    last_bucket = getattr(ctx, "_pacing_bucket_seen", 0)
    if bucket <= last_bucket:
        return None
    ctx._pacing_bucket_seen = bucket
    raw_cost = (accumulated_usage or {}).get("cost")
    cost = float(raw_cost) if raw_cost is not None else None
    cost_text = f"~${cost:.2f}" if cost is not None else "unknown"
    tree_line = ""
    tree_accounted: Optional[float] = None
    tree_cap: Optional[float] = None
    if callable(tree_cost_provider):
        try:
            tree_info = tree_cost_provider()
        except Exception:
            tree_info = None
        if isinstance(tree_info, dict) and tree_info.get("accounted_usd") is not None:
            tree_accounted = float(tree_info["accounted_usd"])
            raw_cap = tree_info.get("root_limit_usd")
            tree_cap = float(raw_cap) if raw_cap is not None else None
            cap_text = f" of ${tree_cap:.2f} tree cap" if tree_cap is not None else ""
            tree_line = (
                f" | Task tree spend: ~${tree_accounted:.2f}{cap_text} "
                "(ledger-accounted incl. in-flight holds, subagents included)"
            )
    _marker_tail = (
        " If you have a current best short answer, record it with a `FINAL ANSWER:` line "
        "before continuing so it remains salvageable if later work stalls."
        if _protocol_marker_phrases(ctx) else ""
    )
    text = (
        f"[PACING — ~{elapsed/60:.0f} min elapsed]\n"
        f"Rounds so far: {round_idx} | Elapsed: ~{elapsed/60:.1f} min | Cost so far: {cost_text}"
        f"{tree_line}\n"
        "Planning context, not a command to stop. Periodically confirm you are still on the "
        "shortest path to a verifiable result; if a passing artifact or service already exists, "
        "prefer preserving and verifying it over speculative improvements." + _marker_tail
    )
    checkpoint = {
        "checkpoint_kind": "intrinsic_pacing",
        "elapsed_sec": round(elapsed, 3),
        "rounds": int(round_idx),
        "cost": round(cost, 4) if cost is not None else None,
    }
    if tree_accounted is not None:
        checkpoint["tree_accounted_usd"] = round(tree_accounted, 4)
        checkpoint["tree_cap_usd"] = round(tree_cap, 4) if tree_cap is not None else None
    return PacingNote(text=text, checkpoint=checkpoint)
