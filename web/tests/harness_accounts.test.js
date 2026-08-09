import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import {
    ATTACH_FALLBACK_MS,
    UNCONFIRMED_TEXT,
    accountLoginConfirmed,
    accountRows,
    attachFallbackDue,
    confirmLoginLive,
    daemonStatusLine,
    deviceCodeDisclosure,
    failureText,
    jobDetail,
    jobStateSummary,
    loginCardFace,
    loginCardHtml,
    loginInputSupport,
    loginStatusLine,
    loginVerdict,
    normalizeProfileName,
    pollResponseApplies,
    preserveCardFocus,
    promptProfileName,
    quotaSummary,
    runtimeActionLabel,
    submitLoginInput,
    verificationBadge,
    cancelLoginJob,
    loginSettleProven,
} from '../modules/harness_accounts.js';

test('managed runtime keeps one contextual Connect intent across install, repair, and update', () => {
    const payload = (runtime, daemon = {}) => ({ daemon: { state: 'not_provisioned', runtime, ...daemon } });

    // The owner-locked dictionary is exactly four labels, independent of the
    // connected state: Connect | Install & connect | Update & connect | Fix & connect.
    assert.equal(runtimeActionLabel(payload({ state: 'missing' })), 'Install & connect');
    assert.equal(runtimeActionLabel(payload({ state: 'error' })), 'Fix & connect');
    assert.equal(runtimeActionLabel(payload({ state: 'update_available' })), 'Update & connect');
    assert.equal(runtimeActionLabel(payload({ state: 'ready' })), 'Connect');

    assert.ok(daemonStatusLine(payload({ state: 'missing' })).text.includes('installs Claudexor'));
    assert.ok(daemonStatusLine(payload({ state: 'ready', version: '3.3.7' })).text.includes('3.3.7 is ready'));
    assert.ok(daemonStatusLine(payload({ state: 'installing', target_version: '3.3.7' })).text.includes('Claudexor 3.3.7'));
    const staged = daemonStatusLine(payload(
        { state: 'update_staged', staged_version: '3.3.7' },
        { state: 'running', engine_version: '3.2.1' },
    ));
    assert.equal(staged.tone, 'warn');
    assert.ok(staged.text.includes('3.3.7 is ready'));
    assert.ok(staged.text.includes('Engine 3.2.1 keeps running'));
    const repair = daemonStatusLine(payload({ state: 'error', last_error: 'checksum mismatch' }));
    assert.equal(repair.tone, 'error');
    assert.ok(repair.text.includes('Connect retries automatically'));
});

test('the login card explains foreground runtime preparation and retries the same intent', () => {
    const preparing = loginCardHtml({
        harness: 'claude', profile: '', job: null, preparingRuntime: true,
        error: '', verdict: null, confirming: false,
    });
    assert.ok(preparing.includes('Installing or checking Claudexor…'));
    assert.ok(!preparing.includes('data-login-retry'));

    const failed = loginCardHtml({
        harness: 'claude', profile: '', job: null, preparingRuntime: false,
        error: 'checksum mismatch', verdict: null, confirming: false,
    });
    assert.ok(failed.includes('checksum mismatch'));
    assert.ok(failed.includes('data-login-retry'));
    assert.ok(!failed.includes('Installing or checking Claudexor…'));
});

// GOLDEN fixture: the real /v2/credential-profiles body, produced by PARSING a
// sample through Claudexor's own Zod ControlCredentialProfilesResponse schema
// (packages/schema/src/credential-profile.ts) — not a hand-written flat map.
// If the upstream shape drifts, regenerate this file from the schema; the JS
// must consume whatever the schema emits.
const CREDENTIAL_PROFILES_RESPONSE = JSON.parse(readFileSync(
    fileURLToPath(new URL('./fixtures/credential_profiles_response.json', import.meta.url)),
    'utf-8',
));

test('both verification statuses are honest: vendor is trusted, local is neutral, never a permanent alarm', () => {
    // Q2-а: the local status has lied before (verification: passed a minute
    // before a 401), so it must never render as trusted. Finding #2: some
    // harnesses (cursor) have NO vendor probe in the engine, so a warn-toned
    // "not verified" there is an alarm nothing can ever clear — the local
    // state stays labeled unverified in WORDS, in a neutral tone.
    const vendor = verificationBadge({ status: {
        verification: 'passed', verification_source: 'vendor', last_verified_at: '2026-08-03T10:00:00Z',
    } });
    assert.equal(vendor.tone, 'ok');
    assert.ok(vendor.label.startsWith('verified live'));

    const local = verificationBadge({ status: { verification: 'passed', verification_source: 'local_store' } });
    assert.equal(local.tone, 'muted');
    assert.equal(local.label, 'local session — not verified live');

    assert.equal(verificationBadge({ status: {} }).label, 'not logged in');
    assert.equal(verificationBadge({ status: { verification: 'failed', verification_source: 'vendor' } }).tone, 'error');
});

// `freshness` is a REQUIRED member of the daemon's quota snapshot
// (@claudexor/schema quota.ts, `z.enum(['fresh','stale','unknown'])`), so every
// fixture here carries it exactly as the wire does.
test('an exhausted window is shown with its reset time, never hidden', () => {
    const snapshots = [{
        subject: { harness: 'codex', subject_id: 'koshak' }, freshness: 'fresh',
        constraints: [{ used_ratio: 1.0, resets_at: '2026-08-04T00:00:00Z' }],
    }];
    const summary = quotaSummary(snapshots, 'codex', 'koshak');
    assert.equal(summary.exhausted, true);
    assert.equal(summary.resetsAt, '2026-08-04T00:00:00Z');
    assert.ok(summary.label.includes('resets 2026-08-04T00:00:00Z'));

    const healthy = quotaSummary([{
        subject: { harness: 'codex' }, freshness: 'fresh', constraints: [{ used_ratio: 0.42 }],
    }], 'codex');
    assert.equal(healthy.exhausted, false);
    assert.equal(healthy.label, '42% of window used');
    assert.deepEqual(quotaSummary([], 'codex'), { label: '', exhausted: false, resetsAt: '' });
});

