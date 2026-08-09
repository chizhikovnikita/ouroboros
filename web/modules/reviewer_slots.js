// Reviewer slots UI (phase 6.2/6.3, revised per owner finding #6) — the
// Models-page rows over the ONE structured setting (OUROBOROS_REVIEWER_SLOTS).
//
// Shape rules are the owner's:
//  * The ROUTE select carries routes only: "API model" plus one entry per
//    login-capable harness — never the full flat model catalog (hundreds of
//    options made the list unusable, finding #6).
//  * On the API route the model id is a FREE-TEXT input with a datalist of
//    catalog suggestions — the same catalog-assisted entry the model cards
//    use. On a harness route the MODEL is a dropdown fed by Claudexor
//    discovery. No invalid combinations can be composed.
//  * The provider shown for a delegated row is the HARNESS NAME (codex,
//    claude, cursor, …) — never "Claudexor", and never a `provider::model`
//    string syntax.
//  * Effort is the EXISTING per-model effort mechanism, one dropdown per row.
// Capability badges DISPLAY facts; they configure nothing.
//
// Pure helpers live at the top and are node-tested without a DOM.

import { apiFetch } from './api_client.js';
import { formatRelativeAge } from './ui_helpers.js';
import { escapeHtmlAttr as escapeHtml } from './utils.js';

export const ROUTE_KIND_API = 'api_chat';
export const ROUTE_KIND_SESSION = 'agent_session';
// The route select's single API entry. The target model id never lives in
// the select — display always matches the stored target (finding #6c: a
// fresh row used to DISPLAY the first catalog model while storing '').
export const API_ROUTE_CHOICE = 'api';

export const EFFORT_CHOICES = ['none', 'low', 'medium', 'high', 'xhigh', 'max'];

// ---------------------------------------------------------------------------
// Pure helpers.
// ---------------------------------------------------------------------------

export function mintSlotId(prefix, takenIds) {
    const taken = new Set(takenIds || []);
    for (let attempt = 0; attempt < 1000; attempt += 1) {
        const candidate = `${prefix}_${Math.random().toString(36).slice(2, 8)}`;
        if (!taken.has(candidate)) return candidate;
    }
    return `${prefix}_${Date.now().toString(36)}`;
}

export function encodeRouteChoice(row) {
    const kind = row?.route?.kind || ROUTE_KIND_API;
    const target = String(row?.route?.target_id || '');
    if (kind === ROUTE_KIND_SESSION) {
        return `session:${splitSessionTarget(target).harness}`;
    }
    return API_ROUTE_CHOICE;
}

export function decodeRouteChoice(value) {
    const raw = String(value || '');
    if (raw.startsWith('session:')) {
        return { kind: ROUTE_KIND_SESSION, harness: raw.slice('session:'.length) };
    }
    // Every non-session choice is the API route; the target comes from the
    // free-text input beside the select, never from the choice value.
    return { kind: ROUTE_KIND_API };
}

// Claudexor's own reviewer-panel spelling: harness[=model]. Never '::'.
export function composeSessionTarget(harness, model) {
    const h = String(harness || '').trim();
    const m = String(model || '').trim();
    return m ? `${h}=${m}` : h;
}

export function splitSessionTarget(target) {
    const raw = String(target || '');
    const eq = raw.indexOf('=');
    if (eq < 0) return { harness: raw, model: '' };
    return { harness: raw.slice(0, eq), model: raw.slice(eq + 1) };
}

