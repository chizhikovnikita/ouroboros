"""Nanny tools: run a subagent's cognition on an already-paid subscription session.

A delegated subagent is an ORDINARY Ouroboros subagent acting as a NANNY: it lives in
the task tree with its own deadline and authority, but instead of thinking on metered
API tokens it starts a Claudexor run, watches it, and brings the result home. Because
the nanny IS the host, verification receipts stay host-authored and the harness's
output is a claim, not proof.

Three verbs, not four (TZ revision): ``delegate_start``, a time-bounded
``delegate_wait``, and ``delegate_cancel``. There is no ``hurry`` — Claudexor's only
control verb is ``cancel``, and cancelling a reviewer destroys the verdict you wanted.

Read-only and mutating children share ONE nanny and ONE transport. The only difference
is the access profile the HOST derives from the calling task's authority (``readonly``
vs ``workspace_write``) and the run shape that follows from it; there is no second
pipeline and no second slot. The child gets a broker tool, never a shell, so it can ask
the host to run something but never choose with what powers.

Custody: the daemon token never leaves ``gateways.claudexor``; nothing here puts it in
a ToolContext, a child environment, or a harness sandbox. WHICH run belongs to WHICH
task is decided by ``ouroboros.delegate_custody`` against the durable event log, not by
a dict this process happens to still hold.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import pathlib
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ouroboros import delegate_custody as custody
from ouroboros import delegate_progress as progress
from ouroboros.delegate_custody import RunCustody as _RunCustody
from ouroboros.tool_capabilities import tool_result_limit
from ouroboros.tools.registry import ToolContext, ToolEntry, active_repo_dir_for
from ouroboros.utils import truncate_review_artifact
# The staged-output + read-receipt cluster lives in its own module (size gate);
# re-exported here because sibling code, the tests and the convergence census all
# name it on THIS surface, and `_READ_COVERAGE` must stay the same object.
from ouroboros.delegate_output import (  # noqa: F401
    _ARTIFACT_SUBDIR,
    _READ_COVERAGE,
    _READ_COVERAGE_MAX_KEYS,
    _covered_whole,
    _safe_run_filename,
    _stage_full_output,
    acknowledge_staged_output_read,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ouroboros.subagents import DelegatedRunShape

log = logging.getLogger(__name__)

_TERMINAL_STATES = custody.TERMINAL_STATES

# The containment verifiers moved to `ouroboros/delegate_containment.py` whole (the
# module-size gate); re-exported here because the nanny's seams and the existing
# tests address them through this module.
from ouroboros.delegate_containment import (  # noqa: E402
    _ACCESS_UNVERIFIED,  # noqa: F401  (re-export: tests address it through this module)
    _Breach,
    _home_isolation_breach,
    _inside_operator_home,  # noqa: F401  (re-export)
    _widened_access,
)
_POLL_INTERVAL_SEC = 3.0
# Claudexor's own schema bound on maxSeconds (packages/schema/src/control.ts).
_CLAUDEXOR_MAX_SECONDS = 604_800

# The process-local memo of the durable custody rows (the authority lives in the module
# above); re-bound here because sibling code and tests name it on this surface.
_CUSTODY = custody._CUSTODY

# Room inside the delivery budget for the JSON scaffold and the delivery block itself.
_PAYLOAD_ENVELOPE_HEADROOM = 2_000
_PREVIEW_STEPS = (6_000, 3_000, 1_200, 400, 0)
_BULK_FIELDS = ("final_summary", "primary_output")
_STRUCTURED_FIELDS = ("outcome_banner", "outcome_facts", "output_conformance", "failure")


def _fail(tool: str, code: str, detail: str, **extra: Any) -> str:
    payload = {"status": "refused", "tool": tool, "reason": code, "detail": detail, **extra}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _emit(ctx: ToolContext, kind: str, payload: Dict[str, Any]) -> None:
    custody.emit(custody.custody_root(ctx), kind, {
        "task_id": str(getattr(ctx, "task_id", "") or ""), **payload,
    })


def _owned_run(ctx: ToolContext, tool: str, run_id: str) -> Tuple[Optional[str], Optional[_RunCustody]]:
    """Resolve custody for a run, or return a typed refusal payload.

    The daemon bearer token grants the ENTIRE Claudexor API, so a run id is not a
    capability the way a file descriptor is — anything that can name a run can reach it,
    read it, or CANCEL it, and cancelling a reviewer destroys the verdict that was the
    point of running it. Ownership is therefore replayed from the durable start row:
    a restarted worker keeps its runs, and an id with NO durable record is UNKNOWN
    (refused as unresolvable), which is a different fact from a run that demonstrably
    belongs to someone else.
    """
    status, entry = custody.lookup(custody.custody_root(ctx), str(getattr(ctx, "task_id", "") or ""), run_id)
    if status == custody.UNKNOWN:
        return _fail(tool, "run_ownership_unknown",
                     "No durable record of that run id exists on this drive, so ownership "
                     "cannot be established. Unknown ownership is refused, not waved through.",
                     run_id=run_id), None
    if status == custody.FOREIGN:
        return _fail(tool, "run_not_owned",
                     "That run belongs to another task. A delegated run may only be "
                     "waited on or cancelled by the task that started it.", run_id=run_id), None
    return None, entry


# Layered onto every lane by Claudexor (native system-prompt channel per harness, so no
# dialect here). It states the SAME prohibitions an ordinary subagent carries — the
# delegated child is a worker inside the nanny's worktree, not a second committer. It is
# a statement, not the enforcement: the enforcement is the access profile plus the
# nanny's own workspace-patch capture, which invalidates itself if HEAD moved.
_HOST_INSTRUCTIONS = (
    "You are a delegated worker running inside another agent's working tree. Your "
    "authority is everything INSIDE this root and nothing outside it. Do not run git "
    "commit, tag, push, rebase, reset or any other history-moving command: your host "
    "takes the diff of this tree and integrates it itself, and a moved HEAD invalidates "
    "that diff and destroys your work. Do not review or accept your own change, do not "
    "touch the host's runtime controls, skills, or memory, and do not write outside "
    "this root."
)

# DESTINATION 2 of the disclosure (AGENTS.md "Disclose instead of forbid": the durable
# record, the CHILD'S PROMPT, and the parent's result). A mutating delegated child is the
# only lane that asks for an OS boundary, and the boundary is a REQUEST: the engine applies
# one where it has a mechanism for this host and applies none where it does not, and no
# version number distinguishes the two. The child therefore cannot be told at start that it
# IS confined — nothing at start knows — so it is told the only true thing, which is also
# the useful one: behave as though nothing is stopping you, and do not claim in your answer
# an isolation you cannot show. What was actually applied reaches the parent afterwards,
# from the run's own artifacts, through `_containment_evidence`.
_UNPROVEN_BOUNDARY_INSTRUCTION = (
    " An OS-enforced filesystem boundary was REQUESTED for this run but is NOT guaranteed: "
    "your engine applies one only where it has a mechanism for this host, and your host "
    "reads back from your own attempt records what was actually applied. Work as if there "
    "is no boundary — stay inside this root, do not read the operator's home directory, "
    "credential stores, or the harness runtime tree, and do NOT describe yourself in your "
    "answer as sandboxed or confined. If your own environment shows you whether a boundary "
    "was in force, say so plainly."
)


def _host_instructions(authority: "DelegatedRunShape") -> str:
    """The system-prompt text this run's shape earns. One builder, no dialect."""
    if authority.delegated:
        return _HOST_INSTRUCTIONS + _UNPROVEN_BOUNDARY_INSTRUCTION
    return _HOST_INSTRUCTIONS


def _derive_authority(ctx: ToolContext) -> "DelegatedRunShape":
    """Derive the run shape from the task's own authority — one question, asked here.

    Host-derived, never model-supplied: the child asks the host to run something, and
    the host decides with what powers. Ouroboros asks for an access PROFILE and lets
    Claudexor pick the mechanism (fs sandbox, tool allowlist, ...) — no harness branch.

    The SHAPE itself belongs to ``subagents.delegated_run_shape``, which the dispatcher
    also reads: this function only answers "is this an acting child", which is the one
    part that needs the live ``ToolContext``.
    """
    from ouroboros.subagents import delegated_run_shape
    from ouroboros.tool_access import active_tool_profile

    return delegated_run_shape(active_tool_profile(ctx) == "acting_subagent")


def _containment_breach(detail: Dict[str, Any], authority: "DelegatedRunShape") -> Optional[_Breach]:
    """Everything the ENGINE enforced, checked against what the host asked for.

    ONE reader for both halves of containment — the access profile and the harness
    HOME — because they fail identically: the request is only a request, the engine
    derives the truth, and a verification written for one half leaves the other
    trusting an echo. The HOME half is asked only of a run that carried the marker;
    a read-only child is scoped by Claudexor's ordinary envelope and asks for nothing.
    """
    widened = _widened_access(detail, authority.access)
    if widened:
        return _Breach(
            "access_profile_widened",
            f"The delegated run was enforced at access profile {widened!r} while this "
            f"task is only entitled to {authority.access!r}.",
            {"entitled_access": authority.access, "effective_access": widened},
        )
    if authority.delegated:
        return _home_isolation_breach(detail)
    return None


