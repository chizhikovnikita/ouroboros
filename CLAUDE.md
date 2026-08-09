# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read First

Ouroboros is a self-modifying agent: it reads, reviews, and commits its own source. Four documents are
**authoritative governance artifacts**, and substantive changes require reading them in full:

- `BIBLE.md` — constitution (P0–P12). Principles are cited by number throughout the code and docs
  (e.g. "P1 continuity", "P3 immune integrity", "P7 minimalism").
- `docs/ARCHITECTURE.md` — the operational map. **Every structural change (new module, endpoint, data
  file, UI page) must update this file in the same commit.** It is not a changelog.
- `docs/DEVELOPMENT.md` — engineering handbook: naming, entity types, complexity gates, review protocol,
  design system, anti-patterns (each anti-pattern section records a real incident — read before
  re-introducing the pattern).
- `docs/CHECKLISTS.md` — the pre-commit review checklist SSOT. Never fork or duplicate checklist policy.

`CONTRIBUTING.md` covers the PR flow. Reuse existing modules, contracts, and single sources of truth
instead of inventing parallel mechanisms.

## Commands

```bash
# Setup (Python >= 3.10)
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps

# Run the server (web UI at http://127.0.0.1:8765)
ouroboros server          # or: python server.py

# Tests (default local lanes; costly/browser/network lanes excluded via pyproject addopts)
make test                 # python3 -m pytest tests/ -q --tb=short
python -m pytest tests/test_smoke.py -q                       # one file
python -m pytest tests/test_smoke.py::test_function_count_reasonable -q   # one test
python -m pytest tests/ -k "workspace and not docker" -q      # by name

# Lint (matches the CI gate — deterministic F-rules only, not the full ruff config)
python -m pip install ruff   # not in requirements.txt; CI installs it separately
make lint                    # python3 -m ruff check . --select F

# Codebase health metrics (complexity, module sizes)
make health

# Web UI (ES modules, no bundler)
node --test web/tests/*.test.js
node --check web/app.js
```

### CI parity

CI splits the default suite into a parallel and a serial pass — reproduce it locally with:

```bash
python -m pytest tests/ -m "not serial and not integration and not browser and not ui_browser and not ui_browser_docker and not portable_detail and not skill_smoke" -n auto --dist loadscope --max-worker-restart=0 -q
python -m pytest tests/ -m "serial and not integration and not browser and not ui_browser and not ui_browser_docker and not portable_detail and not skill_smoke" -q
```

Opt-in lanes (`-m integration`, `-m browser`, `-m ui_browser`, `-m ui_browser_docker`,
`-m portable_detail`, `-m skill_smoke`) need real API keys, Playwright, Docker, or live network.
Registering a new lane means: add the marker to `pyproject.toml`, add a collect-only zero-test guard in
`.github/workflows/ci.yml`, and keep default local `addopts` token-safe.

**Parallel-safety rules** (CI runs `-n auto --max-worker-restart=0`, so one crashing test fails its whole
co-located batch): mark tests that spawn real processes, bind real ports, or mutate module globals with
`@pytest.mark.serial` (or add the file to `_SERIAL_TEST_FILES` in `tests/conftest.py`). Everything else
must use `tmp_path` and `monkeypatch.setenv/setattr` — never fixed paths or bare `os.environ[...] = ...`.

Since v6.88.0 the hermetic commit gate (`ouroboros/preflight_runner.py::run_hermetic_pytest`) runs the
**same two-pass split** as CI, so the interpreter at `OUROBOROS_AGENT_PYTHON` (or `sys.executable`) must
carry `pytest-xdist>=3.5` and `pytest-timeout>=2.1` — both are now real runtime dependencies, not CI-only.
Without them every commit fails closed with `PREFLIGHT_PLUGIN_MISSING` rather than degrading to serial.
`OUROBOROS_PREFLIGHT_SERIAL=1` is the temporary legacy fallback; set
`OUROBOROS_PREFLIGHT_REQUIRE_PLUGINS=1` whenever a run is being treated as evidence, so an
unprovisioned interpreter cannot produce a clean-looking run with the real proofs skipped.

## Architecture

### Process model

```
launcher.py (PyWebView desktop shell — immutable, never self-modified)
  └── spawns server.py (Starlette + uvicorn, self-editable)
        ├── ouroboros/gateway/     HTTP/WS route modules (router.py wires them)
        ├── web/                   SPA, ES modules in web/modules/
        ├── supervisor/            background thread: queue, worker pool, events, git ops
        └── worker processes ──► ouroboros/  (agent core: loop, tools, review, memory)
```

Runtime data lives **outside** the repo at `~/Ouroboros/` (`repo/`, `data/`, `Deliverables/`). The
full data-layout tree is in `docs/ARCHITECTURE.md` §1 — consult it before adding any persistent file.

