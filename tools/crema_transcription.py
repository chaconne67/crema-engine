"""Crema's PC recording selection, independent of Hermes' automatic voice setup."""

from agent.secret_scope import get_secret
from crema_startup import server_tools_use_desktop

CLOUD_KEYS = {"openai": "OPENAI_API_KEY", "groq": "GROQ_API_KEY"}
PICK_METHOD = "설정 › 부가 기능 연동 › 녹음 받아쓰기에서 방식을 먼저 골라 주세요."


def desktop_provider():
    """None leaves Hermes in charge. A PC always returns a selected backend or 'none'."""
    if not server_tools_use_desktop():
        return None
    from hermes_cli.config import read_raw_config

    crema = read_raw_config().get("crema")
    provider = crema.get("recordingTranscription") if isinstance(crema, dict) else None
    return provider if isinstance(provider, str) and (provider == "local" or provider in CLOUD_KEYS) else "none"


def desktop_cloud_key(provider):
    """Use the connection the user can see in Crema's settings."""
    return (get_secret(CLOUD_KEYS[provider]) or "").strip()
