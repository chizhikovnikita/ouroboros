from types import SimpleNamespace


def _messages(count=41):
    return [{"role": "assistant", "content": f"msg-{idx}"} for idx in range(count)]


def test_routine_compaction_runs_for_low_remote_but_not_max_remote(monkeypatch, tmp_path):
    from ouroboros import loop

    calls = []

    def fake_checkpoint(messages, **kwargs):
        calls.append(("checkpoint", kwargs["reason"], kwargs["keep_recent"]))
        return True

    def fake_compact(messages, keep_recent, **kwargs):
        calls.append(("compact", keep_recent, kwargs.get("drive_root"), kwargs.get("task_id")))
        return [{"role": "system", "content": "compacted"}], {"prompt_tokens": 1}

    monkeypatch.setattr(loop, "_persist_compaction_checkpoint", fake_checkpoint)
    monkeypatch.setattr(loop, "compact_tool_history_llm", fake_compact)

    base = dict(
        tools=SimpleNamespace(_ctx=SimpleNamespace(_pending_compaction=None)),
        drive_root=tmp_path,
        drive_logs=tmp_path / "logs",
        task_id="task-1",
        round_idx=7,
        event_queue=None,
        checkpoint_injected=False,
        emit_progress=lambda _msg: None,
    )

    low_messages, low_usage = loop._run_round_compaction(
        _messages(),
        loop._CompactionRoundContext(active_use_local=False, active_context_mode="low", **base),
    )
    assert low_messages == [{"role": "system", "content": "compacted"}]
    assert low_usage == {"prompt_tokens": 1}
    assert calls == [("checkpoint", "routine", 20), ("compact", 20, tmp_path, "task-1")]

    calls.clear()
    max_messages, max_usage = loop._run_round_compaction(
        _messages(),
        loop._CompactionRoundContext(active_use_local=False, active_context_mode="max", **base),
    )
    assert len(max_messages) == 41
    assert max_usage is None
    assert calls == []

    local_messages, local_usage = loop._run_round_compaction(
        _messages(),
        loop._CompactionRoundContext(active_use_local=True, active_context_mode="max", **base),
    )
    assert local_messages == [{"role": "system", "content": "compacted"}]
    assert local_usage == {"prompt_tokens": 1}


def test_emergency_compaction_shrinks_keep_recent_to_span_count(monkeypatch, tmp_path):
    """Emergency compaction must pass keep_recent BELOW the span count or the
    compactor no-ops exactly when the transcript is too big (<=50 huge rounds
    over the byte threshold never compacted at all)."""
    from ouroboros import loop

    calls = []

    def fake_checkpoint(messages, **kwargs):
        calls.append(("checkpoint", kwargs["reason"], kwargs["keep_recent"]))
        return True

    def fake_compact(messages, keep_recent, **kwargs):
        calls.append(("compact", keep_recent))
        return [{"role": "system", "content": "compacted"}], None

    monkeypatch.setattr(loop, "_persist_compaction_checkpoint", fake_checkpoint)
    monkeypatch.setattr(loop, "compact_tool_history_llm", fake_compact)
    monkeypatch.setattr(loop, "_estimate_messages_chars", lambda _m: 10**9)

    # 30 tool rounds -> emergency keep_recent must be 15 (30 // 2), not 50.
    messages = []
    for i in range(30):
        messages.append({
            "role": "assistant", "content": f"r{i}",
            "tool_calls": [{"id": f"c{i}", "function": {"name": "x", "arguments": "{}"}}],
        })
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "ok"})

    ctx = loop._CompactionRoundContext(
        tools=SimpleNamespace(_ctx=SimpleNamespace(_pending_compaction=None)),
        drive_root=tmp_path,
        drive_logs=tmp_path / "logs",
        task_id="task-em",
        round_idx=3,
        event_queue=None,
        active_use_local=False,
        active_context_mode="max",
        checkpoint_injected=False,
        emit_progress=lambda _msg: None,
    )
    compacted, _usage = loop._run_round_compaction(messages, ctx)

    assert compacted == [{"role": "system", "content": "compacted"}]
    assert calls == [("checkpoint", "emergency_context_size", 15), ("compact", 15)]

    # Few huge rounds (<= 6 spans): keep_recent clamps BELOW the span count so
    # the compactor's len(spans) <= keep_recent gate cannot no-op forever.
    calls.clear()
    small = []
    for i in range(4):
        small.append({
            "role": "assistant", "content": f"r{i}",
            "tool_calls": [{"id": f"s{i}", "function": {"name": "x", "arguments": "{}"}}],
        })
        small.append({"role": "tool", "tool_call_id": f"s{i}", "content": "huge"})
    loop._run_round_compaction(small, ctx)
    assert calls == [("checkpoint", "emergency_context_size", 3), ("compact", 3)]


