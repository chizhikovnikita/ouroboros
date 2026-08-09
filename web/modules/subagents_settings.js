// Subagents section (Models page, sibling of Reviewer Slots) — the owner-facing
// face of the delegated-subagent capability.
//
// Until this section existed the whole capability shipped invisible: delegation
// (OUROBOROS_SUBAGENT_HARNESS) and the write permission
// (OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS) were reachable only by hand-editing
// settings.json, so the default install ran with delegation off and no control
// said so.
//
// Shape rules, same house style as Reviewer Slots:
//  * The harness list comes from the SAME source the Harness Accounts panel
//    reads (accountRows over /api/claudexor/status) — one catalog path, one
//    login-capable discriminator, no second inventory.
//  * The MODEL is the owner's default for delegated runs (owner, 2026-08-04):
//    the `=model` tail of OUROBOROS_SUBAGENT_HARNESS, picked from the same
//    engine discovery the reviewer rows use — "Engine default model" = empty
//    tail. Effort stays derived per call; a hand-written `:effort` remainder
//    rides through verbatim and has no control here.
//  * With no subscription connected the section says so instead of rendering a
//    toggle that cannot do anything.
//
// Pure helpers live at the top and are node-tested without a DOM.

import { apiFetch } from './api_client.js';
import { accountRows } from './harness_accounts.js';
import { renderSegmentedField } from './page_header.js';
import { sessionModelOptions } from './reviewer_slots.js';
import { formatRelativeAge } from './ui_helpers.js';
import { escapeHtmlAttr as escapeHtml } from './utils.js';

// The owner's explicit "delegation off" (see subagents.parse_subagent_harness):
// distinguishable from an empty, never-decided value, which is what lets the
// connected-subscription default exist without overriding a real decision.
export const DELEGATION_OFF = 'off';

// ---------------------------------------------------------------------------
// Pure helpers.
// ---------------------------------------------------------------------------

export function parseSubagentRoute(value) {
    // `harness[=model][:effort]`. This UI authors the harness AND the `=model`
    // tail (the owner's default model select); a hand-written `:effort`
    // remainder is carried through VERBATIM rather than silently dropped on
    // the next save — the owner wrote it on purpose.
    const raw = String(value || '').trim();
    if (!raw || raw.toLowerCase() === DELEGATION_OFF) {
        return { harness: '', suffix: '', decided: Boolean(raw) };
    }
    const eq = raw.indexOf('=');
    if (eq < 0) return { harness: raw, suffix: '', decided: true };
    return { harness: raw.slice(0, eq), suffix: raw.slice(eq), decided: true };
}

export function composeSubagentRoute(harness, suffix) {
    const h = String(harness || '').trim();
    if (!h) return DELEGATION_OFF;
    return `${h}${String(suffix || '')}`;
}

export function splitSubagentTail(suffix) {
    // The `=model[:effort]` tail, split at the FIRST ':' exactly like the
    // backend parser (subagents.parse_subagent_harness): `model` is the owner
    // default the select now edits, `effortSuffix` is the hand-written effort
    // remainder — kept VERBATIM, leading ':' included, so composing back is
    // byte-identical when the model is untouched.
    const raw = String(suffix || '');
    const body = raw.startsWith('=') ? raw.slice(1) : raw;
    const colon = body.indexOf(':');
    if (colon < 0) return { model: body, effortSuffix: '' };
    return { model: body.slice(0, colon), effortSuffix: body.slice(colon) };
}

export function composeSubagentTail(model, effortSuffix) {
    const m = String(model || '');
    const e = String(effortSuffix || '');
    return m || e ? `=${m}${e}` : '';
}

export function lastDelegationLine(entry) {
    // ONE muted receipt line under the section: what the last delegated run
    // REALLY ran as. Absence is shown as absence — no record, no line; an
    // undisclosed applied model says so instead of echoing the request; a
    // requested≠applied pair is disclosed loudly (owner decision: the run
    // completes on what the engine gave, the mismatch is advisory).
    if (!entry || typeof entry !== 'object' || !entry.run_id) return '';
    const requested = String(entry.requested_model || '');
    const applied = String(entry.applied_model || '');
    const parts = [String(entry.route || '') || 'unknown route'];
    if (requested && applied && requested !== applied) {
        parts.push(`requested ${requested} → ran ${applied}`);
    } else {
        parts.push(applied || 'model not disclosed');
    }
    const when = formatRelativeAge(Date.parse(entry.ts || ''), 'just now');
    if (when) parts.push(when);
    return `Last delegated run: ${parts.join(' · ')}`;
}