### Agent core flow

`agent.py` (orchestrator) → `agent_task_pipeline.py` (task execution) → `loop.py` (LLM tool loop) →
`loop_llm_call.py` (one round + usage accounting) → `loop_tool_execution.py` (tool dispatch).
Tools live in `ouroboros/tools/*.py` and are registered through `ouroboros/tools/registry.py`.
`llm.py` routes across OpenRouter / OpenAI / Anthropic / Cloud.ru / GigaChat / local.

**Delegated execution (Claudexor, v6.89+).** A delegated subagent is an ordinary subagent acting as a
*nanny*: it keeps its place in the task tree, its deadline and its authority, but starts a Claudexor run
instead of thinking on metered API tokens. `ouroboros/gateways/claudexor.py` is pure transport and is the
only module that ever touches the daemon bearer token (it grants the whole `/v2` surface — never put it in
a `ToolContext`, child env, or sandbox). Custody, output, containment and progress live in
`ouroboros/delegate_*.py`; the tool is `ouroboros/tools/delegate.py`. Because the nanny *is* the host,
verification receipts stay host-authored and harness output is a claim to check, never proof. See
`docs/ARCHITECTURE.md` §"Delegated subagents" and `docs/DELEGATED_ADMISSION.md`.

### Single sources of truth (do not create a second authority)

| Concern | SSOT |
|---|---|
| Settings defaults, paths, PID lock | `ouroboros/config.py` |
| Messaging | `supervisor/message_bus.py` |
| Monetary spend | `data/state/usage_attempts.jsonl`. Two-layer, one-way seam: `ouroboros/usage_ledger.py` is the durable substrate (locking, atomic append+fsync, row validation, torn-tail quarantine); `ouroboros/usage_accounting.py` is the policy on top (scopes, reservations, settlement, budget fences, projections) and imports *from* the substrate, never the reverse. State/UI totals are projections; unknown cost is never a false zero |
| Context-doc layout / budgets | `ouroboros/context_layout.py`, `ouroboros/context_budget.py` |
| Review checklists | `docs/CHECKLISTS.md` |
| Complexity gate constants | `ouroboros/review.py` |
| String truncation | `utils.truncate_review_artifact` (never hand-rolled `text[:N] + marker`) |
| Cron/timezone validation | `ouroboros/schedule_contract.py` |
| Protected-path policy | `ouroboros/runtime_mode_policy.py` |

### Frozen ABI

`ouroboros/contracts/` declares structurally frozen contracts (`ToolContextProtocol`, tool ABI,
`api_v1` WS/HTTP envelopes, `PluginAPI`, `SkillManifest`, `task_contract`). Extending the ABI requires
all three: add the field under `ouroboros/contracts/`, document it in `docs/ARCHITECTURE.md` §11.1, and
pin it in `tests/test_contracts.py`. Removal is a deliberate break needing a version bump + migration
note. The gateway JS mirror `web/modules/api_types.js` is kept in parity by `tests/test_gateway_parity.py`.

### Skills / extensions

External skills (`data/skills/{native,clawhub,ouroboroshub,external}/`) execute only when enabled **and**
their live-computed multi-model review verdict is executable **and** the stored content hash matches the
payload. `type: extension` skills with isolated deps or native markers must be dispatched through
`extension_process_runner` child processes — never imported into `server.py`. Author guide:
`docs/CREATING_SKILLS.md`.

## Conventions and Gates

### Hard gates enforced by `tests/test_smoke.py`

- Module length: 1600 lines (target ~1000). Exceptions are explicit entries in
  `ouroboros/review.py::GRANDFATHERED_OVERSIZED_MODULES` — do not add to it casually.
- Function length: 300 lines (target <150). Function parameters: <8.
- Total runtime function count: `ouroboros/review.py::MAX_TOTAL_FUNCTIONS`. Bumping the constant requires
  a comment justifying the growth.

### Adding a new tool

Four wiring points, all required (missing any one degrades silently):

1. Export it from the module's `get_tools()` using the `ToolEntry` pattern in `ouroboros/tools/registry.py`.
2. Add an explicit entry to `ouroboros/safety.py::TOOL_POLICY` (`POLICY_SKIP` for trusted built-ins,
   `POLICY_CHECK` for opaque/outward-facing ones) — otherwise it falls through to a per-invocation LLM call.
3. Declare visibility in `ouroboros/tool_capabilities.py` (`CORE_TOOL_NAMES`, subagent allowlists,
   parallel/truncation sets).
4. If workspace tasks should see it, update the workspace allowlist in `ouroboros/tools/registry.py`.

