"""Phase 5 review lanes: the agent_session route as a delegated Claudexor run.

Deterministic OFFLINE fixtures (owner test rule: cheap, weak, no live harness):
a FakeGateway stands in for the Claudexor /v2 control plane with the same
semantics the real engine documents — capability catalog, idempotent starts,
terminal details, artifact serving — and a FakeLLM answers the one sanctioned
light-model extraction call. No network, no daemon, no subscription.
"""

import json
from types import SimpleNamespace

import pytest

from ouroboros import delegate_custody as custody
from ouroboros.review_execution import (
    REVIEW_SESSION_ROUTE_ENV,
    SCOPE_REVIEW_ROUTES_ENV,
    TRIAD_REVIEW_ROUTES_ENV,
    ReviewRouteKind,
    canonicalize_session_verdict,
    configured_review_routes,
)
from ouroboros.review_substrate import (
    ReviewRequest,
    ReviewSlot,
    reviewer_slots,
    run_review_request,
    scope_reviewer_slots,
)
from ouroboros.triad_review import empty_array_is_verified_clean


@pytest.fixture(autouse=True)
def _owned_gateway_uses_each_test_transport(monkeypatch):
    from ouroboros import claudexor_daemon
    from ouroboros.gateways import claudexor as gateway_module

    monkeypatch.setattr(
        claudexor_daemon,
        "ensure_owned_gateway",
        lambda: gateway_module.ClaudexorGateway(),
    )


# ---------------------------------------------------------------------------
# Offline fixtures
# ---------------------------------------------------------------------------


def _terminal_detail(text, *, state="succeeded", conformance="", truncated=False,
                     path="", reported_bytes=None, model="fake-small"):
    summary = {
        "state": state,
        "model": model,
        "spendUsd": 0.0,
        "spendEstimated": False,
    }
    if conformance:
        summary["outputConformance"] = conformance
    primary = {"text": text, "truncated": truncated}
    if path:
        primary["path"] = path
    if reported_bytes is not None:
        primary["bytes"] = reported_bytes
    return {"summary": summary, "primaryOutput": primary, "lastSeq": 3}


class FakeGateway:
    """The /v2 surface the executor drives, with recorded evidence."""

    instances = []
    catalog_entry = {}
    manifest_capabilities = {}
    detail = {}
    # Optional scripted behaviors.
    start_error = None            # exception raised on the FIRST start only
    artifact_bytes = None
    artifact_error = None
    nonterminal = False
    project_unregistered = False

    def __init__(self, *args, **kwargs):
        FakeGateway.instances.append(self)
        self.start_requests = []
        self.start_keys = []
        self.cancels = []
        self.artifact_gets = []
        self.health_asked = []
        self.project_lookups = []
        self.registrations = []
        self.removals = []
        self.engine_version = "3.3.7"

    @classmethod
    def reset(cls):
        # Faithful to the real /v2 split: the agent-capability catalog row
        # (CatalogHarness) carries NO transport flags — json_schema_output and
        # interactive live only on the /v2/harnesses row's manifest. A fixture
        # that invents a catalog flag would keep alive exactly the dead read
        # this suite exists to catch.
        cls.instances = []
        cls.catalog_entry = {
            "id": "fake-review", "enabled": True, "status": "ok",
            "accessProfilesSupported": ["readonly", "workspace_write"],
        }
        cls.manifest_capabilities = {"json_schema_output": True}
        cls.detail = _terminal_detail('{"findings": []}', conformance="passed")
        cls.start_error = None
        cls.artifact_bytes = None
        cls.artifact_error = None
        cls.nonterminal = False
        cls.project_unregistered = False

    def handshake(self, **_kw):
        return {"compatible": True, "protocolMajor": 3, "engine": {"version": self.engine_version}}

    def agent_capabilities(self):
        return {"harnesses": [dict(FakeGateway.catalog_entry)]}

    def harnesses(self):
        return [{
            "id": FakeGateway.catalog_entry["id"],
            "status": FakeGateway.catalog_entry.get("status", "ok"),
            "manifest": {"capabilities": dict(FakeGateway.manifest_capabilities)},
        }]

    def quota_snapshots(self):
        return []

    def find_project_id(self, root):
        self.project_lookups.append(root)
        return "" if FakeGateway.project_unregistered else "proj-1"

    def register_project(self, root):
        self.registrations.append(root)
        return "proj-new"

    def remove_project(self, project_id):
        self.removals.append(project_id)
        return {"removed": True}

    def start_run(self, request, *, idempotency_key=""):
        self.start_requests.append(dict(request))
        self.start_keys.append(str(idempotency_key))
        if FakeGateway.start_error is not None:
            exc = FakeGateway.start_error
            FakeGateway.start_error = None
            raise exc
        return {"runId": "run-1", "runDir": "/tmp/fake-run"}

    def get_run(self, run_id, **_kw):
        if FakeGateway.nonterminal:
            return {"summary": {"state": "running"}, "lastSeq": 1}
        return json.loads(json.dumps(FakeGateway.detail))

    def get_run_artifact(self, run_id, path):
        self.artifact_gets.append((run_id, path))
        if FakeGateway.artifact_error is not None:
            raise FakeGateway.artifact_error
        return FakeGateway.artifact_bytes or b""

    def cancel_run(self, run_id, *, reason=""):
        self.cancels.append((run_id, reason))
        return {"accepted": True}

    def close(self):
        pass


class FakeLLM:
    """Answers only the light-model extraction call."""

    def __init__(self, reply="[]"):
        self.reply = reply
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return {"content": self.reply}, {"prompt_tokens": 5, "completion_tokens": 2, "cost": 0.0001}


@pytest.fixture()
def fake_route(monkeypatch):
    FakeGateway.reset()
    monkeypatch.setattr("ouroboros.gateways.claudexor.ClaudexorGateway", FakeGateway)
    monkeypatch.setenv(REVIEW_SESSION_ROUTE_ENV, "fake-review=fake-small:low")
    monkeypatch.delenv(TRIAD_REVIEW_ROUTES_ENV, raising=False)
    monkeypatch.delenv(SCOPE_REVIEW_ROUTES_ENV, raising=False)
    # Custody memoization is process-local; a stale entry from another test's
    # run-1 would confuse ownership replay.
    custody._CUSTODY.clear()
    return FakeGateway


def _agent_request(**overrides):
    base = dict(
        surface="scope_review",
        goal="Review the staged change.",
        task_id="t-agent",
        call_type="scope_review",
        session_root="/tmp/fake-repo",
        session_task="Review the staged diff of this repository: run `git diff --cached`.",
    )
    base.update(overrides)
    return ReviewRequest(**base)


def _agent_slot(**overrides):
    base = dict(slot_id="scope_slot_1", model="api/model-a", timeout_sec=30,
                route=ReviewRouteKind.AGENT_SESSION)
    base.update(overrides)
    return ReviewSlot(**base)


# ---------------------------------------------------------------------------
# 5.4 — the typed verdict
# ---------------------------------------------------------------------------


def test_schema_conformant_clean_verdict_survives(tmp_path, fake_route):
    """The full clean path: schema asked, conformance passed, empty findings —
    the actor's raw text is the bare `[]` the constitutional predicate accepts."""
    llm = FakeLLM()
    result = run_review_request(_agent_request(), slots=[_agent_slot()],
                                drive_root=tmp_path, llm=llm)
    actor = result.actors[0]
    assert actor["status"] == "ok"
    assert actor["raw_text"] == "[]"
    assert empty_array_is_verified_clean(actor["raw_text"])
    assert actor["usage"]["verdict_method"] == "schema"
    assert llm.calls == []  # no extraction spent, no api pack sent

    gateway = fake_route.instances[0]
    start = gateway.start_requests[0]
    assert start["authPreference"] == "subscription"
    assert start["access"] == "readonly" and start["mode"] == "ask"
    assert start["primaryHarness"] == "fake-review"
    assert start["model"] == "fake-small" and start["effort"] == "low"
    assert start["maxSeconds"] == 30
    # The manifest declared a non-interactive structured-output transport, so
    # the schema was asked on the EFFECTIVE route (D19).
    assert "outputSchema" in start
    assert gateway.start_keys[0]  # the invocation id rode the wire


def test_schema_is_not_asked_when_the_manifest_does_not_declare_it(tmp_path, fake_route):
    fake_route.manifest_capabilities = {}
    fake_route.detail = _terminal_detail("[]")
    result = run_review_request(_agent_request(), slots=[_agent_slot()],
                                drive_root=tmp_path, llm=FakeLLM())
    assert "outputSchema" not in fake_route.instances[0].start_requests[0]
    actor = result.actors[0]
    assert actor["raw_text"] == "[]"
    assert actor["usage"]["verdict_method"] == "strict"
    # Landing below the requested structured verdict is disclosed (D4).
    deltas = actor["usage"]["capability_delta"]
    assert any(d["reason"] == "schema_unavailable_on_effective_route" for d in deltas)


def test_the_run_request_pins_the_route_as_the_explicit_pool(tmp_path, fake_route):
    """CLAIM-1 regression: `primaryHarness` alone only fronts the engine's
    auto-pool (orchestrator orderPool) — other doctor-OK harnesses stay
    eligible and a plain ask run fails over across them. The honest pinning
    contract is the explicit one-element `harnesses` pool; `n` must stay off
    the wire because the engine refuses it on plain ask-mode runs."""
    run_review_request(_agent_request(), slots=[_agent_slot()],
                       drive_root=tmp_path, llm=FakeLLM())
    start = fake_route.instances[0].start_requests[0]
    assert start["harnesses"] == ["fake-review"]
    assert start["primaryHarness"] == "fake-review"
    assert "n" not in start  # refused by mode/strategy coherence on ask runs