function harnessModels(payload, harnessId) {
    for (const harness of payload?.harnesses || []) {
        if (String(harness?.id || '') === String(harnessId || '')) {
            return harness?.models || [];
        }
    }
    return [];
}

export function connectedHarnesses(payload) {
    // "Connected" = the accounts panel's own answer, not a second definition:
    // an account row whose verification the daemon observed AND passed — the
    // same `=== 'passed'` predicate accountLoginConfirmed applies (native rows
    // project 'passed' | ''). A failed/expired verification renders red in the
    // accounts panel; counting it as connected here would let the default-on
    // rule auto-pin a route whose every dispatch falls back to native.
    const names = {};
    for (const harness of payload?.harnesses || []) {
        const id = String(harness?.id || '');
        if (id) names[id] = String(harness.display_name || id);
    }
    const out = [];
    for (const row of accountRows(payload)) {
        if (String(row?.status?.verification || '') !== 'passed') continue;
        if (out.some((item) => item.id === row.harness)) continue;
        out.push({ id: row.harness, label: names[row.harness] || row.harness });
    }
    return out;
}

export function delegationView({ saved = '', payload = null, statusError = '', edit = null, loaded = true } = {}) {
    // The whole section as ONE value: which state to render, what the harness
    // select offers, and the muted sentence under it. `edit` is the owner's
    // unsaved choice laid over the saved value — it goes through the same
    // function so the sentence can never describe a different state than the
    // controls above it (it did: turning delegation off still read "on by
    // default", because the note was computed from the saved value alone).
    const route = parseSubagentRoute(saved);
    if (!loaded) {
        // Not read YET is not "nothing connected": until the accounts arrive the
        // section states only that it is reading (collect() already guards on the
        // same fact, so this renders nothing it would then author).
        return { state: 'loading', enabled: false, harness: '', model: '', modelOptions: [], suffix: '', options: [],
            note: 'Reading your coding-agent accounts…' };
    }
    if (statusError) {
        // A failed accounts read decides the WHOLE view, saved harness or not.
        // Offering the live control here let Save answer "saved" while collect()
        // returned {} — and any sentence about where delegated work runs would be
        // a guess: this page failing to read the accounts says nothing about the
        // daemon-side account, so the note stops at the error.
        return { state: 'unknown', enabled: false, harness: '', model: '', modelOptions: [], suffix: '', options: [],
            note: `Could not read the coding-agent accounts (${statusError}). Delegation is unchanged; reopen Settings when the connection is back.` };
    }
    const connected = connectedHarnesses(payload);
    const savedHarness = route.harness;

    if (!savedHarness && !connected.length) {
        // Nothing to delegate to, so there is no control to offer. A decided
        // `off` is the owner's own answer and never re-defaults, so promising
        // "turns on by itself" over it would announce an override that will not
        // happen.
        return route.decided
            ? { state: 'no_subscription', enabled: false, harness: '', model: '', modelOptions: [], suffix: '', options: [],
                note: 'Delegation is off because you turned it off, and it stays off until you turn it back on. No coding-agent subscription is connected right now; sign one in under Providers → Harness Accounts to make delegation available again.' }
            : { state: 'no_subscription', enabled: false, harness: '', model: '', modelOptions: [], suffix: '', options: [],
                note: 'No coding-agent subscription is connected, so there is nothing to delegate to. Sign one in under Providers → Harness Accounts and delegation turns on by itself.' };
    }

    const defaultOn = !route.decided && connected.length > 0;
    const enabled = edit ? Boolean(edit.enabled) : (Boolean(savedHarness) || defaultOn);
    const harness = enabled
        ? String((edit && edit.harness) || savedHarness || connected[0]?.id || '')
        : '';
    // A hand-written model/effort tail belongs to the harness it was written
    // for; carrying it to another one would pin a model that harness may not serve.
    const savedTail = splitSubagentTail(harness && harness === savedHarness ? route.suffix : '');
    // The model select's value: the owner's unsaved pick when there is one,
    // else the saved default. A harness switch drops the saved tail above, so
    // the reset to "Engine default model" is VISIBLE on screen before Save —
    // accepted residual, deliberately no per-harness memory. The `:effort`
    // remainder is never edited here and rides through verbatim.
    const model = edit && edit.model !== null && edit.model !== undefined
        ? String(edit.model) : savedTail.model;
    const suffix = composeSubagentTail(model, savedTail.effortSuffix);
    // The SAME options fragment the reviewer rows use, "(not in discovery)"
    // guard included: a Save while the daemon is down must not silently erase
    // the saved model pin.
    const modelOptions = enabled && harness
        ? sessionModelOptions({ models: harnessModels(payload, harness) }, model)
        : [];

    const options = [...connected];
    if (savedHarness && !options.some((item) => item.id === savedHarness)) {
        // A SAVED route keeps an option even when discovery cannot see it right
        // now (daemon down, account signed out): dropping it would make the
        // browser redraw the row as the first connected entry, and the next Save
        // would silently re-point delegation at an account nobody chose.
        options.push({ id: savedHarness, label: `${savedHarness} (no account connected)` });
    }

    let state = 'on';
    let note = 'The coding runs on this subscription, on the model picked here '
        + '("Engine default model" leaves the choice to the coding agent); the subagent '
        + 'itself still runs on the API to drive and check it. Reasoning effort is still '
        + 'derived from each call.';
    if (!enabled) {
        state = 'off';
        note = 'Subagents run entirely on the API. Turn this on to send the coding to a connected subscription.';
    } else if (!connected.some((item) => item.id === harness)) {
        note = `No connected account for ${harness} right now — delegated work runs as an ordinary subagent on the API until it is signed in again.`;
    } else if (!savedHarness) {
        // The owner never authored this; saying "on" without saying it is not
        // stored yet would misreport where subagents actually run today.
        state = 'default_on';
        note = 'On by default now that a subscription is connected. Save Settings to apply it — until then subagents still run on the API.';
    }
    return { state, enabled, harness, model, modelOptions, suffix, options, note };
}