export function routeChoiceGroups({ harnesses = [], currentChoice = '' } = {}) {
    const sessionValues = (harnesses || [])
        .filter((h) => h && h.id)
        .map((h) => ({
            // Provider = the harness name itself; the engine underneath is an
            // implementation detail the row never spells.
            value: `session:${h.id}`,
            label: `${h.display_name || h.id} (coding agent)`,
            disabled: h.status && h.status !== 'ok' && !h.enabled,
        }));
    // A SAVED session route whose harness discovery no longer lists (daemon
    // down, signed out) must keep an option, or the browser silently redraws
    // the row as the first choice — same rule as profileOptionsFor.
    const savedChoice = String(currentChoice || '');
    if (savedChoice.startsWith('session:') && !sessionValues.some((o) => o.value === savedChoice)) {
        sessionValues.push({
            value: savedChoice,
            label: `${savedChoice.slice('session:'.length)} (not in discovery)`,
        });
    }
    const groups = [{ label: 'API', options: [{ value: API_ROUTE_CHOICE, label: 'API model' }] }];
    // With no daemon the group used to VANISH, while the section copy above still
    // promised subscription delivery — the owner saw a broken promise and nowhere
    // to go. Say why it is empty instead of hiding it. Name the DESTINATION by
    // its tab: Harness Accounts lives in the Providers panel, this row lives in
    // Models, so "below" pointed at a page that does not contain it.
    groups.push(sessionValues.length
        ? { label: 'Coding agents — subscriptions', options: sessionValues }
        : { label: 'Coding agents — subscriptions', options: [{
            value: '', disabled: true,
            label: 'None available — sign in under Providers → Harness Accounts',
        }] });
    return groups;
}

export function indexProfilesByHarness(payload) {
    // The REAL ControlCredentialProfilesResponse shape, same reader as
    // harness_accounts.accountRows: `profiles` is an array of WRAPPERS
    // `{profile, status, identity}` carrying snake_case fields. Reading flat
    // camelCase off the wrapper matched nothing, so every session row's
    // credential-profile picker was silently empty. Exported so the wire test can
    // hold it against the same golden body the account list consumes.
    const byHarness = {};
    const profiles = payload?.profiles?.profiles || [];
    for (const wrapper of Array.isArray(profiles) ? profiles : []) {
        const profile = wrapper?.profile || {};
        const harness = String(profile.harness_id || '');
        const id = String(profile.profile_id || '');
        if (!harness || !id) continue;
        (byHarness[harness] = byHarness[harness] || []).push(id);
    }
    return byHarness;
}

export function buildReviewerSlotsSetting(state) {
    const rowOut = (row) => {
        const out = {
            slot_id: String(row.slot_id || ''),
            route: { kind: row.route.kind, target_id: String(row.route.target_id || '') },
        };
        if (row.route.kind === ROUTE_KIND_SESSION && row.route.profile_id) {
            out.route.profile_id = String(row.route.profile_id);
        }
        if (row.effort) out.effort = String(row.effort);
        return out;
    };
    const advisory = state.advisory || {};
    const advisoryOut = {
        enabled: advisory.enabled !== false,
        route: { kind: advisory.route?.kind === ROUTE_KIND_SESSION ? ROUTE_KIND_SESSION : 'api',
                 target_id: String(advisory.route?.target_id || '') },
        effort: advisory.effort || 'low',
    };
    if (advisoryOut.route.kind === ROUTE_KIND_SESSION && advisory.route?.profile_id) {
        advisoryOut.route.profile_id = String(advisory.route.profile_id);
    }
    return JSON.stringify({
        triad: (state.triad || []).map(rowOut),
        scope: (state.scope || []).map(rowOut),
        advisory: advisoryOut,
    });
}

export function describeLastExecution(entry) {
    if (!entry || typeof entry !== 'object') return '';
    const effective = entry.effective || {};
    const parts = [];
    // APPLIED facts only: a session run whose telemetry predates the engine
    // receipt shows honest absence, never the requested value as applied.
    // One quiet line (owner feedback): the route is spelled only when it says
    // more than the row's own delivery badge (which coding agent really ran),
    // and the timestamp is humanized — the raw route + ISO instant stay
    // recoverable in the line's tooltip (lastRunMetaTitle).
    const route = String(effective.route || '');
    if (route.startsWith('agent_session')) {
        const harness = route.slice('agent_session'.length).replace(/^:/, '');
        parts.push(harness ? `${harness} session` : 'agent session');
        parts.push(effective.model || 'model not disclosed');
    } else if (effective.model) {
        parts.push(effective.model);
    }
    if (effective.profile_id) parts.push(`account ${effective.profile_id}`);
    if (effective.access) parts.push(`access ${effective.access}`);
    // No applied effort is rendered: none exists upstream, so the key is not emitted.
    if (effective.verdict_method && effective.verdict_method !== 'structured'
        && effective.verdict_method !== 'strict_parse') {
        parts.push(`verdict via ${effective.verdict_method.replace(/_/g, ' ')}`);
    }
    const deltas = Array.isArray(entry.capability_delta) ? entry.capability_delta.length : 0;
    if (deltas) parts.push(`${deltas} capability delta${deltas === 1 ? '' : 's'} disclosed`);
    const when = formatRelativeAge(Date.parse(entry.ts || ''), 'just now');
    if (when) parts.push(when);
    return parts.join(' · ');
}