def test_schema_is_not_asked_on_an_interactive_transport_route(tmp_path, fake_route):
    """D19: the manifest's `json_schema_output` describes the ADAPTER, not the
    transport. An interactive-capable lane is refused `outputSchema` outright
    by the engine under a daemon (it always arms an interaction channel), so
    asking would kill the whole run typed. The effective-route decision must
    therefore look at BOTH flags — and the landing below the ask is loud."""
    fake_route.manifest_capabilities = {"json_schema_output": True, "interactive": True}
    fake_route.detail = _terminal_detail("[]")
    result = run_review_request(_agent_request(), slots=[_agent_slot()],
                                drive_root=tmp_path, llm=FakeLLM())
    assert "outputSchema" not in fake_route.instances[0].start_requests[0]
    actor = result.actors[0]
    assert actor["raw_text"] == "[]"  # the verdict still lands, via strict parse
    deltas = actor["usage"]["capability_delta"]
    assert any(d["reason"] == "schema_unavailable_on_effective_route" for d in deltas)


def test_a_run_reported_off_the_pinned_route_is_disclosed(tmp_path, fake_route):
    """Belt over the pin: the engine's own receipt of the pool the run used
    must echo the pinned route; drift surfaces as a capability_delta, never as
    a quietly accepted substitute route."""
    fake_route.detail = _terminal_detail('{"findings": []}', conformance="passed")
    fake_route.detail["summary"]["harnesses"] = ["other-route"]
    result = run_review_request(_agent_request(), slots=[_agent_slot()],
                                drive_root=tmp_path, llm=FakeLLM())
    deltas = result.actors[0]["usage"]["capability_delta"]
    assert any(d["reason"] == "session_ran_off_pinned_route" for d in deltas)


def test_conformance_gate_is_the_gate_not_run_success(tmp_path, fake_route):
    """A successful run WITHOUT outputConformance == passed is narrative: the
    strict parser judges it, never the run's success."""
    fake_route.detail = _terminal_detail('{"findings": []}')  # no conformance
    llm = FakeLLM(reply="UNEXTRACTABLE")
    result = run_review_request(_agent_request(), slots=[_agent_slot()],
                                drive_root=tmp_path, llm=llm)
    actor = result.actors[0]
    # '{"findings": []}' is not the contract's array shape: strict refuses, the
    # extractor was consulted and refused too, so the raw narrative survives
    # for forensics instead of being blessed as clean.
    assert actor["raw_text"] == '{"findings": []}'
    assert not empty_array_is_verified_clean(actor["raw_text"])
    assert actor["usage"]["verdict_method"] == "unparsed"
    # The schema was asked and not conformed: the delta is disclosed.
    deltas = actor["usage"]["capability_delta"]
    assert any(d["reason"] == "schema_not_conformed_on_effective_route" for d in deltas)


def test_narrative_clean_verdict_survives_via_light_extraction(tmp_path, fake_route):
    """D19's whole point: a session that says 'no issues found' in prose now
    yields a recognised CLEAN verdict — via the light model, not via loosening
    the constitutional predicate."""
    fake_route.manifest_capabilities = {}
    fake_route.detail = _terminal_detail(
        "I read the staged diff and the touched files. Everything is consistent; "
        "I found no issues worth reporting."
    )
    llm = FakeLLM(reply="[]")
    result = run_review_request(_agent_request(), slots=[_agent_slot()],
                                drive_root=tmp_path, llm=llm)
    actor = result.actors[0]
    assert actor["raw_text"] == "[]"
    assert empty_array_is_verified_clean(actor["raw_text"])
    assert actor["usage"]["verdict_method"] == "light_model_extraction"
    # Exactly one session launch and exactly one extraction call.
    assert len(fake_route.instances[0].start_requests) == 1
    assert len(llm.calls) == 1
    assert llm.calls[0]["reasoning_effort"] == "low"
    # The delta is disclosed on the actor record AND durably.
    deltas = actor["usage"]["capability_delta"]
    assert any(d["reason"] == "extraction_instead_of_schema" for d in deltas)
    rows = [json.loads(line) for line in
            (tmp_path / "logs" / "events.jsonl").read_text().splitlines()]
    assert any(r.get("type") == "review_slot_capability_delta"
               and r.get("verdict_method") == "light_model_extraction" for r in rows)


def test_narrative_findings_pass_through_strict_untouched(tmp_path, fake_route):
    findings = '[{"item": "x", "verdict": "FAIL", "severity": "critical", "reason": "r"}]'
    fake_route.manifest_capabilities = {}
    fake_route.detail = _terminal_detail(findings)
    llm = FakeLLM()
    result = run_review_request(_agent_request(), slots=[_agent_slot()],
                                drive_root=tmp_path, llm=llm)
    actor = result.actors[0]
    assert actor["raw_text"] == findings
    assert actor["usage"]["verdict_method"] == "strict"
    assert llm.calls == []


def test_strict_requires_the_whole_answer_never_an_embedded_array(tmp_path, fake_route):
    """A transcript that merely CONTAINS a JSON array is not a verdict.

    ``_strictly_parseable`` used to SCAN with ``extract_json_array``, so a
    refusal that quoted the contract's own example was passed through
    byte-identical as a TRUSTED ``strict`` verdict and the extraction rail never
    ran. Downstream that is not cosmetic: the quoted example carries ``item`` and
    ``verdict`` keys, so the actor projected ``parse_status='valid'`` /
    ``semantic_verdict='PASS'`` — a clean quorum vote from a session that
    reviewed nothing.
    """
    from ouroboros.review_execution import _strictly_parseable

    refusal = (
        'I reviewed NOTHING: the sandbox denied every read. The contract asked '
        'for entries like [{"item": "P1 honesty", "verdict": "PASS", '
        '"severity": "advisory", "reason": "example only"}]. Please re-run.'
    )
    assert not _strictly_parseable(refusal)

    # The bare payload — the shape the contract actually asks for — stays strict.
    assert _strictly_parseable('[{"item": "x", "verdict": "FAIL"}]')
    assert _strictly_parseable("[]")

    # End to end: the refusal reaches extraction instead of being trusted, and
    # UNEXTRACTABLE keeps the raw narrative rather than inventing a verdict.
    fake_route.manifest_capabilities = {}
    fake_route.detail = _terminal_detail(refusal)
    llm = FakeLLM(reply="UNEXTRACTABLE")
    result = run_review_request(_agent_request(), slots=[_agent_slot()],
                                drive_root=tmp_path, llm=llm)
    actor = result.actors[0]
    assert actor["usage"]["verdict_method"] == "unparsed"
    assert len(llm.calls) == 1  # extraction was consulted, not short-circuited


def test_extraction_never_fabricates_a_clean_verdict():
    """A refusal canonicalizes to UNEXTRACTABLE, never to []: the light model's
    contract forbids blessing a non-review as clean, and an UNEXTRACTABLE reply
    leaves the raw narrative in place (upstream marks it a parse failure)."""
    llm = FakeLLM(reply="UNEXTRACTABLE")
    text, method, _usage = canonicalize_session_verdict(
        "I cannot review this diff.", conformance_passed=False, llm=llm)
    assert text == "I cannot review this diff."
    assert method == "unparsed"


def test_a_fenced_verdict_stays_unparsed_at_this_layer():
    """Disclosed residual (audit 2026-08-05): a verdict wrapped in a ```json
    fence that only the coordinator's downstream scanner parses is telemetered
    `unparsed` HERE — labeling it would need a duplicate parser (drift) or a
    backward import of the coordinator (the one-way seam ARCHITECTURE pins).
    The refusal-quoting-an-array case also stays `unparsed`."""
    llm = FakeLLM(reply="UNEXTRACTABLE")
    fenced = (
        "Review complete. My findings:\n```json\n"
        '[{"item": "x", "verdict": "FAIL", "severity": "critical", "reason": "r"}]\n'
        "```\nEnd of review."
    )
    text, method, _usage = canonicalize_session_verdict(
        fenced, conformance_passed=False, llm=llm)
    assert method == "unparsed"
    assert text == fenced
    assert len(llm.calls) == 1  # extraction was still consulted first (D19 order)

    quoted = ('I reviewed NOTHING. The contract asked for entries like '
              '[{"item": "a", "verdict": "PASS", "severity": "advisory", "reason": "e"}].')
    _text2, method2, _u2 = canonicalize_session_verdict(
        quoted, conformance_passed=False, llm=FakeLLM(reply="UNEXTRACTABLE"))
    assert method2 == "unparsed"

def test_extraction_runs_under_its_own_physical_rail():
    """The extraction is NOT a review call: it claims no send from the reviewing
    actor's two-physical-send rail (D19)."""
    from ouroboros.usage_accounting import physical_attempt_limit

    class RailProbeLLM(FakeLLM):
        def chat(self, **kwargs):
            from ouroboros.usage_accounting import _claim_physical_dispatch

            _claim_physical_dispatch()  # what a real provider send does
            return super().chat(**kwargs)

    llm = RailProbeLLM(reply="[]")
    with physical_attempt_limit(0):  # the actor rail is EXHAUSTED
        text, method, _usage = canonicalize_session_verdict(
            "narrative: clean.", conformance_passed=False, llm=llm)
    assert method == "light_model_extraction" and text == "[]"


