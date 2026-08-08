/** Connections page: the pool of API keys, subscription accounts and endpoints.
 *
 * Built for HUNDREDS of rows, so it is a filterable table rather than a card
 * wall: search, provider/privacy filters and a bulk paste-import are the primary
 * affordances. A credential value is never rendered — the server only reports
 * whether one is configured.
 */

import { apiFetch } from './api_client.js';
import { renderPageHeader } from './page_header.js';
import { PAGE_ICONS } from './page_icons.js';
import { showToast } from './toast.js';
import { escapeHtmlAttr as escapeHtml } from './utils.js';
import { applyStaticTranslations, t } from './i18n.js';

const PRIVACY_TONE = {
    no_training: 'ok',
    trains_on_data: 'warn',
    unknown: 'muted',
};

let allRows = [];

function pageTemplate() {
    return `
        <section class="page app-page-glass" id="page-connections">
            ${renderPageHeader({
                title: 'Connections',
                titleKey: 'connections.title',
                icon: PAGE_ICONS.settings,
                description: 'API keys, subscription accounts and endpoints available to the agent.',
                descriptionKey: 'connections.description',
                actionsHtml: `
                    <button class="btn btn-secondary" type="button" id="connections-refresh" data-i18n="connections.refresh">Refresh</button>
                    <button class="btn btn-primary" type="button" id="connections-add" data-i18n="connections.add">Add connection</button>
                `,
            })}
            <div class="connections-toolbar">
                <input id="connections-search" type="search" class="connections-search"
                       placeholder="Search by id, provider or tag"
                       data-i18n-attr="placeholder:connections.search">
                <select id="connections-filter-provider"></select>
                <select id="connections-filter-privacy"></select>
            </div>
            <div id="connections-summary" class="connections-summary"></div>
            <div id="connections-list" class="connections-list"></div>
            <div id="connections-empty" class="connections-empty" hidden data-i18n="connections.empty">
                No connections yet. Existing provider keys from Settings appear here automatically.
            </div>
        </section>
    `;
}

function privacyLabel(value) {
    if (value === 'no_training') return t('connections.privacy.no_training', 'No training');
    if (value === 'trains_on_data') return t('connections.privacy.trains_on_data', 'Trains on data');
    return t('connections.privacy.unknown', 'Unknown');
}

/** Rating chip. An unmeasured connection says so — it never shows as 0%. */
function ratingHtml(rating) {
    if (!rating || rating.success_rate === null || rating.success_rate === undefined) {
        const attempts = rating ? rating.observations || 0 : 0;
        return `<span class="connections-chip muted">${escapeHtml(
            t('connections.rating_pending', 'not rated yet ({n})', { n: attempts }),
        )}</span>`;
    }
    const percent = Math.round(rating.success_rate * 100);
    const tone = percent >= 90 ? 'ok' : (percent >= 60 ? 'muted' : 'warn');
    return `<span class="connections-chip ${tone}">${escapeHtml(
        t('connections.rating', '{percent}% of {n}', { percent, n: rating.observations }),
    )}</span>`;
}

function rowHtml(row) {
    const credential = row.credential_configured
        ? `<span class="connections-chip ok" data-i18n="connections.credential_set">key set</span>`
        : `<span class="connections-chip warn" data-i18n="connections.credential_missing">no key</span>`;
    const legacy = row.editable
        ? ''
        : `<span class="connections-chip muted" data-i18n="connections.from_settings">from Settings</span>`;
    const tags = (row.tags || [])
        .map((tag) => `<span class="connections-tag">${escapeHtml(tag)}</span>`)
        .join('');
    return `
        <div class="connections-row ${row.enabled ? '' : 'is-disabled'}" data-connection-id="${escapeHtml(row.connection_id)}">
            <div class="connections-row-main">
                <div class="connections-row-title">
                    <strong>${escapeHtml(row.label || row.connection_id)}</strong>
                    <span class="connections-provider">${escapeHtml(row.provider)}</span>
                    <span class="connections-kind">${escapeHtml(row.kind)}</span>
                </div>
                <div class="connections-row-meta">
                    <span class="connections-chip ${PRIVACY_TONE[row.privacy] || 'muted'}">${escapeHtml(privacyLabel(row.privacy))}</span>
                    ${credential}
                    ${ratingHtml(row.rating)}
                    ${legacy}
                    ${tags}
                </div>
            </div>
            <div class="connections-row-actions">
                <button type="button" class="settings-ghost-btn" data-connection-toggle>${
                    row.enabled
                        ? escapeHtml(t('connections.disable', 'Disable'))
                        : escapeHtml(t('connections.enable', 'Enable'))
                }</button>
                ${row.editable
                    ? `<button type="button" class="settings-ghost-btn danger" data-connection-delete data-i18n="connections.delete">Delete</button>`
                    : ''}
            </div>
        </div>
    `;
}