// ---------------------------------------------------------------------------
// DOM section (Models page). State is module-local; collect is synchronous.
// ---------------------------------------------------------------------------

const state = {
    loaded: false,
    saved: '',
    payload: null,
    statusError: '',
    // The owner's unsaved answer only; everything derived from it (which route,
    // which options, which sentence) stays in delegationView. `model: null`
    // means "no unsaved model edit" — '' is a real answer (Engine default).
    enabled: null,
    harness: '',
    model: null,
    onChange: () => {},
};

export function renderSubagentsSection() {
    return `
        <div class="form-section" id="subagents-section">
            <h3>Subagents</h3>
            <div class="settings-section-copy">
                Where Ouroboros's subagents run. By default a subagent is an ordinary child on your
                API budget. Delegation hands the coding to a connected coding-agent subscription —
                that part spends the subscription's window; the subagent itself still runs on the
                API to drive and check it.
            </div>
            <div id="subagents-rows" class="reviewer-slot-rows"></div>
            <div class="settings-effort-card">
                <label>Allow Mutative Subagents</label>
                <input id="s-allow-mutative-subagents" type="hidden" value="on">
                ${renderSegmentedField({
                    target: 's-allow-mutative-subagents',
                    title: 'Applies on the next task; no restart required.',
                    options: [
                        { value: 'off', label: 'Off' },
                        { value: 'auto', label: 'Auto' },
                        { value: 'on', label: 'On' },
                    ],
                })}
                <div class="settings-inline-note">
                    Whether a subagent may WRITE — in an isolated git worktree of this repo, an external
                    workspace, or a from-scratch project — and return patches for the parent to review.
                    Read-only subagents are always allowed, and this applies to delegated and API
                    subagents alike. With no explicit choice (Auto) the runtime mode decides:
                    Advanced and Pro allow every surface; Light allows only children that build
                    OUTSIDE the Ouroboros runtime (external workspaces and from-scratch projects)
                    and keeps worktree-of-this-repo children off. <strong>Human controlled:</strong>
                    the agent cannot self-enable it; applies on the next task, no restart.
                </div>
            </div>
        </div>
    `;
}

function currentView() {
    // The model select renders only while delegation is enabled, and its
    // handler re-asserts enabled=true (same as the harness handler), so a
    // non-null model edit never rides a null enabled.
    return delegationView({
        saved: state.saved,
        payload: state.payload,
        statusError: state.statusError,
        edit: state.enabled === null
            ? null
            : { enabled: state.enabled, harness: state.harness, model: state.model },
        loaded: state.loaded,
    });
}

