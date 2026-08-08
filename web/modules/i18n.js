// Localization runtime: resolves the active language, loads its catalog, and
// exports the translator every UI module uses.
//
// The top-level await is deliberate. `app.js` is an ES module, so importing this
// suspends every consumer until the catalog is in hand — that removes the whole
// class of "string rendered before the catalog arrived" bugs, including strings
// evaluated at module scope.
//
// Outside a browser (the `web/tests` node lane) no IO is attempted at all: the
// catalog stays empty and `t()` returns the English default argument. Importing
// any localized module in a test is therefore safe, silent, and deterministically
// English — never the test machine's own locale.

import {
    FALLBACK_LANGUAGE,
    applyTranslations,
    createTranslator,
    normalizeLanguage,
} from './i18n_core.js';

/**
 * settings.json is the single authority for the language; the browser is only
 * consulted when the owner has not chosen one (UI_LANGUAGE === ''). No
 * localStorage mirror — a second store would drift from the settings file.
 */
/** Whether a real browser document is hosting us (false under node tests). */
function isBrowser() {
    return typeof document !== 'undefined' && typeof fetch === 'function';
}

async function resolveLanguage() {
    if (!isBrowser()) return FALLBACK_LANGUAGE;
    try {
        const resp = await fetch('/api/settings', { cache: 'no-store' });
        if (resp.ok) {
            const data = await resp.json();
            const chosen = normalizeLanguage(data && data.UI_LANGUAGE);
            if (chosen) return chosen;
        }
    } catch {
        // No server (tests) or a failed read: fall through to auto-detection.
    }

    const candidates = (typeof navigator !== 'undefined' && (navigator.languages || [navigator.language])) || [];
    for (const candidate of candidates) {
        const normalized = normalizeLanguage(candidate);
        if (normalized) return normalized;
    }
    return FALLBACK_LANGUAGE;
}

async function loadCatalog(language) {
    if (language === FALLBACK_LANGUAGE || !isBrowser()) return {};
    try {
        const resp = await fetch(`/static/locales/${language}.json`, { cache: 'no-store' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        return data && typeof data === 'object' ? data : {};
    } catch (err) {
        // Disclosed, not fatal: an unreachable catalog degrades to English rather
        // than blanking the interface.
        if (typeof console !== 'undefined') {
            console.warn(`i18n: could not load the ${language} catalog; using English.`, err);
        }
        return {};
    }
}

const activeLanguage = await resolveLanguage();
const catalog = await loadCatalog(activeLanguage);

export const t = createTranslator(catalog);

export function currentLanguage() {
    return activeLanguage;
}

/** Translate declarative `data-i18n` markup and stamp <html lang>. */
export function applyStaticTranslations(root) {
    const scope = root || (typeof document !== 'undefined' ? document : null);
    if (!scope) return 0;
    if (typeof document !== 'undefined' && document.documentElement) {
        document.documentElement.lang = activeLanguage;
    }
    return applyTranslations(scope, t);
}