test('the card reads a window on the same bar the runtime dispatches on', () => {
    // Two ways the card and the runtime disagreed about the SAME snapshot.
    //
    // 1. STALENESS. `harness_window_wait_hint` skips any snapshot that is not
    //    `fresh` ("an old reading must not block a lane"), so a stale spent window
    //    still dispatches — while the card painted it red and named a reset time,
    //    telling the owner a lane was down that was in fact serving.
    const stale = [{
        subject: { harness: 'codex', subject_id: 'koshak' }, freshness: 'stale',
        constraints: [{ used_ratio: 1.0, resets_at: '2026-08-04T00:00:00Z' }],
    }];
    assert.deepEqual(quotaSummary(stale, 'codex', 'koshak'),
        { label: '', exhausted: false, resetsAt: '' });
    assert.equal(quotaSummary([{ ...stale[0], freshness: 'unknown' }], 'codex', 'koshak').exhausted, false);
    assert.equal(quotaSummary([{ ...stale[0], freshness: 'fresh' }], 'codex', 'koshak').exhausted, true);

    // 2. WHICH CONSTRAINT. The runtime spends a profile when ANY of its constraints
    //    is cooling down or full; the card read exhaustion off the single highest
    //    used_ratio, so a cooling 5-hour window hid behind a busier weekly one...
    const cooling = [{
        subject: { harness: 'codex', subject_id: 'koshak' }, freshness: 'fresh',
        constraints: [
            { used_ratio: 0.20, cooldown_until: '2026-08-04T00:00:00Z' },
            { used_ratio: 0.80 },
        ],
    }];
    const summary = quotaSummary(cooling, 'codex', 'koshak');
    assert.equal(summary.exhausted, true);
    assert.equal(summary.resetsAt, '2026-08-04T00:00:00Z');

    // ...and vanished entirely when the cooling constraint reported no ratio at all,
    // because a non-finite used_ratio was skipped before it could be read.
    const ratioless = [{
        subject: { harness: 'codex', subject_id: 'koshak' }, freshness: 'fresh',
        constraints: [{ cooldown_until: '2026-08-04T00:00:00Z' }],
    }];
    assert.equal(quotaSummary(ratioless, 'codex', 'koshak').exhausted, true);
});

test('a named profile\'s exhausted window is never reported as the default account\'s', () => {
    // The daemon stamps the DEFAULT subject with subject_id null and scopes every
    // cooldown to its own subject ("a profiled limit must never cool the default
    // subject down"). The row that names ONE account has to honour that: the old
    // `!subjectId ||` wildcard made the default row match every subject on the
    // harness and paint itself red off someone else's spent window.
    const snapshots = [
        { subject: { harness: 'codex', subject_id: null }, freshness: 'fresh',
          constraints: [{ used_ratio: 0.05 }] },
        { subject: { harness: 'codex', subject_id: 'koshak' }, freshness: 'fresh',
          constraints: [{ used_ratio: 1.0, resets_at: '2026-08-04T00:00:00Z' }] },
    ];
    const defaultRow = quotaSummary(snapshots, 'codex', '');
    assert.equal(defaultRow.exhausted, false);
    assert.equal(defaultRow.label, '5% of window used');
    const namedRow = quotaSummary(snapshots, 'codex', 'koshak');
    assert.equal(namedRow.exhausted, true);
    assert.equal(namedRow.resetsAt, '2026-08-04T00:00:00Z');
});

test('a model-scoped window never paints the whole account exhausted — it is a compact note', () => {
    // The daemon schema's own words (@claudexor/schema quota.ts): a non-null
    // applies_to_models is a per-model cap, and "a model-specific cap never
    // cools a different model on the same subject". Painting the whole account
    // "window exhausted" off one is the same class of misreport as the
    // wildcard-subject bug above — a block reported that will not happen.
    const subject = { harness: 'claude', subject_id: 'abstractdl' };
    const mixed = quotaSummary([{
        subject, freshness: 'fresh',
        constraints: [
            { id: 'fable-window', label: 'Fable window', applies_to_models: ['claude-fable-5'],
              used_ratio: 1.0, resets_at: '2026-08-08T00:00:00Z' },
            { applies_to_models: null, used_ratio: 0.4 },
        ],
    }], 'claude', 'abstractdl');
    assert.equal(mixed.exhausted, false);
    // The account bar stays the GLOBAL window's; the spent scope is still said.
    assert.equal(mixed.label, '40% of window used · Fable window spent');

    // Scoped-only spent (cooldown, no ratio): the note IS the label, no red.
    const scopedOnly = quotaSummary([{
        subject, freshness: 'fresh',
        constraints: [{ id: 'fable-window', label: 'Fable window',
            applies_to_models: ['claude-fable-5'], cooldown_until: '2026-08-08T00:00:00Z', used_ratio: null }],
    }], 'claude', 'abstractdl');
    assert.equal(scopedOnly.exhausted, false);
    assert.equal(scopedOnly.label, 'Fable window spent');

    // A scoped window that is merely busy says nothing at account level.
    assert.deepEqual(quotaSummary([{
        subject, freshness: 'fresh',
        constraints: [{ label: 'Fable window', applies_to_models: ['claude-fable-5'], used_ratio: 0.8 }],
    }], 'claude', 'abstractdl'), { label: '', exhausted: false, resetsAt: '' });

    // A GLOBAL window (applies_to_models null/omitted = every model) keeps the
    // account-level exhausted behavior exactly as before.
    const global = quotaSummary([{
        subject, freshness: 'fresh',
        constraints: [{ applies_to_models: null, used_ratio: 1.0, resets_at: '2026-08-08T00:00:00Z' }],
    }], 'claude', 'abstractdl');
    assert.equal(global.exhausted, true);
    assert.ok(global.label.startsWith('window exhausted'));

    // Without a label, the note falls back to the constraint id, then models.
    assert.equal(quotaSummary([{
        subject, freshness: 'fresh',
        constraints: [{ id: 'fable_5h', applies_to_models: ['claude-fable-5'], used_ratio: 1.0 }],
    }], 'claude', 'abstractdl').label, 'fable_5h spent');
});

// ---------------------------------------------------------------------------
// Add account: pywebview's WKWebView implements no window.prompt (it answers
// null silently), so the flow runs on the in-house input dialog.
// ---------------------------------------------------------------------------

