"""Model catalog settings and self-hosted curation lists.

The picker's curated OpenRouter / Nous Portal lists are the in-repo ones (``OPENROUTER_MODELS``,
``_PROVIDER_MODELS`` in ``hermes_cli.models``). Crema does not fetch upstream Hermes's hosted
catalog: it does not depend on other agent programs' servers, so the lists change with an engine
release. A list the user hosts themselves (``model_catalog.providers.<name>.url``) still replaces the
in-repo one for that provider; ``get_curated_openrouter_models()`` / ``get_curated_nous_models()``
return it, or ``None`` so callers use the in-repo list.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from hermes_cli import __version__ as _HERMES_VERSION

logger = logging.getLogger(__name__)

DEFAULT_TTL_MINUTES = 20
# Legacy key, honoured only when the user set it explicitly; ``ttl_minutes`` is the shipped default.
DEFAULT_TTL_HOURS = DEFAULT_TTL_MINUTES / 60.0
DEFAULT_FETCH_TIMEOUT = 8.0
SUPPORTED_SCHEMA_VERSION = 1

_HERMES_USER_AGENT = f"hermes-cli/{_HERMES_VERSION}"


def _load_catalog_config() -> dict[str, Any]:
    """Load the ``model_catalog`` config block with defaults filled in."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    raw = cfg.get("model_catalog")
    if not isinstance(raw, dict):
        raw = {}

    # ``ttl_hours`` (legacy) is honoured only when ``ttl_minutes`` is still at its default —
    # load_config() deep-merges the default in, so "present" alone doesn't mean "user-set".
    ttl_minutes = raw.get("ttl_minutes")
    try:
        ttl_minutes = float(ttl_minutes) if ttl_minutes not in (None, "") else DEFAULT_TTL_MINUTES
    except (TypeError, ValueError):
        ttl_minutes = DEFAULT_TTL_MINUTES
    if ttl_minutes == DEFAULT_TTL_MINUTES and raw.get("ttl_hours"):
        try:
            ttl_minutes = float(raw["ttl_hours"]) * 60.0
        except (TypeError, ValueError):
            pass
    if ttl_minutes <= 0:
        ttl_minutes = DEFAULT_TTL_MINUTES

    return {
        "enabled": bool(raw.get("enabled", True)),
        "ttl_hours": ttl_minutes / 60.0,
        "providers": raw.get("providers") if isinstance(raw.get("providers"), dict) else {}}


def _fetch_manifest(url: str, timeout: float) -> dict[str, Any] | None:
    """HTTP GET the manifest URL and return a validated dict, or None on failure."""
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": _HERMES_USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        logger.info("model catalog fetch failed (%s): %s", url, exc)
        return None
    except Exception as exc:  # pragma: no cover — defensive
        logger.info("model catalog fetch errored (%s): %s", url, exc)
        return None
    if not _validate_manifest(data):
        logger.info("model catalog at %s failed schema validation", url)
        return None
    return data


def _validate_manifest(data: Any) -> bool:
    """Return True when ``data`` matches the minimum manifest shape."""
    if not isinstance(data, dict):
        return False
    version = data.get("version")
    if not isinstance(version, int) or version > SUPPORTED_SCHEMA_VERSION:
        return False  # future schema we don't understand — refuse rather than guess
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return False
    for pname, pblock in providers.items():
        if not isinstance(pname, str) or not isinstance(pblock, dict):
            return False
        models = pblock.get("models")
        if not isinstance(models, list):
            return False
        if not all(isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"].strip() for m in models):
            return False
    return True


def refresh_interval_seconds() -> float:
    """Return the configured catalog TTL in seconds (the gateway poll cadence)."""
    return max(60.0, _load_catalog_config()["ttl_hours"] * 3600.0)


def refresh_catalogs() -> bool:
    """Force-refresh the live provider lists the picker reads (OpenRouter's live list, Nous Portal
    recommendations), writing each disk cache so the next ``/model`` open in ANY process sees them.
    Blocking; run it off the event loop. False when ``model_catalog.enabled`` is off."""
    if not _load_catalog_config()["enabled"]:
        return False
    try:
        from hermes_cli.models import fetch_nous_recommended_models, fetch_openrouter_models

        fetch_openrouter_models(force_refresh=True)
        fetch_nous_recommended_models(force_refresh=True)
    except Exception:
        logger.debug("provider catalog refresh failed", exc_info=True)
    return True


def _fetch_provider_override(provider: str) -> dict[str, Any] | None:
    """If ``model_catalog.providers.<name>.url`` is set, fetch that instead."""
    cfg = _load_catalog_config()
    if not cfg["enabled"]:
        return None
    provider_cfg = cfg["providers"].get(provider)
    if not isinstance(provider_cfg, dict):
        return None
    override_url = provider_cfg.get("url")
    if not isinstance(override_url, str) or not override_url.strip():
        return None
    # Overrides are usually third-party self-hosted: skip the disk cache, re-request every call.
    return _fetch_manifest(override_url.strip(), DEFAULT_FETCH_TIMEOUT)


def _get_provider_block(provider: str) -> dict[str, Any] | None:
    """The provider's block of its self-hosted override list, or None."""
    block = (_fetch_provider_override(provider) or {}).get("providers", {}).get(provider)
    return block if isinstance(block, dict) else None


def _block_ids(block: dict[str, Any] | None) -> list[tuple[str, dict[str, Any]]]:
    """``(id, entry)`` for every model entry of ``block`` with a non-empty id."""
    models = (block or {}).get("models", [])
    return [(mid, m) for m in models if isinstance(m, dict) and (mid := str(m.get("id") or "").strip())]


def get_curated_openrouter_models() -> list[tuple[str, str]] | None:
    """OpenRouter's curated ``[(id, description), ...]`` from a self-hosted override, or None."""
    rows = _block_ids(_get_provider_block("openrouter"))
    return [(mid, str(m.get("description") or "")) for mid, m in rows] or None


def get_curated_nous_models() -> list[str] | None:
    """Nous Portal's curated model ids from a self-hosted override, or None."""
    return [mid for mid, _ in _block_ids(_get_provider_block("nous"))] or None