function lastRunMetaTitle(entry) {
    // The tooltip keeps the raw facts the visible line compresses away.
    const route = String(entry?.effective?.route || 'api_chat');
    const ts = String(entry?.ts || '');
    return `UI projection of capability_delta (D22) — ran as ${route}${ts ? ` at ${ts}` : ''}`;
}

export function sessionModelOptions(harness, currentModel) {
    // The ONE model-options fragment for a session row's model select (triad,
    // scope, advisory, and the Subagents section import it too). "Engine
    // default model" is the empty tail; a SAVED model discovery no longer
    // lists keeps a "(not in discovery)" option, or the browser silently
    // redraws the select as the first entry and the next Save erases the pin
    // (same rule as profileOptionsFor / routeChoiceGroups).
    const models = harness?.models || [];
    const options = [{ value: '', label: 'Engine default model' },
        ...models.map((m) => ({ value: String(m.id || m.value || m), label: String(m.id || m.label || m) }))];
    if (currentModel && !options.some((o) => o.value === currentModel)) {
        options.push({ value: currentModel, label: `${currentModel} (not in discovery)` });
    }
    return options;
}

export function profileOptionsFor(profiles, savedPin) {
    // Mirrors the model list's own rule: a SAVED pin the daemon no longer discovers
    // (account signed out, daemon down, profile renamed) matched no option, so the
    // select fell back to its first entry and redrew the row as "automatic rotation".
    // The pin only LOOKED gone — until the owner saved the panel, which then really
    // did delete it, silently widening which account the reviewer may spend.
    const options = [{ value: '', label: 'Account: automatic rotation' },
        ...(profiles || []).map((p) => ({ value: p, label: `Account: ${p} (pinned)` }))];
    if (savedPin && !options.some((o) => o.value === savedPin)) {
        options.push({ value: savedPin, label: `Account: ${savedPin} (not in discovery)` });
    }
    return options;
}

export function capabilityBadge(row, harnessesById) {
    // DISPLAY-only facts: never a control (6.2).
    if (row.route.kind === ROUTE_KIND_SESSION) {
        const harness = harnessesById?.[splitSessionTarget(row.route.target_id).harness];
        const status = harness ? (harness.status || 'unknown') : 'not discovered';
        return `agent session — retrieves context with its own tools · route ${status}`;
    }
    return 'API delivery';
}

export function advisoryRouteTransition(prev, decoded, memory = {}) {
    // Route-kind switching must not WIPE a stored target (finding #7c): the
    // old handler wrote target_id:'' on every change, so flipping to a
    // session and back — or just touching the select — silently discarded the
    // saved advisory target on the next Save. Each kind remembers the last
    // route it held; a kind the user returns to is restored, and the stored
    // route only changes when the user actually picks something else.
    const current = (prev && typeof prev === 'object' && prev.kind)
        ? prev : { kind: 'api', target_id: '' };
    const next = { ...memory };
    if (decoded.kind === ROUTE_KIND_SESSION) {
        const prevHarness = current.kind === ROUTE_KIND_SESSION
            ? splitSessionTarget(current.target_id).harness : '';
        if (prevHarness === decoded.harness) return { route: current, memory: next };
        if (current.kind === ROUTE_KIND_SESSION) next.session = { ...current };
        else next.api = { ...current };
        const stash = next.session;
        const route = (stash && splitSessionTarget(stash.target_id).harness === decoded.harness)
            ? { ...stash }
            : { kind: ROUTE_KIND_SESSION, target_id: decoded.harness };
        return { route, memory: next };
    }
    if (current.kind !== ROUTE_KIND_SESSION) return { route: current, memory: next };
    next.session = { ...current };
    return {
        route: next.api ? { ...next.api } : { kind: 'api', target_id: '' },
        memory: next,
    };
}