test('Add account never touches window.prompt and asks through the in-house dialog', async () => {
    // REGRESSION guard for the dead desktop button: the module must not call
    // window.prompt at all — under pywebview it is a silent no-op. (The call
    // form, so a comment may still name the hazard.)
    const source = readFileSync(new URL('../modules/harness_accounts.js', import.meta.url), 'utf8');
    assert.ok(!/window\s*\.\s*prompt\s*\(/.test(source));
    assert.ok(source.includes("from './confirm_dialog.js'"));

    // An already-valid name asks exactly once, for TEXT input.
    const calls = [];
    const name = await promptProfileName({ dialogImpl: async (options) => {
        calls.push(options);
        return { confirmed: true, value: 'backup' };
    } });
    assert.equal(name, 'backup');
    assert.equal(calls.length, 1);
    assert.equal(calls[0].input, true);
    // The alphabet is stated up front, so normalization is never a surprise.
    assert.ok(calls[0].body.includes('anything else becomes "-"'));

    // Cancel, and a name that normalizes to nothing, are quiet no-ops.
    assert.equal(await promptProfileName({ dialogImpl: async () => ({ confirmed: false, value: 'x' }) }), '');
    assert.equal(await promptProfileName({ dialogImpl: async () => ({ confirmed: true, value: '   ' }) }), '');
});

test('a name normalization would change is shown back, editable, BEFORE any login starts', async () => {
    // The owner types "Работа": the profile alphabet turns that into "------",
    // and starting a login under that name silently is exactly the trap the
    // prompt() flow had. The dialog re-opens with the normalized name visible
    // AND editable; only an explicit confirm of a stable name proceeds.
    const rounds = [];
    const answers = [
        { confirmed: true, value: 'Работа' },
        { confirmed: true, value: 'work-2' },
    ];
    const name = await promptProfileName({ dialogImpl: async (options) => {
        rounds.push(options);
        return answers[rounds.length - 1];
    } });
    assert.equal(name, 'work-2');
    assert.equal(rounds.length, 2);
    assert.ok(rounds[1].body.includes('"Работа" will be saved as "------"'));
    assert.equal(rounds[1].initialValue, '------');

    // Accepting the shown normalized name as-is also works (one extra round).
    const folds = [];
    const folded = await promptProfileName({ dialogImpl: async (options) => {
        folds.push(options);
        return { confirmed: true, value: folds.length === 1 ? 'Work' : options.initialValue };
    } });
    assert.equal(folded, 'work');
    assert.equal(folds.length, 2);
    assert.equal(folds[1].initialValue, 'work');

    // The normalization itself, pinned.
    assert.equal(normalizeProfileName(' Work '), 'work');
    assert.equal(normalizeProfileName('Работа'), '------');
    assert.equal(normalizeProfileName('a b/c'), 'a-b-c');
    assert.equal(normalizeProfileName('ok_name-1'), 'ok_name-1');
    assert.equal(normalizeProfileName(''), '');
});

test('the device-code disclosure is found wherever the snapshot nests it', () => {
    const job = { snapshot: { disclosures: { deviceCode: {
        flow: 'chatgptDeviceCode', verificationUrl: 'https://auth.example/device', userCode: 'ABCD-1234',
    } } } };
    assert.deepEqual(deviceCodeDisclosure(job),
        { url: 'https://auth.example/device', code: 'ABCD-1234', flow: 'chatgptDeviceCode' });
    assert.equal(deviceCodeDisclosure({ state: 'running' }), null);
    assert.equal(deviceCodeDisclosure(null), null);
});

test('a URL-ONLY disclosure renders: the flow discriminates, not the code field', () => {
    // Claudexor's SetupDeviceCodeDisclosure (packages/schema/src/setup.ts):
    // `userCode` is EMPTY for the browser-callback (`chatgpt`) and `oauth_url`
    // flows — the latter is the sign-in link a TERMINAL-mode claude/cursor login
    // prints. Requiring both fields matched neither, so a published link showed
    // nothing at all; the login card is the whole point of D30's structural face.
    for (const flow of ['oauth_url', 'chatgpt', 'oauth_url_input']) {
        const job = { snapshot: { disclosures: { deviceCode: {
            flow, verificationUrl: 'https://claude.ai/oauth/authorize?x=1', userCode: '',
        } } } };
        assert.deepEqual(deviceCodeDisclosure(job),
            { url: 'https://claude.ai/oauth/authorize?x=1', code: '', flow }, flow);
        // …and the card must actually pick the structural face for it.
        assert.equal(loginCardFace({ mode: 'attach', attachCommand: 'cmd', job }), 'device', flow);
    }
    // A node carrying neither is still not a disclosure.
    assert.equal(deviceCodeDisclosure({ snapshot: { verificationUrl: 'https://a/b' } }), null);
});

test('job terminal states are the typed set, success is exactly succeeded', () => {
    assert.deepEqual(jobStateSummary({ state: 'succeeded' }),
        { state: 'succeeded', phase: '', terminal: true, succeeded: true });
    for (const bad of ['failed', 'cancelled', 'timed_out', 'not_supported', 'interrupted_unknown']) {
        const summary = jobStateSummary({ state: bad });
        assert.equal(summary.terminal, true, bad);
        assert.equal(summary.succeeded, false, bad);
    }
    assert.equal(jobStateSummary({ state: 'waiting_for_input', phase: 'awaiting_user' }).terminal, false);
});

test('the POLLED snapshot ENVELOPE is read, so the login poll can actually terminate', () => {
    // GET /v2/setup/jobs/:id/snapshot answers ControlSetupJobSnapshot —
    // {job, cursor, sequence, deviceCode?} — while POST /v2/setup/jobs answers a
    // bare ControlSetupJob. Reading only the top level saw a state on the create
    // response and NEVER on a poll, so the card's terminal banner never rendered
    // and the 3-second poll ran forever.
    const envelope = {
        job: { jobId: 'j1', state: 'succeeded', phase: 'completed' },
        cursor: 'c1', sequence: 7,
    };
    assert.deepEqual(jobStateSummary(envelope),
        { state: 'succeeded', phase: 'completed', terminal: true, succeeded: true });
    assert.equal(jobStateSummary({ job: { state: 'failed', phase: 'login' } }).terminal, true);
    assert.equal(jobStateSummary({ job: { state: 'waiting_for_input' } }).terminal, false);
    // The bare create-response job keeps working through the same reader.
    assert.equal(jobStateSummary({ state: 'cancelled' }).terminal, true);
});

test('account rows consume the REAL schema shape: array of {profile,status,identity} wrappers + harnessAccounts array', () => {
    // The status endpoint nests the daemon body under payload.profiles.
    const rows = accountRows({ profiles: CREDENTIAL_PROFILES_RESPONSE });
    assert.equal(rows.length, 2);  // one native pseudo-row + one registered profile

    const native = rows.find((row) => row.kind === 'native');
    assert.equal(native.harness, 'codex');  // read from harness_id (snake_case), not harnessId
    // A native login detected locally is still only local_store evidence.
    assert.equal(verificationBadge(native).label, 'local session — not verified live');

    const profile = rows.find((row) => row.kind === 'profile');
    // Read from the NESTED wrapper.profile.* snake_case fields, not a flat map.
    assert.equal(profile.harness, 'codex');
    assert.equal(profile.profile_id, 'koshak');
    assert.equal(profile.display_name, 'Koshak');
    assert.equal(profile.identity.email, 'koshak@example.com');
    // The vendor-verified status flows straight through from wrapper.status.
    assert.equal(verificationBadge(profile).tone, 'ok');
    assert.ok(verificationBadge(profile).label.startsWith('verified live'));
});

test('the invented flat camelCase shape yields NOTHING (guards against the regression)', () => {
    // The exact shape an earlier draft consumed — a flat map with camelCase
    // keys and harnessAccounts-as-object. The real schema never emits it, so
    // reading it must produce zero rows, not silently-empty harness fields.
    const rows = accountRows({ profiles: {
        harnessAccounts: { codex: { native_login_detected: true } },
        profiles: [{ harnessId: 'codex', profileId: 'backup' }],
    } });
    assert.equal(rows.length, 0);
});

test('DTO end-to-end: EMPTY and MULTI-ACCOUNT schema-parsed bodies', () => {
    // Both fixtures came through Claudexor's own Zod schema. Empty body:
    // zero rows, no invented natives, no crash.
    assert.deepEqual(accountRows({ profiles: { profiles: [], harnessAccounts: [] } }), []);
    assert.deepEqual(accountRows({ profiles: {} }), []);
    assert.deepEqual(accountRows({}), []);

    const MULTI = JSON.parse(readFileSync(
        fileURLToPath(new URL('./fixtures/credential_profiles_multi.json', import.meta.url)),
        'utf-8',
    ));
    const rows = accountRows({ profiles: MULTI });
    // 2 native pseudo-rows + 3 profiles, per harness.
    assert.equal(rows.length, 5);
    assert.deepEqual(rows.filter((r) => r.kind === 'profile').map((r) => `${r.harness}:${r.profile_id}`),
        ['codex:koshak', 'codex:backup', 'claude:main']);
    // Mixed verification renders each truth on its own row.
    const byId = Object.fromEntries(rows.filter((r) => r.kind === 'profile')
        .map((r) => [r.profile_id, verificationBadge(r)]));
    assert.equal(byId.koshak.tone, 'ok');                       // vendor-verified
    assert.equal(byId.backup.label, 'local session — not verified live');
    assert.equal(byId.main.tone, 'error');                      // vendor said failed
    // A claude native row with no login shows "not logged in", not a lie.
    const claudeNative = rows.find((r) => r.kind === 'native' && r.harness === 'claude');
    assert.equal(verificationBadge(claudeNative).label, 'not logged in');
});

test('the attach command is DEMOTED: never a card face, only a due fallback', () => {
    // The owner rejected terminal-first login ("Via your terminal" buttons and
    // an attach-command card body). A job with a command but nothing
    // structured renders the WAITING face; the command surfaces only through
    // attachFallbackDue as a collapsed Advanced affordance.
    const attachOnly = { attachCommand: 'CLAUDEXOR_CONFIG_DIR=/d claudexor setup attach j1', startedAtMs: 1000, job: { state: 'waiting_for_input' } };
    assert.equal(loginCardFace(attachOnly), 'progress');
    // The SAME job once the engine surfaces a structured OAuth disclosure:
    // the structural card wins — no terminal needed.
    assert.equal(loginCardFace({ ...attachOnly, job: {
        state: 'waiting_for_input',
        snapshot: { disclosures: { deviceCode: { flow: 'chatgptDeviceCode', verificationUrl: 'https://a/b', userCode: 'XY-12' } } },
    } }), 'device');
    // Errors outrank everything; nothing at all = progress; no job = none.
    assert.equal(loginCardFace({ error: 'nope', attachCommand: 'cmd', job: {} }), 'error');
    assert.equal(loginCardFace({ job: { state: 'running' } }), 'progress');
    assert.equal(loginCardFace(null), 'none');
});

test('card shape 2 keys on the disclosure FLOW string — the typed enum decides, no harness branching', () => {
    // The engine's 3.3.7 FINAL contract: `oauth_url_input` is the disclosure
    // flow for a job that also accepts a pasted code (claude's
    // manual-callback path); `oauth_url`/`chatgpt` stay link-only. The enum
    // decides for ANY harness — no boolean sidecar, no name fallback.
    const withInput = { snapshot: { disclosures: { deviceCode: {
        flow: 'oauth_url_input', verificationUrl: 'https://platform.claude.com/oauth/authorize?x=1', userCode: '' } } } };
    assert.equal(loginInputSupport(withInput), true);
    // A URL-only disclosure: shape 1, no input — even when the harness that
    // produced it happens to be claude (the flow is the truth, not the name).
    for (const flow of ['oauth_url', 'chatgpt', 'chatgptDeviceCode']) {
        const job = { snapshot: { disclosures: { deviceCode: {
            flow, verificationUrl: 'https://cursor.com/loginDeepControl?x=1', userCode: '' } } } };
        assert.equal(loginInputSupport(job), false, flow);
    }
    // No disclosure at all: no input field.
    assert.equal(loginInputSupport({ state: 'running' }), false);
    assert.equal(loginInputSupport(null), false);
});

test('the verdict never contradicts the state, and never fails off a verification-race read', () => {
    // The owner's live finding: a codex login SUCCEEDED while the card said
    // "Login failed · completed" — the engine's post-login probe read the
    // auth store codex clears at login start. Verification-flavored failures
    // are 'recheck' (judged by live account status), not final failures.
    assert.equal(loginVerdict({ job: { state: 'running', phase: 'awaiting_user' } }).kind, 'pending');
    assert.equal(loginVerdict({ job: { state: 'succeeded', phase: 'completed' } }).kind, 'success');
    for (const reason of ['capability_verification_failed', 'auth_not_ready']) {
        const verdict = loginVerdict({ job: { state: 'failed', phase: 'completed', outcome: { reason } } });
        assert.equal(verdict.kind, 'recheck', reason);
        assert.equal(verdict.reason, reason);
    }
    // A failure with NO typed reason is also unproven — recheck.
    assert.equal(loginVerdict({ job: { state: 'failed', phase: 'completed' } }).kind, 'recheck');
    // Genuine failures stay final, with their typed reason carried.
    const launch = loginVerdict({ job: { state: 'failed', outcome: { reason: 'launch_failed' } } });
    assert.deepEqual(launch, { kind: 'failure', reason: 'launch_failed' });
    assert.equal(loginVerdict({ job: { state: 'timed_out', outcome: { reason: 'timed_out' } } }).kind, 'failure');
    assert.equal(loginVerdict({ job: { state: 'cancelled', outcome: { reason: 'cancelled_by_user' } } }).kind, 'failure');
    // Wording: a real failure names its reason in words, no enum glue.
    assert.equal(failureText('launch_failed'), 'Sign-in failed — launch failed.');
});

test('the live state line renders plain words and NOTHING on a terminal job', () => {
    // "Login failed · completed" is structurally impossible: terminal jobs
    // render a verdict, and this line answers '' for them.
    assert.equal(loginStatusLine({ job: { state: 'failed', phase: 'completed' } }), '');
    assert.equal(loginStatusLine({ job: { state: 'succeeded', phase: 'completed' } }), '');
    assert.equal(loginStatusLine({ job: { state: 'queued', phase: 'preparing' } }), 'Starting the sign-in…');
    assert.equal(loginStatusLine({ job: { state: 'waiting_for_input', phase: 'launching' } }), 'Waiting for the sign-in link…');
    assert.equal(loginStatusLine({ job: { state: 'running', phase: 'verifying' } }), 'Checking the sign-in…');
    const disclosed = { job: { state: 'waiting_for_input', phase: 'awaiting_user' },
        snapshot: { disclosures: { deviceCode: { flow: 'oauth_url', verificationUrl: 'https://a/b', userCode: '' } } } };
    assert.equal(loginStatusLine(disclosed), 'Waiting for you to finish signing in in the browser…');
});

test('accountLoginConfirmed reads the exact harness+profile row from live status', () => {
    const payload = { profiles: {
        harnessAccounts: [
            { harness_id: 'codex', native_login_detected: true, identity: {} },
            { harness_id: 'claude', native_login_detected: false, identity: {} },
        ],
        profiles: [
            { profile: { harness_id: 'codex', profile_id: 'koshak' },
              status: { verification: 'passed', verification_source: 'vendor' }, identity: {} },
        ],
    } };
    // The default account (empty profile id) is confirmed by the daemon's own
    // local-store detection — the same evidence the row badge renders.
    assert.equal(accountLoginConfirmed(payload, 'codex', ''), true);
    assert.equal(accountLoginConfirmed(payload, 'claude', ''), false);
    // A named profile is judged by ITS row, never the native pseudo-row.
    assert.equal(accountLoginConfirmed(payload, 'codex', 'koshak'), true);
    assert.equal(accountLoginConfirmed(payload, 'codex', 'other'), false);
    assert.equal(accountLoginConfirmed({}, 'codex', ''), false);
});

function fakeResponse(status, body) {
    return { ok: status >= 200 && status < 300, status, json: async () => body };
}

test('submitLoginInput posts the code once and types the 404 capability gap (mock fetch)', async () => {
    const calls = [];
    const ok = await submitLoginInput('j 1', 'ABCD-1234', { fetchImpl: async (url, init) => {
        calls.push({ url, init });
        return fakeResponse(200, { ok: true, job: {} });
    } });
    assert.deepEqual(ok, { ok: true, degraded: false, conflict: '', error: '' });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, '/api/claudexor/login/j%201/input');
    assert.equal(calls[0].init.method, 'POST');
    assert.deepEqual(JSON.parse(calls[0].init.body), { value: 'ABCD-1234' });

    // DEGRADED-ENGINE PATH: the gateway's typed 404 (input_not_supported —
    // the engine predates the route or reaped the job) is `degraded`, so the
    // card falls back to Advanced instead of dead-ending on a raw error.
    const degraded = await submitLoginInput('j1', 'X', {
        fetchImpl: async () => fakeResponse(404, { error: 'input route not available', code: 'input_not_supported' }),
    });
    assert.equal(degraded.ok, false);
    assert.equal(degraded.degraded, true);
    // Any other failure is an ordinary error, NOT a capability degrade.
    const busy = await submitLoginInput('j1', 'X', { fetchImpl: async () => fakeResponse(503, { error: 'daemon down' }) });
    assert.deepEqual(busy, { ok: false, degraded: false, conflict: '', error: 'daemon down' });
    const dead = await submitLoginInput('j1', 'X', { fetchImpl: async () => { throw new Error('network gone'); } });
    assert.equal(dead.degraded, false);
    assert.ok(dead.error.includes('network gone'));
});