def test_context_compaction_observability_uses_current_task_drive(monkeypatch, tmp_path):
    from ouroboros import context_compaction
    from ouroboros import llm_observability

    seen = {}

    def fake_chat_observed(_client, **kwargs):
        seen.update(kwargs)
        return {"content": "[round:1]\nsummary"}, {"prompt_tokens": 1}

    monkeypatch.setattr(llm_observability, "chat_observed", fake_chat_observed)
    monkeypatch.setattr(context_compaction, "LLMClient", lambda: object(), raising=False)

    summary, usage = context_compaction._summarize_round_batch(
        [(1, "TOOL_CALL x: {}")],
        drive_root=tmp_path,
        task_id="task-42",
    )

    assert summary == {1: "summary"}
    assert usage == {"prompt_tokens": 1}
    assert seen["drive_root"] == tmp_path
    assert seen["task_id"] == "task-42"


def test_emergency_compaction_necessity_uses_calibrated_density(monkeypatch, tmp_path):
    """NECESSITY is total calibrated pressure: the char budget compared in REAL
    tokens via the main-loop density baseline (neutral 1.0 cold, measured
    supersedes) — on a measured ~1.7x-dense route the trigger fires before the
    raw-char form would; on a cold store behavior is unchanged."""
    from ouroboros import context_fit, loop

    calls = []
    monkeypatch.setattr(
        loop, "_persist_compaction_checkpoint",
        lambda m, **k: calls.append(("checkpoint", k["reason"])) or True,
    )
    monkeypatch.setattr(
        loop, "compact_tool_history_llm",
        lambda m, keep_recent, **k: (calls.append(("compact", keep_recent)) or (m, None)),
    )
    # Raw chars sit BELOW the 1.2M max trigger; ~1.7x measured density puts the
    # calibrated real-token pressure over it.
    monkeypatch.setattr(loop, "_estimate_messages_chars", lambda _m: 800_000)

    def _ctx(density):
        monkeypatch.setattr(context_fit, "main_loop_token_density", lambda _dr, _m: density)
        return loop._CompactionRoundContext(
            tools=SimpleNamespace(_ctx=SimpleNamespace(_pending_compaction=None, _accumulated_usage={})),
            drive_root=tmp_path, drive_logs=tmp_path / "logs", task_id="task-cal",
            round_idx=3, event_queue=None, active_use_local=False,
            active_context_mode="max", checkpoint_injected=False,
            emit_progress=lambda _msg: None,
        )

    messages = []
    for i in range(8):
        messages.append({
            "role": "assistant", "content": f"r{i}",
            "tool_calls": [{"id": f"c{i}", "function": {"name": "x", "arguments": "{}"}}],
        })
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "ok"})

    loop._run_round_compaction(messages, _ctx(1.7))
    assert ("checkpoint", "emergency_context_size") in calls

    calls.clear()
    loop._run_round_compaction(messages, _ctx(1.0))  # cold baseline: unchanged behavior
    assert calls == []