A tool that **writes the repo working tree** also needs the guard surfaces, not just the visibility ones:
add it to `_ROOT_ARG_REPO_WRITE_TOOLS` and canonicalize its target paths (`_PATH_NORMALIZED_TOOLS` for a
top-level `path` arg, or `canonical_repo_relative_path` + `_payload_write_paths` when paths ride inside the
payload). Visibility lists stay green while these are missing, so the gap does not surface as a failing
test — `apply_patch`/`edit_batch` shipped a protected-path bypass exactly this way. Test the real guard
chain; monkeypatching the resolver proves the mechanics, not the fence.

Naming: `Gateway` = transport only, no business logic; `Service` = orchestration; Tool = `{verb}_{noun}`
snake_case, thin wrapper. English identifiers, PEP 8, ruff line-length 120.

### Non-obvious invariants

- **No silent truncation.** Omitted context carries a visible marker; bounded lists carry an explicit
  omitted count (and a hash of the full set when the identity is compared downstream).
- **Owner-facing surfaces show full text** — truncation budgets exist for model context, not for humans.
- **Provider independence.** Ouroboros must stay fully operational on a *single* configured provider
  (including a local model). Wiring a new provider means defaults, credential detection, reviewer-slot
  fallbacks, pricing, and catalog listing — leave no slot pointing at an unconfigured provider.
- **Identity is captured at ingress and passed by value**, never re-derived by hashing/matching text later.
- **No hand-maintained model pricing tables.** Query the provider catalog; unknown cost stays `None`.
- **Do not fix tool-choice failures by adding prose to `prompts/SYSTEM.md`** — fix the tool description at
  the schema source or add a structural affordance.
- **`devtools/` is operator tooling**: benchmark harnesses and smoke runners. Runtime modules
  (`ouroboros/`, `server.py`, `web/`, build scripts) must never import it.
- **No inline styles in JS** (`web/modules/`); styles belong in the CSS files. See the Design System
  section of `docs/DEVELOPMENT.md`.

### Commit / review protocol

Agent-side edit tools, narrowest first: `edit_text` (one exact replacement), `edit_batch` (several exact
replacements or a counted replace-all; a count mismatch aborts the whole batch before any write),
`apply_patch` (scattered multi-file, context-anchored hunks validated across all files before the first
write), `write_file` (new files or intentional full rewrites).

Self-modification lands through a two-step gate: `advisory_review` (must be fresh — any edit staled it),
then `commit_reviewed`, which runs the triad reviewers (`ouroboros/tools/review.py`) and scope review
(`ouroboros/tools/scope_review.py`) in parallel against the exact staged snapshot. Scope review requires a
≥1M-context reviewer. Three blocked attempts on a byte-identical diff hits `attempt_cap_reached`.
`skip_advisory_review=True` is an audited absolute bypass. Details: `docs/DEVELOPMENT.md` §Review & Commit
Protocol.

For external PRs, `scripts/run_external_review.py --contributor` reproduces triad + scope over a committed
range without committing; it yields `READY_FOR_INTEGRATION`, not merge authority.

### Branches and release carriers

`main` is protected and untouched by self-modification; `ouroboros` is the working branch and the base for
PRs; `ouroboros-stable` is recovery. The managed update feed is separate from the local work branch:
`OUROBOROS_UPDATE_CHANNEL` maps Stable→`main` (newest plain `vX.Y.Z` tag present in both `main` and
`ouroboros-stable`), QA→`ouroboros-stable`, Development→`ouroboros` (see `ouroboros/update_channels.py`
and the Managed Update Rule in `docs/DEVELOPMENT.md`). Release carriers must stay in sync: `VERSION`, `pyproject.toml`
(PEP 440 form), `web/package.json`, README badge + version-history row, the `docs/ARCHITECTURE.md` header,
and the `v*` git tag. Contributors do **not** bump versions — maintainers assign them at squash-landing.

### Remote push rule (owner rule — absolute)

**Never push anything to the parent project `razzant/ouroboros`.** Not a branch, not a
tag, not a force-push, not a PR-backing ref, and never as a "convenience" when a command
seems to want an upstream. This fork publishes to `origin`
(`https://github.com/chizhikovnikita/ouroboros`) and nowhere else.

- Push with `git push origin <branch>`. If a tool or instruction implies pushing to
  `upstream`, treat that as a mistake and stop — ask instead of substituting a remote.
- Fetching/pulling from `upstream` stays allowed; only pushing is forbidden.
- Two local guards enforce this and must not be removed or bypassed:
  `.git/hooks/pre-push` refuses any push whose target URL matches `razzant/ouroboros`
  (by name or by direct URL), and `remote.upstream.pushurl` is set to a non-repository
  placeholder so `git push upstream` cannot resolve a destination.
- Do not "fix" a blocked push by editing the hook, clearing `pushurl`, adding a new
  remote, or pushing a raw URL. A blocked push means the target was wrong.