// ---------------------------------------------------------------------------
// DOM section (Models page). State is module-local; collect is synchronous.
// ---------------------------------------------------------------------------

const state = {
    loaded: false,
    configError: '',
    loadError: '',
    source: '',
    triad: [],
    scope: [],
    advisory: { enabled: true, route: { kind: 'api', target_id: '' }, effort: 'low' },
    limits: { triad: 10, scope: 4, advisory: 1 },
    lastExecutions: {},
    catalogModels: [],
    harnesses: [],
    profilesByHarness: {},
    onChange: () => {},
};

// Per-kind memory for the advisory route select (see advisoryRouteTransition):
// seeded from the LOADED setting so a kind round-trip restores the saved target.
const advisoryRouteMemory = { api: null, session: null };

export function renderReviewerSlotsSection() {
    return `
        <div class="form-section" id="reviewer-slots-section">
            <h3>Reviewer Slots</h3>
            <div class="settings-section-copy">
                Who reviews each commit: the triad rows, the scope rows, and one optional advisory
                pre-reviewer. Each row picks its delivery — an API model, or a coding agent on your
                subscription — plus its own reasoning effort.
            </div>
            <div class="settings-inline-note">
                Coding-agent delivery covers <strong>commit review only</strong>. If every triad or
                scope row is delegated, plan review, task acceptance and skill review fall back to
                the shipped default models and spend API budget — keep at least one API row.
            </div>
            <div id="reviewer-slots-error" class="ui-status" data-tone="error" hidden></div>
            <datalist id="reviewer-api-model-catalog"></datalist>
            <h4 class="reviewer-slots-heading">Triad slots <span class="muted" id="reviewer-triad-limit" title="The commit gate's real ceiling"></span></h4>
            <div id="reviewer-triad-rows" class="reviewer-slot-rows"></div>
            <div class="settings-toolbar">
                <button type="button" class="settings-ghost-btn" id="btn-add-triad-slot">Add triad slot</button>
            </div>
            <h4 class="reviewer-slots-heading">Scope slots <span class="muted" id="reviewer-scope-limit" title="The scope pool's real width"></span></h4>
            <div id="reviewer-scope-rows" class="reviewer-slot-rows"></div>
            <div class="settings-toolbar">
                <button type="button" class="settings-ghost-btn" id="btn-add-scope-slot">Add scope slot</button>
            </div>
            <h4 class="reviewer-slots-heading">Advisory pre-reviewer</h4>
            <div id="reviewer-advisory-row" class="reviewer-slot-rows"></div>
            <div class="settings-inline-note">
                Disabling the advisory is a standing decision with a constitutional consequence:
                every reviewed commit then records an <strong>audited bypass</strong> instead of an
                advisory verdict. Nothing is skipped silently.
            </div>
        </div>
    `;
}

function harnessesById() {
    const map = {};
    for (const h of state.harnesses) map[h.id] = h;
    return map;
}

function selectHtml(attrs, groups, selected) {
    const options = groups.map((group) => {
        const body = group.options.map((opt) => {
            const sel = opt.value === selected ? ' selected' : '';
            const dis = opt.disabled ? ' disabled' : '';
            return `<option value="${escapeHtml(opt.value)}"${sel}${dis}>${escapeHtml(opt.label)}</option>`;
        }).join('');
        return group.label ? `<optgroup label="${escapeHtml(group.label)}">${body}</optgroup>` : body;
    }).join('');
    return `<select ${attrs}>${options}</select>`;
}

function effortSelectHtml(attrs, selected, surfaceDefault) {
    // Compact closed state (owner feedback on field proportions): the wordy
    // "Default (scope review effort)" label made this select as wide as the
    // model field. Which setting the default follows moves to the tooltip.
    const options = [
        { value: '', label: 'Default effort' },
        ...EFFORT_CHOICES.map((effort) => ({ value: effort, label: effort })),
    ];
    return selectHtml(`${attrs} title="Reasoning effort — default: ${escapeHtml(surfaceDefault)}"`,
        [{ label: '', options }], selected || '');
}