def test_emergency_compaction_necessity_counts_tool_schemas(monkeypatch, tmp_path):
    """NECESSITY is TOTAL pressure: the tool schemas travel beside `messages` on
    the wire, so they must count. A transcript just under the trigger plus a big
    schema envelope is over it — the submarine class where compaction fired a
    whole tool envelope (~148K chars) late."""
    from ouroboros import context_fit, loop

    calls = []
    monkeypatch.setattr(
        loop, "_persist_compaction_checkpoint",
        lambda m, **k: calls.append(("checkpoint", k["reason"])) or True,
    )
    monkeypatch.setattr(
        loop, "compact_tool_history_llm",
        lambda m, keep_recent, **k: (calls.append(("compact", keep_recent)) or (m, None)),
    )
    monkeypatch.setattr(context_fit, "main_loop_token_density", lambda _dr, _m: 1.0)
    # 1.19M chars ≈ 297.5K tokens: just UNDER the 1.2M-char (300K-token) max trigger.
    monkeypatch.setattr(loop, "_estimate_messages_chars", lambda _m: 1_190_000)

    messages = []
    for i in range(8):
        messages.append({
            "role": "assistant", "content": f"r{i}",
            "tool_calls": [{"id": f"c{i}", "function": {"name": "x", "arguments": "{}"}}],
        })
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "ok"})

    def _ctx(schemas):
        return loop._CompactionRoundContext(
            tools=SimpleNamespace(_ctx=SimpleNamespace(_pending_compaction=None, _accumulated_usage={})),
            drive_root=tmp_path, drive_logs=tmp_path / "logs", task_id="task-schemas",
            round_idx=3, event_queue=None, active_use_local=False,
            active_context_mode="max", checkpoint_injected=False,
            emit_progress=lambda _msg: None, tool_schemas=schemas,
        )

    loop._run_round_compaction(messages, _ctx(None))
    assert calls == []  # transcript alone stays under the trigger

    # ~40K tokens of schemas (the submarine envelope) push the TOTAL over it.
    schemas = [
        {"type": "function", "function": {"name": f"tool_{i}", "description": "d" * 4000,
                                          "parameters": {"type": "object"}}}
        for i in range(40)
    ]
    assert context_fit.tool_schema_tokens(schemas) > 30_000
    loop._run_round_compaction(messages, _ctx(schemas))
    assert ("checkpoint", "emergency_context_size") in calls


def test_emergency_compaction_arms_when_nothing_is_compactable(monkeypatch, tmp_path):
    """A frozen frame over the trigger with UNDER two tool rounds: the compactor
    structurally no-ops (`len(spans) <= keep_recent`), so running it bought nothing
    and wrote a forensic checkpoint every round. It must arm the hysteresis instead
    — and the arm has to hold with an EMPTY compactable region, where a 20%-growth
    test on zero never suppresses."""
    from ouroboros import context_fit, loop
    from ouroboros.context_budget import COMPACTION_HYSTERESIS_ROUNDS

    calls, progress, events = [], [], []
    monkeypatch.setattr(
        loop, "_persist_compaction_checkpoint",
        lambda m, **k: calls.append("checkpoint") or True,
    )
    monkeypatch.setattr(
        loop, "compact_tool_history_llm",
        lambda m, keep_recent, **k: (calls.append("compact") or (m, None)),
    )
    monkeypatch.setattr(loop, "_emit_checkpoint_event", lambda _q, _t, _l, row: events.append(row))
    monkeypatch.setattr(context_fit, "main_loop_token_density", lambda _dr, _m: 1.0)
    # A low-mode frozen frame (~630K chars) is already over the 400K trigger in the
    # task's FIRST rounds, before any tool round exists.
    monkeypatch.setattr(loop, "_estimate_messages_chars", lambda _m: 630_000)

    state = {}
    frame = [{"role": "system", "content": "frame"}, {"role": "user", "content": "go"}]

    def _ctx(round_idx):
        return loop._CompactionRoundContext(
            tools=SimpleNamespace(_ctx=SimpleNamespace(_pending_compaction=None, _accumulated_usage=state)),
            drive_root=tmp_path, drive_logs=tmp_path / "logs", task_id="task-nc",
            round_idx=round_idx, event_queue=None, active_use_local=False,
            active_context_mode="low", checkpoint_injected=True,
            emit_progress=progress.append,
        )

    for rnd in range(1, COMPACTION_HYSTERESIS_ROUNDS):
        loop._run_round_compaction(frame, _ctx(rnd))

    assert calls == []  # no summarizer call, no forensic checkpoint churn
    assert len(progress) == 1  # disclosed exactly once, not per round
    assert state["_compaction_hysteresis"] == {"round": 1, "region_chars": 0}
    assert [e["reason"] for e in events] == ["nothing_compactable"]