function matchesFilters(row, { query, provider, privacy }) {
    if (provider && row.provider !== provider) return false;
    if (privacy && row.privacy !== privacy) return false;
    if (!query) return true;
    const haystack = [row.connection_id, row.label, row.provider, ...(row.tags || [])]
        .join(' ')
        .toLowerCase();
    return haystack.includes(query);
}

function fillSelect(select, values, allLabel) {
    if (!select) return;
    const previous = select.value;
    select.innerHTML = `<option value="">${escapeHtml(allLabel)}</option>`
        + values.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join('');
    select.value = values.includes(previous) ? previous : '';
}

function render() {
    const listEl = document.getElementById('connections-list');
    const emptyEl = document.getElementById('connections-empty');
    const summaryEl = document.getElementById('connections-summary');
    if (!listEl) return;

    const filters = {
        query: (document.getElementById('connections-search')?.value || '').trim().toLowerCase(),
        provider: document.getElementById('connections-filter-provider')?.value || '',
        privacy: document.getElementById('connections-filter-privacy')?.value || '',
    };
    const visible = allRows.filter((row) => matchesFilters(row, filters));

    listEl.innerHTML = visible.map(rowHtml).join('');
    applyStaticTranslations(listEl);
    if (emptyEl) emptyEl.hidden = allRows.length > 0;

    if (summaryEl) {
        const enabled = allRows.filter((row) => row.enabled).length;
        // The omitted count is explicit: a filtered view must never read as the
        // whole pool.
        summaryEl.textContent = t(
            'connections.summary',
            'Showing {shown} of {total} — {enabled} enabled',
            { shown: visible.length, total: allRows.length, enabled },
        );
    }
}