function rowHtml(row, group) {
    const choice = encodeRouteChoice(row);
    const groups = routeChoiceGroups({ harnesses: state.harnesses, currentChoice: choice });
    const session = row.route.kind === ROUTE_KIND_SESSION;
    const split = session ? splitSessionTarget(row.route.target_id) : { harness: '', model: '' };
    const harness = session ? harnessesById()[split.harness] : null;
    const modelOptions = sessionModelOptions(harness, split.model);
    const profiles = session ? (state.profilesByHarness[split.harness] || []) : [];
    const profileOptions = profileOptionsFor(profiles, row.route.profile_id);
    const last = state.lastExecutions[row.slot_id];
    const lastText = last ? describeLastExecution(last) : '';
    // ONE quiet meta line per row (owner feedback): the delivery badge and the
    // last-run projection share it; nothing is dropped — the raw route + ISO
    // timestamp live in the tooltip.
    const metaParts = [capabilityBadge(row, harnessesById())];
    if (lastText) metaParts.push(`Last run: ${lastText}`);
    const surfaceDefault = group === 'scope' ? 'scope review effort' : 'review effort';
    return `
        <div class="reviewer-slot-row" data-slot-group="${group}" data-slot-id="${escapeHtml(row.slot_id)}">
            <div class="reviewer-slot-controls">
                ${selectHtml(`data-slot-route aria-label="Reviewer route"`, groups, choice)}
                ${session ? '' : `<input data-slot-custom-api list="reviewer-api-model-catalog" placeholder="provider/model-id" value="${escapeHtml(row.route.target_id || '')}" spellcheck="false" aria-label="API model id">`}
                ${session ? selectHtml('data-slot-model aria-label="Harness model"', [{ label: '', options: modelOptions }], split.model) : ''}
                ${session && profileOptions.length > 1 ? selectHtml('data-slot-profile aria-label="Credential account"', [{ label: '', options: profileOptions }], row.route.profile_id || '') : ''}
                ${effortSelectHtml('data-slot-effort aria-label="Reasoning effort"', row.effort, surfaceDefault)}
                <button type="button" class="settings-ghost-btn" data-slot-remove title="Remove this slot">Remove</button>
            </div>
            <div class="reviewer-slot-meta muted"${last ? ` title="${escapeHtml(lastRunMetaTitle(last))}"` : ''}>${escapeHtml(metaParts.join(' · '))}</div>
        </div>
    `;
}