def test_extraction_reads_the_whole_transcript_no_window():
    """CLAIM-2 regression: the extraction rail must see the artifact WHOLE. A
    head+tail window silently dropped everything mid-transcript, so a finding
    reported there never reached the light model — and a faithful "[]" over
    the visible cut fabricated a verified-clean verdict."""
    finding = ("CRITICAL FINDING: the retry path drops the durable ledger row "
               "(reported verbatim, mid-transcript).")
    raw = ("transcript begins\n" + "context line\n" * 500      # > old 4k head
           + finding + "\n"
           + "trailing tool output\n" * 6_000)                 # > old 60k tail
    canonical = ('[{"item": "retry path", "verdict": "FAIL", '
                 '"severity": "critical", "reason": "drops the ledger row"}]')
    llm = FakeLLM(reply=canonical)
    text, method, _usage = canonicalize_session_verdict(
        raw, conformance_passed=False, llm=llm)
    assert method == "light_model_extraction"
    assert text == canonical
    prompt = llm.calls[0]["messages"][0]["content"]
    assert finding in prompt  # the mid-transcript finding reached extraction


def test_oversized_transcript_is_typed_extraction_incomplete_never_clean(tmp_path, fake_route):
    """The single-send rail has one hard bound; past it extraction REFUSES with
    the typed disposition — no send at all, so no windowed read a light model
    could faithfully canonicalize into a clean verdict."""
    from ouroboros.review_execution import _EXTRACT_MAX_CHARS

    big = "narrative that never parses\n" * (_EXTRACT_MAX_CHARS // 20)
    assert len(big) > _EXTRACT_MAX_CHARS
    llm = FakeLLM(reply="[]")  # would fabricate clean IF it were consulted
    text, method, _usage = canonicalize_session_verdict(
        big, conformance_passed=False, llm=llm)
    assert method == "extraction_incomplete"
    assert text == big                       # forensics keep the raw transcript
    assert not empty_array_is_verified_clean(text)
    assert llm.calls == []                   # the light model was never shown a cut

    # End to end: the slot discloses the refusal as a capability_delta and the
    # verdict method rides the actor record — never a silent clean.
    fake_route.manifest_capabilities = {}
    fake_route.detail = _terminal_detail(big)
    result = run_review_request(_agent_request(), slots=[_agent_slot()],
                                drive_root=tmp_path, llm=llm)
    actor = result.actors[0]
    assert actor["usage"]["verdict_method"] == "extraction_incomplete"
    assert not empty_array_is_verified_clean(actor["raw_text"])
    deltas = actor["usage"]["capability_delta"]
    assert any(d["reason"] == "extraction_incomplete_transcript_exceeds_bound"
               for d in deltas)
    assert llm.calls == []


# ---------------------------------------------------------------------------
# Delivery mechanics
# ---------------------------------------------------------------------------


def test_failed_session_state_is_an_error_actor_not_a_verdict(tmp_path, fake_route):
    fake_route.detail = _terminal_detail("partial…", state="failed")
    result = run_review_request(_agent_request(), slots=[_agent_slot()],
                                drive_root=tmp_path, llm=FakeLLM())
    actor = result.actors[0]
    assert actor["status"] == "error"
    assert "ended failed" in actor["error"]


def test_applied_access_is_the_receipt_alone_never_the_request_echoed_back(tmp_path, fake_route):
    """`applied_access` promises APPLIED facts, verbatim from the run's own telemetry
    receipt. The daemon computes `access` as `effectiveAccess ?? the client's own parsed
    request`, so falling back to it published our ASK as if the engine had confirmed it —
    the same non-witness `_widened_access` already refuses to read."""
    detail = _terminal_detail("[]", conformance="passed")
    detail["summary"]["access"] = "workspace_write"   # the request, echoed
    fake_route.detail = detail
    result = run_review_request(_agent_request(), slots=[_agent_slot()],
                                drive_root=tmp_path, llm=FakeLLM())
    assert result.actors[0]["usage"]["applied_access"] == ""

    detail = _terminal_detail("[]", conformance="passed")
    detail["summary"]["effectiveAccess"] = "readonly"  # the derived witness
    detail["summary"]["access"] = "workspace_write"
    fake_route.detail = detail
    custody._CUSTODY.clear()
    result = run_review_request(_agent_request(), slots=[_agent_slot()],
                                drive_root=tmp_path / "b", llm=FakeLLM())
    assert result.actors[0]["usage"]["applied_access"] == "readonly"


def _exhausted_window_detail():
    """A terminal whose RunFailure states a spent subscription window, verbatim in the
    engine's own shape (`RunFailureCode` + the STRUCTURAL `resetsAt`)."""
    detail = _terminal_detail("", state="failed")
    detail["summary"]["failure"] = {
        "phase": "routing", "category": "harness_unavailable",
        "code": "subscription_window_exhausted",
        "safeMessage": "every credential profile for this route is spent",
        "resetsAt": "2030-01-01T00:00:00Z",
        "nextActions": ["wait for the window to reopen"],
    }
    return detail


def test_a_typed_terminal_refusal_keeps_its_code_and_its_reset_time(tmp_path, fake_route):
    """The engine says WHY in a typed RunFailure. Flattening it into prose — and
    truncating that prose at 500 chars — threw away both the `code` a caller
    classifies on and the `resetsAt` it is meant to schedule against."""
    from ouroboros.gateways.claudexor import ClaudexorSubscriptionWindowExhausted
    from ouroboros.review_execution import AgentSessionReviewExecutor, ReviewAssignment

    fake_route.detail = _exhausted_window_detail()
    executor = AgentSessionReviewExecutor(
        ReviewAssignment(request=_agent_request(), slot=_agent_slot(),
                         call_id="c-window", call_type="scope_review",
                         custody_root=tmp_path),
        llm=FakeLLM(),
    )
    with pytest.raises(ClaudexorSubscriptionWindowExhausted) as excinfo:
        executor.execute()
    assert excinfo.value.code == "subscription_window_exhausted"
    assert excinfo.value.reset_at == "2030-01-01T00:00:00Z"


def test_a_typed_refusal_is_not_relaunched_into_a_second_billed_session(tmp_path, fake_route):
    """The P3 slot rail is allowed two physical sends, for a transport transient or a
    format repair. A typed Claudexor refusal is neither: it says "this transport is not
    usable", so the second send is a deterministic re-refusal that spends vendor money
    for zero extra verdicts."""
    fake_route.detail = _exhausted_window_detail()
    result = run_review_request(_agent_request(), slots=[_agent_slot()],
                                drive_root=tmp_path, llm=FakeLLM())
    assert sum(len(inst.start_requests) for inst in fake_route.instances) == 1
    actor = result.actors[0]
    assert actor["status"] == "error"
    assert "subscription_window_exhausted" in actor["error"]


def test_timeout_cancels_the_run_and_fails_typed(tmp_path, fake_route):
    """The nanny owns the time cap: a run that never terminates is cancelled
    through the verified-cancel path and the slot fails as an ordinary timeout.
    Driven at the executor (the coordinator's own queue wait shares the same
    clock, so an end-to-end race would test the scheduler, not the cap)."""
    from ouroboros.review_execution import AgentSessionReviewExecutor, ReviewAssignment

    fake_route.nonterminal = True
    executor = AgentSessionReviewExecutor(
        ReviewAssignment(request=_agent_request(), slot=_agent_slot(timeout_sec=1),
                         call_id="c-timeout", call_type="scope_review",
                         custody_root=tmp_path),
        llm=FakeLLM(),
    )
    with pytest.raises(TimeoutError):
        executor.execute()
    assert any(reason == "review_slot_timeout"
               for _rid, reason in fake_route.instances[0].cancels)


def test_truncated_primary_output_is_resolved_from_the_full_artifact(tmp_path, fake_route):
    """D7: the verdict is read from the FULL artifact, never a bounded preview."""
    full = "narrative " * 10 + "\n[]\nNO_FINDINGS"
    fake_route.manifest_capabilities = {}
    fake_route.detail = _terminal_detail(full[:20], truncated=True, path="primary.md",
                                         reported_bytes=len(full.encode()))
    fake_route.artifact_bytes = full.encode()
    result = run_review_request(_agent_request(), slots=[_agent_slot()],
                                drive_root=tmp_path, llm=FakeLLM())
    actor = result.actors[0]
    assert fake_route.instances[0].artifact_gets == [("run-1", "primary.md")]
    assert actor["status"] == "ok"
    assert empty_array_is_verified_clean(actor["raw_text"])


def test_unresolvable_truncated_output_refuses_instead_of_judging_a_preview(tmp_path, fake_route):
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    fake_route.detail = _terminal_detail("head…", truncated=True, path="primary.md",
                                         reported_bytes=999_999)
    fake_route.artifact_error = ClaudexorUnavailable("http_410", "reclaimed", status_code=410)
    result = run_review_request(_agent_request(), slots=[_agent_slot()],
                                drive_root=tmp_path, llm=FakeLLM())
    actor = result.actors[0]
    assert actor["status"] == "error"
    assert "never read from a preview" in actor["error"]
    # And it is refused ONCE. The session SUCCEEDED and was fully billed; only
    # reading its transcript back failed, deterministically (the artifact is
    # reclaimed — a second identical fetch cannot find it). Relaunching bought a
    # second billed session and no second verdict.
    assert sum(len(inst.start_requests) for inst in fake_route.instances) == 1


def test_transport_retry_reuses_the_pending_invocation_id(tmp_path, fake_route):
    """Job-4 scheme: an indefinite start failure leaves the invocation PENDING,
    and the slot's permitted retry presents the SAME wire key with the same
    body, so the engine can return the run it already accepted."""
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    fake_route.start_error = ClaudexorUnavailable("daemon_unreachable", "boom", status_code=0)
    result = run_review_request(_agent_request(), slots=[_agent_slot()],
                                drive_root=tmp_path, llm=FakeLLM())
    assert result.actors[0]["status"] == "ok"  # the retry launched and finished
    keys = [k for inst in fake_route.instances for k in inst.start_keys]
    assert len(keys) == 2 and keys[0] == keys[1]
    bodies = [b for inst in fake_route.instances for b in inst.start_requests]
    assert bodies[0] == bodies[1]  # byte-identical replay, maxSeconds included


def _run_session_directly(tmp_path, **overrides):
    """Call the shared session runner with explicit knobs (the B-class surface)."""
    from ouroboros.review_execution import (
        SessionInvocation, run_delegated_review_session,
    )

    invocation = dict(task_id="t-b", surface="scope_review", slot_id="scope_slot_1",
                      timeout_sec=30)
    kwargs = dict(prompt="review this", root="/tmp/fake-repo", custody_drive=tmp_path)
    for key in list(overrides):
        if key in ("task_id", "surface", "slot_id", "timeout_sec", "logical_key_extra",
                   "output_schema", "session_route", "instructions", "retry_state"):
            invocation[key] = overrides.pop(key)
    kwargs.update(overrides)
    return run_delegated_review_session(invocation=SessionInvocation(**invocation), **kwargs)


def _lineage_scope():
    from ouroboros.usage_accounting import UsageScope

    return UsageScope(task_id="t-agent", root_task_id="t-root", parent_task_id="t-parent")


def _custody_rows(drive_root):
    return [json.loads(line) for line in
            custody.event_log_path(drive_root).read_text().splitlines() if line.strip()]


def test_custody_rows_carry_lineage_from_the_bound_usage_scope(tmp_path, fake_route):
    """#112: BOTH custody writers — the pre-POST request row and the STARTED
    row — carry root/parent from the ambient UsageScope (the coordinator binds
    review_usage_scope per slot thread). Unbound stays EMPTY: the settlement
    layer owns the task_id fallback convention, never these writers."""
    from ouroboros.usage_accounting import usage_scope

    with usage_scope(_lineage_scope()):
        _run_session_directly(tmp_path, task_id="t-agent")

    rows = _custody_rows(tmp_path)
    requested = [r for r in rows if r["type"] == custody.START_REQUESTED]
    started = [r for r in rows if r["type"] == custody.STARTED]
    assert requested and started
    for row in (requested[-1], started[-1]):
        assert row["root_task_id"] == "t-root", row
        assert row["parent_task_id"] == "t-parent", row

    # No ambient scope → empty lineage, never an `or task_id` fallback here.
    custody._CUSTODY.clear()
    _run_session_directly(tmp_path / "unbound", task_id="t-agent")
    unbound = [r for r in _custody_rows(tmp_path / "unbound")
               if r["type"] == custody.STARTED]
    assert unbound and unbound[-1]["root_task_id"] == ""
    assert unbound[-1]["parent_task_id"] == ""


def test_restart_reconciliation_settles_review_spend_to_the_recorded_root(
    tmp_path, fake_route, monkeypatch
):
    """#112 Path A: a run whose worker died before settling is reconciled by
    the SUPERVISOR (no ambient scope). The replayed custody must carry the
    recorded lineage, so the subscription-session ledger row lands on the real
    root "t-root" — not on the review's own task id as a fake root."""
    import ouroboros.usage_accounting as ua
    from ouroboros.usage_accounting import usage_scope

    # The live run's ledger write fails, leaving an unsettled STARTED row.
    with monkeypatch.context() as m:
        m.setattr(ua, "record_subscription_session",
                  lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ledger down")))
        with usage_scope(_lineage_scope()):
            facts = _run_session_directly(tmp_path, task_id="t-agent")
    assert facts["settlement"]["settled"] is False

    # Restart: the in-process memo is gone and no scope is bound.
    custody._CUSTODY.clear()
    outcomes = custody.reconcile_orphaned_runs(
        tmp_path, running_task_ids=set(), gateway_factory=lambda: FakeGateway(),
    )
    assert [o["action"] for o in outcomes] == ["settled"]

    ledger = [json.loads(line) for line in
              (tmp_path / "state" / "usage_attempts.jsonl").read_text().splitlines()
              if line.strip()]
    sessions = [r for r in ledger if r.get("kind") == "subscription_session"]
    assert sessions, "reconciliation must write the subscription-session row"
    assert sessions[-1]["task_id"] == "t-agent"
    assert sessions[-1]["root_task_id"] == "t-root", sessions[-1]
    assert sessions[-1]["parent_task_id"] == "t-parent"


def test_pending_invocation_recovery_replays_the_recorded_lineage(tmp_path, fake_route):
    """#112 Path B: a start whose POST outcome stayed unknown leaves ONLY the
    START_REQUESTED row. Its pending-invocation record must carry the lineage,
    and the sweep's recovery must replay it onto the recovered run's custody
    and ledger row."""
    from ouroboros.gateways.claudexor import ClaudexorUnavailable
    from ouroboros.usage_accounting import usage_scope

    fake_route.start_error = ClaudexorUnavailable("daemon_unreachable", "boom", status_code=0)
    state: dict = {}
    with usage_scope(_lineage_scope()):
        with pytest.raises(ClaudexorUnavailable):
            _run_session_directly(tmp_path, task_id="t-agent", retry_state=state)
    assert state["pending_invocation_id"]

    pending = custody.pending_invocations(tmp_path)
    assert len(pending) == 1
    record = pending[0]
    assert record["root_task_id"] == "t-root"
    assert record["parent_task_id"] == "t-parent"

    # The sweep recovers the invocation with NO ambient scope: the stored
    # record is the single source of the replay's facts, lineage included.
    result = custody._recover_pending_invocation(tmp_path, FakeGateway(), record)
    assert result["action"] == "settled"
    recovered = [r for r in _custody_rows(tmp_path)
                 if r["type"] == custody.STARTED
                 and r.get("recovered_from_pending_invocation")]
    assert recovered and recovered[-1]["root_task_id"] == "t-root"
    assert recovered[-1]["parent_task_id"] == "t-parent"
    ledger = [json.loads(line) for line in
              (tmp_path / "state" / "usage_attempts.jsonl").read_text().splitlines()
              if line.strip()]
    sessions = [r for r in ledger if r.get("kind") == "subscription_session"]
    assert sessions and sessions[-1]["root_task_id"] == "t-root"


def test_retry_replays_the_stored_route_and_registers_nothing_new(tmp_path, fake_route,
                                                                 monkeypatch):
    """A retry replays the STORED invocation, so every fact about it comes from the
    record — not from the environment as it stands at retry time.

    The old order computed the current route's project, key and schema ask BEFORE
    reading the pending invocation, so a retry POSTed the recorded body while checking
    the health of a route the run never used, re-registering a project the original
    attempt already bound, and writing a durable record that contradicted the bytes on
    the wire.
    """
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    # Attempt 1: indefinite failure leaves the invocation PENDING.
    state: dict = {}
    fake_route.start_error = ClaudexorUnavailable("daemon_unreachable", "boom", status_code=0)
    with pytest.raises(ClaudexorUnavailable):
        _run_session_directly(tmp_path, retry_state=state)
    pending = state["pending_invocation_id"]
    assert pending

    # The environment is RECONFIGURED between the attempts.
    monkeypatch.setenv(REVIEW_SESSION_ROUTE_ENV, "other-route=other-model:high")
    before = [len(i.registrations) for i in fake_route.instances]

    facts = _run_session_directly(tmp_path, retry_state=state)

    assert facts["idempotent_recovery"] is True
    # The replay ran the ORIGINAL route, not the reconfigured one.
    assert facts["route_id"] == "fake-review"
    retry_gateway = fake_route.instances[-1]
    assert retry_gateway.start_requests[0]["primaryHarness"] == "fake-review"
    assert retry_gateway.start_requests[0]["harnesses"] == ["fake-review"]
    # No project lookup or registration happened on the retry: the original
    # attempt's project rides the record.
    assert retry_gateway.project_lookups == []
    assert retry_gateway.registrations == []
    assert sum(before) == sum(len(i.registrations) for i in fake_route.instances)
    # Same wire key, byte-identical body.
    assert retry_gateway.start_keys == [pending]


def test_retry_refuses_typed_when_the_stored_prompt_diverges(tmp_path, fake_route):
    """The replay sends the RECORDED bytes. If this call describes a different review,
    that is a typed refusal — never a silent review of something else."""
    from ouroboros.gateways.claudexor import ClaudexorUnavailable
    from ouroboros.review_execution import ReviewRouteUnavailable

    state: dict = {}
    fake_route.start_error = ClaudexorUnavailable("daemon_unreachable", "boom", status_code=0)
    with pytest.raises(ClaudexorUnavailable):
        _run_session_directly(tmp_path, retry_state=state, prompt="review THIS")

    with pytest.raises(ReviewRouteUnavailable, match="prompt"):
        _run_session_directly(tmp_path, retry_state=state, prompt="review SOMETHING ELSE")
    with pytest.raises(ReviewRouteUnavailable, match="session root"):
        _run_session_directly(tmp_path, retry_state=state, prompt="review THIS",
                              root="/tmp/other-repo")


def test_definite_refusal_retires_the_registration_it_orphaned(tmp_path, fake_route):
    """A DEFINITE 4xx proves no run bound this registration, so the project this
    start created is retired. Only then: an unknown outcome must never destroy state
    a live run may still be using."""
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    # This start registers the project itself (nothing pre-existing to reuse).
    fake_route.project_unregistered = True
    fake_route.start_error = ClaudexorUnavailable("bad_request", "nope", status_code=400)
    state: dict = {}
    with pytest.raises(ClaudexorUnavailable):
        _run_session_directly(tmp_path, retry_state=state)

    gateway = fake_route.instances[-1]
    assert gateway.registrations == ["/tmp/fake-repo"]
    assert gateway.removals == ["proj-new"], gateway.removals
    # A definitely refused invocation is retired, never replayed.
    assert "pending_invocation_id" not in state


def test_unknown_outcome_retains_the_registration_and_says_why(tmp_path, fake_route):
    """A transport error leaves the POST's fate UNKNOWN: a run may be live against
    this registration, so it is RETAINED and the durable row names the reason."""
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    fake_route.project_unregistered = True
    fake_route.start_error = ClaudexorUnavailable("daemon_unreachable", "boom", status_code=0)
    state: dict = {}
    with pytest.raises(ClaudexorUnavailable):
        _run_session_directly(tmp_path, retry_state=state)

    assert fake_route.instances[-1].removals == []
    assert state["pending_invocation_id"]
    rows = [json.loads(ln) for ln in
            custody.event_log_path(tmp_path).read_text().splitlines() if ln.strip()]
    failed = [r for r in rows if r.get("type") == custody.START_FAILED]
    assert failed and failed[-1]["project_retention_reason"] == (
        "start_outcome_unknown_run_may_exist"), failed[-1]


def test_started_run_reports_whether_its_custody_row_landed(tmp_path, fake_route,
                                                            monkeypatch):
    """record_started's answer is a FACT the caller needs: a run whose authoritative
    row did not land is custodied by this process alone, and reporting a plainly
    started run over that state is how a live run becomes unfindable."""
    import ouroboros.delegate_custody as custody_mod

    assert _run_session_directly(tmp_path)["custody_durable"] is True

    monkeypatch.setattr(custody_mod, "record_started", lambda *_a, **_k: False)
    assert _run_session_directly(tmp_path)["custody_durable"] is False


def test_session_is_never_restarted_for_format_repair(tmp_path, fake_route):
    """5.5: a resend over bad output performs local extraction over the already
    collected transcript — the session is not relaunched."""
    from ouroboros.review_execution import AgentSessionReviewExecutor, ReviewAssignment

    fake_route.manifest_capabilities = {}
    fake_route.detail = _terminal_detail("prose without a verdict")
    llm = FakeLLM(reply="UNEXTRACTABLE")
    executor = AgentSessionReviewExecutor(
        ReviewAssignment(request=_agent_request(), slot=_agent_slot(),
                         call_id="c1", call_type="scope_review",
                         custody_root=tmp_path),
        llm=llm,
    )
    first = executor.execute()
    second = executor.execute()  # the coordinator's permitted resend
    assert len(fake_route.instances) == 1
    assert len(fake_route.instances[0].start_requests) == 1
    assert first.raw_text == second.raw_text == "prose without a verdict"
    assert len(llm.calls) == 2  # local extraction ran each time, no new session


# ---------------------------------------------------------------------------
# 5.1/5.3 — routes on slots, typed refusals, quorum shape
# ---------------------------------------------------------------------------


def test_unconfigured_session_route_is_a_typed_refusal(tmp_path, fake_route, monkeypatch):
    monkeypatch.delenv(REVIEW_SESSION_ROUTE_ENV, raising=False)
    monkeypatch.delenv("OUROBOROS_SUBAGENT_HARNESS", raising=False)
    llm = FakeLLM()
    result = run_review_request(_agent_request(), slots=[_agent_slot()],
                                drive_root=tmp_path, llm=llm)
    actor = result.actors[0]
    assert actor["status"] == "error"
    assert "no configured session route" in actor["error"]
    assert llm.calls == []  # never a silent fallback onto the api route


def test_unhealthy_route_refuses_typed_never_falls_back(tmp_path, fake_route):
    fake_route.catalog_entry["status"] = "degraded"
    llm = FakeLLM()
    result = run_review_request(_agent_request(), slots=[_agent_slot()],
                                drive_root=tmp_path, llm=llm)
    actor = result.actors[0]
    assert actor["status"] == "error"
    assert "route_status_degraded" in actor["error"]
    assert llm.calls == []
    assert not any(inst.start_requests for inst in fake_route.instances)


def test_mixed_panel_failed_agent_slot_does_not_shrink_n(tmp_path, fake_route, monkeypatch):
    """5.3: one panel, two deliveries. The agent slot failing typed leaves the
    panel at its configured size — the failed slot is an error actor on its own
    row, never a smaller N."""
    monkeypatch.delenv(REVIEW_SESSION_ROUTE_ENV, raising=False)
    monkeypatch.delenv("OUROBOROS_SUBAGENT_HARNESS", raising=False)

    class ApiFindingsLLM(FakeLLM):
        def chat(self, **kwargs):
            self.calls.append(kwargs)
            return ({"content": '{"verdict": "PASS", "findings": [], "summary": "ok"}'},
                    {"prompt_tokens": 3, "completion_tokens": 2})

    llm = ApiFindingsLLM()
    slots = [
        ReviewSlot(slot_id="scope_slot_1", model="api/model-a", timeout_sec=10),
        _agent_slot(slot_id="scope_slot_2"),
    ]
    result = run_review_request(_agent_request(), slots=slots,
                                drive_root=tmp_path, llm=llm)
    assert len(result.actors) == 2
    by_id = {a["slot_id"]: a for a in result.actors}
    assert by_id["scope_slot_1"]["status"] == "ok"
    assert by_id["scope_slot_2"]["status"] == "error"
    assert len(llm.calls) == 1  # the api row; the agent row never touched chat


def test_configured_review_routes_parsing(monkeypatch):
    monkeypatch.setenv(TRIAD_REVIEW_ROUTES_ENV, "api_chat, agent_session")
    routes = configured_review_routes(TRIAD_REVIEW_ROUTES_ENV, 3)
    assert routes == [ReviewRouteKind.API_CHAT, ReviewRouteKind.AGENT_SESSION,
                      ReviewRouteKind.API_CHAT]
    monkeypatch.setenv(TRIAD_REVIEW_ROUTES_ENV, "codex")
    with pytest.raises(ValueError):
        configured_review_routes(TRIAD_REVIEW_ROUTES_ENV, 1)


def test_scope_rows_carry_their_configured_routes(monkeypatch):
    monkeypatch.setenv(SCOPE_REVIEW_ROUTES_ENV, "agent_session")
    rows = scope_reviewer_slots(["m1", "m2"])
    assert rows[0].route is ReviewRouteKind.AGENT_SESSION
    assert rows[1].route is ReviewRouteKind.API_CHAT
    assert rows[0].slot_id == "scope_slot_1" and rows[1].slot_id == "scope_slot_2"


def test_scope_rows_default_to_the_configured_scope_review_effort(monkeypatch):
    """Regression (v6.89.0): with no structured reviewer slots, the legacy path took
    this function's old literal default ("medium") instead of the owner's configured
    OUROBOROS_EFFORT_SCOPE_REVIEW — the BLOCKING constitutional scope reviewer
    silently ran below its configured reasoning strength on every stock install."""
    monkeypatch.delenv("OUROBOROS_REVIEWER_SLOTS", raising=False)
    monkeypatch.delenv(SCOPE_REVIEW_ROUTES_ENV, raising=False)
    monkeypatch.setenv("OUROBOROS_SCOPE_REVIEW_MODELS", "some/model")
    monkeypatch.delenv("OUROBOROS_EFFORT_SCOPE_REVIEW", raising=False)
    assert [row.effort for row in scope_reviewer_slots()] == ["high"]  # config default
    monkeypatch.setenv("OUROBOROS_EFFORT_SCOPE_REVIEW", "xhigh")
    assert [row.effort for row in scope_reviewer_slots()] == ["xhigh"]
    # An explicit effort still wins for callers that rebuild one positional row.
    assert [row.effort for row in scope_reviewer_slots(["m"], effort="low")] == ["low"]


def _persisted_response_payloads(drive_root):
    """Every persisted review response payload under ``drive_root``."""
    import gzip

    payloads = []
    for path in sorted((drive_root / "observability" / "blobs").glob("*.json.gz")):
        try:
            payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        except Exception:  # pragma: no cover - non-json blob
            continue
        if isinstance(payload, dict) and "message" in payload:
            payloads.append(payload)
    return payloads


@pytest.mark.parametrize(
    "transcript, conformance",
    [
        # Schema-conformant: canonicalization legitimately reduces this to "[]".
        ('{"findings": []}', "passed"),
        # Narrative: light extraction replaces the whole thing.
        ("I reviewed the diff at length and found nothing that blocks.\nNO_FINDINGS", ""),
    ],
)
def test_delegated_transcript_survives_canonicalization_durably(
    tmp_path, fake_route, transcript, conformance
):
    """P1: the session's own output is the cognitive artifact, and canonicalization
    destroys it — `{"findings": []}` becomes `[]`, and extraction replaces a narrative
    wholesale. Keeping it only in the executor made the decision unreconstructible
    once the process ended; it must reach the DURABLE record with its provenance."""
    fake_route.detail = _terminal_detail(transcript, conformance=conformance)
    result = run_review_request(_agent_request(), slots=[_agent_slot()],
                               drive_root=tmp_path, llm=FakeLLM())
    actor = result.actors[0]

    # The canonical form is still what the gate parses...
    assert actor["raw_text"] == "[]"
    # ...and the RAW transcript is recoverable from the durable response payload.
    carriers = [p for p in _persisted_response_payloads(tmp_path)
                if str((p.get("message") or {}).get("session_transcript") or "")]
    assert carriers, "no persisted payload carried the session transcript"
    assert any((p["message"]["session_transcript"] == transcript) for p in carriers), carriers

    prov = actor["usage"]["verdict_provenance"]
    assert prov["raw_transcript_chars"] == len(transcript)
    assert prov["canonical_chars"] == len("[]")
    assert prov["raw_transcript_sha256"] != prov["canonical_sha256"]
    assert prov["verdict_method"] == actor["usage"]["verdict_method"]
    assert prov["conformance_trusted"] is (conformance == "passed")


def test_mixed_scope_fanout_sends_each_row_over_its_own_route(tmp_path, monkeypatch):
    """A MIXED scope configuration must deliver each row over the route it was
    configured with.

    `_call_scope_llm` rebuilt its slot from `scope_reviewer_slots([model])`, and a
    one-element list always re-reads ROUTES **row 1** — so with
    `agent_session,api_chat` the configured api row inherited agent_session while
    its request carried the api pack and no session task: a deterministic
    ReviewRouteUnavailable error actor that failed the blocking scope gate.
    """
    import ouroboros.tools.scope_review as scope_mod

    monkeypatch.setenv(SCOPE_REVIEW_ROUTES_ENV, "agent_session,api_chat")
    dispatched: list = []

    def _capture(request, *, slots, drive_root, llm, usage_ctx=None):
        slot = slots[0]
        dispatched.append((slot.slot_id, slot.model, slot.route.value,
                           bool(request.session_task), bool(request.messages)))
        return SimpleNamespace(actors=[{
            "slot_id": slot.slot_id, "model": slot.model, "status": "ok",
            "raw_text": json.dumps(_scope_matrix_rows()),
            "usage": {}, "prompt_ref": {}, "response_ref": {},
        }])

    monkeypatch.setattr("ouroboros.review_substrate.run_review_request", _capture)
    monkeypatch.setattr(scope_mod, "_build_scope_prompt",
                        lambda *_a, **_k: ("assembled api pack", None))
    monkeypatch.setattr(scope_mod, "_scope_window",
                        lambda *_a, **_k: scope_mod.ReviewerWindow(
                            window_tokens=1_000_000, status="confirmed"))

    for slot in scope_reviewer_slots(["m/session", "m/api"]):
        scope_mod.run_scope_review(
            _scope_ctx(tmp_path), "mixed route fan-out",
            scope_model=slot.model, slot_id=slot.slot_id, route=slot.route,
        )

    # Row 1 is the session (task, no api pack); row 2 is api (pack, no task).
    assert dispatched == [
        ("scope_slot_1", "m/session", "agent_session", True, False),
        ("scope_slot_2", "m/api", "api_chat", False, True),
    ], dispatched


def test_acceptance_rows_stay_api_even_when_triad_routes_delegate(monkeypatch):
    """D15: task acceptance and plan review are pinned to the API. The triad's
    route list must not leak into surfaces that pass no route_env_key."""
    monkeypatch.setenv(TRIAD_REVIEW_ROUTES_ENV, "agent_session,agent_session,agent_session")
    rows = reviewer_slots(["m1", "m2"], effort="high", role_hint="task acceptance")
    assert all(row.route is ReviewRouteKind.API_CHAT for row in rows)


def test_agent_slot_without_session_task_refuses_the_api_pack(tmp_path, fake_route):
    """5.2: the giant assembled pack is not sendable to a session. A surface
    that supplied no route-owned session task gets a typed refusal, not a
    silently forwarded api pack."""
    request = _agent_request(session_task="",
                             messages=[{"role": "system", "content": "GIANT PACK"}])
    llm = FakeLLM()
    result = run_review_request(request, slots=[_agent_slot()],
                                drive_root=tmp_path, llm=llm)
    actor = result.actors[0]
    assert actor["status"] == "error"
    assert "no session task" in actor["error"]
    assert llm.calls == []
    assert not any(inst.start_requests for inst in fake_route.instances)


# ---------------------------------------------------------------------------
# 5.2/5.6/5.7 — surface wiring: scope and triad deliver sessions without packs
# ---------------------------------------------------------------------------


def _scope_matrix_rows():
    from ouroboros.tools.scope_review_contract import SCOPE_REQUIRED_ITEMS

    return [
        {"item": item, "verdict": "PASS", "severity": "advisory",
         "reason": "checked the relevant code path and its consumers thoroughly"}
        for item in sorted(SCOPE_REQUIRED_ITEMS)
    ]


def _scope_ctx(tmp_path):
    from ouroboros.tools.registry import ToolContext

    gov = tmp_path / "gov"
    drive = tmp_path / "data"
    gov.mkdir(exist_ok=True)
    drive.mkdir(exist_ok=True)
    return ToolContext(repo_dir=gov, drive_root=drive)


def test_scope_session_delivery_never_builds_the_pack(tmp_path, fake_route, monkeypatch):
    """5.2 on scope: a delegated scope row goes out as a compact session task —
    checklist, contract and intent context intact (5.3), retrieval pointers and
    nav maps (5.7) instead of the assembled diff/touched/atlas pack — and the
    coverage manifest is forensics, not a gate (5.6): coverage_incomplete rides
    as a non-blocking fact on a run that PASSES.

    The row is given SOURCED window evidence so it clears the session authority
    floor: coverage is then the only thing that could possibly gate it — and does
    not. (Authority itself is covered by the session-floor tests below.)"""
    import ouroboros.tools.scope_review as scope_mod
    from ouroboros.review_execution import ReviewRouteKind

    def _pack_must_not_build(*_a, **_k):  # pragma: no cover - the point is silence
        raise AssertionError("the api pack builder ran for a session slot")

    monkeypatch.setattr(scope_mod, "_build_scope_prompt", _pack_must_not_build)
    monkeypatch.setattr(scope_mod, "_scope_window",
                        lambda *_a, **_k: scope_mod.ReviewerWindow(
                            window_tokens=1_000_000, status="confirmed"))
    fake_route.detail = _terminal_detail(
        json.dumps({"findings": _scope_matrix_rows()}), conformance="passed",
    )
    result = scope_mod.run_scope_review(
        _scope_ctx(tmp_path), "session-delivery scope run",
        scope_model="api/scope-model", slot_id="scope_slot_1",
        route=ReviewRouteKind.AGENT_SESSION,
    )
    assert result.blocked is False
    assert result.status == "responded"
    assert len(result.parsed_items) == 8
    manifest = result.context_manifest
    assert manifest["delivery"] == "agent_session"
    assert manifest["coverage"] == "agent_retrieval"
    assert manifest["coverage_incomplete"] is True  # forensic, visibly non-blocking
    assert manifest["excluded_sensitive"] == {"policy": "preserved", "host_enforced": False}

    start = fake_route.instances[0].start_requests[0]
    prompt = start["prompt"]
    assert "Intent / Scope Review Checklist" in prompt
    assert "intent_alignment" in prompt and "implicit_contracts" in prompt
    assert "session delivery" in prompt          # retrieval pointers, not packs
    assert "git diff --cached" in prompt
    assert "navigation map" in prompt            # 5.7: atlas as a map
    assert "There is no all-clear shortcut in this mode" in prompt  # matrix contract


def _run_session_scope(tmp_path, fake_route, monkeypatch, *, window, provenance, rows=None):
    """One session-delivered scope row under a given window evidence pair."""
    import ouroboros.tools.scope_review as scope_mod
    from ouroboros.review_execution import ReviewRouteKind

    # Ported onto the evidence-typed resolver (ReviewerWindow): sourced provenance
    # rides `status`; the conservative fallback is NO evidence (window_tokens=0,
    # sizing falls back); the designated-default sentinel is a NUMBER with no
    # status — a routing grant, never a measurement.
    if provenance in ("confirmed", "asserted"):
        _resolved = scope_mod.ReviewerWindow(window_tokens=int(window), status=provenance)
    elif provenance == "designated_default_sentinel":
        _resolved = scope_mod.ReviewerWindow(window_tokens=int(window), status="")
    else:
        _resolved = scope_mod.ReviewerWindow(window_tokens=0, status="")
    monkeypatch.setattr(scope_mod, "_scope_window", lambda *_a, **_k: _resolved)
    fake_route.detail = _terminal_detail(
        json.dumps({"findings": rows if rows is not None else _scope_matrix_rows()}),
        conformance="passed",
    )
    return scope_mod.run_scope_review(
        _scope_ctx(tmp_path), "session-delivered scope row",
        scope_model="session/reviewer", slot_id="scope_slot_1",
        route=ReviewRouteKind.AGENT_SESSION,
    )


def _scope_matrix_with_critical():
    rows = _scope_matrix_rows()
    rows[0] = {**rows[0], "verdict": "FAIL", "severity": "critical",
               "reason": "the change contradicts a documented invariant on a live path"}
    return rows


@pytest.mark.parametrize(
    "window, provenance",
    [
        # The conservative fallback resolves to exactly the session floor NUMBER. It is
        # not evidence, and a numeric-only floor would have admitted it.
        (200_000, "unknown_conservative"),
        # Sourced, but genuinely below the floor.
        (131_072, "confirmed"),
        # The designated-default sentinel is a routing grant, never a measurement.
        (1_000_000, "designated_default_sentinel"),
    ],
)
def test_session_scope_without_sourced_window_evidence_is_advisory_only(
    tmp_path, fake_route, monkeypatch, window, provenance
):
    """A retrieving scope row's FINDINGS certify nothing without SOURCED window
    evidence >= the session floor — and the row BLOCKS, as its api twin does.

    The previous shape skipped `_apply_scope_authority` for `agent_session`
    entirely — a session verdict gated commits with NO window test at all, while
    its own manifest recorded coverage_incomplete/host_enforced=False.

    Two facts live in one result and are easy to conflate: the findings are
    demoted to ADVISORY (an unestablished window cannot certify a verdict), and
    the commit is BLOCKED (the panel is short an authoritative verdict). Returning
    `blocked=False` here is what made the P3 gate fail open — the api row's
    `sub_floor` twin blocked on the identical panel shape.
    """
    result = _run_session_scope(
        tmp_path, fake_route, monkeypatch, window=window, provenance=provenance,
        rows=_scope_matrix_with_critical(),
    )

    assert result.status == "session_advisory", result.status
    assert result.blocked is True
    assert "authoritative scope verdict required to commit" in result.block_message
    # The critical was preserved as advisory evidence, not discarded...
    assert result.critical_findings == []
    reasons = " ".join(str(f.get("reason") or "") for f in result.advisory_findings)
    assert "[advisory-only session scope reviewer]" in reasons
    assert "contradicts a documented invariant" in reasons
    # ...and the reason it cannot gate is disclosed on the record.
    items = {str(f.get("item") or "") for f in result.advisory_findings}
    assert "scope_review_session_window_unproven" in items, items
    assert "SCOPE_SESSION_ADVISORY_ONLY" in reasons


def test_session_scope_with_sourced_window_evidence_keeps_blocking_authority(
    tmp_path, fake_route, monkeypatch
):
    """Sourced evidence at or above the session floor IS authority: the row's
    criticals gate the commit and it counts as an authoritative responder."""
    from ouroboros import config as cfg

    monkeypatch.setattr(cfg, "get_review_enforcement", lambda: "blocking")
    for window in (200_000, 1_000_000):
        result = _run_session_scope(
            tmp_path, fake_route, monkeypatch, window=window, provenance="confirmed",
            rows=_scope_matrix_with_critical(),
        )
        assert result.status == "responded", (window, result.status)
        assert result.blocked is True, window
        assert result.critical_findings, window
        items = {str(f.get("item") or "") for f in result.advisory_findings}
        assert "scope_review_session_window_unproven" not in items, window


def test_api_scope_row_keeps_the_1m_floor_and_still_blocks_sub_floor(
    tmp_path, fake_route, monkeypatch
):
    """The api (push) delivery is untouched: its authority rests on the assembled
    pack fitting, so a sub-1M reviewer is still the loud `sub_floor` block."""
    import ouroboros.tools.scope_review as scope_mod

    monkeypatch.setattr(scope_mod, "_scope_window",
                        lambda *_a, **_k: scope_mod.ReviewerWindow(
                            window_tokens=200_000, status="confirmed"))
    monkeypatch.setattr(scope_mod, "_build_scope_prompt",
                        lambda *_a, **_k: ("assembled api pack", None))
    monkeypatch.setattr(
        scope_mod, "_call_scope_llm",
        lambda *_a, **_k: (json.dumps(_scope_matrix_with_critical()), {}, ""),
    )
    result = scope_mod.run_scope_review(
        _scope_ctx(tmp_path), "api row, sub-floor window",
        scope_model="api/small-window", slot_id="scope_slot_1",
    )
    assert result.status == "sub_floor", result.status
    assert result.blocked is True
    assert "does not establish the required >=1M floor" in result.block_message


def test_scope_quorum_refuses_a_session_advisory_row_as_authoritative(tmp_path, monkeypatch):
    """The scope quorum must not count a non-host-attested session row as the
    authoritative verdict, and must disclose the shortfall it leaves."""
    from ouroboros.tools import parallel_review, review
    from ouroboros.tools.scope_review import ScopeReviewResult

    rows = {
        "api/big": ScopeReviewResult(blocked=False, status="responded", model_id="api/big"),
        "session/row": ScopeReviewResult(
            blocked=False, status="session_advisory", model_id="session/row",
            advisory_findings=[{
                "verdict": "FAIL", "severity": "advisory",
                "item": "scope_review_session_window_unproven",
                "reason": "SCOPE_SESSION_ADVISORY_ONLY: window not sourced-proven",
            }],
        ),
    }
    monkeypatch.setattr(parallel_review, "run_scope_review",
                        lambda _ctx, _msg, **kwargs: rows[kwargs["scope_model"]])
    monkeypatch.setattr(parallel_review, "scope_reviewer_slots", lambda *_a, **_k: [
        SimpleNamespace(model="api/big", slot_id="scope_slot_1", route=None,
                        effort="", session_target="", session_profile=""),
        SimpleNamespace(model="session/row", slot_id="scope_slot_2", route=None,
                        effort="", session_target="", session_profile=""),
    ])
    monkeypatch.setattr(parallel_review, "run_cmd", lambda *_a, **_k: "staged diff")
    monkeypatch.setattr(review, "_run_unified_review", lambda *_a, **_k: None)

    ctx = SimpleNamespace(
        repo_dir=tmp_path, drive_root=tmp_path, task_id="scope-quorum",
        pending_events=[], _review_history=[], _review_advisory=[], _scope_review_history={},
    )
    parallel_review.run_parallel_review(ctx, "quorum commit")

    manifest = (ctx._last_scope_raw_result or {}).get("context_manifest") or {}
    # Two configured rows, adaptive quorum 2 — but only ONE authoritative verdict.
    assert manifest["scope_responded_count"] == 1, manifest
    assert manifest["scope_session_advisory_only_count"] == 1, manifest
    assert any("scope_session_advisory_only" in str(r)
               for r in manifest["scope_degraded_reasons"]), manifest


def test_triad_mixed_panel_builds_the_pack_once_for_api_rows_only(tmp_path, fake_route, monkeypatch):
    """5.2/5.3 on the triad: one panel, two deliveries. The api row gets the
    historical pack; the session row gets the compact task; an all-session
    panel never assembles the pack at all."""
    import ouroboros.tools.review as review_mod
    from ouroboros.review_execution import ReviewRouteKind

    chat_calls = []

    class PanelLLM:
        def chat(self, **kwargs):
            chat_calls.append(kwargs)
            return {"content": "[]\nNO_FINDINGS"}, {"prompt_tokens": 4, "completion_tokens": 2}

    monkeypatch.setattr(review_mod, "LLMClient", PanelLLM)
    monkeypatch.setattr(review_mod, "review_drive_root", lambda _ctx: tmp_path)
    fake_route.detail = _terminal_detail('{"findings": []}', conformance="passed")

    result = json.loads(review_mod._handle_multi_model_review(
        None,
        content="Review the staged diff and context provided in the instructions above.",
        prompt="INSTRUCTIONS BODY",
        models=["api/model-a", "api/model-b"],
        stable_prefix_len=0,
        routes=[ReviewRouteKind.API_CHAT, ReviewRouteKind.AGENT_SESSION],
        session_task="Review the staged diff: run `git diff --cached` yourself.",
        session_root="/tmp/fake-repo",
    ))
    rows = result["results"]
    assert len(rows) == 2
    assert rows[0]["slot_id"] == "slot_1" and rows[0]["text"] == "[]\nNO_FINDINGS"
    assert rows[1]["slot_id"] == "slot_2" and rows[1]["text"] == "[]"
    assert len(chat_calls) == 1  # ONE api send; the session row never used chat
    # The session start carried the compact task, not the giant pack.
    session_prompt = fake_route.instances[0].start_requests[0]["prompt"]
    assert "git diff --cached" in session_prompt
    assert "INSTRUCTIONS BODY" not in session_prompt
    # And the pack never reaches the session slot's DURABLE record either: the
    # api pack text must appear only in the api row's persisted prompt (gzip
    # content-addressed blobs), never in the session row's request payload.
    import gzip

    hits = []
    for record in tmp_path.rglob("*.gz"):
        text = gzip.decompress(record.read_bytes()).decode("utf-8", errors="replace")
        if "INSTRUCTIONS BODY" in text:
            hits.append(text)
    assert hits, "the api row's own durable prompt record should carry the pack"
    assert not any('"slot_id": "slot_2"' in text for text in hits)

    # All-session panel: the api pack (prompt) may be empty and nothing chats.
    chat_calls.clear()
    fake_route.reset()
    result = json.loads(review_mod._handle_multi_model_review(
        None,
        content="Review the staged diff and context provided in the instructions above.",
        prompt="",
        models=["api/model-a"],
        stable_prefix_len=0,
        routes=[ReviewRouteKind.AGENT_SESSION],
        session_task="Review the staged diff yourself.",
        session_root="/tmp/fake-repo",
    ))
    assert "error" not in result
    assert result["results"][0]["text"] == "[]"
    assert chat_calls == []


def test_triad_session_task_carries_criteria_and_nav_maps_not_evidence():
    import ouroboros.tools.review as review_mod

    task = review_mod._triad_session_task(
        None,
        goal_section="## Goal\nDo the thing.",
        scope_section="## Scope\nOnly here.",
        checklist_section="## Review Checklist\n- correctness",
        rebuttal_section="",
        review_history_section="",
        dev_guide_text="# Dev\n\n## Rules\n\ntext\n",
        architecture_text="# Arch\n\n## Modules\n\ntext\n",
    )
    assert "## Review Checklist" in task
    assert "## Goal" in task and "## Scope" in task
    assert "git diff --cached" in task           # subject pointer, not the diff
    assert "DEVELOPMENT.md (navigation map)" in task
    assert "ARCHITECTURE.md (navigation map)" in task
    assert "Read BIBLE.md in full" in task


# ---------------------------------------------------------------------------
# The blocking scope gate must not fail OPEN on an all-retrieving panel.
# ---------------------------------------------------------------------------


def _all_session_scope_panel(tmp_path, monkeypatch, *, window, provenance):
    """The REAL fan-out + aggregate over a panel of two retrieving rows.

    Only the two genuinely external things are faked: the reviewer's window
    evidence and the model call. Everything the gate actually decides with —
    `run_scope_review`, `_apply_scope_authority`, `session_scope_authority`,
    `run_parallel_review`'s quorum, `aggregate_review_verdict` — runs for real.
    """
    from ouroboros import config as cfg
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewSlot
    from ouroboros.tools import parallel_review, review
    import ouroboros.tools.scope_review as scope_mod

    if provenance:
        resolved = scope_mod.ReviewerWindow(window_tokens=int(window), status=provenance)
    else:
        resolved = scope_mod.ReviewerWindow(window_tokens=0, status="")
    monkeypatch.setattr(cfg, "get_review_enforcement", lambda: "blocking")
    monkeypatch.setattr(scope_mod, "_scope_window", lambda *_a, **_k: resolved)
    monkeypatch.setattr(
        scope_mod, "_call_scope_llm",
        lambda *_a, **_k: (json.dumps(_scope_matrix_rows()), {}, ""),
    )
    monkeypatch.setattr(parallel_review, "scope_reviewer_slots", lambda *_a, **_k: [
        ReviewSlot(slot_id="scope_slot_1", model="codex=gpt-5.6-sol",
                   route=ReviewRouteKind.AGENT_SESSION, session_target="codex=gpt-5.6-sol"),
        ReviewSlot(slot_id="scope_slot_2", model="claude=fable-5",
                   route=ReviewRouteKind.AGENT_SESSION, session_target="claude=fable-5"),
    ])
    monkeypatch.setattr(parallel_review, "run_cmd", lambda *_a, **_k: "staged diff")
    monkeypatch.setattr(review, "_run_unified_review", lambda *_a, **_k: None)

    ctx = _scope_ctx(tmp_path)
    ctx._review_history = []
    ctx._review_advisory = []
    ctx._scope_review_history = {}
    ctx.task_id = "scope-fail-open"
    ctx.pending_events = []
    args = parallel_review.run_parallel_review(ctx, "all-retrieving scope panel")
    blocked, message, reason, _findings, _advisory = parallel_review.aggregate_review_verdict(
        *args, ctx, "all-retrieving scope panel", 0.0, tmp_path,
    )
    manifest = (ctx._last_scope_raw_result or {}).get("context_manifest") or {}
    return blocked, message or "", reason, manifest


def test_all_retrieving_scope_panel_blocks_instead_of_failing_open(tmp_path, monkeypatch):
    """A scope panel of retrieving rows with no sourced window evidence yields ZERO
    authoritative verdicts — and must BLOCK, exactly as the api panel does.

    This is the fail-open the adversarial panel measured on a6a3c1f: the same panel
    shape gave `api_chat status=sub_floor -> BLOCKED=True` and
    `agent_session status=session_advisory -> BLOCKED=False`. Nothing downstream
    could recover it — `partial_quorum_shortfall` only fires above zero responders,
    so a zero-authoritative run walked straight through the blocking scope gate of
    BIBLE P3 while looking armed.
    """
    blocked, message, reason, manifest = _all_session_scope_panel(
        tmp_path, monkeypatch, window=0, provenance="",
    )

    assert blocked is True, "the blocking scope gate must not pass a zero-authoritative panel"
    assert reason == "scope_blocked", reason
    assert "SCOPE_REVIEW_BLOCKED" in message
    assert "authoritative scope verdict required to commit" in message
    # The shortfall is still disclosed, not merely converted into a block.
    assert manifest["scope_responded_count"] == 0, manifest
    assert manifest["scope_session_advisory_only_count"] == 2, manifest
    assert any("scope_session_advisory_only" in str(r)
               for r in manifest["scope_degraded_reasons"]), manifest


def test_retrieving_and_api_panels_agree_on_an_unestablished_window(tmp_path, monkeypatch):
    """The asymmetry itself is the defect: an unestablished window blocks on BOTH
    deliveries, and SOURCED evidence at the row's own floor authorises on both."""
    import ouroboros.tools.scope_review as scope_mod

    # Retrieving row, SOURCED at the session floor -> authoritative, no block.
    blocked, _msg, _reason, manifest = _all_session_scope_panel(
        tmp_path, monkeypatch, window=200_000, provenance="confirmed",
    )
    assert blocked is False, "sourced >=200K evidence must restore an authoritative verdict"
    assert manifest["scope_responded_count"] == 2, manifest

    # api row, window below its own floor -> blocks (the twin, unchanged).
    monkeypatch.setattr(scope_mod, "_build_scope_prompt",
                        lambda *_a, **_k: ("assembled api pack", None))
    monkeypatch.setattr(scope_mod, "_scope_window",
                        lambda *_a, **_k: scope_mod.ReviewerWindow(
                            window_tokens=200_000, status="confirmed"))
    monkeypatch.setattr(
        scope_mod, "_call_scope_llm",
        lambda *_a, **_k: (json.dumps(_scope_matrix_rows()), {}, ""),
    )
    api_result = scope_mod.run_scope_review(
        _scope_ctx(tmp_path), "api row, sub-floor window", scope_model="api/small",
        slot_id="scope_slot_1",
    )
    assert api_result.blocked is True and api_result.status == "sub_floor"


def test_a_retrieving_row_can_actually_reach_sourced_evidence(tmp_path, monkeypatch):
    """The >=200K floor must be REACHABLE, not decorative.

    Retrieving rows were excluded from Capability-Evidence probing and their opaque
    `harness[=model]` target does not resolve through `provider_for_model`, so no
    product path could ever take such a row to `confirmed`/`asserted`: advisory-only
    was the mode's ONLY possible outcome. The settings save now offers the row its
    ack against its own floor, and acking that exact route restores authority.
    """
    from ouroboros import capability_evidence as ce
    from ouroboros.gateway import settings as smod
    from ouroboros.reviewer_window import SESSION_ROUTE_PROVIDER
    from ouroboros.tools.scope_review_session import (
        SESSION_WINDOW_FLOOR,
        session_window_is_authoritative,
    )
    from ouroboros.tools.scope_window import scope_window

    monkeypatch.setattr(ce, "DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    monkeypatch.setattr(smod, "_candidate_scope_models", lambda _s: [])
    slots = json.dumps({
        "triad": [{"slot_id": "t1", "route": {"kind": "api_chat", "target_id": "api/m"}}],
        "scope": [{"slot_id": "s1", "route": {"kind": "agent_session",
                                              "target_id": "codex=gpt-5.6-sol"}}],
    })

    notices = smod._review_capability_notices({"OUROBOROS_REVIEWER_SLOTS": slots})
    assert len(notices) == 1, notices
    notice = notices[0]
    assert notice["surface"] == "scope_review_session"
    assert notice["floor_tokens"] == SESSION_WINDOW_FLOOR
    ack_route = notice["needs_ack"]
    assert ack_route["provider"] == SESSION_ROUTE_PROVIDER, ack_route
    assert ack_route["model"] == "codex=gpt-5.6-sol"

    # Before the ack the row cannot authorise...
    before = scope_window("codex=gpt-5.6-sol", session=True)
    assert session_window_is_authoritative(before.window_tokens, before.status) is False

    # ...and the ack the UI records against that exact route is what restores it.
    ce.record_owner_ack(tmp_path, provider=ack_route["provider"], model=ack_route["model"],
                        base_url=ack_route["base_url"], window_tokens=SESSION_WINDOW_FLOOR)
    after = scope_window("codex=gpt-5.6-sol", session=True)
    assert session_window_is_authoritative(after.window_tokens, after.status) is True


def test_session_schema_floor_matches_each_surfaces_clean_contract():
    """`{"findings": []}` is the honest clean verdict for a TRIAD session, but on
    scope (eight mandatory rows) and advisory (empty checklist rejected by design)
    it is a schema-conformant answer that can only land as parse_failure and block
    the commit. The floor rides the schema so a conforming engine refuses the empty
    answer up front while the session can still regenerate."""
    from ouroboros.review_execution import (
        REVIEW_SESSION_OUTPUT_SCHEMA,
        review_session_output_schema,
    )

    assert review_session_output_schema("commit_review") is REVIEW_SESSION_OUTPUT_SCHEMA
    # Advisory keeps the clean-capable shared schema: its ORDINARY mode's required
    # clean verdict is exactly the empty array, so a floor would starve it of the
    # one answer its contract demands (checklist coverage is checked downstream).
    assert review_session_output_schema("advisory_review") is REVIEW_SESSION_OUTPUT_SCHEMA
    assert "minItems" not in REVIEW_SESSION_OUTPUT_SCHEMA["properties"]["findings"]
    shaped = review_session_output_schema("scope_review")
    assert shaped["properties"]["findings"]["minItems"] == 1
    # A shaped copy, never a mutation of the shared schema.
    assert "minItems" not in REVIEW_SESSION_OUTPUT_SCHEMA["properties"]["findings"]
