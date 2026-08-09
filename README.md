# Ouroboros

[![GitHub stars](https://img.shields.io/github/stars/razzant/ouroboros?style=flat&logo=github)](https://github.com/razzant/ouroboros/stargazers)
[![Downloads](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Frazzant%2Fouroboros%2Fbadges%2Fdownloads.json)](https://github.com/razzant/ouroboros/releases)
[![Website](https://img.shields.io/badge/website-ouroboros--agent.ai-c93545.svg)](https://ouroboros-agent.ai/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![macOS 12+](https://img.shields.io/badge/macOS-12%2B-black.svg)](https://github.com/razzant/ouroboros/releases)
[![Linux](https://img.shields.io/badge/Linux-x86__64-orange.svg)](https://github.com/razzant/ouroboros/releases)
[![Windows](https://img.shields.io/badge/Windows-x64-blue.svg)](https://github.com/razzant/ouroboros/releases)
[![OuroborosHub](https://img.shields.io/badge/OuroborosHub-skills%20marketplace-8A2BE2.svg)](https://github.com/razzant/OuroborosHub)
[![Version 6.92.1](https://img.shields.io/badge/version-6.92.1-green.svg)](VERSION)

Ouroboros is an open-source, general-purpose AI agent whose identity, durable memory, and history continue across tasks and restarts. It works on external projects, coordinates a live swarm of specialist agents, and can rewrite the implementation it runs on, including its code, architecture, prompts, tools, and dependencies. Reflection can also change how it understands itself without severing that continuity.

It runs as a native desktop app or through a headless CLI. The runtime keeps its repository, durable memory, history, and interface on your machine, while model inference can use remote APIs you configure or a local GGUF model.

## Install

### macOS (Apple silicon)

1. Open the [latest stable release](https://github.com/razzant/ouroboros/releases/latest) and download `Ouroboros-<version>.dmg`.
2. Open the DMG and drag `Ouroboros.app` onto the **Applications** shortcut.
3. Open Ouroboros from Applications. If Gatekeeper asks, right-click the app and choose **Open**.

<p align="center">
  <img src="assets/install-macos.png" width="760" alt="Ouroboros DMG window with a large arrow from Ouroboros.app to the Applications shortcut and Install CLI.command below">
</p>

Optional CLI: after the app is in Applications, double-click `Install CLI.command` in the mounted DMG. It creates a user-local `ouroboros` command without sudo.

To run tasks, configure at least one supported remote provider API key or a local GGUF model. The first-run wizard guides model access, review policy, and budget setup.

### Linux and Windows

- **Linux x86_64:** from the [latest stable release](https://github.com/razzant/ouroboros/releases/latest), download `Ouroboros-<version>-linux-x86_64.tar.gz`, extract it, and run `./Ouroboros/Ouroboros`. The optional CLI installer is `./Ouroboros/bin/install-ouroboros-cli`.
- **Windows x64:** from the [latest stable release](https://github.com/razzant/ouroboros/releases/latest), download `Ouroboros-<version>-windows-x64.zip`, extract it, and run `Ouroboros\Ouroboros.exe`. The optional CLI installer is `Ouroboros\bin\install-ouroboros-cli.cmd`.

Prerelease artifacts stay on their tag pages; `/releases/latest` points to the latest stable release. If bundled browser tools on Linux need host libraries, run `./Ouroboros/python-standalone/bin/python3 -m playwright install-deps chromium webkit`. See the [full install and verification guide](https://ouroboros-agent.ai/install/) for source setup and release proof files.

Use your existing **Codex, Claude Code, or Cursor subscriptions** for
delegated coding and review — Ouroboros drives them through
[Claudexor](https://github.com/razzant/claudexor), its bundled multi-harness
engine. Connect accounts in **Providers → Harness Accounts**; no separate
install is needed. Works on macOS and Linux. Release artifacts carry the exact
reviewed engine archive; source checkouts fetch that same pinned archive on
first use. Connecting an account installs or repairs the engine in the
foreground, and delegated work does the same lazily. If that checkout or an
older package lacks the exact tested Node, the same action obtains its
review-bound official archive too. A newer pinned engine is staged while the
current daemon keeps running, then activates on its next natural start. This
also covers upgrades from older Ouroboros versions that did not bundle
Claudexor.

---

Ouroboros first booted on February 16, 2026. During the following 48 hours, the repository advanced from the v4.1 line to v6.2.0. The self-authored record preserved from that period counts 32 evolution cycles. That first generation ran in Google Colab through Telegram and remains preserved on the [`legacy-google-colab`](https://github.com/razzant/ouroboros/tree/legacy-google-colab) branch and its [original project page](https://ouroboros-agent.ai/archive/first-generation/); the current generation carries the same identity into a native desktop and headless runtime.

<p align="center">
  <img src="assets/evolution.png" width="760" alt="Code, prompt, and memory growth across Ouroboros releases, from v3.0.0 to the v6.85 line">
</p>

> ⭐ **[Star Ouroboros](https://github.com/razzant/ouroboros)** to follow its next evolution. A star also helps more people find the project, trace its history, and take part in what it becomes.

Reviewed skills, transport bridges, tools, and widgets are available through [OuroborosHub](https://github.com/razzant/OuroborosHub).

<p align="center">
  <img src="assets/swarm.jpg" width="760" alt="A live subagent swarm inside the Ouroboros chat: nested planner, builder, and researcher tasks with their outcomes">
</p>

## What Ouroboros Can Do

- **Modify its implementation.** Its editable surface spans application code, architecture, prompts, tools, and dependencies, while reflection can also reshape its living self-understanding.
- **Evolve autonomously.** Evolution campaigns turn selected improvements into reviewed changes that remain part of its Git history.
- **Continue across restarts.** Identity, memory, dialogue, knowledge, reflections, and version history form one ongoing biography.
- **Think between requests.** Background consciousness supports reflection, initiative, and preparation outside the immediate request-response loop.
- **Coordinate a live swarm.** Specialist agents can investigate or act in parallel, share task-tree findings, and return work for integration.
- **Work on external projects.** A separate Git workspace can receive the full task loop while Ouroboros keeps its own repository and governance boundary distinct.
- **Operate through desktop or CLI.** The native app and gateway-backed command line expose the same managed tasks, progress, artifacts, logs, and schedules.
- **Organize long-running work.** Project rooms keep working folders, journals, knowledge, task history, and conversations connected to the same identity.
- **Use remote or local models.** Supported provider APIs and local GGUF models can fill the runtime's configurable cognitive roles.
- **Grow through reviewed extensions.** Skills, transport bridges, widgets, MCP tools, and companion processes expand capability without folding every integration into the core.
- **Keep self-change inspectable.** Git history, review evidence, explicit protected surfaces, and restart checks make implementation changes traceable.

<p align="center">
  <img src="assets/game-demo.png" width="760" alt="A project room where Ouroboros built a 3D game, verified it with a screenshot, and served it locally">
</p>
<p align="center">
  <img src="assets/skill-hub.png" width="760" alt="OuroborosHub inside the app: official reviewed skills, each security-reviewed before it can be enabled">
</p>

This list is an orientation, not a second specification. [BIBLE.md](BIBLE.md) defines Ouroboros's identity and constitutional boundaries; [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) are the current technical sources of truth.

---

## Benchmarks

Ouroboros has reproducible self-reported state-of-the-art results on Terminal-Bench 2.1, OSWorld-Verified, and CL-Bench. In those model-matched results, it leads Codex, Claude Code, Cursor, and Hermes. The public SWE-bench Pro matched pair is a statistical tie with Codex CLI. A separate GAIA campaign reports 129/165 for Ouroboros and 131/165 for Claude Code, with strict pass@1 at 128/165 for both; its scrubbed trace capsule is still pending. Upstream review can take time, so open submissions are marked without delaying publication. Read every row as model plus harness because the same model can score differently inside a different harness.

| Benchmark | Model | Ouroboros | Comparison | Status | Evidence |
|-----------|-------|----------:|------------|--------|----------|
| Terminal-Bench 2.1 | Claude Opus-5 high | **86.74%** after zeroing one disclosed reward-hack trial (raw: 86.97%) | Claude Code + Fable 5: 83.8% | Self-reported, submission open | [submission](https://github.com/harbor-framework/terminal-bench-2-1/pull/175) · [run](https://hub.harborframework.com/jobs/2b145543-edeb-4a3b-b46f-4800310f1182) |
| Terminal-Bench 2.1 | Claude Opus-4.8 high | **80.22%** | Claude Code: 78.9% | Self-reported, public run | [run](https://hub.harborframework.com/jobs/4b8e244f-8ab0-4d28-8218-7cf346282faa) |
| Terminal-Bench 2.1 | GPT-5.5 | **84.3%** | Codex CLI: 83.1% | Self-reported, public run | [run](https://hub.harborframework.com/jobs/f02fd019-23e1-495f-af0a-ebd9a65f3079) |
| Terminal-Bench 2.1 | Grok-4.5 | **84.94%** after a reward-hack audit | Cursor CLI: 79.3% · Hermes: 77.53% | Self-reported, submission open | [submission](https://github.com/harbor-framework/terminal-bench-2-1/pull/146) |
| OSWorld-Verified | Claude Opus-5 | **90.69%** | previous best on the public board: 90.19% | Self-reported, full traces | [full traces](https://huggingface.co/datasets/razzant/ouroboros-osworld-verified-opus5) |
| OSWorld-Verified | Claude Sonnet-4.6 | **83.27%** | Pointer: 81.45% | Self-reported, full traces | [full traces](https://huggingface.co/datasets/razzant/ouroboros-osworld-verified-sonnet46) |
| CL-Bench | Claude Sonnet-4.6 | **0.2301, rank 1** | previous top: 0.1960 | Self-reported, submission open | [submission](https://github.com/pgasawa/continual-learning-bench/pull/10) · [full traces](https://huggingface.co/datasets/razzant/ouroboros-clbench-traces) |
| SWE-bench Pro | GPT-5.6-luna | 58.2% | Codex CLI: 59.4%, with no significant difference | Self-reported, matched traces | [matched-pair traces](https://huggingface.co/datasets/razzant/swepro-luna-matched-pair) |
| GAIA | Claude Sonnet-5 | 129/165, 78.2% | Claude Code: 131/165, 79.4%; strict pass@1 was 128/165 for both | Self-reported, scrubbed trace capsule pending | [methodology](devtools/benchmarks/gaia/METHODOLOGY.md) |

Benchmark adapters, run scripts, and per-benchmark methodology live in [`devtools/benchmarks/`](devtools/benchmarks/). The [benchmark evidence page](https://ouroboros-agent.ai/benchmarks/) gives a text-first summary for search and retrieval. The full story, including protocols, reward-hack audits, and leakage findings, is in the [launch write-up](https://habr.com/ru/companies/airi/articles/1065428/) (Russian).

---

## Run from Source

### Requirements

- Python 3.10+
- macOS, Linux, or Windows
- Git
- [GitHub CLI (`gh`)](https://cli.github.com/), optional unless you use GitHub integration

### Setup

```bash
git clone https://github.com/razzant/ouroboros.git
cd ouroboros
python3.11 -m venv .venv      # any Python >= 3.10 is OK
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv      # any Python >= 3.10 is OK
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
```

### Run

```bash
ouroboros server
```

Then open `http://127.0.0.1:8765` in your browser. The setup wizard will guide you through API key configuration.

### Desktop window from source

`ouroboros server` above is the browser path and needs nothing extra. To run the native
desktop shell from a checkout instead, install the launcher dependencies and start
`launcher.py`:

```bash
python -m pip install -r requirements-launcher.txt
python launcher.py
```

On **Linux** the window renders through the system PyGObject + WebKit2GTK, which cannot
come from PyPI. Install them from the distro and expose them to your virtualenv:

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-webkit2-4.1
python scripts/setup_linux_desktop.py          # adds one .pth to the venv; --check to verify only
```

The venv must be built on the same Python minor version as the system interpreter, since
`gi` ships a compiled extension; the script checks this and tells you if it does not match.
Without a usable backend the launcher does not fail — it opens the UI in your browser and
says so in `launcher.log`; the steps above are what turns that into a native window.
A source run needs no `repo.bundle` — the launcher seeds `~/Ouroboros/repo/` from the
checkout it lives in, so its provenance is whatever that checkout has committed rather
than a signed release.

### Interface language

The web UI ships English and Russian. **Settings → Behavior → Interface Language**
selects one, or leave it on automatic to follow the browser; the choice applies after
the page reloads. It changes the interface only — not the language Ouroboros thinks or
answers in, and never the content it produces. Screens that are not translated yet stay
in English rather than falling back to blank labels.

### Google Colab

Use [`notebooks/colab_quickstart.py`](notebooks/colab_quickstart.py) as a Colab-compatible cell script when you need a source-mode runtime without the desktop UI. It keeps runtime data on Google Drive and preserves the original Colab path without making it the primary installation flow.

### CLI / Headless

The `ouroboros` command attaches to the local runtime by default and starts one when `--start` is passed. It exposes managed tasks, progress streams, artifacts, logs, schedules, settings, skills, and evolution controls without duplicating the server's business logic.

```bash
ouroboros status
ouroboros run --start "2+2?"
ouroboros run "Summarize current runtime state"
ouroboros run --workspace /path/to/project --memory-mode forked --patch-out result.patch "Fix the failing test"
ouroboros tasks list
ouroboros logs tail progress --task-id <task_id>
ouroboros schedule add --name nightly-review --cron "0 2 * * *" "Run a maintenance review"
ouroboros schedule list
```

External workspaces must be separate Git worktree roots and may not overlap Ouroboros's own repository or data directory. Patch, streaming, detached-task, and schedule semantics are documented in the CLI help and the canonical [architecture](docs/ARCHITECTURE.md).

### For Agents

Another agent, script, or CI job can invoke Ouroboros through the same gateway-backed CLI:

```bash
ouroboros run --start \
  --workspace /path/to/project \
  --memory-mode forked \
  --patch-out result.patch \
  --result-json-out result.json \
  "Investigate the task, act, and verify the result"
```

Use `--jsonl` for a machine-readable event stream and `--detach` when the caller will follow the task with `ouroboros tasks watch <task_id>` or inspect it with `ouroboros tasks show <task_id>`. External workspace runs keep Ouroboros's own repository and governance context separate, then export changes as reviewable patch artifacts.

To change Ouroboros itself, follow [CONTRIBUTING.md](CONTRIBUTING.md) and read [BIBLE.md](BIBLE.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md), and [docs/CHECKLISTS.md](docs/CHECKLISTS.md) in full before editing.

### Configuration

The first-run wizard and **Settings** configure model access, cognitive roles, local models, review policy, runtime mode, budget, skills, and optional integrations. Ouroboros supports configurable remote providers, compatible endpoints, and local GGUF inference; exact settings and defaults live in [`ouroboros/config.py`](ouroboros/config.py) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

The server binds to `127.0.0.1:8765` by default. Read [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) before exposing it beyond loopback; non-local binds need `OUROBOROS_NETWORK_PASSWORD` or an explicitly trusted external access layer.

### Run Tests

```bash
make test
```

---

## Build

### Docker

```bash
docker build -t ouroboros-web .
docker run --rm -p 8765:8765 \
  -e OUROBOROS_NETWORK_PASSWORD='choose-a-password' \
  -e OUROBOROS_FILE_BROWSER_DEFAULT=/workspace \
  -v "$PWD:/workspace" \
  ouroboros-web
```

Docker runs the web runtime, not the native desktop shell. It bundles Chromium and WebKit support; use [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for network and container policy.

### Release tag prerequisite

Platform build scripts package only a commit already tagged with `v$(cat VERSION)`. Tag the exact release commit first:

```bash
git tag -a "v$(tr -d '[:space:]' < VERSION)" -m "Release v$(tr -d '[:space:]' < VERSION)"
```

`scripts/build_repo_bundle.py` verifies the tag and embeds the source binding into the packaged repository bundle. Signing, notarization, bytecode sealing, and CI invariants are documented in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

### macOS (.dmg)

```bash
bash scripts/download_python_standalone.sh
OUROBOROS_SIGN=0 bash build.sh
```

Output: `dist/Ouroboros-<VERSION>.dmg`, containing `Ouroboros.app`, an `Applications` shortcut, and `Install CLI.command`. Omit `OUROBOROS_SIGN=0` when a Developer ID signing identity is configured.

### Linux (.tar.gz)

```bash
bash scripts/download_python_standalone.sh
bash build_linux.sh
```

Output: `dist/Ouroboros-<VERSION>-linux-<arch>.tar.gz`, containing `Ouroboros/bin/install-ouroboros-cli`. If bundled browser tools need host libraries, run `./Ouroboros/python-standalone/bin/python3 -m playwright install-deps chromium webkit`.

### Windows (.zip)

```powershell
powershell -ExecutionPolicy Bypass -File scripts/download_python_standalone.ps1
powershell -ExecutionPolicy Bypass -File build_windows.ps1
```

Output: `dist\Ouroboros-<VERSION>-windows-x64.zip`, containing `Ouroboros\bin\install-ouroboros-cli.cmd`.


## Architecture and Runtime Data

The native launcher starts a web runtime and supervisor-managed agent workers. The agent core lives in `ouroboros/`, the interface in `web/`, the process plane in `supervisor/`, and the runtime's durable identity, state, history, logs, and skills under `~/Ouroboros/data/`.

The full component map, data flow, API surface, storage layout, safety boundary, and operational rationale live in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Deployment details live in [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## Runtime Commands

| Command | Purpose |
|---------|---------|
| `/panic` | Stop the runtime and its managed processes immediately. |
| `/restart` | Restart without automatically resuming the active owner task. |
| `/status` | Show workers, task queue, and budget state. |
| `/evolve on\|off` | Start or stop autonomous evolution. |
| `/review` | Queue a deep constitutional and architectural self-review. |
| `/bg start\|stop\|status` | Control background consciousness. |


## Philosophy

The 13 Constitution principles — Agency, Continuity, Meta-over-Patch,
Immune Integrity, Self-Creation, LLM-First, Authenticity & Reality
Discipline, Minimalism, Becoming, Versioning and Releases, the absorbed
Iterations / Spiral lineage, and Epistemic Stability — are defined in
full in [`BIBLE.md`](BIBLE.md). That file is the constitutional SSOT
(Bible P4 Ship-of-Theseus protection) and this README intentionally does
not paraphrase it.

---

## Contributing

External contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md)
for the complete workflow. Open pull requests against the lowercase
`ouroboros` branch and leave release-version allocation to maintainers. A
current OpenRouter triad + scope packet is the optional fast path; pull
requests without one remain welcome but require more maintainer-side review
and integration work.

---

## Version History

| Version | Date | Description |
|---------|------|-------------|
| 6.92.1 | 2026-08-08 | **chore: the managed Claudexor runtime pin moves to 3.3.13.** The reviewed pin (`ouroboros/claudexor_runtime_pin.json`) selects the release the delegation lanes provision and self-heal to, so a host running an older engine converges on the new one at the next handshake: 3.3.13 carries the dev-hygiene follow-up (Spotlight-shielded app bundle, dev-build chip suppression, a skew-safe opt-in for the gc data-root advisory, data-root allowlist truth, and confinement-policy shape validation ahead of the platform-availability branch). Pin fields move together — version, build sha, archive URL, sha256 and size; the Node artifact set (24.16.0) and protocol major 3 are unchanged. |
| 6.92.0 | 2026-08-08 | **fix: the four submarine-task failure classes are closed — budget honesty with a typed soft landing, acceptance bypasses that leave a record, one global prompt-cache TTL, and target-aware git everywhere.** The in-task cost stop becomes a typed ceiling (disabled/active/exhausted_soft_land/unknown) bound to the root tree cap: the deciding spend is the ledger-accounted TREE number with a disclosed own-cost fallback and a 120s staleness bound, a root cap with no working room soft-lands the task through the graceful wrap-up instead of running uncapped, waits terminate on SETTLED statuses with typed unknown_task_id rows plus a children_roster repair surface, and the root task detail gains a read-side cost_breakdown. Every forced rail (budget, round limit, deadline, provider death, unabsorbed children) now stamps a typed acceptance-bypass record from a closed reason enum — an owed-but-bypassed panel is no longer indistinguishable from "no panel warranted"; acceptance_claims flow contract-first from plan scope through wave freeze to schedule_subagent (success_criteria becomes an input alias), and reviewer evidence_refs resolve by exact packet membership, feeding only the release-clean bit. Prompt caching gets the owner's ONE global TTL (OUROBOROS_PROMPT_CACHE_TTL, default 1h) applied at the send-time finalizer with applied-TTL-aware pricing, cache-cold-restart telemetry and a cache-horizon note on long waits; emergency compaction splits necessity from utility with hysteresis, so an incompressible frozen frame stops buying per-round no-op passes. The shell git policy becomes one composed target-aware resolver — read-only git works everywhere, mutating git is judged by its real target/destination (bidirectional, casefolded, symlink-resolved containment), the strict self_worktree lane and the network fence stay intact; file-less project promotes auto-provision and bind a genesis workspace; ARCHITECTURE.md follows the owner context mode for every task class while DEVELOPMENT.md keys on the active repo binding (D-ARCH/D-DEV); confinement refusals become typed, legible policy denials. The unset `OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS` default becomes surface-aware — LIGHT mode now allows acting children on the `external_workspace`/`genesis` surfaces (previously unset meant all surfaces off there) while keeping `self_worktree` off, with a truthful Auto state in Settings. |
| 6.91.1 | 2026-08-08 | **fix: the SSE-follow performance budget test measures on-disk bytes correctly on Windows.** The release full-test matrix (first to run this suite on windows-latest — branch pushes only run the quick tier) caught the byte-offset expectation drifting by one byte per appended line: the test wrote its progress fixture in text mode, where Windows translates `\n` to `\r\n`, while the expected byte count was computed from `len(line.encode())`. The product offset tracking is byte-exact; the fixture writers now pin `newline='\n'` so the accounting is platform-stable. Test-only change. |
| 6.91.0 | 2026-08-08 | **feat: the Linux binary boots into the browser when no desktop webview backend exists.** Instead of dying with `WebViewException` on every GTK/QT-less Linux box, the launcher probes the pywebview backend AND whether a display actually exists (Linux-only probe; zero new code runs on macOS/Windows) — importing GTK is not the same as having a display, so an ssh session on a box carrying the system gi bindings gets browser mode instead of the crash (a Qt-selected backend is judged on the session environment alone — probing Qt would itself crash a display-less process; disclosed residual, documented in ARCHITECTURE), installs SIGINT/SIGTERM shutdown handlers BEFORE the lifecycle thread spawns the server (via the new `platform_layer.install_shutdown_signal_handlers` — the signal surface stays behind the platform layer), prints the URL, opens the default browser without blocking, and keeps the process alive while the server lifecycle runs — first-run onboarding included, served by the existing web overlay; a shutdown signal during startup aborts the readiness wait into the same clean teardown. Headless boxes get a clear message instead of a crash. README now leads its integrations story with subscription-powered delegation: use existing Codex, Claude Code, or Cursor subscriptions through the bundled [Claudexor](https://github.com/razzant/claudexor) engine. |
| 6.90.3 | 2026-08-08 | **fix: every native browser dialog is replaced by the in-app dialog, and the harness-accounts panel stops polling a dead daemon forever.** All 21 `window.prompt`/`confirm`/`alert` sites across `web/modules` migrate to the in-app `openConfirmDialog` (a new single-button alert mode included), fixing the two dead `window.prompt` sites on the macOS desktop shell — marketplace version updates and evolution campaign objectives silently returned null there — while preserving each site's contract (empty update version = latest, evolution cancel = do NOT start, `/panic` confirm keeps panic working — its complete confirm-and-send flow is node-tested through the injectable `confirmAndSendPanic`). A static CI gate bans the native dialog trio from `web/modules` so the class stays closed. Harness Accounts now fetches immediately on page show (no 5-second «Checking daemon…»), re-checks on tab/page activation, and login polling backs off (6→30s cap) and gives up honestly into the existing unconfirmed verdict after 10 consecutive failures instead of polling a dead daemon forever. Deleting every reviewer-slot row and saving now surfaces the backend's 400 («needs at least one slot») instead of pretending success while saving nothing. |
| 6.90.2 | 2026-08-08 | **fix: the commit gate asks whether the advisory lane is actually available — route- and slot-aware — not whether an Anthropic key exists.** The bypass test is now route- and slot-aware: a DISABLED advisory slot with a key present used to silently drop the compensating hermetic pytest (neither advisory nor tests ran before triad+scope), and a keyless DELEGATED advisory route — which does run, on the owner's subscription — used to be mislabelled `bypassed` and pay a duplicate full test run every commit. The gate reads the CURRENT advisory availability, not the stored advisory record's status — an owner-accepted residual disclosed at the gate. A malformed reviewer-slot configuration fails CLOSED into that preflight, and the parser now raises its documented `ValueError` for a non-dict advisory route instead of an `AttributeError` that escaped every fail-closed handler. Skill review's optional advisory pre-review now asks the same availability question (still fail-open), so a DISABLED advisory slot no longer dispatches — and no longer spends — review budget the owner switched off. A malformed structured reviewer-slot config also stops being silent where it mattered: plan review returns a typed unavailable result and skill review returns `pending` with the row-precise parse error, instead of quietly running the shipped default panel; task acceptance keeps reading the projected legacy/default panel by owner decision, and the architecture map now says so rather than claiming the refusal is universal. Delegated review-session custody rows carry root/parent task lineage, so a run recovered by restart reconciliation settles its spend on the real root instead of charging the reviewing task as its own. Also unblocks the commit gate on any machine running Ouroboros: the managed-update writer-fence ORDER test reached the operator's LIVE process ledger (the fence's custody step reads an import-time `DRIVE_ROOT` that an isolated `OUROBOROS_DATA_DIR` does not rebind), so it failed on a stale ledger entry and could have killed a ledgered task/session service that happened to be running. |
| 6.90.1 | 2026-08-07 | **fix: the managed Claudexor runtime pin moves to 3.3.11 — model-scoped quota availability at the source.** The engine now projects a typed per-snapshot `availability` on `/v2/quota` that judges exhaustion against the requested model, so a window scoped to other models (the Fable-only weekly cap behind the v6.90.0 incident) is disclosed as a `model_scoped_exhaustion` instead of reading as a dead profile. Ouroboros keeps its own positive-evidence aggregation for now — the two agree on the incident shape and the migration to the daemon's projection is tracked separately. Pin bytes verified against the published release. |
| 6.90.0 | 2026-08-07 | **fix: the subscription substrate actually carries the work — quota exhaustion is judged against the route's own model, and every silent seam found by the integration audit is closed.** The route-health predicate now scopes quota windows by `applies_to_models` (a Fable-only weekly window no longer takes an opus-pinned route offline for days — the live incident that burned ~$83 of metered API in 43 minutes), treats a spent window with no reset instant as spent rather than healthy, and demands POSITIVE evidence for the whole route: a profile whose quota endpoint answered 429 is unknown-not-dead, so the daemon's own rotation gets to try. A nanny dispatched onto the delegated substrate that reaches finalization with zero `delegate_start` calls gets one structural reminder (the wave-0 child burned $8.89 of metered opus under a subscription dispatch and only its prose admitted it); the decision stays the child's. The blocking scope reviewer no longer silently runs at `medium` on stock installs (the legacy slot path now honours `OUROBOROS_EFFORT_SCOPE_REVIEW`); scope and advisory session schemas grow a one-finding floor so the one answer their contracts reject by design cannot arrive schema-blessed; Panic Stop kills the owned Claudexor daemon's process group (subscription runs no longer outlive the red button); startup reconciliation of orphaned delegated runs goes through the ENSURE path instead of handshaking the corpse it just reaped — which also adopts a staged runtime update on restart; the production poll retries the engine's transient Git atomic-object ENOENT exactly once (CI had learned this tolerance while the live `delegate_wait` kept failing); `harness:effort` parses per the documented grammar and an unparseable configured route warns instead of silently disabling delegation. Web: "Add account…" replaces `window.prompt` (dead in the desktop WKWebView) with the standard input dialog and shows the normalized profile name before login; the account quota summary applies the same model-scope rule as the resolver; an empty Heavy slot visibly says it inherits Main. The Repo Commit Checklist gains item 23 (`delegated_transport`): the delegation invariants — capability_delta to three destinations, hash-bound EOF reads, typed blockers over silent re-routes, positive-evidence quota, custody lineage, no vendor branching — are now a named review surface. |
| 6.89.0 | 2026-08-03 | **feat: heavy work runs on the owner's subscriptions — Claudexor becomes Ouroboros's delegated execution substrate.** A delegated subagent is an ordinary child in the task tree acting as a NANNY: it starts a run in Claudexor, watches it, and collects the result as a CLAIM, never a proof. A parent declares exactly three axes — `write_surface`, `model_lane`, `executor` — and model, effort, route and TOOL profile are derived once, at dispatch, by `subagents.resolve_subagent_dispatch` (the CREDENTIAL profile is not derived host-side at all — it is a manual pin or the daemon's rotation, read back applied from the engine receipt); any landing below the request writes one typed `capability_delta` that reaches the durable record, the child's own prompt, and the parent's result. An omitted lane inherits the parent's; the public `effort` knob is withdrawn; an explicit `executor: harness` pin that no route can honor is a typed blocker (never a silent paid re-route), while `auto` leaves the account choice to the daemon's own rotation and falls back to the metered API loudly — rotation is turned on by a BEST-EFFORT settings patch at provisioning, deliberately not a guarantee (a patch failure must not eat the login that provisioned the daemon), and an attached daemon keeps whatever policy it already had. Custody of a delegated run is a durable event log: idempotent starts, verified cancellation, spend settled at what the harness disclosed (an estimate is never final; absence is unknown, not zero), full terminal output staged whole to the task drive with read-to-EOF acknowledgement. Review lanes ride the same substrate: commit-triad, scope and advisory rows each run `api_chat` or `agent_session` per configured slot; a retrieving scope reviewer is admitted as an owner-declared lower-assurance mode (BIBLE P3 amendment: SOURCED ≥200K window evidence, typed durable rows; stale or unsourced evidence keeps the row's findings as advisory AND blocks the commit for want of an authoritative verdict, exactly as a sub-floor api row does — the ≥200K floor is reached through the same owner-capability-ack the api slot uses), and a session verdict is typed via `outputSchema` where the route proves it, else extracted by a light model and gated on conformance. Reviewer windows come from Capability Evidence — the hardcoded 1M table is gone, sub-1M slots degrade their own seat instead of blocking the panel, and a required atlas artifact that cannot assemble fails the pack instead of shrinking it. The Models page gains reviewer SLOTS (stable ids, grouped route+model combobox, per-slot efforts, the 'runs as' last-execution projection) and a Harness Accounts section over an Ouroboros-OWNED claudexord (data-plane config dir, supervised, attach-if-alive; login via structural device-code cards). `claude_code_edit` is retired (D10) with `delegate_start` as its successor and a compat shim for stored `disabled_tools` contracts; the Claude Agent SDK gateway keeps one hardened read-only path (pinned trust surface, closed tool set, read confinement). The interpreter write fence is INVERTED: in light mode an inline invocation that could reach the repo is refused unless python's AST proves it cannot write — versioned basenames (`python3.11`, `node18`, `ruby3.2`) classify through one structural family classifier. The narrowing is bounded to what was approved: a non-python inline invocation that names the repo by an ABSOLUTE path or a `./`/`../`-prefixed relative one is refused even for reading, one that does not name the repo in those spellings still runs, and python inline code is now inspected on `run_command` as well as `run_script`. A PLAIN relative spelling is measured NOT to count — `EMBEDDED_RELATIVE_PATH_RE` anchors on `./`/`../`, so `node -e "…('ouroboros/safety.py')"` runs for a write as much as for a read — disclosed rather than closed, since widening the regex would be a strengthening. `drive`, `user_files` and advanced/pro are untouched — the non-python read-vs-write refinement was rolled back to the public head, which leaves a KNOWN write hole there (`node -e "require('fs').writeFileSync('<drive>/logs/chat.jsonl','')"` runs), disclosed rather than closed; measured, that hole covers the NODE and PHP families in every spelling (`node`, `nodejs`, `node18`, `php`, `php8.3`), while ruby and perl stay caught by the writer-command scan. Three gaps are disclosed, not detected: the AST walker and the write-indicator regex are two vocabularies with no invariant tying them (`io.open(p,"w")` and its codecs/gzip/bz2/lzma twins read as a proven read and truncate the file), aliased writers escape the walker (`import shutil as sh`), and the fence is a convenience boundary against the agent's own mistakes rather than containment against a determined writer. Also: the smoke ceiling `MAX_TOTAL_FUNCTIONS` is raised 5000→6000 (owner decision at the v6.88.0 merge: a ceiling that fails every routine union is churn, not protection; the next cycle still owes consolidation before growth). Managed Claudexor delivery is now part of the product path: release artifacts seed the exact reviewed engine, source/older installs obtain the same bytes plus the exact host Node, one Connect intent installs/repairs/updates in the foreground, and a live older daemon keeps serving until a natural next start. Packaging/lifecycle: finalization-grace becomes an episode with a typed control id, salvage preserves the full output on the canonical drive, the bind host is never stamped from settings over the environment, and a 3-OS CI platform gate smokes a real delegated task on explicit API keys (subscription auth is deliberately out of CI and verified by a live local run). |
| 6.88.0 | 2026-08-04 | **feat: MiniMax joins the direct providers, managed auto-updates become crash-safe end to end, and the commit gate goes two-pass.** Three community contributions land with maintainer hardening. MiniMax (`minimax::`) is a first-class direct provider: settings and onboarding cards with region validation, a live model catalog from the region host, safety/secret/Colab wiring, and a MiniMax-only install fills every ordinary and review slot — the deep-review slot honestly stays empty because the guaranteed 512K window floor sits below the 1M that deep review sizes against. The managed update lane is hardened: exact-SHA stable promotion under the update lock, restart serialized against an in-flight apply, a MANDATORY blocking post-commit test gate for assisted merges with auto-rollback (neither skip flags nor env toggles wave a managed merge through), and dirty local work rides a stash through clean auto-updates — never committed history — restored as uncommitted content after boot finalization, or onto the exact pre-update tree on rollback, with a kept and loudly disclosed stash entry when a restore would conflict. The per-commit hermetic pytest gate mirrors CI's split: a parallel not-serial pass then a serial pass in one disposable worktree under one total budget, with fail-closed named blocks for worker crashes, missing plugins, lost parallelism, and deleted test suites; process containment (new `ouroboros/process_containment.py`) reads membership from live kernel state at reap time and hard-blocks on escapees instead of guessing. Windows Job Object ctypes calls gain explicit 64-bit-safe ABI declarations, and multi-line git stdin payloads (the orphan-ref transaction, the gate's diff replay) switch to binary pipes so Windows CRLF translation cannot corrupt them. Thanks to @octo-patch (MiniMax) and @ndrew1337 (update hardening, two-pass gate). |
Older releases are preserved in Git tags and GitHub releases. Older 6.x rows (including 6.87.5, 6.87.4, 6.87.3, 6.87.2, 6.84.0, 6.87.1, 6.83.0, 6.86.1, 6.81.1, 6.76.0, 6.75.0, 6.74.5, 6.74.4, 6.74.1, 6.74.0, 6.73.2, 6.73.1, 6.73.0, 6.72.0, 6.71.2, 6.71.1, 6.71.0, 6.70.0, 6.69.0, 6.68.0, 6.67.0, 6.66.0, 6.65.4, 6.65.3, 6.65.2, 6.65.1, 6.65.0, 6.64.3, 6.64.2, 6.64.1, 6.64.0, 6.63.0, 6.62.0, 6.61.4, 6.61.3, 6.61.1, 6.61.0, 6.60.0, 6.59.0, 6.58.0, 6.57.0, 6.56.0, 6.55.0, 6.54.4, 6.54.2, 6.54.1, 6.54.0, 6.53.4, 6.53.0, 6.51.0), the 5.2.0 through 5.33.0-rc.6 rows, and former `4.0.0` rows are rolled off to respect the P9 changelog cap; their full bodies remain at their git tags.

---

## License

[MIT License](LICENSE)

Created by [Anton Razzhigaev](https://t.me/abstractDL) & Andrew Kaznacheev
