import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import {
    FALLBACK_LANGUAGE,
    SUPPORTED_LANGUAGES,
    applyTranslations,
    createTranslator,
    interpolate,
    normalizeLanguage,
} from '../modules/i18n_core.js';

const WEB_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const RU = JSON.parse(readFileSync(path.join(WEB_DIR, 'locales', 'ru.json'), 'utf8'));

// --- translator behavior ---

test('a missing key renders the English default, never a blank or a raw key', () => {
    const t = createTranslator({});
    assert.equal(t('nav.chat', 'Main Chat'), 'Main Chat');
    // An empty catalog entry is treated as absent: a half-filled catalog must not
    // blank out chrome that has a perfectly good English string at the call site.
    assert.equal(createTranslator({ 'nav.chat': '' })('nav.chat', 'Main Chat'), 'Main Chat');
});

test('a present key overrides the English default', () => {
    const t = createTranslator({ 'nav.chat': 'Главный чат' });
    assert.equal(t('nav.chat', 'Main Chat'), 'Главный чат');
});

test('a key with no default at all falls back to the key itself', () => {
    assert.equal(createTranslator({})('some.key'), 'some.key');
});

test('interpolation fills known placeholders and leaves unknown ones verbatim', () => {
    assert.equal(interpolate('Step {n} of {total}', { n: 1, total: 5 }), 'Step 1 of 5');
    assert.equal(interpolate('Step {n} of {total}', { n: 1 }), 'Step 1 of {total}');
    assert.equal(interpolate('no placeholders', { n: 1 }), 'no placeholders');
});

test('translated text is substituted through the catalog with interpolation', () => {
    const t = createTranslator({ 'onboarding.step_counter': 'Шаг {n} из {total}' });
    assert.equal(t('onboarding.step_counter', 'Step {n} of {total}', { n: 2, total: 5 }), 'Шаг 2 из 5');
});

test('language tags normalize to a supported base language, or empty', () => {
    assert.equal(normalizeLanguage('ru-RU'), 'ru');
    assert.equal(normalizeLanguage('RU'), 'ru');
    assert.equal(normalizeLanguage('en_US'), 'en');
    assert.equal(normalizeLanguage('de-DE'), '');
    assert.equal(normalizeLanguage(''), '');
    assert.equal(normalizeLanguage(null), '');
    assert.equal(normalizeLanguage(undefined), '');
});

test('English is the fallback language and is supported', () => {
    assert.equal(FALLBACK_LANGUAGE, 'en');
    assert.ok(SUPPORTED_LANGUAGES.includes('en'));
    assert.ok(SUPPORTED_LANGUAGES.includes('ru'));
});

// --- declarative markup ---

function fakeElement(attrs, text) {
    const store = { ...attrs };
    return {
        textContent: text,
        getAttribute: (name) => (name in store ? store[name] : null),
        setAttribute: (name, value) => { store[name] = value; },
        attrs: store,
    };
}

function fakeRoot(elements) {
    return {
        querySelectorAll: (selector) => {
            const attr = selector.replace(/[[\]]/g, '');
            return elements.filter((el) => el.getAttribute(attr) !== null);
        },
    };
}

test('applyTranslations rewrites data-i18n text using the element text as default', () => {
    const el = fakeElement({ 'data-i18n': 'nav.files' }, 'Files');
    applyTranslations(fakeRoot([el]), createTranslator({ 'nav.files': 'Файлы' }));
    assert.equal(el.textContent, 'Файлы');
});

test('applyTranslations leaves the English text when the catalog lacks the key', () => {
    const el = fakeElement({ 'data-i18n': 'nav.files' }, 'Files');
    applyTranslations(fakeRoot([el]), createTranslator({}));
    assert.equal(el.textContent, 'Files');
});

test('applyTranslations rewrites listed attributes only', () => {
    const el = fakeElement(
        { 'data-i18n-attr': 'title:nav.files,aria-label:nav.files', title: 'Files', 'aria-label': 'Files' },
        'untouched',
    );
    applyTranslations(fakeRoot([el]), createTranslator({ 'nav.files': 'Файлы' }));
    assert.equal(el.attrs.title, 'Файлы');
    assert.equal(el.attrs['aria-label'], 'Файлы');
    assert.equal(el.textContent, 'untouched');
});

