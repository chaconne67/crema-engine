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