function advisoryHtml() {
    const advisory = state.advisory;
    // TRAP: the advisory state carries kind='api' while ROUTE_KIND_API is
    // 'api_chat', so every branch here tests `!== ROUTE_KIND_SESSION` — a
    // naive `=== ROUTE_KIND_API` silently never matches.
    const session = advisory.route?.kind === ROUTE_KIND_SESSION;
    const split = session ? splitSessionTarget(advisory.route.target_id)
        : { harness: '', model: '' };
    const choice = session ? `session:${split.harness}` : API_ROUTE_CHOICE;
    // Both optgroups are labeled (finding #7): an unlabeled first group made
    // the API entry read as stray next to the labeled subscriptions group.
    const groups = [
        { label: 'API', options: [{ value: API_ROUTE_CHOICE, label: 'Claude (Anthropic API key)' }] },
        ...routeChoiceGroups({ harnesses: state.harnesses, currentChoice: choice }).slice(1),
    ];
    // Session branch: the SAME model-options fragment the triad rows use —
    // rewriting it here would lose the "(not in discovery)" guard and let a
    // Save with the daemon down erase the owner's model. Api branch: free-text
    // WITHOUT the catalog datalist — the advisory api route is the Claude
    // Agent SDK, whose model spellings (sonnet, opus[1m], claude-…) are not
    // the OpenRouter catalog ids the datalist suggests.
    const modelOptions = session ? sessionModelOptions(harnessesById()[split.harness], split.model) : [];
    const profiles = session ? (state.profilesByHarness[split.harness] || []) : [];
    const profileOptions = session ? profileOptionsFor(profiles, advisory.route?.profile_id) : [];
    const last = state.lastExecutions.advisory_slot_1;
    const lastText = last ? describeLastExecution(last) : '';
    const metaParts = [capabilityBadge({ route: advisory.route || {} }, harnessesById())];
    if (lastText) metaParts.push(`Last run: ${lastText}`);
    return `
        <div class="reviewer-slot-row" data-advisory-row>
            <div class="reviewer-slot-controls">
                <label class="local-toggle"><input type="checkbox" data-advisory-enabled ${advisory.enabled !== false ? 'checked' : ''}> Enabled</label>
                ${selectHtml('data-advisory-route aria-label="Advisory route"', groups, choice)}
                ${session
                    ? selectHtml('data-advisory-model aria-label="Advisory harness model"', [{ label: '', options: modelOptions }], split.model)
                    : `<input data-advisory-api-model placeholder="Claude model — empty = default" value="${escapeHtml(advisory.route?.target_id || '')}" spellcheck="false" aria-label="Advisory Claude model">`}
                ${session && profileOptions.length > 1 ? selectHtml('data-advisory-profile aria-label="Advisory credential account"', [{ label: '', options: profileOptions }], advisory.route?.profile_id || '') : ''}
                ${effortSelectHtml('data-advisory-effort aria-label="Advisory effort"', advisory.effort === 'low' ? '' : advisory.effort, 'low')}
            </div>
            <div class="reviewer-slot-meta muted"${last ? ` title="${escapeHtml(lastRunMetaTitle(last))}"` : ''}>${escapeHtml(metaParts.join(' · '))}</div>
        </div>
    `;
}

function renderRows() {
    const errorBox = document.getElementById('reviewer-slots-error');
    if (errorBox) {
        errorBox.hidden = !(state.configError || state.loadError);
        errorBox.textContent = state.configError
            ? `Saved reviewer-slot configuration is invalid and blocks reviews: ${state.configError}. `
              + 'To repair it, add at least one triad slot and one scope slot below, then Save'
              + (state.triad.length && state.scope.length ? '.' : ' — Save will report the missing rows.')
            : (state.loadError
                ? `Could not reach the reviewer-slot settings — ${state.loadError}. Your saved configuration is unchanged; retry when the connection is back.`
                : '');
    }
    const triadBox = document.getElementById('reviewer-triad-rows');
    const scopeBox = document.getElementById('reviewer-scope-rows');
    const advisoryBox = document.getElementById('reviewer-advisory-row');
    if (!triadBox || !scopeBox || !advisoryBox) return;
    triadBox.innerHTML = state.triad.map((row) => rowHtml(row, 'triad')).join('')
        || '<div class="muted">No triad slots configured.</div>';
    scopeBox.innerHTML = state.scope.map((row) => rowHtml(row, 'scope')).join('')
        || '<div class="muted">No scope slots configured.</div>';
    advisoryBox.innerHTML = advisoryHtml();
    const datalist = document.getElementById('reviewer-api-model-catalog');
    if (datalist) {
        datalist.innerHTML = state.catalogModels
            .map((id) => `<option value="${escapeHtml(id)}"></option>`).join('');
    }
    // The count stays in the heading; what the limit MEANS moved to the span's
    // title (owner feedback: headers carried parenthetical jargon).
    const triadLimit = document.getElementById('reviewer-triad-limit');
    if (triadLimit) triadLimit.textContent = `${state.triad.length}/${state.limits.triad}`;
    const scopeLimit = document.getElementById('reviewer-scope-limit');
    if (scopeLimit) scopeLimit.textContent = `${state.scope.length}/${state.limits.scope}`;
    const addTriad = document.getElementById('btn-add-triad-slot');
    if (addTriad) addTriad.disabled = state.triad.length >= state.limits.triad;
    const addScope = document.getElementById('btn-add-scope-slot');
    if (addScope) addScope.disabled = state.scope.length >= state.limits.scope;
    bindRowEvents();
}

function findRow(group, slotId) {
    const rows = group === 'scope' ? state.scope : state.triad;
    return rows.find((row) => row.slot_id === slotId) || null;
}

