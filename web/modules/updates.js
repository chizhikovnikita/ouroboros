import { escapeHtmlAttr as escapeHtml } from './utils.js';
import { openConfirmDialog } from './confirm_dialog.js';
import { showToast } from './toast.js';
import { apiClient, apiFetch, updateStrategyForPlan } from './api_client.js';

export function initUpdates({ mount, state }) {
    const page = document.createElement('div');
    page.id = 'page-updates';
    page.className = 'settings-embedded-content settings-updates-panel';
    // Keep the update action beside the status it refreshes.
    page.innerHTML = `
        <div class="updates-scroll">
            <section class="updates-card" id="updates-status-card">
                <div class="updates-card-head">
                    <div class="updates-card-head-main">
                        <div class="section-title">Official Updates</div>
                        <div class="updates-summary" id="updates-summary">Loading update status...</div>
                    </div>
                    <div class="updates-head-actions">
                        <span class="status-badge offline" id="updates-badge">Idle</span>
                        <button class="btn btn-default btn-sm" id="btn-update-check">Check for updates</button>
                    </div>
                </div>
                <div class="updates-meta" id="updates-meta"></div>
                <div class="updates-actions">
                    <button class="btn btn-primary" id="btn-update-apply" disabled>Update Now</button>
                </div>
                <details class="updates-recovery">
                    <summary>Recovery</summary>
                    <p>Replace the active checkout with the exact official version from the selected channel. A rescue copy is saved first, but this is intentionally more destructive than Update Now.</p>
                    <button class="btn btn-danger btn-sm" id="btn-update-replace">Replace with Official Version (Recovery)</button>
                </details>
            </section>
            <section class="updates-card">
                <div class="evo-versions-header">
                    <div id="updates-current" class="evo-versions-branch"></div>
                    <button class="btn btn-primary" id="updates-promote">Promote to QA</button>
                </div>
                <div class="evo-versions-cols">
                    <div class="evo-versions-col">
                        <h3 class="section-title">Local Recovery: Recent Commits</h3>
                        <div id="updates-commits" class="log-scroll evo-versions-list"></div>
                    </div>
                    <div class="evo-versions-col">
                        <h3 class="section-title">Official Releases</h3>
                        <div id="updates-official-tags" class="log-scroll evo-versions-list"></div>
                    </div>
                    <div class="evo-versions-col">
                        <h3 class="section-title">Local Recovery: Local Tags</h3>
                        <div id="updates-tags" class="log-scroll evo-versions-list"></div>
                    </div>
                </div>
            </section>
        </div>
    `;
    mount.appendChild(page);

    const checkBtn = page.querySelector('#btn-update-check');
    const applyBtn = page.querySelector('#btn-update-apply');
    const replaceBtn = page.querySelector('#btn-update-replace');
    const badge = page.querySelector('#updates-badge');
    const summary = page.querySelector('#updates-summary');
    const meta = page.querySelector('#updates-meta');
    const current = page.querySelector('#updates-current');
    const commitsDiv = page.querySelector('#updates-commits');
    const officialTagsDiv = page.querySelector('#updates-official-tags');
    const tagsDiv = page.querySelector('#updates-tags');
    let latestStatus = null;

    function setBadge(kind, text) {
        badge.className = `status-badge ${kind}`;
        badge.textContent = text;
    }

    function divergenceText(data) {
        const parts = [];
        if (data.behind) parts.push(`${data.behind} incoming`);
        if (data.ahead) parts.push(`${data.ahead} local`);
        if (data.dirty_count) parts.push(`${data.dirty_count} dirty`);
        return parts.join(' / ') || 'clean';
    }

    function renderStatus(data) {
        latestStatus = data;
        const unmanaged = data.managed === false
            || (Array.isArray(data.warnings) && data.warnings.includes('managed_updates_unavailable'));
        if (unmanaged) {
            summary.textContent = 'Managed updates are unavailable for this checkout.';
            meta.innerHTML = `
                <span class="evo-runtime-chip"><strong>Mode:</strong> source checkout</span>
                <span class="evo-runtime-chip"><strong>Action:</strong> use git or install a launcher-managed build</span>
            `;
            applyBtn.disabled = true;
            replaceBtn.disabled = true;
            applyBtn.dataset.safe = '0';
            applyBtn.textContent = 'Unavailable';
            setBadge('offline', 'Unavailable');
            return;
        }
        if (!data.from_cache && Array.isArray(data.warnings) && data.warnings.includes('official_status_requires_check')) {
            summary.textContent = 'Click Check for updates to refresh official update status.';
            meta.innerHTML = '<span class="evo-runtime-chip"><strong>Official repo:</strong> razzant/ouroboros</span>';
            applyBtn.disabled = true;
            replaceBtn.disabled = false;
            applyBtn.dataset.safe = '0';
            applyBtn.textContent = 'Check Required';
            setBadge('offline', 'Not checked');
            return;
        }
        if (data.check_ok === false) {
            summary.textContent = 'Could not check the official update channel. Try again when the network is available.';
            meta.innerHTML = '<span class="evo-runtime-chip"><strong>Official repo:</strong> razzant/ouroboros</span>';
            applyBtn.disabled = true;
            replaceBtn.disabled = false;
            applyBtn.dataset.safe = '0';
            applyBtn.textContent = 'Check Failed';
            setBadge('error', 'Check failed');
            return;
        }
        const currentVersion = data.current_version || 'unknown';
        const latestVersion = data.latest_version || 'unknown';
        const currentSha = data.current_short_sha || '?';
        const latestSha = data.latest_short_sha || '?';
        const latestMsg = data.latest_message || 'No remote message.';
        const canUpdate = Boolean(data.available);
        const safe = Boolean(data.safe_to_apply);
        summary.textContent = canUpdate
            ? `Update available: ${currentVersion} (${currentSha}) -> ${latestVersion} (${latestSha})`
            : `Ouroboros is up to date at ${currentVersion} (${currentSha}).`;
        meta.innerHTML = [
            `<span class="evo-runtime-chip"><strong>Official repo:</strong> razzant/ouroboros</span>`,
            `<span class="evo-runtime-chip"><strong>Remote ref:</strong> ${escapeHtml(data.remote || 'managed')}/${escapeHtml(data.remote_branch || '')}</span>`,
            `<span class="evo-runtime-chip"><strong>Channel:</strong> ${escapeHtml(data.update_channel || 'stable')}</span>`,
            `<span class="evo-runtime-chip"><strong>Divergence:</strong> ${escapeHtml(divergenceText(data))}</span>`,
            `<span class="evo-runtime-chip"><strong>Latest:</strong> ${escapeHtml(latestMsg)}</span>`,
        ].join('');
        applyBtn.disabled = !canUpdate;
        replaceBtn.disabled = false;
        applyBtn.dataset.safe = safe ? '1' : '0';
        applyBtn.textContent = !canUpdate ? 'No Update Available' : 'Update Now';
        setBadge(canUpdate ? (safe ? 'online' : 'starting') : 'offline', canUpdate ? 'Available' : 'Current');
    }

    async function loadStatus({ fetchRemote = false } = {}) {
        checkBtn.disabled = true;
        setBadge('starting', fetchRemote ? 'Checking...' : 'Loading...');
        try {
            const data = await (fetchRemote ? apiClient.updateCheck() : apiClient.updateStatus());
            renderStatus(data);
            renderOfficialTags(data.official_tags || []);
        } catch (err) {
            summary.textContent = `Failed to load update status: ${err.message || err}`;
            meta.innerHTML = '';
            applyBtn.disabled = true;
            replaceBtn.disabled = true;
            setBadge('error', 'Error');
        } finally {
            checkBtn.disabled = false;
        }
    }

    function renderVersionRow(item, labelText, targetId) {
        const row = document.createElement('div');
        row.className = 'log-entry evo-versions-row';
        const date = (item.date || '').slice(0, 16).replace('T', ' ');
        const msg = escapeHtml((item.message || '').slice(0, 72));
        row.innerHTML = `
            <span class="log-type tools evo-versions-row-label">${escapeHtml(labelText)}</span>
            <span class="log-ts">${escapeHtml(date)}</span>
            <span class="log-msg evo-versions-row-msg">${msg}</span>
            <button class="btn btn-danger btn-xs" data-target="${escapeHtml(targetId)}">Restore</button>
        `;
        row.querySelector('button').addEventListener('click', () => rollback(targetId));
        return row;
    }

    function renderOfficialTags(tags) {
        officialTagsDiv.innerHTML = '';
        (tags || []).forEach((tag) => {
            const row = document.createElement('div');
            row.className = 'log-entry evo-versions-row';
            row.innerHTML = `
                <span class="log-type tools evo-versions-row-label">${escapeHtml(tag.tag || '')}</span>
                <span class="log-msg evo-versions-row-msg">${escapeHtml((tag.sha || '').slice(0, 12))}</span>
            `;
            officialTagsDiv.appendChild(row);
        });
        if (!tags?.length) officialTagsDiv.innerHTML = '<div class="evo-empty">Check for updates to load official releases.</div>';
    }

    async function loadVersions() {
        try {
            const resp = await apiFetch('/api/git/log', { cache: 'no-store' });
            if (!resp.ok) throw new Error('Git log API error ' + resp.status);
            const data = await resp.json();
            current.textContent = `Branch: ${data.branch || '?'} @ ${data.sha || '?'}`;
            commitsDiv.innerHTML = '';
            (data.commits || []).forEach((commit) => {
                commitsDiv.appendChild(renderVersionRow(commit, commit.short_sha || commit.sha?.slice(0, 8), commit.sha));
            });
            if (!data.commits?.length) commitsDiv.innerHTML = '<div class="evo-empty">No commits found</div>';
            tagsDiv.innerHTML = '';
            (data.tags || []).forEach((tag) => {
                tagsDiv.appendChild(renderVersionRow(tag, tag.tag, tag.tag));
            });
            if (!data.tags?.length) tagsDiv.innerHTML = '<div class="evo-empty">No tags found</div>';
        } catch (err) {
            const msg = `<div class="evo-empty evo-empty-error">Failed to load: ${escapeHtml(err.message || err)}</div>`;
            commitsDiv.innerHTML = msg;
            tagsDiv.innerHTML = msg;
            current.textContent = 'Branch: unknown';
        }
    }

    async function rollback(target) {
        const confirmed = await openConfirmDialog({
            title: 'Roll back',
            body: `Roll back to ${target}?\n\nA rescue snapshot of the current state will be saved. The server will restart.`,
            confirmLabel: 'Roll back',
            danger: true,
        });
        if (!confirmed) return;
        try {
            const resp = await apiFetch('/api/git/rollback', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ target }),
            });
            const data = await resp.json();
            if (data.status === 'ok') {
                showToast(`Rollback successful: ${data.message}. Server is restarting...`, 'success');
            } else if (data.status === 'restart_required') {
                showToast(`Rollback completed: ${data.message}. Restart Ouroboros to finish.`, 'error');
            } else {
                const suffix = data.restart_required
                    ? ' Runtime shutdown was incomplete; restart Ouroboros before retrying.'
                    : '';
                showToast(`Rollback failed: ${data.error || 'unknown error'}${suffix}`, 'error');
            }
        } catch (err) {
            showToast('Rollback failed: ' + (err.message || err), 'error');
        }
    }

    async function applyUpdate() {
        if (!latestStatus?.available) return;
        applyBtn.disabled = true;
        applyBtn.textContent = 'Checking...';
        try {
            const preflight = await apiClient.updatePreflight();
            const plan = preflight?.merge_plan || {};
            const strategy = updateStrategyForPlan(plan);
            if (!strategy) throw new Error(plan.error || 'No actionable update plan is available.');
            applyBtn.textContent = 'Applying...';
            const data = await apiClient.updateApply(strategy, plan);
            if (data.status === 'assisted_started') {
                showToast('Ouroboros is resolving the update merge under review. Watch progress in chat.', 'success');
            } else if (data.status === 'restart_required') {
                showToast('Update landed, but automatic restart failed. Restart Ouroboros to finish.', 'error');
            } else {
                showToast('Update applied. Server is restarting.', 'success');
            }
        } catch (err) {
            const restartRequired = Boolean(err?.body?.restart_required);
            const suffix = restartRequired ? ' Runtime shutdown was incomplete; restart Ouroboros before retrying.' : '';
            showToast('Update failed: ' + (err.message || err) + suffix, 'error');
            applyBtn.disabled = restartRequired;
            applyBtn.textContent = restartRequired ? 'Restart Required' : 'Update Now';
        }
    }

    async function replaceWithOfficial() {
        const proceed = await openConfirmDialog({
            title: 'Replace with official version',
            body: 'Recovery will replace the active checkout with the exact official version from the selected channel.\n\nA rescue snapshot and a local keep branch preserve a copy, but the active branch will be reset. Continue?',
            confirmLabel: 'Replace checkout',
            danger: true,
        });
        if (!proceed) return;
        replaceBtn.disabled = true;
        try {
            const preflight = await apiClient.updatePreflight();
            const plan = preflight?.merge_plan || {};
            if (!plan.base_sha || !plan.target_sha) {
                throw new Error(plan.error || 'Could not resolve an exact recovery target.');
            }
            const data = await apiClient.updateApply('replace', plan, { confirmRecovery: true });
            if (data.status === 'restart_required') {
                showToast('Recovery landed, but automatic restart failed. Restart Ouroboros to finish.', 'error');
            } else {
                showToast('Official version restored. Server is restarting.', 'success');
            }
        } catch (err) {
            const restartRequired = Boolean(err?.body?.restart_required);
            const suffix = restartRequired ? ' Runtime shutdown was incomplete; restart Ouroboros before retrying.' : '';
            showToast('Recovery failed: ' + (err.message || err) + suffix, 'error');
            replaceBtn.disabled = restartRequired;
        }
    }

    checkBtn.addEventListener('click', () => {
        loadStatus({ fetchRemote: true });
        loadVersions();
    });
    applyBtn.addEventListener('click', applyUpdate);
    replaceBtn.addEventListener('click', replaceWithOfficial);
    page.querySelector('#updates-promote').addEventListener('click', async () => {
        const confirmedPromote = await openConfirmDialog({
            title: 'Promote to stable',
            body: 'Promote current ouroboros branch to ouroboros-stable?',
            confirmLabel: 'Promote',
        });
        if (!confirmedPromote) return;
        try {
            const resp = await apiFetch('/api/git/promote', { method: 'POST' });
            const data = await resp.json();
            if (data.status === 'ok') {
                showToast(data.message, 'success');
            } else {
                showToast('Error: ' + (data.error || 'unknown'), 'error');
            }
        } catch (err) {
            showToast('Failed: ' + (err.message || err), 'error');
        }
    });

    window.addEventListener('ouro:dashboard-subtab-shown', (event) => {
        if (event.detail?.tab !== 'updates' || state.activePage !== 'dashboard') return;
        loadStatus({ fetchRemote: false });
        loadVersions();
    });
}