test('a 409 input conflict carries the engine code: the callback already completed', async () => {
    // Typed by the engine (final contract): setup_input_not_applicable means
    // the flow moved past the code step — e.g. claude's localhost callback
    // completed on its own. An ANSWER, not an error: the card shows a quiet
    // "no code needed" note and lets the job poll land the verdict.
    const result = await submitLoginInput('j1', 'ABCD', {
        fetchImpl: async () => fakeResponse(409, {
            error: 'input is not applicable to this flow/phase',
            code: 'setup_input_not_applicable',
        }),
    });
    assert.deepEqual(result, {
        ok: false, degraded: false, conflict: 'setup_input_not_applicable',
        error: 'input is not applicable to this flow/phase',
    });
});

test('a 409 repeat is typed too: the server is authoritative over the double-submit guard', async () => {
    // setup_input_already_submitted: our busy/sent guard prevents UI repeats,
    // but the server owns the truth (e.g. a second tab already sent a code).
    // The card treats it as already-sent, never as a failure.
    const result = await submitLoginInput('j1', 'ABCD', {
        fetchImpl: async () => fakeResponse(409, {
            error: 'a code was already submitted for this job',
            code: 'setup_input_already_submitted',
        }),
    });
    assert.equal(result.conflict, 'setup_input_already_submitted');
    assert.equal(result.degraded, false);
    assert.equal(result.ok, false);
    // A 409 with no code still classifies as a conflict, never a raw error.
    const untyped = await submitLoginInput('j1', 'ABCD', {
        fetchImpl: async () => fakeResponse(409, { error: 'conflict' }),
    });
    assert.equal(untyped.conflict, 'conflict');
});

