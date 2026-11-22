"""Crema's Copilot provider never probes another CLI by default."""
from providers import get_provider_profile


def test_copilot_ambient_login_requires_opt_in(monkeypatch):
    from agent import credential_sources
    from plugins.model_providers.copilot import catalog, credentials

    for key in credentials.COPILOT_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    probes = []
    monkeypatch.setattr(credentials, "_try_gh_cli_token", lambda: probes.append("gh") or "ghu_offline_fixture")
    monkeypatch.setattr(credential_sources, "adopt_external_logins_enabled", lambda: False)
    assert credentials.resolve_copilot_token() == ("", "")
    assert catalog._copilot_cli_config_tokens() == []
    assert not get_provider_profile("copilot-acp").external_process_allowed()
    assert probes == []
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghu_explicit_fixture")
    assert credentials.resolve_copilot_token() == ("ghu_explicit_fixture", "COPILOT_GITHUB_TOKEN")
    assert probes == []
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN")
    monkeypatch.setattr(credential_sources, "adopt_external_logins_enabled", lambda: True)
    assert get_provider_profile("copilot-acp").external_process_allowed()
    assert credentials.resolve_copilot_token() == ("ghu_offline_fixture", "gh auth token")


def test_pool_seeding_honors_source_suppression_and_explicit_selection(monkeypatch):
    from plugins.model_providers.copilot import credentials

    profile = get_provider_profile("copilot")
    source = "env:COPILOT_GITHUB_TOKEN"
    actions = []

    def resolve():
        actions.append("resolve")
        return "test-token", "COPILOT_GITHUB_TOKEN"

    def exchange(token):
        actions.append("exchange")
        return "exchanged-token", "https://copilot.example"

    monkeypatch.setattr(credentials, "resolve_copilot_token", resolve)
    monkeypatch.setattr(credentials, "get_copilot_api_token", exchange)
    assert profile.seed_pool_credentials(
        suppressed_sources=frozenset(credentials._POOL_SOURCES), explicitly_configured=True,
    ) == []
    assert actions == []
    assert profile.seed_pool_credentials(suppressed_sources=frozenset({source}), explicitly_configured=True) == []
    assert actions == ["resolve"]
    actions.clear()
    assert profile.seed_pool_credentials(suppressed_sources=frozenset(), explicitly_configured=False) == [
        {"source": source, "access_token": "test-token", "base_url": None, "label": "COPILOT_GITHUB_TOKEN"},
    ]
    assert actions == ["resolve"]
    actions.clear()
    assert profile.seed_pool_credentials(suppressed_sources=frozenset(), explicitly_configured=True) == [
        {"source": source, "access_token": "exchanged-token", "base_url": "https://copilot.example",
         "label": "COPILOT_GITHUB_TOKEN"},
    ]
    assert actions == ["resolve", "exchange"]
