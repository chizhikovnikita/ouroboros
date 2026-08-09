import { escapeHtmlAttr as escapeHtml } from './utils.js';

let activeDialog = null;
let activeClose = null;

export function openConfirmDialog({
    title,
    body,
    input = false,
    initialValue = '',
    // Alert mode (v6.90.3): one OK-style button, no cancel button. Escape,
    // backdrop, and the header Close still resolve the promise (false), same
    // as a cancelled confirm — callers treat any resolution as "seen".
    alert = false,
    confirmLabel = alert ? 'OK' : 'Continue',
    cancelLabel = 'Cancel',
    danger = false,
} = {}) {
    if (activeClose) activeClose(false);
    return new Promise((resolve) => {
        const backdrop = document.createElement('div');
        backdrop.className = 'marketplace-modal-backdrop confirm-dialog-backdrop';
        backdrop.innerHTML = `
            <div class="marketplace-modal confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-dialog-title">
                <div class="marketplace-modal-head">
                    <h3 id="confirm-dialog-title">${escapeHtml(title || 'Confirm action')}</h3>
                    <button type="button" class="btn btn-default btn-sm" data-confirm-cancel aria-label="Close">Close</button>
                </div>
                <div class="marketplace-modal-body">
                    <p>${escapeHtml(body || 'Continue?')}</p>
                    ${input ? `<input class="files-modal-input confirm-dialog-input" data-confirm-input type="text" value="${escapeHtml(initialValue)}">` : ''}
                </div>
                <div class="marketplace-modal-actions">
                    ${alert ? '' : `<button type="button" class="btn btn-default" data-confirm-cancel>${escapeHtml(cancelLabel)}</button>`}
                    <button type="button" class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-confirm-ok>${escapeHtml(confirmLabel)}</button>
                </div>
            </div>
        `;
        let settled = false;
        const finish = (value) => {
            if (settled) return;
            settled = true;
            document.removeEventListener('keydown', onKey);
            if (activeDialog === backdrop) activeDialog = null;
            if (activeClose === cancel) activeClose = null;
            backdrop.remove();
            resolve(value);
        };
        const result = (confirmed) => input
            ? { confirmed, value: confirmed ? (backdrop.querySelector('[data-confirm-input]')?.value || '') : '' }
            : confirmed;
        // Supersession must honor the MODE's contract: a newer dialog cancelling
        // this one resolves the same shape a user cancel would ({confirmed:false,
        // value:''} in input mode), never a bare false the docs do not promise.
        const cancel = () => finish(result(false));
        backdrop.addEventListener('click', (event) => {
            if (event.target === backdrop || event.target.closest('[data-confirm-cancel]')) {
                finish(result(false));
            } else if (event.target.closest('[data-confirm-ok]')) {
                finish(result(true));
            }
        });
        const onKey = (event) => {
            if (event.key === 'Escape' && activeDialog === backdrop) {
                finish(result(false));
            } else if (input && event.key === 'Enter' && event.target?.matches?.('[data-confirm-input]')) {
                event.preventDefault();
                finish(result(true));
            }
        };
        document.addEventListener('keydown', onKey);
        document.body.appendChild(backdrop);
        activeDialog = backdrop;
        activeClose = cancel;
        (backdrop.querySelector(input ? '[data-confirm-input]' : '[data-confirm-ok]'))?.focus();
        backdrop.querySelector('[data-confirm-input]')?.select?.();
    });
}