test('confirmLoginLive re-polls live account status briefly instead of trusting one stale read', async () => {
    // First poll: the account still looks logged out (the stale window).
    // Second poll: the login shows up — confirmed, loop ends early.
    const cold = { profiles: { harnessAccounts: [{ harness_id: 'codex', native_login_detected: false }], profiles: [] } };
    const warm = { profiles: { harnessAccounts: [{ harness_id: 'codex', native_login_detected: true }], profiles: [] } };
    let polls = 0;
    const slept = [];
    const confirmed = await confirmLoginLive('codex', '', {
        fetchImpl: async () => fakeResponse(200, ++polls >= 2 ? warm : cold),
        attempts: 4, delayMs: 7, sleepImpl: async (ms) => { slept.push(ms); },
    });
    assert.equal(confirmed.confirmed, true);
    assert.equal(polls, 2);
    assert.deepEqual(slept, [7]);   // no sleep before the first poll
    assert.deepEqual(confirmed.payload, warm);

    // Still cold after every attempt: unconfirmed, with the last payload so
    // the caller can render the rows it actually saw.
    let coldPolls = 0;
    const unconfirmed = await confirmLoginLive('codex', '', {
        fetchImpl: async () => { coldPolls += 1; return fakeResponse(200, cold); },
        attempts: 3, delayMs: 1, sleepImpl: async () => {},
    });
    assert.equal(unconfirmed.confirmed, false);
    assert.equal(coldPolls, 3);   // bounded — it does not poll forever
    assert.deepEqual(unconfirmed.payload, cold);

    // A card closed mid-check aborts without a verdict.
    const stale = await confirmLoginLive('codex', '', {
        fetchImpl: async () => fakeResponse(200, cold),
        attempts: 3, delayMs: 1, sleepImpl: async () => {}, isStale: () => true,
    });
    assert.equal(stale.stale, true);
});

