"""Localization seams that live on the Python side.

The web-facing translator is covered by ``web/tests/i18n.test.js``; what needs a
Python test is the part JavaScript cannot reach: the settings key that carries the
owner's choice, and the wizard catalog that has to travel INSIDE the document
because the onboarding script is inlined, not imported.
"""

from __future__ import annotations

import json

import pytest

from ouroboros.config import SETTINGS_DEFAULTS, SETTINGS_KEYS_NOT_EXPORTED_TO_ENV
from ouroboros.onboarding_wizard import build_onboarding_html


def test_ui_language_defaults_to_auto_detection():
    # "" is load-bearing: an existing install must keep following its browser
    # rather than being switched to a language nobody chose.
    assert SETTINGS_DEFAULTS["UI_LANGUAGE"] == ""


def test_ui_language_is_not_exported_to_child_environments():
    assert "UI_LANGUAGE" in SETTINGS_KEYS_NOT_EXPORTED_TO_ENV


@pytest.mark.parametrize("host_mode", ["desktop", "web"])
def test_onboarding_html_inlines_the_catalog_for_both_hosts(host_mode):
    html = build_onboarding_html({"UI_LANGUAGE": "ru"}, host_mode=host_mode)

    assert "__ONBOARDING_I18N__" not in html, "placeholder was left unsubstituted"
    assert "window.__OURO_ONBOARDING_I18N__" in html
    # The wizard cannot fetch: if the Russian strings are not in the document,
    # they do not exist for it.
    assert "Доступ к моделям" in html or "\\u0414" in html


def test_onboarding_catalog_carries_the_configured_language():
    html = build_onboarding_html({"UI_LANGUAGE": "ru"}, host_mode="web")
    payload = _extract_i18n_payload(html)
    assert payload["language"] == "ru"
    assert "ru" in payload["catalogs"]
    assert payload["catalogs"]["ru"]["onboarding.continue"]


def test_onboarding_catalog_ships_even_when_no_language_is_chosen():
    # First run has no UI_LANGUAGE yet and only the browser knows its locale, so
    # the catalog must be present for the page to choose from.
    payload = _extract_i18n_payload(build_onboarding_html({}, host_mode="desktop"))
    assert payload["language"] == ""
    assert "ru" in payload["catalogs"]


def test_onboarding_still_builds_when_the_catalog_is_unreadable(monkeypatch):
    """A broken catalog degrades to English chrome instead of failing the wizard."""
    import ouroboros.onboarding_wizard as module

    real_read = module._read_asset

    def _fail_for_locales(path, *args, **kwargs):
        if "locales" in str(path):
            raise OSError("unreadable catalog")
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(module, "_read_asset", _fail_for_locales)
    payload = _extract_i18n_payload(build_onboarding_html({"UI_LANGUAGE": "ru"}, host_mode="web"))
    assert payload["catalogs"] == {}
    assert payload["language"] == "ru"


def _extract_i18n_payload(html: str) -> dict:
    marker = "window.__OURO_ONBOARDING_I18N__ = "
    start = html.index(marker) + len(marker)
    end = html.index(";\n", start)
    return json.loads(html[start:end])