test('applyTranslations tolerates a missing root instead of throwing', () => {
    assert.equal(applyTranslations(null, createTranslator({})), 0);
});

// --- catalog / source parity ---

function localizedSources() {
    const files = [path.join(WEB_DIR, 'app.js'), path.join(WEB_DIR, 'index.html')];
    for (const name of readdirSync(path.join(WEB_DIR, 'modules'))) {
        // i18n_core.js documents the attribute syntax in prose; scanning it would
        // harvest example keys that no screen actually renders.
        if (name.endsWith('.js') && name !== 'i18n_core.js') {
            files.push(path.join(WEB_DIR, 'modules', name));
        }
    }
    return files.map((file) => readFileSync(file, 'utf8')).join('\n');
}

function keysUsedInSource(blob) {
    const keys = new Set();
    // The lookbehind keeps `format(`, `split(`, `assert(` — any identifier ending
    // in "t" — from being read as a t() call.
    for (const m of blob.matchAll(/(?<![A-Za-z0-9_$.])t\(\s*'([a-z0-9_.]+)'/g)) {
        // A trailing dot is the literal PREFIX of a composed key
        // (`t('onboarding.step.' + id + '.title', …)`), not a key in its own right.
        if (!m[1].endsWith('.')) keys.add(m[1]);
    }
    for (const m of blob.matchAll(/data-i18n="([a-z0-9_.]+)"/g)) keys.add(m[1]);
    // Declarative descriptors (renderTabStrip items, renderPageHeader) name their
    // key as data. Listed explicitly rather than matched as `*Key:` so unrelated
    // descriptor fields (settingKey, stateKey, …) can never be read as i18n keys.
    // `[:=]` covers both forms: an object property (`titleKey: 'x'`) and a default
    // parameter value (`titleKey = 'x'`).
    for (const m of blob.matchAll(/(?:i18nKey|titleKey|descriptionKey)\s*[:=]\s*'([a-z0-9_.]+)'/g)) keys.add(m[1]);
    for (const m of blob.matchAll(/data-i18n-attr="([^"]+)"/g)) {
        for (const pair of m[1].split(',')) {
            const parts = pair.split(':');
            if (parts.length === 2) keys.add(parts[1].trim());
        }
    }
    return keys;
}

test('the Russian catalog has no orphan keys', () => {
    // An orphan is a translation for a string no screen renders any more — dead
    // weight that silently rots as the UI changes.
    const used = keysUsedInSource(localizedSources());
    // Step titles/copy are keyed by runtime step id (onboarding.step.<id>.title),
    // so their literal key never appears in source.
    const orphans = Object.keys(RU).filter((key) => !used.has(key) && !key.startsWith('onboarding.step.'));
    assert.deepEqual(orphans, [], `orphan catalog keys: ${orphans.join(', ')}`);
});

test('every key used in the localized UI is translated', () => {
    const used = [...keysUsedInSource(localizedSources())].sort();
    const untranslated = used.filter((key) => !(key in RU));
    assert.deepEqual(untranslated, [], `keys with no Russian string: ${untranslated.join(', ')}`);
});

test('every Russian string is a non-empty string', () => {
    for (const [key, value] of Object.entries(RU)) {
        assert.equal(typeof value, 'string', `${key} must be a string`);
        assert.notEqual(value.trim(), '', `${key} must not be empty`);
    }
});

test('translation keys are always literals, never computed from data', () => {
    // The load-bearing safety rule: t() localizes interface CHROME. If a key could
    // be an expression, a model name or a chat message could be routed through the
    // catalog and silently rewritten. Only the onboarding wizard composes keys, and
    // only from its own fixed step ids.
    const offenders = [];
    for (const name of readdirSync(path.join(WEB_DIR, 'modules'))) {
        if (!name.endsWith('.js') || name === 'i18n_core.js' || name === 'onboarding_wizard.js') continue;
        const body = readFileSync(path.join(WEB_DIR, 'modules', name), 'utf8');
        for (const m of body.matchAll(/(?<![A-Za-z0-9_$.])t\(\s*([^'\s)][^,)]*)/g)) {
            offenders.push(`${name}: t(${m[1].slice(0, 40)}`);
        }
    }
    assert.deepEqual(offenders, [], `non-literal translation keys:\n${offenders.join('\n')}`);
});