test('the Advanced fallback is due on a disclosure that never comes, or an engine that predates the modes', () => {
    const base = { attachCommand: 'CLAUDEXOR_CONFIG_DIR=/d claudexor setup attach j1', startedAtMs: 100000, engineDegraded: false, job: { state: 'waiting_for_input' } };
    // Inside the grace window: not due — the card just says it is waiting.
    assert.equal(attachFallbackDue(base, 100000 + ATTACH_FALLBACK_MS - 1), false);
    // Window elapsed with no disclosure: due.
    assert.equal(attachFallbackDue(base, 100000 + ATTACH_FALLBACK_MS), true);
    // An engine the create answer flagged as pre-disclosure: due immediately.
    assert.equal(attachFallbackDue({ ...base, engineDegraded: true }, 100001), true);
    // A rendered disclosure keeps the fallback hidden (link-first, always)…
    const disclosed = { ...base, job: { snapshot: { disclosures: { deviceCode: {
        flow: 'oauth_url', verificationUrl: 'https://a/b', userCode: '' } } } } };
    assert.equal(attachFallbackDue(disclosed, 100000 + ATTACH_FALLBACK_MS * 2), false);
    // …unless the engine is degraded (the input route 404'd mid-flow).
    assert.equal(attachFallbackDue({ ...disclosed, engineDegraded: true }, 100001), true);
    // No command = nothing to fall back to (the daemon-hosted codex flow).
    assert.equal(attachFallbackDue({ ...base, attachCommand: '' }, 100000 + ATTACH_FALLBACK_MS * 2), false);
    assert.equal(attachFallbackDue(null, 999999), false);
});

// ---------------------------------------------------------------------------
// Card rendering: the sign-in link is a PRIMARY click target, the verdict owns
// the card once it lands, and a re-check that ran out is not a failure.
// ---------------------------------------------------------------------------

function cardWithUrl(url, extra = {}) {
    return {
        harness: 'claude', profile: '', jobId: 'j1', attachCommand: '', startedAtMs: 0,
        job: { state: 'waiting_for_input', phase: 'awaiting_user',
            snapshot: { disclosures: { deviceCode: { flow: 'oauth_url', verificationUrl: url, userCode: '' } } } },
        ...extra,
    };
}

test('the disclosed sign-in URL is rendered only for http/https, through the house helper', () => {
    // The link is the card's primary action now — one click, engine-supplied
    // text. utils.safeExternalHrefAttr is the single house gate for that
    // (http/https only, escaped by the helper), and everything else must
    // render NO clickable link rather than a scheme the browser will execute.
    const safe = loginCardHtml(cardWithUrl('https://platform.claude.com/oauth/authorize?x=1&y=2'), 0);
    assert.ok(safe.includes('href="https://platform.claude.com/oauth/authorize?x=1&amp;y=2"'));
    assert.ok(safe.includes('data-open-signin'));
    assert.ok(loginCardHtml(cardWithUrl('http://127.0.0.1:1455/callback'), 0).includes('data-open-signin'));

    for (const hostile of [
        'javascript:alert(document.cookie)',
        'JavaScript:alert(1)',
        'data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==',
        'vbscript:msgbox(1)',
        'file:///etc/passwd',
        'not a url at all',
        '//evil.example/oauth',
    ]) {
        const html = loginCardHtml(cardWithUrl(hostile), 0);
        assert.ok(!html.includes('data-open-signin'), hostile);
        assert.ok(!html.includes('href='), hostile);
        assert.ok(html.includes('data-unsafe-signin-link'), hostile);
        // …and the raw scheme never reaches the DOM as an attribute value.
        assert.ok(!html.includes(hostile), hostile);
    }
});

test('a settled verdict silences the live status line, so the card never says both', () => {
    // The owner hit a card reading "Waiting for the sign-in link…" beside a
    // verdict: an overlapping poll tick applied a snapshot captured before the
    // job settled. Two guards, and this is the rendering half.
    const pending = cardWithUrl('https://a.example/b');
    assert.ok(loginCardHtml(pending, 0).includes('data-login-state'));

    const settled = { ...pending, verdict: { kind: 'success', reason: '' } };
    const html = loginCardHtml(settled, 0);
    assert.ok(!html.includes('data-login-state'));
    assert.ok(html.includes('Connected.'));
    // Same while the live re-check is deciding.
    assert.ok(!loginCardHtml({ ...pending, confirming: true }, 0).includes('data-login-state'));
});

test('an exhausted re-check says the sign-in is UNCONFIRMED, never that it failed', () => {
    // The row it waits for routinely lands a tick after the bounded re-poll
    // gives up, so a hard "Sign-in failed" there is a lie about a login that
    // may have succeeded. A genuine typed failure keeps its own wording.
    const unconfirmed = loginCardHtml(cardWithUrl('https://a.example/b', {
        verdict: { kind: 'unconfirmed', reason: 'auth_not_ready' } }), 0);
    assert.ok(unconfirmed.includes(UNCONFIRMED_TEXT));
    assert.ok(!unconfirmed.includes('Sign-in failed'));
    assert.ok(UNCONFIRMED_TEXT.includes('Refresh'));

    const failed = loginCardHtml(cardWithUrl('https://a.example/b', {
        verdict: { kind: 'failure', reason: 'launch_failed' } }), 0);
    assert.ok(failed.includes(failureText('launch_failed')));
    assert.ok(!failed.includes(UNCONFIRMED_TEXT));
});

test("a settled non-success verdict carries the engine's own explanation", () => {
    // The masking bug the owner hit: a codex login ended `auth_not_ready` and
    // the card showed only the fixed UNCONFIRMED_TEXT ("check the account row
    // above"), which reads as "wait a moment" — while the daemon had already
    // settled it terminally and said why. That sentence was in the snapshot
    // the card was holding and reached no reader; the two verdict texts are
    // fixed constants, so nothing else could ever carry it.
    const message = 'codex native session was not ready before the verification'
        + ' deadline: native Codex session is not logged in';
    // The POLL envelope NESTS the job, which is where the field really lands.
    const nested = cardWithUrl('https://a.example/b', {
        job: { job: { state: 'failed', phase: 'completed', message } },
        verdict: { kind: 'unconfirmed', reason: 'auth_not_ready' },
    });
    const unconfirmed = loginCardHtml(nested, 0);
    assert.ok(unconfirmed.includes('data-login-detail'));
    assert.ok(unconfirmed.includes(message));
    // The verdict wording itself is unchanged — this is additive.
    assert.ok(unconfirmed.includes(UNCONFIRMED_TEXT));

    // A typed failure gets it too: its reason is a category, not a sentence.
    assert.ok(loginCardHtml({ ...nested, verdict: { kind: 'failure', reason: 'launch_failed' } }, 0)
        .includes('data-login-detail'));

    // Never beside "Connected." (a stale message must not contradict success),
    // and never while the job is unsettled (the status line owns the card).
    assert.ok(!loginCardHtml({ ...nested, verdict: { kind: 'success', reason: '' } }, 0)
        .includes('data-login-detail'));
    assert.ok(!loginCardHtml({ ...nested, verdict: null }, 0).includes('data-login-detail'));
    assert.ok(!loginCardHtml({ ...nested, confirming: true, verdict: null }, 0)
        .includes('data-login-detail'));

    // Engine-supplied text is escaped like every other disclosure on this card.
    const hostile = loginCardHtml(cardWithUrl('https://a.example/b', {
        job: { job: { state: 'failed', message: '<img src=x onerror=alert(1)>' } },
        verdict: { kind: 'unconfirmed', reason: 'auth_not_ready' },
    }), 0);
    assert.ok(!hostile.includes('<img'));
    assert.ok(hostile.includes('&lt;img'));

    // jobDetail itself: both levels, trimmed, and total over junk.
    assert.equal(jobDetail({ message: '  hi  ' }), 'hi');
    assert.equal(jobDetail({ job: { message: 'deep' } }), 'deep');
    assert.equal(jobDetail({ message: '   ', job: { message: 'deep' } }), 'deep');
    assert.equal(jobDetail({ message: 42 }), '');
    assert.equal(jobDetail({}), '');
    assert.equal(jobDetail(null), '');
});

