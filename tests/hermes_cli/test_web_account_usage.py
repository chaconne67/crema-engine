"""Crema's usage chip: subscriptions get the provider's own usage windows, API keys are told to use session cost."""
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent import account_usage, anthropic_credentials
from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
from hermes_cli.web_routers import analytics


def _client():
    app = FastAPI()
    app.include_router(analytics.router)
    return TestClient(app)


def _snapshot(provider, windows, raw=None):
    return AccountUsageSnapshot(provider=provider, source="usage_api", fetched_at=datetime.now(timezone.utc),
                                windows=tuple(windows), raw=raw)


def test_codex_subscription_returns_windows(monkeypatch):
    reset = datetime(2026, 11, 4, 5, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(account_usage, "fetch_account_usage", lambda provider: _snapshot(
        provider, [AccountUsageWindow("Session", 28.0, reset), AccountUsageWindow("Weekly", 61.5, None)]))
    body = _client().get("/api/analytics/account-usage?provider=openai-codex").json()
    assert body == {"provider": "openai-codex", "subscription": True, "plan": None, "extra_usage": None, "windows": [
        {"label": "Session", "used_percent": 28.0, "reset_at": "2026-11-04T05:00:00+00:00"},
        {"label": "Weekly", "used_percent": 61.5, "reset_at": None}]}


def test_claude_oauth_is_subscription_with_extra_usage(monkeypatch):
    monkeypatch.setattr(anthropic_credentials, "resolve_anthropic_token", lambda: "oauth")
    monkeypatch.setattr(anthropic_credentials, "_is_oauth_token", lambda token: token == "oauth")
    extra = {"is_enabled": True, "used_credits": 3.2, "monthly_limit": 50, "currency": "USD"}
    monkeypatch.setattr(account_usage, "fetch_account_usage", lambda provider: _snapshot(
        provider, [AccountUsageWindow("Current session", 40.0)], raw={"extra_usage": extra}))
    body = _client().get("/api/analytics/account-usage?provider=anthropic").json()
    assert body["subscription"] is True
    assert body["windows"][0]["label"] == "Current session"
    assert body["extra_usage"] == extra

    extra["monthly_limit"] = None  # no known limit: not shown as $0.00
    assert _client().get("/api/analytics/account-usage?provider=anthropic").json()["extra_usage"] is None


def test_claude_api_key_is_per_token(monkeypatch):
    monkeypatch.setattr(anthropic_credentials, "resolve_anthropic_token", lambda: "sk-ant-api")
    monkeypatch.setattr(anthropic_credentials, "_is_oauth_token", lambda token: False)
    monkeypatch.setattr(account_usage, "fetch_account_usage", lambda provider: (_ for _ in ()).throw(AssertionError))
    assert _client().get("/api/analytics/account-usage?provider=anthropic").json() == {
        "provider": "anthropic", "subscription": False}


def test_api_key_providers_are_per_token():
    assert _client().get("/api/analytics/account-usage?provider=openrouter").json() == {
        "provider": "openrouter", "subscription": False}


def test_subscription_without_answer_has_no_windows(monkeypatch):
    monkeypatch.setattr(account_usage, "fetch_account_usage", lambda provider: None)
    body = _client().get("/api/analytics/account-usage?provider=openai-codex").json()
    assert body["subscription"] is True and body["windows"] == []
