"""Single source of truth for AGENT-context size budgets.

These govern the size of Ouroboros's OWN working context: the main-loop
assembled prompt, tool-history compaction triggers, and the background
consciousness context guards.

They are deliberately SEPARATE from the REVIEW-prompt budget family
(``ouroboros.tools.review_helpers.REVIEW_PROMPT_TOKEN_BUDGET`` and the
``ouroboros.tools.scope_review`` window constants), which sizes reviewer
prompts, not the agent's own context. Merging the two would couple unrelated
concerns and is explicitly avoided.

Constants only (no functions) so this module stays free against the codebase
function-count gate (``ouroboros.review.MAX_TOTAL_FUNCTIONS``). Profile-keyed
resolution (the low/max context modes) is layered on later without renaming
these constants.

Char-based guards assume the ~chars/4 estimate (``ouroboros.utils.estimate_tokens``);
the comments give the approximate token equivalents.
"""

from __future__ import annotations

# Main-loop emergency tool-history compaction trigger (~300K tokens at chars/4).
# Remote routine compaction stays off by design; this is the overflow backstop.
# NECESSITY is judged on this budget in CALIBRATED real tokens (chars/4 × the
# main-loop measured density, neutral 1.0 cold): on a ~1.7×-dense Claude route
# the raw-char form silently meant ~500K real tokens, engaging only deep into
# the task and then thrashing (see the hysteresis constants below).
#
# THE NUMBER IS A REAL-TOKEN BUDGET, NOT A CHAR COUNT — say the consequence out
# loud, because it is the opposite of what the name reads like. The comparison is
# (chars/4 + tool_schema_tokens) × density > CONST/4, so the transcript the agent
# may hold shrinks with BOTH density and the tool envelope. Measured fire points
# (tests/test_loop_compaction.py::test_emergency_trigger_fire_points_are_pinned
# drives the real decision and pins these):
#   max, density 1.0, no schemas ......... ~1.20M chars  (the constant, verbatim)
#   max, density 1.0, 37K schema tokens ... ~1.06M chars  (1.14x earlier)
#   max, density 1.7, no schemas .......... ~708K chars   (1.70x earlier)
#   max, density 1.7, 37K schema tokens ... ~559K chars   (2.14x earlier)
#   low, density 1.7, 37K schema tokens ... ~91K chars    (4.40x earlier)
# The dense Claude lane (the production main loop) is therefore ~2.1x more eager
# in max mode than the raw constant suggests — deliberate under the v6.91 owner
# decision "necessity = total calibrated pressure" (an immutable core otherwise
# overflows the window with no trigger at all), and affordable only because the
# UTILITY hysteresis below stops a futile pass from refiring every round. If that
# eagerness ever has to come down, move the CONSTANT with these numbers in hand;
# do not density-scale the threshold, which would cancel the calibration out.
EMERGENCY_COMPACTION_CHARS = 1_200_000

# Emergency-compaction hysteresis (the necessity-vs-utility split). NECESSITY
# (compact at all?) is total calibrated pressure — the frozen frame (system
# blocks + tools + protected/kept rounds) counts toward the provider window.
# UTILITY (can a pass help?) is the COMPACTABLE region only: after a pass that
# could NOT get the context below the trigger (the frame alone exceeds it —
# the submarine wave3 shape: 35/35 rounds fired, each pass a light-model call
# plus a transcript rewrite that collapsed the prompt cache to the static
# floor), further passes are suppressed until the compactable transcript grows
# by this factor or this many rounds pass, whichever is first.
COMPACTION_HYSTERESIS_REGION_GROWTH = 1.2
COMPACTION_HYSTERESIS_ROUNDS = 10

# Background-consciousness assembled-context guards. P1: fail fast, never
# silently truncate cognitive artifacts.
BG_CONTEXT_WARN_CHARS = 600_000   # ~150K tokens: warn but proceed
BG_CONTEXT_MAX_CHARS = 1_200_000  # ~300K tokens: skip the wakeup cycle

# Drive-state JSON injection guard inside the consciousness context.
BG_STATE_JSON_WARN_CHARS = 200_000

# WARN threshold for a single oversized governance/knowledge context section.
LARGE_CONTEXT_SECTION_CHARS = 200_000