function bindRowEvents() {
    const section = document.getElementById('reviewer-slots-section');
    if (!section) return;
    section.querySelectorAll('.reviewer-slot-row[data-slot-id]').forEach((rowEl) => {
        const group = rowEl.dataset.slotGroup;
        const row = findRow(group, rowEl.dataset.slotId);
        if (!row) return;
        rowEl.querySelector('[data-slot-route]')?.addEventListener('change', (event) => {
            const decoded = decodeRouteChoice(event.target.value);
            if (decoded.kind === ROUTE_KIND_SESSION) {
                const prevHarness = row.route.kind === ROUTE_KIND_SESSION
                    ? splitSessionTarget(row.route.target_id).harness : '';
                if (prevHarness !== decoded.harness) {
                    row.route = { kind: ROUTE_KIND_SESSION, target_id: decoded.harness, profile_id: '' };
                }
            } else if (row.route.kind !== ROUTE_KIND_API) {
                // A session spec is not an API model id: the input starts
                // empty (placeholder visible), display matching state.
                row.route = { kind: ROUTE_KIND_API, target_id: '' };
            }
            renderRows();
            state.onChange();
        });
        rowEl.querySelector('[data-slot-custom-api]')?.addEventListener('input', (event) => {
            row.route.target_id = String(event.target.value || '').trim();
            state.onChange();
        });
        rowEl.querySelector('[data-slot-model]')?.addEventListener('change', (event) => {
            const split = splitSessionTarget(row.route.target_id);
            row.route.target_id = composeSessionTarget(split.harness, event.target.value);
            state.onChange();
        });
        rowEl.querySelector('[data-slot-profile]')?.addEventListener('change', (event) => {
            row.route.profile_id = String(event.target.value || '');
            state.onChange();
        });
        rowEl.querySelector('[data-slot-effort]')?.addEventListener('change', (event) => {
            row.effort = String(event.target.value || '');
            state.onChange();
        });
        rowEl.querySelector('[data-slot-remove]')?.addEventListener('click', () => {
            const rows = group === 'scope' ? state.scope : state.triad;
            const index = rows.indexOf(row);
            if (index >= 0) rows.splice(index, 1);
            renderRows();
            state.onChange();
        });
    });
    const advisoryEl = section.querySelector('[data-advisory-row]');
    if (advisoryEl) {
        advisoryEl.querySelector('[data-advisory-enabled]')?.addEventListener('change', (event) => {
            state.advisory.enabled = Boolean(event.target.checked);
            state.onChange();
        });
        advisoryEl.querySelector('[data-advisory-route]')?.addEventListener('change', (event) => {
            const result = advisoryRouteTransition(
                state.advisory.route, decodeRouteChoice(event.target.value), advisoryRouteMemory);
            state.advisory.route = result.route;
            Object.assign(advisoryRouteMemory, result.memory);
            renderRows();
            state.onChange();
        });
        // Mirrors of the triad-row model/api/profile handlers. These controls
        // exist only on their own kind's branch of advisoryHtml, so each
        // querySelector binds at most one of them per render.
        advisoryEl.querySelector('[data-advisory-model]')?.addEventListener('change', (event) => {
            const split = splitSessionTarget(state.advisory.route?.target_id);
            state.advisory.route.target_id = composeSessionTarget(split.harness, event.target.value);
            state.onChange();
        });
        advisoryEl.querySelector('[data-advisory-api-model]')?.addEventListener('input', (event) => {
            state.advisory.route.target_id = String(event.target.value || '').trim();
            state.onChange();
        });
        advisoryEl.querySelector('[data-advisory-profile]')?.addEventListener('change', (event) => {
            state.advisory.route.profile_id = String(event.target.value || '');
            state.onChange();
        });
        advisoryEl.querySelector('[data-advisory-effort]')?.addEventListener('change', (event) => {
            state.advisory.effort = String(event.target.value || '') || 'low';
            state.onChange();
        });
    }
}