function renderRows() {
    const host = document.getElementById('subagents-rows');
    if (!host) return;
    const view = currentView();
    const offerControls = view.state !== 'no_subscription' && view.state !== 'unknown' && view.state !== 'loading';
    const options = view.options.map((item) => {
        const selected = item.id === view.harness ? ' selected' : '';
        return `<option value="${escapeHtml(item.id)}"${selected}>${escapeHtml(item.label)}</option>`;
    }).join('');
    const modelOptions = view.modelOptions.map((opt) => {
        const selected = opt.value === view.model ? ' selected' : '';
        return `<option value="${escapeHtml(opt.value)}"${selected}>${escapeHtml(opt.label)}</option>`;
    }).join('');
    const lastLine = lastDelegationLine(state.payload?.subagent_last_delegation);
    host.innerHTML = `
        <div class="reviewer-slot-row" data-subagent-row>
            ${offerControls ? `
            <div class="reviewer-slot-controls">
                <select data-subagent-delegation aria-label="Delegate subagents">
                    <option value="off"${view.enabled ? '' : ' selected'}>Subagents run on the API</option>
                    <option value="on"${view.enabled ? ' selected' : ''}>Delegate to a coding agent</option>
                </select>
                ${view.enabled ? `<select data-subagent-harness aria-label="Coding agent">${options}</select>` : ''}
                ${view.enabled ? `<select data-subagent-model aria-label="Coding agent model">${modelOptions}</select>` : ''}
            </div>` : ''}
            <div class="reviewer-slot-meta muted">${escapeHtml(view.note)}</div>
            ${lastLine ? `<div class="reviewer-slot-meta muted">${escapeHtml(lastLine)}</div>` : ''}
        </div>
    `;
    bindRowEvents();
}

function bindRowEvents() {
    const host = document.getElementById('subagents-rows');
    if (!host) return;
    host.querySelector('[data-subagent-delegation]')?.addEventListener('change', (event) => {
        // Only the ANSWER is stored; which route that resolves to (the saved one,
        // or the first connected subscription) stays delegationView's decision.
        state.enabled = event.target.value === 'on';
        renderRows();
        state.onChange();
    });
    host.querySelector('[data-subagent-harness]')?.addEventListener('change', (event) => {
        state.enabled = true;
        state.harness = String(event.target.value || '');
        // The model belongs to the harness it was picked for: switching resets
        // the unsaved pick, and delegationView drops the saved tail, so the
        // select visibly shows "Engine default model" (accepted residual).
        state.model = null;
        renderRows();
        state.onChange();
    });
    host.querySelector('[data-subagent-model]')?.addEventListener('change', (event) => {
        state.enabled = true;
        state.model = String(event.target.value || '');
        renderRows();
        state.onChange();
    });
}

export function applySubagentsSettings(settings) {
    state.saved = String(settings?.OUROBOROS_SUBAGENT_HARNESS ?? '').trim();
    state.enabled = null;
    state.harness = '';
    state.model = null;
    renderRows();
}

export async function reloadSubagentsSection() {
    try {
        // ?include=models: the same status payload, plus per-harness model
        // discovery for the default-model select (claudexor_accounts).
        const resp = await apiFetch('/api/claudexor/status?include=models', { cache: 'no-store' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        state.payload = data;
        state.statusError = '';
    } catch (error) {
        state.payload = null;
        state.statusError = String(error.message || error);
    }
    state.loaded = true;
    renderRows();
}

export function initSubagentsSection({ onChange } = {}) {
    state.onChange = typeof onChange === 'function' ? onChange : () => {};
    // The initial load is driven by settings.js loadSettings(), which awaits
    // reloadSubagentsSection() BEFORE taking the clean-draft baseline — otherwise
    // the async arrival of the accounts would read as an unsaved edit.
}

export function collectSubagentsSettings() {
    // Never author the route from an UNLOADED or unreadable view: an unrelated
    // save must not turn delegation off because this page could not reach the
    // daemon (same rule as collectReviewerSlots).
    if (!state.loaded || state.statusError) return {};
    const view = currentView();
    if (view.state === 'no_subscription') return {};
    return { OUROBOROS_SUBAGENT_HARNESS: composeSubagentRoute(view.harness, view.suffix) };
}