test("the engine explanation reaches the card from EITHER envelope level", () => {
    // The dual-level read is asserted on jobDetail() above, but the RENDER path
    // was only ever exercised with the POLL envelope ({job:{...}}). CREATE
    // answers a BARE ControlSetupJob, and the login card holds whichever of the
    // two last landed on it — `startLogin` writes `data.job` from the create
    // answer, and the poll tick overwrites it later. So a regression that
    // reached only one level would leave the other silently mute.
    const message = 'native Codex session is not logged in';
    const levels = {
        create_bare_job: { state: 'failed', phase: 'completed', message },
        poll_envelope: { job: { state: 'failed', phase: 'completed', message } },
    };
    for (const [label, job] of Object.entries(levels)) {
        const html = loginCardHtml(cardWithUrl('https://a.example/b', {
            job, verdict: { kind: 'unconfirmed', reason: 'auth_not_ready' },
        }), 0);
        assert.ok(html.includes('data-login-detail'), label);
        assert.ok(html.includes(message), label);
    }
    // Precedence when BOTH levels speak: the top level wins. Not a preference —
    // the poll writes the envelope it received, so the outer value is the fresher
    // reading of the two and must not be shadowed by a stale nested one.
    assert.equal(jobDetail({ message: 'outer', job: { message: 'inner' } }), 'outer');
});

test("the engine explanation is escaped in full and never truncated", () => {
    // Untrusted external text on an owner-facing surface, so two separate
    // properties. ESCAPING: the existing suite asserts `<img …>` only, while the
    // house helper escapes six characters — an unescaped `&` or quote is the same
    // class of defect one character over, and this line sits inside an element
    // whose attributes are built by the same interpolation.
    const hostile = `Tom & Jerry's "quoted" <b>bold</b> \`tick\``;
    const html = loginCardHtml(cardWithUrl('https://a.example/b', {
        job: { job: { state: 'failed', message: hostile } },
        verdict: { kind: 'failure', reason: 'launch_failed' },
    }), 0);
    for (const raw of ['&', '<', '>', '"', "'", '`']) {
        // Each hostile character reaches the DOM only in escaped form: the raw
        // one may still appear as HTML the card itself wrote (its own tags), so
        // the assertion is on the escaped entity being present…
        assert.ok(html.includes({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;',
        }[raw]), raw);
    }
    // …and on no fragment of the payload surviving as live markup.
    assert.ok(!html.includes('<b>bold</b>'));
    assert.ok(html.includes('&lt;b&gt;bold&lt;/b&gt;'));

    // NO TRUNCATION (BIBLE P1): this is the only place a settled login says WHY,
    // so a long engine sentence must arrive whole. The daemon's real ones already
    // chain a cause onto a summary; nothing bounds their length.
    const long = `${'the daemon explained at length: '.repeat(80)}end.`;
    const longHtml = loginCardHtml(cardWithUrl('https://a.example/b', {
        job: { job: { state: 'failed', message: long } },
        verdict: { kind: 'unconfirmed', reason: 'auth_not_ready' },
    }), 0);
    assert.ok(longHtml.includes(long));
    assert.ok(!longHtml.includes('…]'));   // no omission marker of any house shape
});

test('a settled failure with NO engine sentence renders the verdict alone', () => {
    // The absence path, in the render surface rather than only on jobDetail():
    // most settled jobs carry no `message` at all, so the common case must add
    // no empty element and — the specific hazard of interpolating an optional
    // field — no stringified `undefined`/`null` where a sentence would go.
    for (const job of [
        { job: { state: 'failed', phase: 'completed' } },          // absent
        { job: { state: 'failed', message: '' } },                 // empty
        { job: { state: 'failed', message: '   ' } },              // whitespace
        { job: { state: 'failed', message: null } },               // explicit null
    ]) {
        const html = loginCardHtml(cardWithUrl('https://a.example/b', {
            job, verdict: { kind: 'unconfirmed', reason: 'auth_not_ready' },
        }), 0);
        assert.ok(!html.includes('data-login-detail'), JSON.stringify(job));
        assert.ok(!html.includes('undefined'), JSON.stringify(job));
        assert.ok(!html.includes('null'), JSON.stringify(job));
        // The verdict itself is untouched by the missing detail.
        assert.ok(html.includes(UNCONFIRMED_TEXT), JSON.stringify(job));
    }
});

test('the verify-race incident, composed end to end: recheck runs out and the card still says why', async () => {
    // The owner's actual incident shape. Its three steps are each asserted
    // above in isolation, which is exactly how the defect survived: every part
    // worked and the composition still rendered only a fixed constant. This
    // walks the same steps the settle path walks, in order, on one job.
    //
    // (settleVerdict itself is not exported — it re-renders the live DOM — so
    // this composes the exported steps rather than executing that function. It
    // pins the CHAIN, not settleVerdict's own wiring; that remains untested.)
    const message = 'codex native session was not ready before the verification'
        + ' deadline: native Codex session is not logged in';
    const job = { job: { state: 'failed', phase: 'completed', message, outcome: { reason: 'auth_not_ready' } } };

    // 1. The job settled failed, but on a reason a verification race fabricates.
    const verdict = loginVerdict(job);
    assert.equal(verdict.kind, 'recheck');
    assert.equal(verdict.reason, 'auth_not_ready');

    // 2. The bounded live re-check never sees the row appear.
    const cold = { profiles: { harnessAccounts: [{ harness_id: 'codex', native_login_detected: false }], profiles: [] } };
    const check = await confirmLoginLive('codex', '', {
        fetchImpl: async () => fakeResponse(200, cold),
        attempts: 2, delayMs: 1, sleepImpl: async () => {},
    });
    assert.equal(check.confirmed, false);

    // 3. So the card takes the unconfirmed verdict — and BOTH halves land: the
    //    honest "unknown" wording AND the daemon's own sentence. Before the fix
    //    step 3 produced the constant alone, which reads as "wait a moment" for
    //    a job the daemon had already settled terminally.
    const html = loginCardHtml(cardWithUrl('https://a.example/b', {
        job, verdict: check.confirmed ? { kind: 'success', reason: '' } : { kind: 'unconfirmed', reason: verdict.reason },
    }), 0);
    assert.ok(html.includes(UNCONFIRMED_TEXT));
    assert.ok(html.includes(message));
    assert.ok(!html.includes('Sign-in failed'));
});