_NO_BOUNDARY_NOTE = (
    "NO OS-ENFORCED BOUNDARY was applied to this run. The engine reported no confinement "
    "mechanism for it, so the only containment it had is a scoped HOME — a redirect of "
    "`~`-relative lookups, which leaves the operator's home, credential stores and the "
    "Claudexor daemon token readable at their absolute paths. The run was allowed and its "
    "work is usable; do NOT describe it as sandboxed, confined or isolated, and weigh its "
    "output as coming from an unconfined shell in this worktree"
)


def _containment_evidence(detail: Dict[str, Any]) -> Dict[str, Any]:
    """What the ARTIFACTS prove about this run's containment — never what was asked.

    DESTINATION 3 of the disclosure: this is what the nanny hands its parent.

    BOTH halves, in one reader, because a report that states only the scoped HOME is the
    defect this function was rewritten to remove: a run with a kernel-enforced boundary
    and a run with none produced BYTE-IDENTICAL evidence here, both reading
    ``verified: true`` with a note about the HOME. Claudexor's own confinement document
    says the scoped home "is not a boundary and must never be reported as one".

    The predicate is what the engine says it APPLIED (``confinement_mechanism`` plus the
    denied path it proved), never which OS this host is. Ouroboros does not know what the
    engine did — only the artifact does — and a platform test would additionally freeze
    today's answer: the day a boundary ships for another OS, this reader is already right.

    Judged by the SAME predicate that halts a breached run, not by having been reached
    after it: a report whose honesty depends on its call site is one refactor away from
    claiming a containment nobody checked.

    This is also where a MISSING fact lands, because it is a reporting question and not an
    enforcement one: an attempt that disclosed nothing proves nothing, so ``verified``
    stays false and ``disclosed`` says how much of the run is actually covered. Silence
    read as success and silence enforced as a fault are the two ways to be wrong here,
    and stating the count avoids both.
    """
    from ouroboros.gateways.claudexor import attempt_containment

    attempts = attempt_containment(str(custody.summary_of(detail).get("runDir") or ""))
    disclosed = sum(1 for attempt in attempts if attempt.home_isolated is not None)
    # An engine that reported nothing is indistinguishable from one that applied nothing,
    # and the mechanisms the ATTEMPTS name are the vocabulary — Ouroboros keeps no list of
    # its own to fall out of date. "Every attempt" and not "any": one unconfined attempt
    # is an unconfined run.
    mechanisms = sorted({attempt.boundary_mechanism for attempt in attempts})
    boundary = mechanisms[0] if attempts and len(mechanisms) == 1 and mechanisms[0] else ""
    report = {"verified": False, "attempts": len(attempts), "disclosed": disclosed,
              "os_boundary": boundary}
    breach = _home_isolation_breach(detail)
    if breach is not None:
        return {**report, "note": breach.detail}
    if not disclosed:
        return {**report, "note":
                "this run recorded no harness-HOME fact, so its confinement is UNPROVEN "
                "— do not report it as isolated"}
    if disclosed < len(attempts):
        return {**report, "note":
                "not every attempt of this run recorded a harness-HOME fact, so its "
                "confinement is UNPROVEN — do not report it as isolated"}
    if not boundary:
        return {**report, "note": _NO_BOUNDARY_NOTE}
    return {**report, "verified": True, "note":
            f"every attempt recorded a scoped harness HOME outside the operator's own AND "
            f"an applied {boundary} boundary, proven against a path it denies"}


def _terminal_payload(run_id: str, detail: Dict[str, Any],
                      authority: "DelegatedRunShape") -> Dict[str, Any]:
    summary = custody.summary_of(detail)
    payload = {
        "status": "terminal",
        "run_id": run_id,
        "state": str(summary.get("state") or ""),
        # The APPLIED model, from the engine's own summary — '' when the run
        # never disclosed one (live unpinned runs really do), shown as absence
        # rather than the requested model dressed up as the applied one.
        "model": str(summary.get("model") or ""),
        "outcome_banner": detail.get("outcomeBanner"),
        "outcome_facts": summary.get("outcomeFacts"),
        "output_conformance": summary.get("outputConformance"),
        "final_summary": detail.get("finalSummary"),
        "primary_output": detail.get("primaryOutput"),
        "failure": summary.get("failure"),
        "last_seq": int(detail.get("lastSeq") or 0),
        "cost": _reported_cost(summary),
        # The ACCESS half of the same honesty, on EVERY terminal payload — see
        # `_access_evidence`. Both lanes: `readonly` staying `readonly` is the profile
        # that matters most, while `containment` is asked only of marker-carrying runs.
        "access_evidence": _access_evidence(detail, authority.access),
    }
    if authority.delegated:
        payload["containment"] = _containment_evidence(detail)
    return payload


def _access_evidence(detail: Dict[str, Any], expected: str) -> Dict[str, Any]:
    """What the engine's own DERIVED profile proves about this finished run.

    ``effectiveAccess`` is the only witness: ``summary["access"]`` is computed as
    ``effectiveAccess ?? the client's own request``, so reading it compares the request
    against itself and always passes. A WIDER profile is already a breach before this
    runs; an ABSENT one cannot be enforced on a run that is over — cancelling a
    succeeded run to punish missing evidence would destroy the result the lane exists
    to fetch (the v6.87.37 lesson) — so it is named here instead.
    """
    summary = custody.summary_of(detail)
    effective = str(summary.get("effectiveAccess") or "")
    state = str(summary.get("state") or "")
    report = {"requested": expected, "effective": effective,
              "verified": bool(effective), "state": state}
    if effective:
        return report
    if state in custody.SUCCEEDED_STATES:
        return {**report, "note":
                "this run SUCCEEDED without ever disclosing an effective access "
                f"profile, so there is no evidence the engine enforced {expected!r} — "
                "do not report its containment as verified"}
    return {**report, "note":
            "no effective access profile was disclosed; a run that did not succeed may "
            "never have had one, so this is absence of evidence, not a breach"}


def _record_containment(ctx: ToolContext, entry: Optional[_RunCustody],
                        payload: Dict[str, Any]) -> None:
    """DESTINATION 1 of the disclosure: the durable record, written once per run.

    A missing boundary is not a fault and produces no refusal, which is exactly why it
    needs a durable line of its own — the run succeeds, its patch is integrated, and
    nothing else in the record would ever say the work came out of an unconfined shell.
    Emitted from what the PARENT was told, so the two cannot disagree.

    "Once per run" is now a DURABLE fact rather than a process-local one: the custody
    entry is replayed from the event log, so a restarted worker polling an already
    terminal run does not append a second identical finding.
    """
    containment = payload.get("containment")
    if not isinstance(containment, dict) or containment.get("os_boundary"):
        return
    if entry is not None and entry.containment_disclosed:
        return
    _emit(ctx, custody.UNCONFINED, {
        "run_id": entry.run_id if entry is not None else "",
        "route": entry.route_id if entry is not None else "",
        "state": str(payload.get("state") or ""),
        "os_boundary": "",
        "attempts": containment.get("attempts"),
        "home_disclosed": containment.get("disclosed"),
        "note": containment.get("note"),
    })
    if entry is not None:
        entry.containment_disclosed = True


def _reported_cost(summary: Dict[str, Any]) -> Dict[str, Any]:
    """What this run cost, as the AGENT will read it.

    This is the payload the nanny relays to its parent, so it must tell the same story
    the ledger does. It used to hardcode `$0.00 / final` — the exact shape the settlement
    fix was written to eliminate — so a run that really charged money settled honestly in
    the ledger and then told the reasoning path the work was free.
    """
    spend, estimated = custody.disclosed_spend(summary)
    if spend is None:
        return {
            "cost_usd": None,
            "cost_final": False,
            "note": "the harness disclosed no spend for this run; treat the cost as UNKNOWN, not zero",
        }
    if estimated:
        # The amount is the best fact anyone has, so it rides; the FINALITY does not. An
        # estimated zero is not a proven free session and an estimated charge is not a
        # closed book — both are `cost_final: False`, matching the ledger row exactly.
        return {
            "cost_usd": spend,
            "cost_final": False,
            "note": "the harness ESTIMATED this run's spend rather than settling it; treat "
                    "the amount as APPROXIMATE and the cost as NOT final",
        }
    if spend > 0:
        return {
            "cost_usd": spend,
            "cost_final": True,
            "note": "this run was BILLED — it did not ride the subscription",
        }
    return {
        "cost_usd": 0.0,
        "cost_final": True,
        "note": "subscription session — already paid; the nanny's own model calls are metered separately",
    }


# -- output delivery -----------------------------------------------------------