# Main-loop assembled-context soft cap (tokens). A no-op recorder
# (P1 no-silent-truncation); the live transcript is bounded by compaction below.
CONTEXT_SOFT_CAP_TOKENS = 200_000

# --- Low-profile (≈200K window / local models) overrides -------------------
# These tighten the live working set in low context mode. They never shorten the
# memory HORIZON: recent dialogue is only coarsened when older dialogue is
# already represented by valid consolidation, and tool-history transcript
# compaction persists a forensic checkpoint before summarizing.

# Raw recent-dialogue tail shown when no valid consolidation can represent older
# dialogue. Low mode keeps this horizon rather than silently shortening it.
MAX_RECENT_CHAT_TAIL = 1000

# Low fires emergency tool-history compaction earlier (~100K tokens at chars/4)
# to fit a ~200K window, and (unlike max) also enables remote routine compaction.
# The owner low/max context MODE is the SSOT for the agent's own operating
# window (v6.33.0 BIBLE P1): low => this 400K trigger + routine compaction; max
# => the 1.2M emergency-only trigger. There is no per-model window table; the
# reactive provider-overflow detector (context.py) is the safety net if a route's
# real window is smaller than the mode assumes.
LOW_EMERGENCY_COMPACTION_CHARS = 400_000

# --- Native image blocks (v6.26.0 multimodal chat) ---------------------------
# Char-equivalent for ONE image block in chars/4 token estimates (~1.1K tokens):
# vision models bill per tile, not per base64 char.
IMAGE_BLOCK_CHAR_EQUIVALENT = 4_400
# Live image blocks kept in the transcript (single counter across owner
# uploads, browser screenshots, and transport injections). Older images are
# replaced by a caption placeholder pointing to the re-view path.
# 3 -> 5 (v6.81.1, owner decision 2026-07-29): with tool-result images now
# auto-attached (screenshots arrive every observation round), 3 kept too little
# visual history for compare-two-screens reasoning; 5 costs at most ~2.2K extra
# estimated tokens per request and only when that many images are actually live.
MAX_LIVE_IMAGE_BLOCKS = 5

# --- Scratchpad size thresholds (SSOT; previously scattered literals) -------
# Context-section soft budget for the rendered scratchpad (warn-only).
SCRATCHPAD_SECTION_BUDGET_CHARS = 90_000
# Health-invariant bloat warning ("extract durable insights to knowledge").
SCRATCHPAD_BLOAT_WARN_CHARS = 50_000
# Block-storage consolidation trigger (consolidator compresses oldest blocks).
SCRATCHPAD_CONSOLIDATION_THRESHOLD_CHARS = 30_000

# --- Hot-store growth thresholds (health invariant; bytes) -------------------
# Deterministic tripwires for the append-only stores whose interactive readers
# degrade with file size (BIBLE P2: the class was caught by the owner, not by
# any instrument — these thresholds are the instrument). Same family as
# SCRATCHPAD_BLOAT_WARN_CHARS above: a health-invariant WARNING, not a gate.
#
# Ledger: measured evidence in ouroboros/usage_ledger.py::_locked — a ~20MB
# usage_attempts.jsonl costs ~0.5s per full re-read UNDER THE MONETARY LOCK,
# starving concurrent workers (the 2026-07-23 lock-timeout incident). Warn at
# exactly that measured degradation point.
USAGE_LEDGER_WARN_BYTES = 20_000_000
# events/tools logs have no rotation and no per-request reader on the hot path
# today (health scans are tail-bounded); the thresholds are deliberately
# generous — they exist to flag runaway growth long before a full read of the
# file becomes a seconds-scale operation, not to nag normal accumulation.
EVENTS_LOG_WARN_BYTES = 100_000_000
TOOLS_LOG_WARN_BYTES = 100_000_000
# progress.jsonl is expected to be ROTATION-BOUNDED (the chat.jsonl rotation
# pattern, 800KB cap in supervisor/state.py::rotate_chat_log_if_needed,
# generalized to progress by the perf/lifecycle sprint). 8MB = 10x that cap:
# this warning fires only if rotation is broken or missing — a deliberate
# regression tripwire, not a size preference.
PROGRESS_LOG_WARN_BYTES = 8_000_000
