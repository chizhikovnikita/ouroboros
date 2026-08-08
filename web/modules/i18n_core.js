// Pure localization logic — no network, no DOM globals at module scope.
//
// Split from i18n.js on purpose: `web/tests/*.test.js` import UI modules directly
// under plain node, so anything those modules pull in must be importable with no
// server and no browser. All IO lives in i18n.js; everything decidable lives here
// and is unit-tested.

export const SUPPORTED_LANGUAGES = ['en', 'ru'];
export const FALLBACK_LANGUAGE = 'en';

/** Reduce a BCP-47 tag ('ru-RU', 'RU') to a supported base language, or ''. */
export function normalizeLanguage(raw) {
    const tag = String(raw == null ? '' : raw).trim().toLowerCase();
    if (!tag) return '';
    const base = tag.split(/[-_]/)[0];
    return SUPPORTED_LANGUAGES.includes(base) ? base : '';
}

/** Substitute {name} placeholders. An unknown placeholder is left verbatim. */
export function interpolate(text, params) {
    const source = String(text == null ? '' : text);
    if (!params) return source;
    return source.replace(/\{(\w+)\}/g, (match, name) => (
        Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match
    ));
}

/**
 * Build `t(key, defaultMessage, params)` over a catalog of overrides.
 *
 * The ENGLISH text is the second argument at every call site, not a second
 * catalog file. Two reasons, both load-bearing:
 *   - the source stays readable and reviewable (you see the real string, not an
 *     opaque key), and there is no en.json that can silently drift out of sync;
 *   - a module importing this under node with no catalog still renders correct
 *     English, which is exactly what the existing web tests assert.
 * A missing translation is therefore never a blank or a raw key.
 */
export function createTranslator(catalog) {
    const table = catalog && typeof catalog === 'object' ? catalog : {};
    return function t(key, defaultMessage, params) {
        const override = Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
        const chosen = typeof override === 'string' && override !== ''
            ? override
            : (defaultMessage == null ? key : defaultMessage);
        return interpolate(chosen, params);
    };
}

/**
 * Translate declarative markup: `data-i18n="key"` sets textContent, and
 * `data-i18n-attr="title:key,aria-label:key"` sets attributes.
 *
 * The element's existing text/attribute is the English default, so index.html
 * stays meaningful on its own and degrades to English if a catalog is missing.
 */
export function applyTranslations(root, t) {
    if (!root || typeof root.querySelectorAll !== 'function') return 0;
    let applied = 0;

    for (const el of root.querySelectorAll('[data-i18n]')) {
        const key = el.getAttribute('data-i18n');
        if (!key) continue;
        el.textContent = t(key, el.textContent);
        applied += 1;
    }

    for (const el of root.querySelectorAll('[data-i18n-attr]')) {
        const spec = el.getAttribute('data-i18n-attr') || '';
        for (const pair of spec.split(',')) {
            const [attr, key] = pair.split(':').map((part) => (part || '').trim());
            if (!attr || !key) continue;
            el.setAttribute(attr, t(key, el.getAttribute(attr) || ''));
            applied += 1;
        }
    }

    return applied;
}