def _preview_payload(full: Dict[str, Any], text: str, artifact: Optional[Dict[str, Any]],
                     budget: int, consumed: bool = False, full_ok: bool = True,
                     full_note: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Shrink the inline view until it FITS, and say so in typed fields.

    The bulk fields are renamed to ``*_preview`` rather than silently shortened: a
    consumer reading ``primary_output`` gets nothing instead of a cut string it would
    mistake for the whole answer.

    ``consumed`` is the DURABLE fact (the D7 acknowledgement row exists), never an
    assumption: on first delivery it is False, and a re-wait on the same terminal run
    reports True only after the artifact really was read whole. ``full_ok`` is whether
    the staged content is the VERIFIED full result; ``full_note`` is the typed
    disclosure of how the engine's bounded primary-output preview was (or was not)
    resolved to the full artifact.
    """
    delivery: Dict[str, Any] = {
        "complete": False,
        "consumed": bool(consumed),
        "inline_is_preview": True,
        "total_chars": len(text),
        "artifact": artifact,
        "read_next": ({"tool": "read_file", "root": artifact["root"], "path": artifact["path"],
                       "start_line": 1, "max_lines": 2000} if artifact else None),
        "note": (
            (("PARTIAL inline, but the staged artifact has already been read whole — the "
              "durable acknowledgement exists, so this result counts as obtained."
              if consumed else
              "PARTIAL. The inline fields are a bounded preview; the whole terminal payload "
              "is the artifact above. Read it in chunks with read_file(root=..., path=..., "
              "start_line=N, max_lines=M) — start_line is a stable cursor over an immutable "
              "file — until your reads have covered EVERY character, contiguously. Delivery "
              "is char-bounded: a window longer than the tool-result budget is cut at "
              "delivery, and the cut remainder only counts as read once you advance WITHIN "
              "it via start_char. A review or research result is NOT consumed, and must not "
              "be reported as its verdict, until the whole artifact has been read.")
             if full_ok else
             "PARTIAL and INCOMPLETE AT THE SOURCE: the engine reported its primary output "
             "as a bounded preview and the full artifact could not be matched to the size "
             "or the preview the run itself reported (see primary_output_full; the engine "
             "publishes no content hash for it, so that match is the whole of the check). "
             "Treat this result as incomplete evidence, not as "
             "the verdict; it can never be acknowledged as fully read.")
            if artifact else
            "PARTIAL and UNRECOVERABLE INLINE: the full payload could not be staged to the "
            "task drive. Treat this result as incomplete evidence, not as the verdict."
        ),
    }
    if full_note is not None:
        delivery["primary_output_full"] = full_note
    payload: Dict[str, Any] = {}
    for preview_chars in _PREVIEW_STEPS:
        payload = {key: value for key, value in full.items() if key not in _BULK_FIELDS}
        for field in _BULK_FIELDS:
            raw = full.get(field)
            if raw is None:
                continue
            body = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            payload[f"{field}_preview"] = body[:preview_chars]
        for field in _STRUCTURED_FIELDS:
            value = payload.get(field)
            if value is not None and len(json.dumps(value, ensure_ascii=False)) > preview_chars:
                payload[field] = {"omitted": "see output_delivery.artifact"}
        payload["output_delivery"] = delivery
        # Same threshold as the complete branch: the headroom covers the JSON scaffold
        # and the settlement block the caller appends afterwards.
        if len(json.dumps(payload, ensure_ascii=False, indent=2)) <= budget - _PAYLOAD_ENVELOPE_HEADROOM:
            return payload
    return payload


# Tolerance for the preview-prefix consistency check below: the engine redacts the
# preview over a bounded prefix window with a 1 KiB overlap, so a secret spanning the
# preview boundary may redact differently in the full serve than in the preview tail.
_PREVIEW_PREFIX_SLACK = 2_048


def _resolve_full_primary_output(gateway: Any, run_id: str,
                                 primary: Any) -> Tuple[Any, bool, Optional[Dict[str, Any]]]:
    """Resolve the engine's bounded primary-output preview to the verified FULL text.

    ``primaryOutput.text`` on the run detail is a 256 KiB PREVIEW (control-api
    ``PRIMARY_OUTPUT_PREVIEW_BYTES``), with ``bytes`` (on-disk size) and ``truncated``
    beside it. A truncated preview must NEVER be staged, delivered or acknowledged as
    the result: the full file is fetched from ``GET /v2/runs/:id/artifacts/<path>`` and
    verified against what the run reported before it may wear the plain name.

    The engine reports NO content hash for the primary output, so verification is what
    the contract actually offers: the served size equal to the reported ``bytes``
    (exact), or — because the artifact route serves text through ``redactSecrets``,
    which can legally change the length — the fetched text carrying the preview as its
    prefix (up to a bounded slack at the preview boundary, where the engine's own
    redaction overlap can differ). Anything less keeps the preview, marked incomplete,
    with a typed disclosure — never a partial result wearing a full one's name.

    Returns ``(primary_output, full_ok, disclosure)``; ``disclosure`` is None when the
    engine never reported a truncation.
    """
    if not isinstance(primary, dict) or primary.get("truncated") is not True:
        return primary, True, None
    path = str(primary.get("path") or "")
    reported_bytes = primary.get("bytes")
    preview_text = primary.get("text") if isinstance(primary.get("text"), str) else ""
    disclosure: Dict[str, Any] = {"requested": True, "fetched": False, "verified": "",
                                  "path": path, "reported_bytes": reported_bytes}
    if not path or gateway is None:
        disclosure["reason"] = "no_artifact_path" if not path else "no_transport"
        return primary, False, disclosure
    try:
        raw = gateway.get_run_artifact(run_id, path)
    except Exception as exc:
        disclosure["reason"] = truncate_review_artifact(
            f"{getattr(exc, 'code', type(exc).__name__)}: {exc}", 300)
        return primary, False, disclosure
    disclosure["fetched"] = True
    disclosure["fetched_bytes"] = len(raw)
    full_text = raw.decode("utf-8", errors="replace")
    if isinstance(reported_bytes, int) and not isinstance(reported_bytes, bool) \
            and len(raw) == reported_bytes:
        disclosure["verified"] = "size"
    else:
        prefix = preview_text[:max(0, len(preview_text) - _PREVIEW_PREFIX_SLACK)]
        if prefix and full_text.startswith(prefix) and len(full_text) >= len(preview_text):
            disclosure["verified"] = "preview_prefix"
        else:
            disclosure["reason"] = "verification_failed_size_and_prefix"
            return primary, False, disclosure
    resolved = {**primary, "text": full_text, "truncated": False,
                "full_fetched": True, "verified_by": disclosure["verified"]}
    return resolved, True, disclosure


def _delivered_terminal_payload(ctx: ToolContext, run_id: str, detail: Dict[str, Any],
                                authority: "DelegatedRunShape",
                                entry: Optional[_RunCustody] = None,
                                gateway: Any = None) -> Dict[str, Any]:
    """The terminal payload, delivered whole or declared partial — never head-cut.

    ``final_summary``/``primary_output`` carry the run's real work product, and Claudexor
    returns a preview of up to 256 KiB. Outer truncation would head-cut that at the tool
    result limit and sever the JSON mid-string, which destroys the document rather than
    shortening it. So the payload bounds ITSELF against the same limit the truncator
    applies, and the remainder becomes a readable artifact — after the engine's bounded
    preview has been resolved to the verified full artifact, because a payload built on
    a truncated preview delivers 256 KiB wearing the whole result's name.
    """
    full = _terminal_payload(run_id, detail, authority)
    # Requested-vs-applied model, the review lane's own lexicon and rule
    # (AgentSessionReviewExecutor): compared only when BOTH are non-empty —
    # the engine writes aliases ('sonnet' beside 'claude-opus-5'), so a
    # mismatch is an advisory disclosure, never a failure of the run.
    requested_model = str(getattr(entry, "model", "") or "") if entry is not None else ""
    applied_model = str(full.get("model") or "")
    if requested_model and applied_model and requested_model != applied_model:
        full["capability_delta"] = [{
            "kind": "capability_delta",
            "requested": f"model {requested_model}",
            "effective": f"model {applied_model}",
            "reason": "session_route_resolves_its_own_model",
        }]
    primary, full_ok, full_note = _resolve_full_primary_output(
        gateway, run_id, full.get("primary_output"))
    full["primary_output"] = primary
    budget = tool_result_limit("delegate_wait")
    text = json.dumps(full, ensure_ascii=False, indent=2)
    if len(text) <= budget - _PAYLOAD_ENVELOPE_HEADROOM:
        full["output_delivery"] = {
            # An unresolved engine-side truncation makes even an inline-fitting payload
            # NOT the whole result: complete/consumed follow the verified fact.
            "complete": full_ok, "consumed": full_ok, "inline_is_preview": False,
            "total_chars": len(text), "artifact": None, "read_next": None,
            "note": ("The whole terminal payload is inline." if full_ok else
                     "INLINE BUT INCOMPLETE AT THE SOURCE: the engine reported its "
                     "primary output as a bounded preview and the full artifact could "
                     "not be matched to the size or the preview the run itself reported "
                     "(see primary_output_full). Treat this "
                     "as incomplete evidence, not as the verdict."),
        }
        if full_note is not None:
            full["output_delivery"]["primary_output_full"] = full_note
        return full
    artifact = _stage_full_output(ctx, run_id, text)
    _emit(ctx, custody.OUTPUT_SPILLED, {"run_id": run_id, "total_chars": len(text),
                                        "artifact": (artifact or {}).get("path", ""),
                                        "bytes": (artifact or {}).get("bytes"),
                                        "sha256": (artifact or {}).get("sha256", ""),
                                        "staged": artifact is not None,
                                        "full_content": bool(full_ok and artifact is not None)})
    if entry is not None and artifact is not None:
        if entry.output_consumed and entry.output_sha and artifact["sha256"] != entry.output_sha:
            # The ack named OTHER bytes: a re-stage of different content at the same
            # path owes a fresh acknowledgement — consumed never transfers by path.
            entry.output_consumed = False
        entry.output_sha = artifact["sha256"]
        entry.output_artifact = artifact["path"]
        entry.output_complete = bool(full_ok)
    return _preview_payload(full, text, artifact, budget,
                            consumed=bool(entry is not None and entry.output_consumed),
                            full_ok=full_ok, full_note=full_note)


# -- tools --------------------------------------------------------------------


def _resolved(path: Any) -> Optional[pathlib.Path]:
    """Resolve a path, or None when it cannot be resolved at all (null byte, symlink
    loop, unreadable parent). One predicate, so no call site re-enumerates the set."""
    try:
        return pathlib.Path(str(path)).resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _mutating_run_root(ctx: ToolContext, authority: "DelegatedRunShape") -> tuple[str, str]:
    """The directory the run may write, cross-checked against the authority that allowed it.

    AUTHORITY and ROOT came from two different predicates and were never compared.
    Authority is derived from `task_constraint` via `active_tool_profile`; the root came
    from `active_repo_dir_for`, and `ToolContext.active_repo_dir()` falls back to
    `repo_dir` — the LIVE Ouroboros source tree — whenever `is_workspace_mode()` is false,
    which `workspace_mode_block_reason` makes happen for an overlapping worktree root or a
    task record missing its workspace fields. In that state the host would have handed an
    external shell `workspace_write` + `live` on its own repository, and every per-tool
    guard is irrelevant because the harness is a shell, not a tool. The registry already
    hard-blocks `write_file`/`run_command` and their coding siblings in exactly that state; this
    verb was simply not on the list.

    A mutating run therefore requires an ACTIVE workspace whose root is the very
    `write_root` the constraint granted. Disagreement is a typed refusal, never a
    best-effort guess. Read-only runs keep the ordinary active root: they write nothing.
    """
    root = str(active_repo_dir_for(ctx))
    if authority.access != "workspace_write":
        return root, ""
    constraint = getattr(ctx, "task_constraint", None)
    granted = str(
        (constraint.get("write_root") if isinstance(constraint, dict)
         else getattr(constraint, "write_root", "")) or ""
    ).strip()
    if not granted:
        return "", _fail(
            "delegate_start", "write_root_missing",
            "This child is allowed to write, but its task constraint names no write_root, "
            "so there is no directory the host can honestly confine the run to.",
        )
    # AGREEMENT IS NOT ENOUGH: `active_repo_dir_for` falls back to `repo_dir` when
    # workspace mode is off, so a constraint whose write_root happens to name that same
    # directory made the comparison pass and handed a shell the live repository — the
    # very case this guard was written for. Require the workspace to be genuinely ACTIVE.
    # ONE predicate, the one the registry already owns. `workspace_mode_block_reason`
    # returns "" precisely WHEN `workspace_mode` is empty, so "no block reason" is
    # satisfied by the absence of a workspace — the condition was true in exactly the
    # case it was written to refuse. `is_workspace_mode()` is the question actually
    # being asked, and `active_repo_dir()` branches on that same call.
    if not (callable(getattr(ctx, "is_workspace_mode", None)) and ctx.is_workspace_mode()):
        return "", _fail(
            "delegate_start", "workspace_not_active",
            "A delegated run may only WRITE inside an ACTIVE workspace, and this task "
            "has none. Refusing rather than falling back to the repository root.",
        )
    # "Can this path be resolved at all" is ONE question, not an exception set to
    # re-enumerate: an embedded null raises ValueError and a symlink loop RuntimeError,
    # and either escaping here would abort delegate_start with a traceback instead of the
    # typed refusal this function exists to produce.
    resolved_root, resolved_grant = _resolved(root), _resolved(granted)
    same = resolved_root is not None and resolved_root == resolved_grant
    if not same:
        return "", _fail(
            "delegate_start", "write_root_mismatch",
            "The active root and the granted write_root disagree, so the run would write "
            "somewhere this task was never given. Refusing rather than guessing.",
            active_root=root, granted_write_root=granted,
        )
    return root, ""

def _start_request(ctx: ToolContext, route: Any, authority: "DelegatedRunShape",
                   root: str, text: str, seconds: int) -> Dict[str, Any]:
    """The POST body for one delegated run, built from the derived SHAPE.

    Extracted so the caller stays inside the method-size gate, and so the body has ONE
    author: the shape decides the mode, the instructions and whether the delegated
    marker rides along, and nothing here re-derives any of them.

    ``seconds`` arrives PRE-BOUNDED rather than being derived here: a transport retry
    of a pending invocation must present a byte-identical body for the engine's replay
    match, and the deadline-derived bound changes with the clock, so the caller decides
    whether to recompute it or replay the recorded one.
    """
    request: Dict[str, Any] = {
        "prompt": text,
        # Built from the SHAPE, so a mutating delegated child is told that its
        # boundary is a request and not a fact — the same disclosure the durable
        # record and the parent's result carry, in the one place the child can read.
        "instructions": _host_instructions(authority),
        # The engine's default authPreference is `auto` = subscription-first WITH
        # policy fallback to a paid API key. That fallback is invisible to us and
        # would be settled at a confident $0.00 — the one shape the ledger must
        # never produce. Ask for the substrate we are actually claiming.
        "authPreference": "subscription",
        # The run SHAPE comes from the derived authority, not from re-deriving it
        # here: one predicate decides what this child may do, and the mode follows it.
        "mode": authority.mode,
        "scope": {"kind": "project", "root": root},
        # PIN, not preference: `primaryHarness` only fronts the engine's
        # auto-pool, which still holds every other doctor-OK harness — the run
        # could fail over onto a route the owner never configured. The
        # explicit one-element `harnesses` pool is the engine's pinning
        # contract (its own MCP surface spells a forced route exactly this
        # way): the child rides THIS route or the start refuses typed.
        "harnesses": [route.route_id],
        "primaryHarness": route.route_id,
        "access": authority.access,
    }
    if authority.isolation:
        # `delegated` rides WITH the isolation, from the same record, because they
        # are the same decision: `live` is in-place, and in place is exactly where
        # Claudexor would otherwise hand the harness the operator's real `$HOME`
        # — daemon control token included. Sending one without the other is the
        # containment hole, so neither is assembled separately.
        request["execution"] = {
            "isolation": authority.isolation,
            "delegated": authority.delegated,
        }
    if route.model:
        request["model"] = route.model
    if route.effort:
        request["effort"] = route.effort
    if seconds:
        request["maxSeconds"] = seconds
    return request


def _validated_invocation(drive: Any, retry_token: str, task_id: str,
                          text: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """The stored invocation a retry may replay, or the typed refusal that stops it.

    Six ways a token is not replayable, each answered by name: no record, another
    task's, already bound, definitely refused (its id is retired — replaying wedges a
    permanent 409), no canonical body, prompt disagrees. One author for all six.
    """
    record = custody.invocation_record(drive, retry_token)
    if record is None:
        return None, _fail("delegate_start", "unknown_invocation",
                           "retry_of names an invocation with no durable record on this "
                           "drive. Start a new run with a plain delegate_start.",
                           retry_of=retry_token)
    if record["task_id"] != task_id:
        return None, _fail("delegate_start", "invocation_not_owned",
                           "retry_of names another task's invocation. A delegated start "
                           "may only be retried by the task that requested it.",
                           retry_of=retry_token)
    if record["state"] == "started":
        return None, _fail("delegate_start", "invocation_already_started",
                           "That invocation already bound a run — do not re-post it. "
                           "Wait on the existing run instead.",
                           retry_of=retry_token, run_id=record["run_id"])
    if record["state"] == "failed_definite":
        return None, _fail("delegate_start", "invocation_definitely_refused",
                           "That invocation was definitively refused by the daemon; its "
                           "id is retired. Start a new run with a plain delegate_start.",
                           retry_of=retry_token)
    body = record["request"]
    if not isinstance(body, dict) or not body:
        return None, _fail("delegate_start", "invocation_request_unrecorded",
                           "That invocation's durable row carries no canonical request "
                           "body, so it cannot be replayed byte-identically. Start a "
                           "new run with a plain delegate_start.",
                           retry_of=retry_token)
    if str(body.get("prompt") or "") != text:
        return None, _fail("delegate_start", "retry_prompt_mismatch",
                           "retry_of replays the RECORDED invocation, but the prompt "
                           "you passed differs from the one it sent. Pass the original "
                           "prompt to retry, or drop retry_of to start a new run.",
                           retry_of=retry_token)
    return record, ""


def _delegate_start(ctx: ToolContext, prompt: str, max_seconds: Optional[int] = None,
                    retry_of: Optional[str] = None) -> str:
    from ouroboros.claudexor_daemon import ensure_owned_gateway
    from ouroboros.gateways.claudexor import ClaudexorUnavailable
    from ouroboros.subagents import (
        DelegatedRunShape, DelegationRoute, get_subagent_harness,
        resolve_subagent_executor, route_health,
    )

    text = str(prompt or "").strip()
    if not text:
        return _fail("delegate_start", "empty_prompt", "prompt is required")
    if _deadline_expired(ctx):
        # An EXPIRED nanny cannot honestly bound anything: the deadline-less fallback
        # would hand the run the absolute task ceiling, hours past the instant this
        # task's own deadline demanded it stop. Refused before the daemon is touched.
        return _fail(
            "delegate_start", "task_deadline_expired",
            "This task's deadline has already passed, so a delegated run started now "
            "would outlive it by design. Finalize with what you have — do not start "
            "new work a deadline has already closed.",
        )

    drive = custody.custody_root(ctx)
    owned_project_id = ""
    invocation_id = ""
    # ONE logical invocation id per INTENDED invocation, and reuse ONLY by explicit
    # token — never by content-matching, because two identical intentions are still
    # two intentions (the owner's contract: an intended new start is a NEW id). An
    # ordinary call mints a fresh id and records the CANONICAL body beside it; a
    # start whose outcome was unknown hands its id back as a retry token, and only
    # a call that presents that token replays the invocation — the STORED body,
    # byte-identical by construction, under the SAME wire Idempotency-Key, so the
    # engine returns the run it already accepted instead of starting a second one
    # (a re-derived body would digest differently and 409 at the engine).
    retry_token = str(retry_of or "").strip()
    recovering = bool(retry_token)
    if recovering:
        # The stored invocation is the SINGLE SOURCE of EVERY fact about a retry — the
        # health-checked route, the shape, the root, the project, the lookup key — not
        # only of the wire bytes: re-deriving any of them POSTed the recorded body while
        # the record and the parent's result described a configuration the run never had.
        # Validated BEFORE any daemon call, so a refused token registers nothing.
        record, refusal = _validated_invocation(
            drive, retry_token, str(getattr(ctx, "task_id", "") or ""), text)
        if refusal:
            return refusal
        request_body = record["request"]
        execution = (request_body.get("execution")
                     if isinstance(request_body.get("execution"), dict) else {})
        scope = request_body.get("scope") if isinstance(request_body.get("scope"), dict) else {}
        route = DelegationRoute(route_id=str(request_body.get("primaryHarness") or ""),
                                model=str(request_body.get("model") or ""),
                                effort=str(request_body.get("effort") or ""))
        authority = DelegatedRunShape(access=str(request_body.get("access") or ""),
                                      mode=str(request_body.get("mode") or ""),
                                      isolation=str(execution.get("isolation") or ""),
                                      delegated=bool(execution.get("delegated")))
        root = str(scope.get("root") or "")
        key = str(record.get("idempotency_key") or "")
        project_id = str(record.get("project_id") or "")
        owned_project_id = project_id if record.get("project_owned") else ""
        seconds = int(request_body.get("maxSeconds") or 0)
        invocation_id = retry_token
        if authority.access == "workspace_write":
            # The replay will WRITE at the recorded root, so containment is re-asked
            # against the task's PRESENT workspace — and the answer must be the very
            # root the invocation recorded. A workspace that moved between the
            # attempts makes the replay a write into a tree this task no longer
            # holds, which is a refusal, never a re-derivation.
            current_root, root_error = _mutating_run_root(ctx, authority)
            if root_error:
                return root_error
            resolved_current, resolved_recorded = _resolved(current_root), _resolved(root)
            if resolved_current is None or resolved_current != resolved_recorded:
                return _fail(
                    "delegate_start", "retry_root_divergence",
                    "This retry replays a MUTATING invocation recorded against a root "
                    "this task no longer holds: the active write root has moved since "
                    "the original attempt. Start a new run for the current root.",
                    retry_of=retry_token, recorded_root=root, active_root=current_root)
    else:
        route = get_subagent_harness()
        if route is None:
            resolution = resolve_subagent_executor("harness", route=None)
            return _fail(
                "delegate_start", resolution.reason,
                "No delegated route is configured (OUROBOROS_SUBAGENT_HARNESS is empty). "
                "Think natively instead — do not wait for a harness that does not exist.",
                executor=resolution.executor,
            )
        authority = _derive_authority(ctx)

    access = authority.access
    try:
        gateway = ensure_owned_gateway()
    except ClaudexorUnavailable as exc:
        resolution = resolve_subagent_executor("harness", route=route, unavailable_reason=exc.code)
        return _fail("delegate_start", exc.code, str(exc), executor=resolution.executor)

    try:
        # Health is asked about the whole SHAPE, so the same reader that refuses a route
        # which cannot write also refuses an ENGINE that cannot confine a delegated
        # harness's HOME. Both come back here as a typed blocker; neither can degrade
        # into starting the run anyway. On a retry the route and the shape are the
        # STORED invocation's, so the answer is about the run actually being replayed —
        # not about whatever the environment names today.
        unavailable, reset_at = route_health(
            gateway, route.route_id, authority, route_model=route.model,
        )
        resolution = resolve_subagent_executor(
            "harness", route=route, unavailable_reason=unavailable, reset_at=reset_at,
        )
        if resolution.blocked:
            return _fail(
                "delegate_start", resolution.reason,
                "The delegated route cannot run now. This is a typed blocker: do NOT "
                "silently fall back onto metered API spend — decide explicitly "
                "(wait for the reset, deliver partial work, or ask the parent).",
                executor="blocked", reset_at=resolution.reset_at, route=route.route_id,
            )

        if not recovering:
            root, root_error = _mutating_run_root(ctx, authority)
            if root_error:
                return root_error
            existing_project = gateway.find_project_id(root)
            project_id = existing_project or gateway.register_project(root)
            owned_project_id = "" if existing_project else project_id
            key = custody.idempotency_key(getattr(ctx, "task_id", ""), route.route_id, access,
                                          authority.mode, authority.isolation, root, text)
            invocation_id = custody.new_invocation_id()
            seconds = _bounded_max_seconds(ctx, max_seconds)
            request_body = _start_request(ctx, route, authority, root, text, seconds)
        lineage = getattr(ctx, "task_metadata", {}) or {}
        lineage = lineage if isinstance(lineage, dict) else {}
        requested = custody.record_start_requested(
            drive, run_id="", task_id=str(getattr(ctx, "task_id", "") or ""),
            idempotency_key=key, invocation_id=invocation_id,
            max_seconds=seconds, request=request_body, project_id=project_id,
            project_owned=bool(owned_project_id), route=route.route_id,
            # Lineage rides the request row so a run RECOVERED from a pending
            # invocation (P34R.2: worker died between the accepted POST and
            # record_started) can still attribute its ledger row to the task tree.
            root_task_id=str(lineage.get("root_task_id") or ""),
            parent_task_id=str(lineage.get("parent_task_id") or ""))
        if not requested:
            # The POST is CONDITIONAL on the durable request row: launching anyway
            # would start an overpowered run that nothing durable names, and a worker
            # death before record_started would leave it live and unfindable. Nothing
            # was sent on THIS attempt — so a fresh start's registration is
            # definitively retirable, but a RETRY's project belongs to the original
            # attempt, whose POST may have bound a live run: its fate stays unknown
            # and its invocation stays pending, or a local disk error would strand a
            # run the daemon may well be executing.
            return _fail(
                "delegate_start", "start_request_row_unwritable",
                "The durable start-request row could not be written, so the run was "
                "NOT started: a run launched without its custody trail would be "
                "unfindable if this worker died. Fix the drive/event log and retry.",
                **_retire_orphaned_registration(ctx, gateway, owned_project_id,
                                                definite_refusal=not recovering,
                                                reason="start_request_row_unwritable",
                                                invocation_id=invocation_id))
        handle = gateway.start_run(request_body, idempotency_key=invocation_id)
        # A 202 answers with `jobId` and no `runId` when the run has not bound a run dir
        # inside the daemon's start timeout. The run IS durably enqueued and will execute,
        # and `jobId` is a usable handle for GET and /control — discarding it left a live
        # run nobody could wait on, cancel or settle, and invited a duplicate start.
        run_id = str(handle.get("runId") or handle.get("jobId") or "")
        if not run_id:
            # The POST SUCCEEDED, so a run is more likely live here than on the refusal
            # branch beside it — the registration is retained and durably named through
            # the same path, instead of being silently abandoned as it was.
            return _fail("delegate_start", "queued_without_run_id",
                         f"Claudexor returned a queued handle without a run id: {handle!r}",
                         pending_invocation_id=invocation_id,
                         retry_hint="to retry THIS start call delegate_start with "
                                    "retry_of=pending_invocation_id; a plain call starts a NEW run",
                         **_retire_orphaned_registration(ctx, gateway, owned_project_id,
                                                         definite_refusal=False,
                                                         reason="queued_without_run_id",
                                                         invocation_id=invocation_id))
    except ClaudexorUnavailable as exc:
        # A registration we created BEFORE the start must not outlive a failed start.
        # It used to be left behind with nothing anywhere naming its id.
        status = int(getattr(exc, "status_code", 0) or 0)
        definite = 400 <= status < 500
        # An UNKNOWN outcome hands back the retry token: only the caller can say
        # whether the next call is a retry of this intention or a new intention, and
        # without the token every next call is a new one. A definite refusal retires
        # the id, so no token rides a refusal.
        pending = ({} if definite or not invocation_id else
                   {"pending_invocation_id": invocation_id,
                    "retry_hint": "to retry THIS start call delegate_start with "
                                  "retry_of=pending_invocation_id; a plain call starts a NEW run"})
        return _fail("delegate_start", exc.code, str(exc), executor="blocked",
                     reset_at=getattr(exc, "reset_at", ""), **pending,
                     **_retire_orphaned_registration(ctx, gateway, owned_project_id,
                                                     definite_refusal=definite,
                                                     reason=str(getattr(exc, "code", "")),
                                                     invocation_id=invocation_id))
    except BaseException as exc:
        # EVERY pre-custody exit leaves a durable disposition, including the ones no
        # typed handler claims (a bug here, a timeout, a signal). NEVER retired: an
        # untyped exit says nothing about whether the POST reached the daemon, so a run
        # may be live against it. Named with a typed reason so the sweep's
        # pending-invocation recovery finds it, then re-raised — disclosure, not a swallow.
        _retire_orphaned_registration(ctx, gateway, owned_project_id,
                                      definite_refusal=False,
                                      reason=f"pre_custody_exit_{type(exc).__name__}",
                                      invocation_id=invocation_id)
        raise
    finally:
        gateway.close()

    metadata = getattr(ctx, "task_metadata", {}) or {}
    metadata = metadata if isinstance(metadata, dict) else {}
    # A start whose custody row did not land does not get to wear the plain name, for the
    # same reason a cut field is renamed `*_preview`: the run is live and only THIS process
    # knows it exists. A bare "started" there is the uncustodied-run leak this module was
    # written to close, one surface further out.
    durable = custody.record_started(drive, _RunCustody(
        run_id=run_id,
        task_id=str(getattr(ctx, "task_id", "") or ""),
        route_id=route.route_id,
        model=route.model,
        project_id=project_id,
        project_owned=bool(owned_project_id),
        root_task_id=str(metadata.get("root_task_id") or ""),
        parent_task_id=str(metadata.get("parent_task_id") or ""),
        # The CANONICAL (budget) root, the same one the custody rows themselves live
        # on — never ctx.drive_root, which on a split-root task is the disposable
        # child drive: a ledger row written there misses every budget fence and is
        # erased with the child drive's pruning (P34R.1).
        ledger_root=str(drive),
        idempotency_key=key,
        invocation_id=invocation_id,
    ), shape={
        # The shape rides on the SAME durable row as custody, so a forensic reader never
        # has to join two events to learn what authority a run was started with.
        # `max_seconds` rides too: delegate_wait reports elapsed-vs-cap from this row.
        "effort": route.effort, "access": access, "mode": authority.mode,
        "isolation": authority.isolation, "delegated": authority.delegated, "root": root,
        "max_seconds": seconds,
    })
    # The AUTHORITY guidance and the CUSTODY warning are independent facts about the same
    # start, so both are said. An undurable custody row is the louder one and goes first:
    # a nanny that walks away from an uncustodied MUTATING run leaves a live shell in its
    # own worktree that nothing outside this process can name.
    note = "" if durable else (
        "CUSTODY IS NOT DURABLE: the run started, but its custody row could not be written, "
        "so nothing outside this worker can wait on, cancel or settle it. Do not walk away "
        "from it — finish it or delegate_cancel it in this session. ")
    note += (
        "You are the nanny and the host. Poll with delegate_wait; the run's own "
        "claims are evidence to check, not a verified result."
        + (
            " This run edits YOUR OWN working tree in place, so its changes become "
            "part of the patch you hand up — read them before you claim them, and "
            "never let it commit. It was ASKED to run under a scoped HOME and an "
            "OS-enforced boundary; whether the engine applied either is a per-run "
            "fact that delegate_wait reads back from the run's own artifacts. A "
            "host with no boundary mechanism runs it anyway and says so there."
            if authority.isolation == "live" else
            " This run cannot write anything: it reads and answers."
        )
    )
    return json.dumps({
        "status": "started" if durable else "started_uncustodied",
        "run_id": run_id,
        "run_dir": handle.get("runDir"),
        "route": route.route_id,
        "model": route.model,
        "effort": route.effort,
        "access": access,
        "mode": authority.mode,
        "isolation": authority.isolation or "envelope",
        "idempotent_recovery": recovering,
        # ASKED, not applied. The proof arrives with the run's own artifacts and is
        # relayed by delegate_wait; saying "isolated" here would be the exact claim
        # this whole verification exists to stop anyone from making.
        "scoped_home_requested": authority.delegated,
        "root": root,
        "custody_durable": durable,
        "note": note,
    }, ensure_ascii=False, indent=2)


def _retire_orphaned_registration(ctx: ToolContext, gateway: Any, project_id: str, *,
                                  definite_refusal: bool, reason: str,
                                  invocation_id: str = "") -> Dict[str, Any]:
    """Retire a registration this start created but never bound to a run.

    Only when the daemon gave a DEFINITE negative answer (a 4xx refusal): a transport
    error, a 5xx, or a 2xx handle with no run id all mean the POST's fate is unknown, and
    a run may well be live against this very registration. An unverified outcome is never
    grounds for destroying state — the durable row names the id either way, which is what
    the old code lacked. The caller supplies the verdict, so every failing start reaches
    this one path instead of one branch retiring and its twin abandoning.

    The row also settles the INVOCATION's fate: ``definite: true`` retires the logical
    invocation id (a definitely refused invocation must not be reused — the daemon may
    hold its key against a body a reconfigured route can no longer reproduce, which
    would 409 forever), while an unknown outcome leaves it pending so a transport retry
    presents the same key and lands on whatever the daemon really has. Written even
    with no registration to retire, because the invocation's fate is its own fact.
    """
    retired = False
    if project_id and definite_refusal:
        try:
            gateway.remove_project(project_id)
            retired = True
        except Exception as exc:
            # A registration the daemon does not have is already retired: the same
            # absence-is-discharge fact `retire_project` settles on.
            retired = custody.daemon_says_absent(exc)
            if not retired:
                log.warning("Failed to retire orphaned delegated project %s", project_id, exc_info=True)
    if project_id or invocation_id:
        _emit(ctx, custody.START_FAILED, {"run_id": "", "project_id": project_id,
                                          "project_retired": retired, "reason": reason,
                                          "invocation_id": invocation_id,
                                          "definite": bool(definite_refusal)})
    if not project_id:
        return {"project_retired": False}
    if retired or definite_refusal:
        return {"project_retired": retired, "project_id": project_id}
    return {"project_retired": False, "project_id": project_id,
            "project_retention_reason": "start_outcome_unknown_run_may_exist"}


def _deadline_expired(ctx: ToolContext) -> bool:
    """True when the nanny HAS a deadline and it has already passed.

    The distinction the bound below could not make: ``deadline_remaining_sec`` answers
    0.0 both for "no deadline" and for "the deadline is behind us", and collapsing them
    let an EXPIRED nanny hand a fresh run the absolute task ceiling.
    """
    from ouroboros.deadline_utils import deadline_remaining_sec, parse_deadline_ts

    meta = getattr(ctx, "task_metadata", {})
    meta = meta if isinstance(meta, dict) else {}
    if parse_deadline_ts(meta.get("deadline_at")) is None:
        return False
    return deadline_remaining_sec(ctx) <= 0


def _bounded_max_seconds(ctx: ToolContext, requested: Optional[int]) -> int:
    """Narrow-only: the delegated run may never outlive the nanny's own deadline.

    A caller must ask ``_deadline_expired`` FIRST: an expired deadline cannot produce an
    honest bound at all, and this function's fallback is for a nanny that has NO
    deadline, never for one whose deadline is behind it.
    """
    from ouroboros.deadline_utils import deadline_remaining_sec

    remaining = int(max(0.0, deadline_remaining_sec(ctx)))
    try:
        asked = int(requested) if requested is not None else 0
    except (TypeError, ValueError):
        asked = 0
    candidates = [value for value in (asked, remaining) if value > 0]
    if candidates:
        # Clamp HERE too, not only on the fallback below: `max_seconds` is a model-supplied
        # tool argument with no maximum in its schema, so an explicit ask sailed past the
        # bound the fallback branch was careful about — the same defect, one branch over.
        return min(_CLAUDEXOR_MAX_SECONDS, min(candidates))
    # No positive bound is knowable: either the nanny has no deadline, or its deadline
    # has already passed. Omitting `maxSeconds` — the old behavior — handed the run
    # Claudexor's 7-day schema bound; the cap is damage limitation, and custody (the
    # durable start row plus reconciliation) is what actually stops an orphan.
    from ouroboros.config import get_task_abs_ceiling_sec

    # Claudexor bounds maxSeconds at 7 days (control.ts `.max(604_800)`), and the task
    # ceiling clamps only from BELOW — an owner who raises it past a week would make
    # every deadline-less start send an out-of-schema value.
    return min(_CLAUDEXOR_MAX_SECONDS, int(get_task_abs_ceiling_sec()))


def _halt_breached_run(ctx: ToolContext, gateway: Any, entry: _RunCustody,
                       breach: _Breach) -> str:
    """Stop a run the engine did not contain as asked, and say exactly what failed.

    The BREACH incident goes through ``custody.record_containment_fault``, the same
    writer an unverified cancel uses, so a breached run also surfaces as the CRITICAL
    health invariant that stays open until a terminal receipt resolves it. Emitting a
    look-alike event here instead left the breach out of the open-fault sweep.

    The stop itself goes through ``custody.cancel_and_verify`` — the ONE cancel path,
    with its four typed outcomes — and the sentence handed back to the agent is built
    from the outcome it returns. The ad-hoc cancel this replaced swallowed every
    exception into a log line and then said "The run was cancelled" unconditionally,
    which is precisely what ``record_containment_fault``'s own contract forbids: an
    incident must never surface "as a reassuring string in a tool result". An
    overpowered run that refused to stop was reported to the agent as stopped.
    """
    run_id = entry.run_id
    drive = custody.custody_root(ctx)
    try:
        cancelled = custody.cancel_and_verify(drive, gateway, entry, breach.code)
    except Exception:
        log.warning("Failed to cancel an uncontained delegated run %s", run_id, exc_info=True)
        cancelled = {"outcome": custody.CANCEL_CONTAINMENT_FAULT}
    custody.record_containment_fault(drive, entry, breach.code, breach.detail,
                                     fault=breach.code, **breach.facts)
    outcome = str(cancelled.get("outcome") or custody.CANCEL_CONTAINMENT_FAULT)
    return _fail(
        "delegate_wait", breach.code,
        f"{breach.detail} {_CANCEL_NOTES.get(outcome, '')} Do not retry it: this is a "
        "containment fault in the transport or the engine, not a task failure — report "
        "it and continue within your own authority.",
        run_id=run_id, cancel_outcome=outcome, **breach.facts,
    )


def _delegate_wait(ctx: ToolContext, run_id: str, wait_sec: Optional[int] = None,
                   since_seq: Optional[int] = None) -> str:
    """Time-bounded, progress-aware wait (docs/DEVELOPMENT.md "Timeout & Wait Control").

    HOLDS the window it was given. It returns early only on a terminal state or a
    containment fault; a journal-cursor advance past ``since_seq`` is RECORDED and
    streamed to the human live, and the model is woken once, at expiry, with the whole
    sequence in ``advances``. Returning on the first advance made the caller's window
    meaningless against a healthy run — the only path that ever consulted it was the
    SILENT one, so a streaming run cost a full-context round per event batch (measured:
    18 rounds, 861k prompt tokens, for a run that was doing fine). Progress is the
    JOURNAL cursor, so SSE ``: ping`` keepalives cannot masquerade as it.

    NARROW-ONLY, like ``_bounded_max_seconds``: the wait may not outlive the nanny's own
    deadline, minus the finalization grace it needs to answer at all. This tool is absent
    from ``_DEADLINE_CLAMPED_TOOLS`` (its ToolEntry value IS its outer bound), so nothing
    upstream cuts it — measured, a 2100s window against ten seconds of remaining deadline
    ran the full 2100s and slid the task past its deadline mid-tool, the defect that set
    built for ``web_search``. Clamping HERE keeps the graceful typed ``no_progress``
    return where the outer clamp delivers a thread-kill. Only "no deadline set" is left
    unclamped; a SPENT deadline clamps to the floor, the window is measured from before
    the connection, and every call is BOUNDED by what it has left (``progress.poll_bound``)
    so no read can outrun it as the 60s default could. Only the LAST poll of a spent
    window may go unanswered gracefully; a daemon that fails while the window still has
    time is the typed refusal it was, never a wait reported as quiet.
    """
    from ouroboros.config import get_delegate_wait_max_sec, get_delegate_wait_sec
    from ouroboros.gateways.claudexor import ClaudexorGateway, ClaudexorUnavailable

    rid = str(run_id or "").strip()
    if not rid:
        return _fail("delegate_wait", "missing_run_id", "run_id is required")
    not_mine, entry = _owned_run(ctx, "delegate_wait", rid)
    if not_mine or entry is None:
        return not_mine or _fail("delegate_wait", "run_ownership_unknown", "custody unresolved", run_id=rid)
    ceiling = get_delegate_wait_max_sec()
    try:
        window = int(wait_sec) if wait_sec is not None else get_delegate_wait_sec()
    except (TypeError, ValueError):
        window = get_delegate_wait_sec()
    from ouroboros.deadline_utils import parse_deadline_ts, window_within_deadline

    window = window_within_deadline(ctx, max(1, min(window, ceiling)))

    # The clock starts HERE, before the connection: the window is a promise about how
    # long this CALL holds, and the opening handshake plus first poll are part of it.
    # Started after them, an unbounded connection could spend the whole deadline before
    # the window it was clamped into had begun.
    started = time.monotonic()
    deadline = started + window
    try:
        gateway = ClaudexorGateway()
        gateway.handshake(timeout_sec=progress.poll_bound(deadline - time.monotonic()))
    except ClaudexorUnavailable as exc:
        return _fail("delegate_wait", exc.code, str(exc), run_id=rid)

    # Re-derived from the LIVE context rather than read back from custody: the nanny's
    # authority is the authority, and a lost custody record must not become a wider run.
    authority = _derive_authority(ctx)
    # FACTS against premature cancels: how long the run has actually been going and
    # what its cap really is, from the durable start row. A nanny that cannot see
    # these confabulates "exceeded the cap" out of its own impatience. Absent facts
    # (an old row, an unknown run) stay null — never invented.
    _started_ts, _run_max_seconds = custody.run_timing(custody.custody_root(ctx), rid)
    _started_at = parse_deadline_ts(_started_ts)
    try:
        detail = progress.bounded_poll(gateway, rid, deadline - time.monotonic())
        baseline = int(since_seq) if since_seq is not None else int(detail.get("lastSeq") or 0)
        seen = progress.WindowObservations()
        seen.observe_baseline(detail, baseline)
        while True:
            summary = custody.summary_of(detail)
            state = str(summary.get("state") or "")
            last_seq = int(detail.get("lastSeq") or 0)
            breach = _containment_breach(detail, authority)
            if breach:
                return _halt_breached_run(ctx, gateway, entry, breach)
            if state in _TERMINAL_STATES:
                was_settled = bool(entry.settled)
                settlement = custody.settle_run(custody.custody_root(ctx), gateway, entry, detail)
                payload = _delivered_terminal_payload(ctx, rid, detail, authority, entry, gateway)
                payload["settlement"] = settlement
                # The «last delegated run» settings receipt (Subagents section):
                # requested vs applied model, written ONLY when THIS call performed
                # a SUCCESSFUL settlement — a later wait re-reading an already-settled
                # run must not re-date it (or replace a newer run as "last"), and a
                # settlement whose durable obligations failed must not mint a receipt
                # it would re-mint on every retry. The delegated REVIEW sessions never
                # pass here — they have their own receipt store
                # (reviewer_slot_last_execution.json).
                if not was_settled and bool(settlement.get("settled")):
                    from ouroboros.subagents import record_last_delegation
                    record_last_delegation(
                        route=entry.route_id, requested_model=entry.model,
                        applied_model=str(payload.get("model") or ""), run_id=rid)
                # D7 made load-bearing: settlement is where "paid for and never read"
                # becomes permanent, so the parent is told in WORDS here — not left to
                # infer it from `output_delivery.consumed`. Re-settling an already
                # settled run reports the CURRENT durable fact, so the line disappears
                # once the read has happened rather than echoing a stale omission.
                if custody.record_settled_unread(custody.custody_root(ctx), entry):
                    payload["result_not_collected"] = (
                        "THIS RESULT IS NOT COLLECTED YET: the run is settled and its "
                        "full output is staged, but nothing has read it to EOF. Read the "
                        "artifact named in output_delivery with read_file "
                        "root='task_drive' until it is covered end to end — a result you "
                        "have not read is not a result you may report."
                    )
                # The containment disclosure is read off what the PARENT was told, so the
                # durable line and the relayed payload cannot disagree. It runs on the
                # PREVIEW path too: a payload big enough to spill is exactly the one whose
                # containment block a reader is least likely to reach.
                _record_containment(ctx, entry, payload)
                from ouroboros.tools.control import cache_horizon_note
                _horizon = cache_horizon_note(ctx, time.monotonic() - started)
                if _horizon:
                    payload["cache_horizon_note"] = _horizon
                return json.dumps(payload, ensure_ascii=False, indent=2)
            if last_seq > baseline:
                # The STREAM is not collapsed — the TIMER is. Every advance reaches the
                # live progress surface the instant this loop sees it, so the human's
                # view stays as rich; what stops is waking the MODEL per event batch.
                # The emit is also the frame the supervisor's idle enforcer reads, which
                # a silently blocking wait would starve.
                progress.emit(ctx, rid, seen.record(detail, last_seq, int(time.monotonic() - started)))
                baseline = last_seq          # so the NEXT advance is counted once
            def _expired() -> str:
                rendered = progress.rendered_window(
                    run_id=rid, state=state, last_seq=last_seq, window=window,
                    elapsed_seconds=(None if _started_at is None else max(0, int(
                        (_dt.datetime.now(tz=_dt.timezone.utc) - _started_at).total_seconds()))),
                    max_seconds=_run_max_seconds or None,
                    waiting_on_user=bool(summary.get("waitingOnUser")),
                    detail=detail, seen=seen,
                    budget=tool_result_limit("delegate_wait"))
                from ouroboros.tools.control import cache_horizon_note
                _horizon = cache_horizon_note(ctx, time.monotonic() - started)
                return f"{rendered}\n\n{_horizon}" if _horizon else rendered

            if time.monotonic() >= deadline:
                return _expired()
            time.sleep(min(_POLL_INTERVAL_SEC, max(0.0, deadline - time.monotonic())))
            # BOUNDED whether or not the window is spent: a poll STARTED a moment before
            # expiry still carries the client's 60s read default, so the clamp bounded the
            # sleeping and not the waiting. What an UNANSWERED one MEANS is what differs.
            # The last poll of a spent window is bounded and never skipped — terminal
            # state and breach are judged on fresh data or not at all — and a daemon too
            # slow to answer THAT one is this window's expiry. Earlier, the window still
            # has time and there is no expiry to report: the typed refusal propagates to
            # the handler below, because a daemon that died mid-window relayed as a quiet
            # completed wait is a fabricated duration on top of a run nobody is watching.
            left = deadline - time.monotonic()
            fresh = (progress.expiring_poll(gateway, rid) if left <= 0
                     else progress.bounded_poll(gateway, rid, left))
            if fresh is None:
                return _expired()   # unanswered AT expiry: expire on what is already held
            detail = fresh
    except ClaudexorUnavailable as exc:
        return _fail("delegate_wait", exc.code, str(exc), run_id=rid)
    finally:
        gateway.close()


_CANCEL_NOTES = {
    custody.CANCEL_CONFIRMED: "VERIFIED terminal: the run has stopped. Partial artifacts are "
                              "preserved by Claudexor; a cancelled run has no verdict.",
    custody.CANCEL_REQUESTED: "The daemon ACCEPTED the cancel but the run is not terminal yet. "
                              "It is still running. Call delegate_wait to confirm it stops.",
    custody.CANCEL_FAILED: "The daemon REFUSED the cancel and the run is still live and still "
                           "mutating. Escalate — this is not a stopped run.",
    custody.CANCEL_CONTAINMENT_FAULT: "CONTAINMENT FAULT: the cancel could not be verified, so an "
                                      "overpowered mutating run MAY STILL BE LIVE. A durable "
                                      "incident was recorded and is surfaced as a critical health "
                                      "invariant until a terminal receipt clears it.",
}


def _delegate_cancel(ctx: ToolContext, run_id: str, reason: str = "") -> str:
    """Stop a delegated run. Destructive by nature — a cancelled reviewer has no verdict.

    Reports only what a terminal receipt proves. Saying "cancelled" over an unverified
    control is worse than saying nothing: it retires the operator's attention from a run
    that is still writing to a workspace.
    """
    from ouroboros.gateways.claudexor import ClaudexorGateway, ClaudexorUnavailable

    rid = str(run_id or "").strip()
    if not rid:
        return _fail("delegate_cancel", "missing_run_id", "run_id is required")
    not_mine, entry = _owned_run(ctx, "delegate_cancel", rid)
    if not_mine or entry is None:
        return not_mine or _fail("delegate_cancel", "run_ownership_unknown", "custody unresolved", run_id=rid)
    try:
        gateway = ClaudexorGateway()
        gateway.handshake()
    except ClaudexorUnavailable as exc:
        return _fail("delegate_cancel", exc.code, str(exc), run_id=rid)
    try:
        result = custody.cancel_and_verify(custody.custody_root(ctx), gateway, entry, reason)
    finally:
        gateway.close()
    return json.dumps({
        "status": result["outcome"],
        "run_id": rid,
        "run_may_still_be_live": result["outcome"] != custody.CANCEL_CONFIRMED,
        "accepted": result["accepted"],
        "control_status": result["control_status"],
        "state": result["state"],
        "fault_reason": result["fault_reason"],
        "detail": result["detail"],
        "note": _CANCEL_NOTES.get(result["outcome"], ""),
    }, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("delegate_start", {
            "name": "delegate_start",
            "description": (
                "Start a delegated run on the owner's configured subscription harness and "
                "become its NANNY. Subscription execution is REQUESTED, so the usual case "
                "is no metered API money — but the actual spend is a fact of the finished "
                "run, not a promise of this call: it may come back zero, billed, "
                "estimated, or undisclosed (an expired session, a route that bills by "
                "construction, or an auth fallback all charge real money). Read the "
                "terminal `cost` block from delegate_wait before you treat this as free; "
                "it also costs time, quota and a worker slot. Your working root, "
                "access profile and route come from YOUR task authority; you cannot widen "
                "them, and there is no argument here that would let you try. If you are a "
                "MUTATING subagent the run edits your own working tree in place, so its "
                "changes ride out in the patch you hand to your parent — read them before "
                "you claim them, and never let it commit, because a moved HEAD invalidates "
                "your whole patch. If you are read-only it can only read and answer. "
                "Returns a run_id: watch it with delegate_wait, stop it with "
                "delegate_cancel. The run's output is a CLAIM you must check — you are the "
                "host, so verification receipts are still yours to write. If no route is "
                "configured or it is unavailable you get a typed refusal: think natively "
                "instead of waiting."
            ),
            "parameters": {"type": "object", "required": ["prompt"], "properties": {
                "prompt": {"type": "string", "description": "The complete task for the delegated session."},
                "max_seconds": {"type": "integer", "description":
                    "Wall-clock cap for the run; narrowed to your own remaining deadline. "
                    "Harness runs routinely need 3-5+ minutes end to end, so do not set a "
                    "tight cap for what feels like a quick edit. While delegate_wait shows "
                    "an advancing cursor the run is WORKING, and it enforces this cap "
                    "itself — cancelling a progressing run discards the whole run's spend."},
                "retry_of": {"type": "string", "description":
                    "EXPLICIT retry token: the pending_invocation_id from a start whose "
                    "outcome was unknown (transport failure, lost response). Replays THAT "
                    "invocation byte-identically under its original key, so the engine "
                    "returns the run it already accepted instead of starting a second one. "
                    "Never set it for an intended new run — a plain call always starts a "
                    "NEW invocation, even with an identical prompt."},
            }},
        }, lambda ctx, prompt, max_seconds=None, retry_of=None: _delegate_start(ctx, prompt, max_seconds, retry_of), timeout_sec=120),
        ToolEntry("delegate_wait", {
            "name": "delegate_wait",
            "description": (
                "Wait for a delegated run, bounded in time. Returns IMMEDIATELY only when "
                "the run is TERMINAL or hit a containment fault; otherwise it HOLDS the "
                "window you asked for — the run's narration streams to your human live "
                "while you wait, so you are not the transport for it — and comes back at "
                "expiry with `advances`, every journal-cursor movement seen during the "
                "window, plus elapsed/max seconds and `quiet_for_sec`. A silent window "
                "returns a typed no-progress reason. Keepalives are not progress. Calling "
                "again with a tiny wait_sec to 'check' is a busy-poll: it costs a "
                "full-context round per call and buys nothing, because this call already "
                "waited. Pass since_seq=last_seq to keep following. A large terminal "
                "result is delivered as a bounded preview plus an artifact: read "
                "output_delivery and finish reading the artifact before you rely on it."
            ),
            "parameters": {"type": "object", "required": ["run_id"], "properties": {
                "run_id": {"type": "string", "description": "Run id from delegate_start."},
                "wait_sec": {"type": "integer", "description":
                    "How long THIS call holds before handing control back to you (clamped "
                    "to the configured ceiling and to your own remaining deadline). It is a "
                    "WINDOW, not a quiet cutoff: a run that is streaming keeps streaming to "
                    "your human for the whole of it, and you get the whole batch at the end."},
                "since_seq": {"type": "integer", "description": "Event cursor: advances past it are recorded as progress."},
            }},
        }, lambda ctx, run_id, wait_sec=None, since_seq=None: _delegate_wait(ctx, run_id, wait_sec, since_seq), timeout_sec=2100),
        ToolEntry("delegate_cancel", {
            "name": "delegate_cancel",
            "description": (
                "Cancel a delegated run. Claudexor keeps partial artifacts, but a cancelled "
                "session has no verdict and no finished work product — cancel a stuck or "
                "misdirected run, never one you merely want to hurry. The result is typed: "
                "only `confirmed` means a verified terminal receipt; `requested`, `failed` "
                "and `containment_fault_run_may_still_be_live` all mean it may still be running."
            ),
            "parameters": {"type": "object", "required": ["run_id"], "properties": {
                "run_id": {"type": "string", "description": "Run id from delegate_start."},
                "reason": {"type": "string", "description": "Why you are stopping it."},
            }},
        }, lambda ctx, run_id, reason="": _delegate_cancel(ctx, run_id, reason), timeout_sec=120),
    ]


__all__ = ["get_tools"]
