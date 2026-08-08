"""Shared onboarding wizard helpers for desktop and web."""

from __future__ import annotations

import json
import pathlib
from typing import Tuple

from ouroboros.settings_setup_contract import (
    build_setup_bootstrap,
    validate_setup_payload,
)

_ASSET_ROOT = pathlib.Path(__file__).resolve().parents[1] / "web"
_TEMPLATE_PATH = _ASSET_ROOT / "onboarding_template.html"
_CSS_PATH = _ASSET_ROOT / "onboarding.css"
_JS_PATH = _ASSET_ROOT / "modules" / "onboarding_wizard.js"
_LOCALES_DIR = _ASSET_ROOT / "locales"
# English is the default at every call site, so it needs no catalog file.
_TRANSLATED_LANGUAGES = ("ru",)


from ouroboros.utils import read_text as _read_asset


def _onboarding_i18n(settings: dict) -> dict:
    """Catalogs + configured language for the inlined wizard.

    The wizard is a standalone <script> with no module imports, so its catalog
    travels inside the document rather than over HTTP. Every non-English catalog
    ships (one small file today): on a FIRST run there is no ``UI_LANGUAGE`` yet
    and only the browser knows its own locale, so the choice must be made in the
    page, not here. An unreadable catalog is omitted rather than fatal — the
    wizard then renders the English defaults it already carries.
    """
    catalogs: dict[str, dict] = {}
    for language in _TRANSLATED_LANGUAGES:
        try:
            catalogs[language] = json.loads(_read_asset(_LOCALES_DIR / f"{language}.json"))
        except Exception:
            continue
    return {
        "language": str(settings.get("UI_LANGUAGE") or ""),
        "catalogs": catalogs,
    }


def build_onboarding_html(settings: dict, host_mode: str = "desktop") -> str:
    normalized_host_mode = "web" if host_mode == "web" else "desktop"
    bootstrap = build_setup_bootstrap(settings, normalized_host_mode)
    return (
        _read_asset(_TEMPLATE_PATH)
        .replace("__ONBOARDING_CSS__", _read_asset(_CSS_PATH))
        .replace("__ONBOARDING_BOOTSTRAP__", json.dumps(bootstrap, ensure_ascii=True))
        .replace("__ONBOARDING_I18N__", json.dumps(_onboarding_i18n(settings), ensure_ascii=True))
        .replace("__ONBOARDING_JS__", _read_asset(_JS_PATH))
    )


def prepare_onboarding_settings(data: dict, current_settings: dict) -> Tuple[dict, str | None]:
    return validate_setup_payload(data, current_settings)
