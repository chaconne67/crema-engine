"""Tests for hermes_cli.model_catalog — no remote catalog; self-hosted override lists."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Isolate HERMES_HOME + reset any module-level catalog cache per test."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    yield home


def _valid_manifest() -> dict:
    return {
        "version": 1,
        "updated_at": "2026-04-25T22:00:00Z",
        "metadata": {"source": "test"},
        "providers": {
            "openrouter": {
                "metadata": {"display_name": "OpenRouter"},
                "models": [
                    {"id": "anthropic/claude-opus-4.7", "description": "recommended"},
                    {"id": "openai/gpt-5.4", "description": ""},
                    {"id": "openrouter/elephant-alpha", "description": "free"},
                ],
            },
            "nous": {
                "metadata": {"display_name": "Nous Portal"},
                "models": [
                    {"id": "anthropic/claude-opus-4.7"},
                    {"id": "moonshotai/kimi-k2.6"},
                ],
            },
        },
    }


class TestValidation:
    def test_accepts_well_formed_manifest(self, isolated_home):
        from hermes_cli.model_catalog import _validate_manifest
        assert _validate_manifest(_valid_manifest()) is True

    def test_rejects_non_dict(self, isolated_home):
        from hermes_cli.model_catalog import _validate_manifest
        assert _validate_manifest("string") is False
        assert _validate_manifest([]) is False
        assert _validate_manifest(None) is False


    def test_rejects_non_string_model_id(self, isolated_home):
        from hermes_cli.model_catalog import _validate_manifest
        m = _valid_manifest()
        m["providers"]["openrouter"]["models"][0] = {"id": 42}
        assert _validate_manifest(m) is False


class TestCuratedAccessors:
    _OVERRIDES = {"enabled": True, "ttl_hours": 1.0, "providers": {
        "openrouter": {"url": "http://override"}, "nous": {"url": "http://override"}}}

    def test_openrouter_returns_tuples(self, isolated_home):
        from hermes_cli import model_catalog
        with patch.object(model_catalog, "_load_catalog_config", return_value=self._OVERRIDES), \
             patch.object(model_catalog, "_fetch_manifest", return_value=_valid_manifest()):
            result = model_catalog.get_curated_openrouter_models()
        assert result == [
            ("anthropic/claude-opus-4.7", "recommended"),
            ("openai/gpt-5.4", ""),
            ("openrouter/elephant-alpha", "free"),
        ]

    def test_nous_returns_none_when_override_unreachable(self, isolated_home):
        from hermes_cli import model_catalog
        with patch.object(model_catalog, "_load_catalog_config", return_value=self._OVERRIDES), \
             patch.object(model_catalog, "_fetch_manifest", return_value=None):
            assert model_catalog.get_curated_nous_models() is None


class TestProviderOverride:
    def test_override_url_takes_precedence(self, isolated_home):
        from hermes_cli import model_catalog

        override_payload = {
            "version": 1,
            "providers": {
                "openrouter": {
                    "models": [
                        {"id": "override/model", "description": "custom"},
                    ]
                }
            },
        }

        def fake_fetch(url, timeout):
            if "override" in url:
                return override_payload
            return _valid_manifest()

        with patch.object(
            model_catalog,
            "_load_catalog_config",
            return_value={
                "enabled": True,
                "ttl_hours": 24.0,
                "providers": {"openrouter": {"url": "http://override"}},
            },
        ):
            with patch.object(model_catalog, "_fetch_manifest", side_effect=fake_fetch):
                result = model_catalog.get_curated_openrouter_models()

        assert result == [("override/model", "custom")]


class TestRefreshCadence:
    def test_default_ttl_is_twenty_minutes_and_legacy_hours_honoured(self):
        from hermes_cli import model_catalog

        with patch("hermes_cli.config.load_config", return_value={"model_catalog": {"ttl_minutes": 20}}):
            assert model_catalog.refresh_interval_seconds() == 20 * 60
        # A user-set legacy ttl_hours still wins while ttl_minutes sits at its default.
        with patch("hermes_cli.config.load_config", return_value={"model_catalog": {"ttl_minutes": 20, "ttl_hours": 3}}):
            assert model_catalog.refresh_interval_seconds() == 3 * 3600

    def test_refresh_catalogs_forces_the_enabled_provider_lists(self, monkeypatch):
        from hermes_cli import model_catalog

        monkeypatch.setattr(model_catalog, "_load_catalog_config", lambda: {"enabled": True})
        monkeypatch.setattr("hermes_cli.anon_auth.current_nous_state", lambda: None)
        monkeypatch.setattr("hermes_cli.auth.read_credential_pool", lambda _provider: [])
        with patch("hermes_cli.models.fetch_openrouter_models") as router, \
             patch("hermes_cli.models.fetch_nous_recommended_models") as nous:
            assert model_catalog.refresh_catalogs() is True
        router.assert_called_once_with(force_refresh=True)
        nous.assert_not_called()


class TestIntegrationWithModelsModule:
    """Exercise the fallback paths via the real callers in hermes_cli.models."""



    def test_picker_nous_row_uses_curated_list(self, tmp_path, monkeypatch):
        """The /model picker surfaces the curated ``_PROVIDER_MODELS["nous"]``
        list in curated order — matching the ``hermes model`` CLI — not the live
        ``/v1/models`` catalog or the manifest. Portal free/paid recommendations
        are unioned in when reachable; offline (as here, with the Portal calls
        stubbed out) it's exactly the curated list.
        """
        # We deliberately do NOT use the ``isolated_home`` fixture here:
        # that fixture monkeypatches ``Path.home`` to ``tmp_path``, which
        # trips the auth-store seat-belt in ``_auth_file_path()`` because
        # ``HERMES_HOME / auth.json`` then resolves to the same path the
        # seat-belt thinks is the "real" user store. Use the autouse
        # ``_hermetic_environment`` HERMES_HOME directly instead.
        from hermes_cli.models import get_curated_nous_model_ids
        from hermes_cli.model_switch_providers import list_picker_providers

        active_home = Path(os.environ["HERMES_HOME"])
        (active_home / "auth.json").write_text(
            json.dumps(
                {
                    "providers": {"nous": {"access_token": "fake"}},
                    "credential_pool": {},
                }
            )
        )

        # Stub the Portal recommendation union so the row is deterministic
        # (the curated list alone) and never touches the network. ``expected``
        # is computed from the same source the picker uses internally
        # (``curated["nous"] = get_curated_nous_model_ids()``), so the test
        # stays an invariant — it can't rot as the curated/manifest list grows.
        with patch("hermes_cli.models.check_nous_free_tier", return_value=False), patch(
            "hermes_cli.models.union_with_portal_free_recommendations",
            side_effect=lambda ids, *a, **k: (ids, {}),
        ), patch(
            "hermes_cli.models.union_with_portal_paid_recommendations",
            side_effect=lambda ids, *a, **k: (ids, {}),
        ):
            expected = get_curated_nous_model_ids()
            picker = list_picker_providers(
                current_provider="nous", max_models=99
            )

        nous_row = next((r for r in picker if r["slug"] == "nous"), None)
        assert nous_row is not None, "nous row must appear when authed"
        assert nous_row["models"] == expected

    def test_picker_max_models_cap_semantics(self, tmp_path, monkeypatch):
        """The cap argument has three distinct meanings on the real slicing
        path: ``None`` = unlimited (the cap-removal fix, #48297), ``0`` = no
        models (preserved for slug-only callers), an int N = first N. Guards
        the ``is not None`` distinction the cap-removal follow-up introduced —
        a ``if max_models`` (falsy) check would conflate ``0`` with unlimited.
        """
        from hermes_cli.models import get_curated_nous_model_ids
        from hermes_cli.model_switch import list_authenticated_providers
        from hermes_cli.model_switch_providers import list_picker_providers

        active_home = Path(os.environ["HERMES_HOME"])
        (active_home / "auth.json").write_text(
            json.dumps(
                {
                    "providers": {"nous": {"access_token": "fake"}},
                    "credential_pool": {},
                }
            )
        )
        with patch("hermes_cli.models.check_nous_free_tier", return_value=False), patch(
            "hermes_cli.models.union_with_portal_free_recommendations",
            side_effect=lambda ids, *a, **k: (ids, {}),
        ), patch(
            "hermes_cli.models.union_with_portal_paid_recommendations",
            side_effect=lambda ids, *a, **k: (ids, {}),
        ):
            expected = get_curated_nous_model_ids()
            full = list_picker_providers(current_provider="nous", max_models=None)
            one = list_picker_providers(current_provider="nous", max_models=1)
            # 0 is exercised on list_authenticated_providers (the slug-only
            # path); the picker variant drops empty-model rows entirely, so
            # the empty-list contract lives on the auth-providers call.
            zero = list_authenticated_providers(
                current_provider="nous", max_models=0
            )

        def _nous(rows):
            return next((r for r in rows if r["slug"] == "nous"), None)

        # Only meaningful when the curated list actually exceeds 1 entry.
        assert len(expected) > 1, "test needs a multi-model curated nous list"

        full_row = _nous(full)
        assert full_row is not None and full_row["models"] == expected

        # The Nous row is already curated, so an int cap never trims it (its free tier sits last).
        one_row = _nous(one)
        assert one_row is not None and one_row["models"] == expected

        zero_row = _nous(zero)
        # 0 means an empty model list — NOT unlimited. total_models still real.
        assert zero_row is not None
        assert zero_row["models"] == []
        assert zero_row["total_models"] == len(expected)


# -----------------------------------------------------------------------------
# Drift guard — prevent the in-repo curated lists from going out of sync with
# the docs-hosted manifest at website/static/api/model-catalog.json.
#
# History: qwen/qwen3.6-plus was added to _PROVIDER_MODELS["nous"] in commit
# 9dd6e5510 but website/static/api/model-catalog.json was not regenerated for
# weeks, so free-tier users on a new install fetched a stale manifest and the
# free-tier picker showed "No free models currently available." even though
# the Portal was serving qwen/qwen3.6-plus as free. CI must catch this.
# -----------------------------------------------------------------------------


def test_failed_self_hosted_catalog_backoff_is_profile_scoped(tmp_path, monkeypatch, isolated_home):
    from agent.secret_scope import reset_multiplex_context, set_multiplex_context
    from hermes_cli import model_catalog
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

    other = tmp_path / "other"
    other.mkdir()
    for home in (isolated_home, other):
        (home / "config.yaml").write_text(
            "model_catalog:\n  providers:\n    openrouter:\n      url: https://catalog.example.test/manifest\n",
            encoding="utf-8",
        )
    fetched = []

    def fetch(url, _timeout):
        assert url == "https://catalog.example.test/manifest"
        home = get_hermes_home()
        fetched.append(home)
        return _valid_manifest() if home == other else None

    monkeypatch.setattr(model_catalog, "_fetch_manifest", fetch)
    multiplex = set_multiplex_context(True)
    try:
        for home, expected in [(isolated_home, False), (other, True), (isolated_home, False)]:
            token = set_hermes_home_override(home)
            try:
                assert bool(model_catalog.get_curated_openrouter_models()) is expected
            finally:
                reset_hermes_home_override(token)
    finally:
        reset_multiplex_context(multiplex)
    assert fetched == [isolated_home, other]


class TestManifestMatchesInRepoLists:
    """Fail if the on-disk manifest is out of date relative to in-repo lists."""

    @staticmethod
    def _strip_volatile(catalog: dict) -> dict:
        """Drop fields that always change (timestamps) for diff comparison."""
        out = dict(catalog)
        out.pop("updated_at", None)
        return out

    def test_in_repo_lists_match_manifest(self):
        """``scripts/build_model_catalog.py`` output must match the committed file.

        If this fails, run ``python scripts/build_model_catalog.py`` and
        commit the regenerated ``website/static/api/model-catalog.json``.
        """
        # Resolve the repo root from this test file's location.
        repo_root = Path(__file__).resolve().parents[2]
        manifest_path = repo_root / "website" / "static" / "api" / "model-catalog.json"

        if not manifest_path.exists():
            pytest.skip(f"manifest missing at {manifest_path}")

        # Build expected catalog using the same script CI would.
        import importlib.util
        script_path = repo_root / "scripts" / "build_model_catalog.py"
        if not script_path.is_file():
            pytest.skip("exclude-development: the catalog generator is omitted")
        spec = importlib.util.spec_from_file_location("_build_model_catalog", script_path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        expected = mod.build_catalog()

        with open(manifest_path, encoding="utf-8") as fh:
            actual = json.load(fh)

        assert self._strip_volatile(actual) == self._strip_volatile(expected), (
            "website/static/api/model-catalog.json is out of sync with "
            "_PROVIDER_MODELS['nous'] / OPENROUTER_MODELS. "
            "Run: python scripts/build_model_catalog.py && "
            "git add website/static/api/model-catalog.json"
        )


class TestNoUpstreamCatalog:
    """Crema does not depend on upstream Hermes's servers: the model lists come from the in-repo lists
    (``OPENROUTER_MODELS``, ``_PROVIDER_MODELS``) and never from the Nous-hosted catalog, nor from a
    copy an earlier fetch left in ``<home>/cache/model_catalog.json``."""

    def test_default_config_names_no_catalog_url(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        assert "url" not in DEFAULT_CONFIG["model_catalog"]

    def test_picker_lists_open_no_catalog_url(self, isolated_home, monkeypatch):
        import urllib.request

        from hermes_cli import model_catalog, models

        opened: list[str] = []

        def record(req, *args, **kwargs):
            opened.append(getattr(req, "full_url", str(req)))
            raise OSError("offline")

        monkeypatch.setattr(urllib.request, "urlopen", record)
        cache = isolated_home / "cache"
        cache.mkdir()
        (cache / "model_catalog.json").write_text(json.dumps(_valid_manifest()))

        with patch("hermes_cli.models.fetch_openrouter_models"), \
             patch("hermes_cli.models.fetch_nous_recommended_models"):
            model_catalog.refresh_catalogs()
        assert model_catalog.get_curated_openrouter_models() is None
        assert models.get_curated_nous_model_ids() == list(models._PROVIDER_MODELS["nous"])
        assert models.get_preferred_silent_default_model("openrouter") == models.PREFERRED_SILENT_DEFAULT_MODEL
        assert opened == []