async function reload() {
    try {
        const response = await apiFetch('/api/connections', { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json();
        allRows = Array.isArray(payload.connections) ? payload.connections : [];
        fillSelect(
            document.getElementById('connections-filter-provider'),
            [...new Set(allRows.map((row) => row.provider))].sort(),
            t('connections.all_providers', 'All providers'),
        );
        fillSelect(
            document.getElementById('connections-filter-privacy'),
            Array.isArray(payload.privacy_values) ? payload.privacy_values : [],
            t('connections.all_privacy', 'Any privacy'),
        );
        render();
    } catch (err) {
        showToast(t('connections.load_failed', 'Could not load connections') + `: ${err.message || err}`, 'error');
    }
}

async function toggleRow(connectionId, enabled) {
    const response = await apiFetch(`/api/connections/${encodeURIComponent(connectionId)}/enabled`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
}

async function deleteRow(connectionId) {
    const response = await apiFetch(`/api/connections/${encodeURIComponent(connectionId)}`, {
        method: 'DELETE',
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
}

/** Add-connection dialog. Reuses the shared `.marketplace-modal` surface. */
function openAddDialog() {
    return new Promise((resolve) => {
        const backdrop = document.createElement('div');
        backdrop.className = 'marketplace-modal-backdrop';
        backdrop.innerHTML = `
            <div class="marketplace-modal" role="dialog" aria-modal="true" aria-labelledby="connections-add-title">
                <div class="marketplace-modal-head">
                    <h3 id="connections-add-title" data-i18n="connections.add">Add connection</h3>
                    <button type="button" class="btn btn-default btn-sm" data-conn-cancel data-i18n="connections.close">Close</button>
                </div>
                <div class="marketplace-modal-body">
                    <label class="new-project-field">
                        <span data-i18n="connections.field.id">Identifier</span>
                        <input class="files-modal-input" data-conn-id type="text" placeholder="openai-key-2" maxlength="64">
                    </label>
                    <label class="new-project-field">
                        <span data-i18n="connections.field.provider">Provider</span>
                        <input class="files-modal-input" data-conn-provider type="text" placeholder="openai">
                    </label>
                    <label class="new-project-field">
                        <span data-i18n="connections.field.label">Label</span>
                        <input class="files-modal-input" data-conn-label type="text" placeholder="OpenAI #2">
                    </label>
                    <label class="new-project-field">
                        <span data-i18n="connections.field.kind">Kind</span>
                        <select data-conn-kind>
                            <option value="api_key">api_key</option>
                            <option value="subscription">subscription</option>
                            <option value="endpoint">endpoint</option>
                        </select>
                    </label>
                    <label class="new-project-field">
                        <span data-i18n="connections.field.privacy">Data privacy</span>
                        <select data-conn-privacy>
                            <option value="unknown">${escapeHtml(t('connections.privacy.unknown', 'Unknown'))}</option>
                            <option value="no_training">${escapeHtml(t('connections.privacy.no_training', 'No training'))}</option>
                            <option value="trains_on_data">${escapeHtml(t('connections.privacy.trains_on_data', 'Trains on data'))}</option>
                        </select>
                    </label>
                    <label class="new-project-field">
                        <span data-i18n="connections.field.base_url">Base URL</span>
                        <input class="files-modal-input" data-conn-base type="text" placeholder="https://api.example.com/v1">
                    </label>
                    <label class="new-project-field">
                        <span data-i18n="connections.field.secret">Credential</span>
                        <input class="files-modal-input" data-conn-secret type="password" placeholder="sk-...">
                    </label>
                    <p class="new-project-error" data-conn-error hidden></p>
                </div>
                <div class="marketplace-modal-actions">
                    <button type="button" class="btn btn-default" data-conn-cancel data-i18n="connections.cancel">Cancel</button>
                    <button type="button" class="btn btn-primary" data-conn-save data-i18n="connections.save">Save</button>
                </div>
            </div>
        `;
        applyStaticTranslations(backdrop);
        document.body.appendChild(backdrop);

        const close = (result) => { backdrop.remove(); resolve(result); };
        const errorEl = backdrop.querySelector('[data-conn-error]');
        const value = (selector) => (backdrop.querySelector(selector)?.value || '').trim();

        backdrop.querySelectorAll('[data-conn-cancel]').forEach((btn) => {
            btn.addEventListener('click', () => close(null));
        });

        backdrop.querySelector('[data-conn-save]')?.addEventListener('click', async () => {
            const provider = value('[data-conn-provider]');
            const secret = value('[data-conn-secret]');
            const body = {
                connection_id: value('[data-conn-id]'),
                provider,
                label: value('[data-conn-label]'),
                kind: backdrop.querySelector('[data-conn-kind]')?.value || 'api_key',
                privacy: backdrop.querySelector('[data-conn-privacy]')?.value || 'unknown',
                base_url: value('[data-conn-base]'),
            };
            // The credential travels under its provider's own settings key so the
            // server stores it exactly where that provider's resolver looks.
            if (secret) body.secret = { [`${provider.toUpperCase().replace(/-/g, '_')}_API_KEY`]: secret };

            try {
                const response = await apiFetch('/api/connections', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                const payload = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
                close(payload.connection || null);
            } catch (err) {
                if (errorEl) {
                    errorEl.hidden = false;
                    errorEl.textContent = String(err.message || err);
                }
            }
        });
    });
}


export function initConnections(ctx = {}) {
    const holder = document.createElement('div');
    holder.innerHTML = pageTemplate();
    const page = holder.firstElementChild;
    applyStaticTranslations(page);
    document.getElementById('content').appendChild(page);

    document.getElementById('connections-refresh')?.addEventListener('click', reload);
    document.getElementById('connections-add')?.addEventListener('click', async () => {
        const created = await openAddDialog();
        if (created) {
            showToast(t('connections.added', 'Connection added'), 'success');
            await reload();
        }
    });
    for (const id of ['connections-search', 'connections-filter-provider', 'connections-filter-privacy']) {
        document.getElementById(id)?.addEventListener('input', render);
        document.getElementById(id)?.addEventListener('change', render);
    }

    document.getElementById('connections-list')?.addEventListener('click', async (event) => {
        const row = event.target.closest('[data-connection-id]');
        if (!row) return;
        const connectionId = row.dataset.connectionId;
        try {
            if (event.target.closest('[data-connection-toggle]')) {
                const current = allRows.find((item) => item.connection_id === connectionId);
                await toggleRow(connectionId, !(current && current.enabled));
                await reload();
            } else if (event.target.closest('[data-connection-delete]')) {
                await deleteRow(connectionId);
                await reload();
            }
        } catch (err) {
            showToast(t('connections.action_failed', 'Action failed') + `: ${err.message || err}`, 'error');
        }
    });

    window.addEventListener('ouro:page-shown', (event) => {
        if (event.detail?.page === 'connections') reload();
    });

    reload();
    return { reload };
}