test('a poll answer applies only to the job it was captured for, and only while unsettled', () => {
    // The ordering rule behind the contradictory card: two overlapping async
    // ticks can land out of order, so an OLDER snapshot must never be written
    // over a job that has already settled — or onto a card that has since been
    // closed or reopened for another account.
    const active = { jobId: 'j1' };
    assert.equal(pollResponseApplies(active, active), true);
    assert.equal(pollResponseApplies(active, { jobId: 'j2' }), false);   // reopened
    assert.equal(pollResponseApplies(active, null), false);              // closed
    assert.equal(pollResponseApplies(null, null), false);
    assert.equal(pollResponseApplies({ ...active, verdict: { kind: 'success' } },
        active), false);
    const confirming = { jobId: 'j1', confirming: true };
    assert.equal(pollResponseApplies(confirming, confirming), false);
});

// ---------------------------------------------------------------------------
// The 3-second poll re-render must not eat the caret. Minimal element stubs
// (the repo's house idiom — no jsdom) plus node's fake timers, so the re-render
// cadence itself is what the assertion runs through.
// ---------------------------------------------------------------------------

function fakeCodeInput({ disabled = false, start = 3, end = 5 } = {}) {
    const calls = { focus: 0, range: null };
    return {
        disabled, value: 'ABCD-1234', selectionStart: start, selectionEnd: end,
        hasAttribute: (name) => name === 'data-login-code-input',
        focus() { calls.focus += 1; },
        setSelectionRange(from, to) { calls.range = [from, to]; },
        calls,
    };
}

function fakeCardHost(replacement, focused) {
    return {
        swaps: 0,
        contains: (node) => node === focused,
        querySelector: () => replacement,
    };
}

test('the paste-code field survives every poll re-render, caret and selection intact', (t) => {
    t.mock.timers.enable({ apis: ['setInterval'] });
    const typing = fakeCodeInput({ start: 3, end: 5 });
    const replacement = fakeCodeInput({ start: 0, end: 0 });
    const host = fakeCardHost(replacement, typing);
    const doc = { activeElement: typing };
    // Exactly what the job poll does: swap the card's DOM on every tick.
    setInterval(() => preserveCardFocus(host, () => { host.swaps += 1; }, doc), 3000);

    t.mock.timers.tick(3000);
    assert.equal(host.swaps, 1);
    assert.equal(replacement.calls.focus, 1);
    assert.deepEqual(replacement.calls.range, [3, 5]);
    t.mock.timers.tick(3000);
    assert.equal(host.swaps, 2);
    assert.equal(replacement.calls.focus, 2, 'every tick restores focus, not just the first');
    t.mock.timers.reset();
});

test('a re-render never STEALS focus, and never focuses a field the code already left', () => {
    // Nothing in the card focused: the swap happens, the caret stays wherever
    // the owner actually put it (another field, another section).
    const elsewhere = { hasAttribute: () => false };
    const replacement = fakeCodeInput();
    const host = fakeCardHost(replacement, null);
    preserveCardFocus(host, () => { host.swaps += 1; }, { activeElement: elsewhere });
    assert.equal(host.swaps, 1);
    assert.equal(replacement.calls.focus, 0);

    // Focused, but the code was accepted meanwhile: the replacement renders
    // disabled and must not be focused (nor asked for a selection range).
    const typing = fakeCodeInput();
    const sent = fakeCodeInput({ disabled: true });
    const host2 = fakeCardHost(sent, typing);
    preserveCardFocus(host2, () => { host2.swaps += 1; }, { activeElement: typing });
    assert.equal(host2.swaps, 1);
    assert.equal(sent.calls.focus, 0);
    assert.equal(sent.calls.range, null);

    // No document at all (module imported in node): the swap still runs.
    const host3 = fakeCardHost(replacement, null);
    preserveCardFocus(host3, () => { host3.swaps += 1; }, null);
    assert.equal(host3.swaps, 1);
});


// ---------------------------------------------------------------------------
// C7: login-job serialization — a new login only after the old one is gone.
// ---------------------------------------------------------------------------

test('cancelLoginJob reports gone only on ok/404/410; failures and network death are NOT cancelled', async () => {
    const mk = (status, ok) => async () => ({ ok, status });
    assert.equal(await cancelLoginJob('job-1', mk(200, true)), true);
    assert.equal(await cancelLoginJob('job-1', mk(404, false)), true);   // already gone
    assert.equal(await cancelLoginJob('job-1', mk(410, false)), true);   // already gone
    assert.equal(await cancelLoginJob('job-1', mk(503, false)), false);  // daemon may still run it
    assert.equal(await cancelLoginJob('job-1', mk(500, false)), false);
    assert.equal(await cancelLoginJob('job-1', async () => { throw new Error('net'); }), false);
    assert.equal(await cancelLoginJob('', async () => { throw new Error('must not be called'); }), true);
});

test('startLogin centralizes the C7 guard: cancel-or-refuse BEFORE the new login POST', () => {
    // ESM keeps startLogin internal state untestable directly; pin the control
    // flow at the source level (same source-based technique as the HTML pins
    // in this file): the guard must sit inside startLogin ahead of the POST,
    // and a failed cancellation must return without starting a second job.
    const src = readFileSync(fileURLToPath(new URL('../modules/harness_accounts.js', import.meta.url)), 'utf8');
    const fn = src.slice(src.indexOf('async function startLogin'));
    const guardAt = fn.indexOf('cancelLoginJob(prev.jobId)');
    const postAt = fn.indexOf("apiFetch('/api/claudexor/login'");
    assert.ok(guardAt > -1, 'startLogin must call cancelLoginJob for a live previous job');
    assert.ok(postAt > -1);
    assert.ok(guardAt < postAt, 'the C7 guard must run before the new login POST');
    const guarded = fn.slice(guardAt, postAt);
    assert.match(guarded, /if \(!cancelled && !settledMeanwhile\) \{[\s\S]*?return;/,
        'a failed cancel (with the job still unsettled) must refuse the new login');
});


test('loginSettleProven: only a TERMINAL job snapshot proves the settle — an unconfirmed verdict does NOT', () => {
    assert.equal(loginSettleProven(null), false);
    assert.equal(loginSettleProven({}), false);
    assert.equal(loginSettleProven({ job: { state: 'running' } }), false);
    // Lost contact: the give-up verdict must NEVER read as proof of settle —
    // the job may still be live, and treating it as settled would let a
    // dismiss/restart drop or duplicate a live login (round b7).
    assert.equal(loginSettleProven({ job: { state: 'running' }, verdict: { kind: 'unconfirmed' } }), false);
    assert.equal(loginSettleProven({ job: null, verdict: { kind: 'unconfirmed' } }), false);
    assert.equal(loginSettleProven({ job: { state: 'succeeded' } }), true);
    assert.equal(loginSettleProven({ job: { state: 'failed' } }), true);
    assert.equal(loginSettleProven({ job: { state: 'cancelled' } }), true);
});