def test_emergency_compaction_hysteresis_suppresses_futile_refire(monkeypatch, tmp_path):
    """UTILITY/rearm measured against the REAL compactor in the submarine shape.

    The previous version of this pin stubbed the compactor to return the
    transcript UNCHANGED — the one shape where the growth bar is hard to cross,
    so it stayed green while production thrashed. Here only the light-model
    summarizer is stubbed; `compact_tool_history_llm` really collapses the older
    spans, which is exactly what used to satisfy the "region grew 1.2x" rearm on
    the very next tool round. Wave3 shape: low mode (400K trigger), an
    irreducible ~630K-char frame, and an agent that keeps calling tools.

    Measured on this driver: 34/35 rounds ran a summarizer + full-transcript
    rewrite before the floor-based rearm, 3/35 after.
    """
    from ouroboros import context_compaction, context_fit, loop
    from ouroboros.context_budget import COMPACTION_HYSTERESIS_ROUNDS

    summarizer_calls = []

    def _fake_batch(rendered_blocks, *, drive_root, task_id):
        summarizer_calls.append(len(rendered_blocks))
        return ({start: "summary" for start, _ in rendered_blocks}, None)

    monkeypatch.setattr(context_compaction, "_summarize_round_batch", _fake_batch)
    monkeypatch.setattr(loop, "_persist_compaction_checkpoint", lambda m, **k: True)
    monkeypatch.setattr(context_fit, "main_loop_token_density", lambda _dr, _m: 1.0)

    progress, events = [], []
    monkeypatch.setattr(loop, "_emit_checkpoint_event", lambda _q, _t, _l, row: events.append(row))
    state = {}

    def _ctx(round_idx):
        return loop._CompactionRoundContext(
            tools=SimpleNamespace(_ctx=SimpleNamespace(_pending_compaction=None, _accumulated_usage=state)),
            drive_root=tmp_path, drive_logs=tmp_path / "logs", task_id="task-hyst",
            round_idx=round_idx, event_queue=None, active_use_local=False,
            active_context_mode="low", checkpoint_injected=True,
            emit_progress=progress.append,
        )

    messages = [
        {"role": "system", "content": "F" * 630_000},
        {"role": "user", "content": "solve it"},
    ]
    rounds = 35
    fired = 0
    for rnd in range(1, rounds + 1):
        messages = messages + [
            {"role": "assistant", "content": f"r{rnd}",
             "tool_calls": [{"id": f"h{rnd}", "function": {"name": "shell", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"h{rnd}", "content": "R" * 20_000},
        ]
        before = len(summarizer_calls)
        messages, _usage = loop._run_round_compaction(messages, _ctx(rnd))
        fired += len(summarizer_calls) > before

    # Pre-fix this was 34/35 (every round after the first). The N-round window is
    # the only rearm left while the frozen frame alone carries the pressure.
    assert fired <= (rounds // COMPACTION_HYSTERESIS_ROUNDS) + 1, (
        f"summarizer/rewrite fired in {fired}/{rounds} rounds — the hysteresis is "
        "rearming on growth the pass itself created"
    )
    assert len(progress) == fired + 1  # one loud line per arm, never per round
    assert any("cannot help" in p and "frozen frame" in p for p in progress)
    armed = [e for e in events if e["checkpoint_kind"] == "compaction_hysteresis_armed"]
    assert [e["reason"] for e in armed][0] == "nothing_compactable"
    assert all(e["frame_bound"] for e in armed)  # the floor, not the region, is the blocker


def test_emergency_trigger_fire_points_are_pinned(monkeypatch, tmp_path):
    """The calibrated trigger's real magnitude, per density and tool envelope.

    `EMERGENCY_COMPACTION_CHARS` names 1.2M chars but is compared as a REAL-TOKEN
    budget, so density and the tool schemas both shrink the transcript the agent
    may hold. On the production Claude lane (density ~1.7, ~37K schema tokens)
    max mode fires at ~559K chars — 2.1x earlier than the constant reads. That is
    the owner's "necessity = total calibrated pressure" decision, but its
    magnitude must be a pinned, owner-visible number rather than a side effect of
    a units change: this test binary-searches the REAL decision so any drift is
    loud (it fails, for instance, if the threshold is density-scaled, which would
    cancel the calibration and restore the pre-v6.91 char semantics)."""
    from ouroboros import context_fit, loop
    from ouroboros.context_budget import EMERGENCY_COMPACTION_CHARS, LOW_EMERGENCY_COMPACTION_CHARS

    monkeypatch.setattr(loop, "_persist_compaction_checkpoint", lambda m, **k: True)

    def _fires(mode, density, schema_tokens, total_chars):
        fired = []
        monkeypatch.setattr(
            loop, "compact_tool_history_llm",
            lambda m, keep_recent, **k: (fired.append(1) or (m, None)),
        )
        monkeypatch.setattr(context_fit, "main_loop_token_density", lambda _dr, _m: density)
        monkeypatch.setattr(context_fit, "tool_schema_tokens", lambda _s: schema_tokens)
        per = max(1, total_chars // 6)
        msgs = []
        for i in range(6):
            msgs.append({
                "role": "assistant", "content": f"r{i}",
                "tool_calls": [{"id": f"h{i}", "function": {"name": "x", "arguments": "{}"}}],
            })
            msgs.append({"role": "tool", "tool_call_id": f"h{i}", "content": "R" * per})
        loop._run_round_compaction(msgs, loop._CompactionRoundContext(
            tools=SimpleNamespace(_ctx=SimpleNamespace(_pending_compaction=None, _accumulated_usage={})),
            drive_root=tmp_path, drive_logs=tmp_path / "logs", task_id="t",
            round_idx=3, event_queue=None, active_use_local=False,
            active_context_mode=mode, checkpoint_injected=True,
            emit_progress=lambda _m: None,
        ))
        return bool(fired)

    def _fire_point(mode, density, schema_tokens):
        lo, hi = 1_000, 4_000_000
        assert _fires(mode, density, schema_tokens, hi)
        while hi - lo > 5_000:
            mid = (lo + hi) // 2
            if _fires(mode, density, schema_tokens, mid):
                hi = mid
            else:
                lo = mid
        return hi

    tolerance = 15_000
    assert abs(_fire_point("max", 1.0, 0) - EMERGENCY_COMPACTION_CHARS) < tolerance
    assert abs(_fire_point("max", 1.0, 37_000) - 1_055_000) < tolerance
    assert abs(_fire_point("max", 1.7, 0) - 708_000) < tolerance
    assert abs(_fire_point("max", 1.7, 37_000) - 559_000) < tolerance
    assert abs(_fire_point("low", 1.0, 0) - LOW_EMERGENCY_COMPACTION_CHARS) < tolerance
    assert abs(_fire_point("low", 1.7, 37_000) - 91_000) < tolerance


def test_compaction_floor_blocks_rearm_only_while_the_frame_carries_the_pressure(monkeypatch, tmp_path):
    """The floor gate must not freeze a transcript a pass CAN still rescue.

    Same armed state, two frames: an irreducible frame over the trigger keeps the
    arm regardless of region growth; a small frame lets the ordinary 1.2x growth
    bar rearm on the next round."""
    from ouroboros import context_fit, loop

    calls, progress = [], []
    monkeypatch.setattr(
        loop, "_persist_compaction_checkpoint",
        lambda m, **k: calls.append("checkpoint") or True,
    )
    monkeypatch.setattr(
        loop, "compact_tool_history_llm",
        lambda m, keep_recent, **k: (calls.append("compact") or (m, None)),
    )
    monkeypatch.setattr(context_fit, "main_loop_token_density", lambda _dr, _m: 1.0)

    def _transcript(frame_chars, rounds):
        out = [{"role": "system", "content": "F" * frame_chars}]
        for i in range(rounds):
            out.append({
                "role": "assistant", "content": f"r{i}",
                "tool_calls": [{"id": f"h{i}", "function": {"name": "x", "arguments": "{}"}}],
            })
            out.append({"role": "tool", "tool_call_id": f"h{i}", "content": "R" * 60_000})
        return out

    def _ctx(round_idx, state):
        return loop._CompactionRoundContext(
            tools=SimpleNamespace(_ctx=SimpleNamespace(_pending_compaction=None, _accumulated_usage=state)),
            drive_root=tmp_path, drive_logs=tmp_path / "logs", task_id="task-floor",
            round_idx=round_idx, event_queue=None, active_use_local=False,
            active_context_mode="low", checkpoint_injected=True,
            emit_progress=progress.append,
        )

    armed = {"_compaction_hysteresis": {"round": 4, "region_chars": 120_000}}
    # Frame alone (~630K chars ≈ 157K real tokens) is over the 100K-token
    # trigger: no pass can reach it, so the grown region must NOT rearm.
    loop._run_round_compaction(_transcript(630_000, 6), _ctx(5, armed))
    assert calls == []

    # Same armed state, 40K-char frame: the pass CAN get under the trigger once
    # it drops the older spans, so the ordinary growth bar rearms it.
    small = {"_compaction_hysteresis": {"round": 4, "region_chars": 120_000}}
    loop._run_round_compaction(_transcript(40_000, 6), _ctx(5, small))
    assert calls == ["checkpoint", "compact"]


def test_compaction_floor_counts_protected_old_rounds(monkeypatch, tmp_path):
    """The floor must count older spans the COMPACTOR itself refuses to summarize
    (`_round_has_protected_content` — shared predicate, one SSOT). Omitting them
    forecast a reachable trigger no pass can reach: a transcript dominated by an
    old ⚠️-protected round then bought a futile summarizer pass + full-transcript
    rewrite on every 1.2x region growth — the exact recurring-futile-pass class
    the hysteresis exists to kill."""
    from ouroboros import context_fit, loop

    calls, progress = [], []
    monkeypatch.setattr(
        loop, "_persist_compaction_checkpoint",
        lambda m, **k: calls.append("checkpoint") or True,
    )
    monkeypatch.setattr(
        loop, "compact_tool_history_llm",
        lambda m, keep_recent, **k: (calls.append("compact") or (m, None)),
    )
    monkeypatch.setattr(context_fit, "main_loop_token_density", lambda _dr, _m: 1.0)

    def _transcript(protected):
        head = "⚠️ COMPACTION-PROTECTED review evidence\n" if protected else ""
        out = [
            {"role": "system", "content": "F" * 40_000},
            {"role": "assistant", "content": "r0",
             "tool_calls": [{"id": "h0", "function": {"name": "x", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "h0", "content": head + "P" * 500_000},
        ]
        for i in range(1, 6):
            out.append({
                "role": "assistant", "content": f"r{i}",
                "tool_calls": [{"id": f"h{i}", "function": {"name": "x", "arguments": "{}"}}],
            })
            out.append({"role": "tool", "tool_call_id": f"h{i}", "content": "R" * 60_000})
        return out

    # Floor accounting: the protected old span is part of the irreducible floor
    # (frame 40K + protected 500K + kept tail), and ONLY protection adds it.
    prot_msgs = _transcript(True)
    plain_msgs = _transcript(False)
    prot_floor = loop._compaction_floor_chars(prot_msgs, loop._tool_round_spans(prot_msgs))
    plain_floor = loop._compaction_floor_chars(plain_msgs, loop._tool_round_spans(plain_msgs))
    assert prot_floor - plain_floor >= 500_000

    def _ctx(state):
        return loop._CompactionRoundContext(
            tools=SimpleNamespace(_ctx=SimpleNamespace(_pending_compaction=None, _accumulated_usage=state)),
            drive_root=tmp_path, drive_logs=tmp_path / "logs", task_id="task-prot",
            round_idx=5, event_queue=None, active_use_local=False,
            active_context_mode="low", checkpoint_injected=True,
            emit_progress=progress.append,
        )

    # Armed + region grown past the bar: the protected round keeps the floor over
    # the 100K-token low trigger, so early rearm must NOT fire. Pre-fix the floor
    # omitted it (forecast ~85K tokens) and re-ran a futile pass here.
    armed = {"_compaction_hysteresis": {"round": 4, "region_chars": 120_000}}
    loop._run_round_compaction(_transcript(True), _ctx(armed))
    assert calls == []

    # Same shape unprotected: the pass CAN drop the old round, so growth rearms.
    small = {"_compaction_hysteresis": {"round": 4, "region_chars": 120_000}}
    loop._run_round_compaction(_transcript(False), _ctx(small))
    assert calls == ["checkpoint", "compact"]