function addRow(group) {
    const rows = group === 'scope' ? state.scope : state.triad;
    const limit = group === 'scope' ? state.limits.scope : state.limits.triad;
    if (rows.length >= limit) return;
    const taken = [...state.triad, ...state.scope].map((row) => row.slot_id);
    rows.push({
        slot_id: mintSlotId(group === 'scope' ? 'scope' : 'triad', taken),
        route: { kind: ROUTE_KIND_API, target_id: '' },
        effort: '',
    });
    renderRows();
    state.onChange();
}

export async function reloadReviewerSlots() {
    try {
        const resp = await apiFetch('/api/reviewer-slots', { cache: 'no-store' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        state.loadError = '';
        state.configError = String(data.config_error || '');
        state.source = String(data.source || '');
        state.limits = data.limits || state.limits;
        state.lastExecutions = data.last_executions || {};
        state.triad = Array.isArray(data.triad) ? data.triad.map((row) => ({ ...row })) : [];
        state.scope = Array.isArray(data.scope) ? data.scope.map((row) => ({ ...row })) : [];
        state.advisory = data.advisory
            ? { ...data.advisory, route: { ...(data.advisory.route || {}) } }
            : state.advisory;
        // Seed the per-kind route memory with the SAVED route, so switching
        // the advisory select away and back restores it (finding #7c).
        advisoryRouteMemory[state.advisory.route?.kind === ROUTE_KIND_SESSION ? 'session' : 'api']
            = { ...(state.advisory.route || {}) };
        // The VIEW loaded even when the saved value is invalid — that is exactly the
        // state the owner repairs from, and treating it as "not loaded" made the
        // save drop the repair (see collectReviewerSlots).
        state.loaded = true;
    } catch (error) {
        // A transport failure is NOT a verdict on the saved configuration: the
        // config-error banner accuses the owner's settings of blocking review, and
        // a network blip must never say that. Separate field, separate sentence.
        state.loaded = false;
        state.loadError = `could not load reviewer slots: ${error.message || error}`;
    }
    try {
        const resp = await apiFetch('/api/claudexor/status?include=models', { cache: 'no-store' });
        const data = await resp.json().catch(() => ({}));
        state.harnesses = Array.isArray(data.harnesses) ? data.harnesses : [];
        state.profilesByHarness = indexProfilesByHarness(data);
    } catch (error) {
        state.harnesses = [];
        state.profilesByHarness = {};
    }
    renderRows();
}

export function initReviewerSlots({ onChange } = {}) {
    state.onChange = typeof onChange === 'function' ? onChange : () => {};
    document.getElementById('btn-add-triad-slot')?.addEventListener('click', () => addRow('triad'));
    document.getElementById('btn-add-scope-slot')?.addEventListener('click', () => addRow('scope'));
    document.addEventListener('settings-model-catalog:updated', (event) => {
        const items = event?.detail?.items || [];
        state.catalogModels = items.map((item) => String(item.value || item.id || '')).filter(Boolean);
        renderRows();
    });
    // The initial load is driven by settings.js loadSettings(), which awaits
    // reloadReviewerSlots() BEFORE taking the clean-draft baseline — otherwise
    // the async arrival of the rows would read as an unsaved edit.
}

// #126, pure and node-tested: what the settings save sends for reviewer slots.
// Never author the setting from an UNLOADED view (an unrelated save must not
// overwrite the owner's configuration with an empty page), and a transport
// failure is not a verdict on the saved value either. But a LOADED view always
// sends what it shows — including an empty triad/scope. The old empty-set
// guard silently dropped the key, so deleting every row Saved "successfully"
// while saving nothing; now the backend's own 400 («triad needs at least one
// slot») surfaces through the existing failed-save status. Validation SSOT
// stays on the backend — no client-side duplicate.
export function reviewerSlotsSavePayload({ loaded = false, loadError = '', triad = [], scope = [], advisory } = {}) {
    if (loadError || !loaded) return {};
    return { OUROBOROS_REVIEWER_SLOTS: buildReviewerSlotsSetting({ triad, scope, advisory }) };
}

export function collectReviewerSlots() {
    // A config_error is NOT the unloaded case — the stored value is already
    // invalid and blocking review, the endpoint returns no rows with it, and
    // refusing here made the documented repair path swallow the owner's
    // replacement rows and still report success.
    return reviewerSlotsSavePayload(state);
}
