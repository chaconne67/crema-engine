"""Tests for the unified ``video_generate`` tool dispatch surface."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from agent import video_gen_registry
from agent.video_gen_provider import VideoGenProvider


@pytest.fixture(autouse=True)
def _reset_registry():
    video_gen_registry._reset_for_tests()
    yield
    video_gen_registry._reset_for_tests()


@pytest.fixture(autouse=True)
def _no_download(monkeypatch):
    """No network in these tests: keeping a result's video locally fails, so its link stands."""
    def fail(url, **_):
        raise RuntimeError("no network in tests")
    monkeypatch.setattr("agent.video_gen_provider.save_url_video", fail)


class _RecordingProvider(VideoGenProvider):
    """Captures the kwargs the tool layer hands it."""

    def __init__(self, name: str = "fake"):
        self._name = name
        self.last_kwargs: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return self._name

    def list_models(self) -> List[Dict[str, Any]]:
        return [{"id": "model-a"}]

    def default_model(self) -> Optional[str]:
        return "model-a"

    def capabilities(self) -> Dict[str, Any]:
        return {"modalities": ["text", "image"]}

    def generate(self, prompt, **kwargs):
        self.last_kwargs = {"prompt": prompt, **kwargs}
        modality = "image" if kwargs.get("image_url") else "text"
        return {
            "success": True,
            "video": "https://example.com/v.mp4",
            "model": kwargs.get("model") or "model-a",
            "prompt": prompt,
            "modality": modality,
            "aspect_ratio": kwargs.get("aspect_ratio", ""),
            "duration": kwargs.get("duration") or 0,
            "provider": self._name,
        }


class _RaisingProvider(VideoGenProvider):
    @property
    def name(self) -> str:
        return "raises"

    def generate(self, prompt, **kwargs):
        raise RuntimeError("boom")


class TestUnifiedDispatch:
    def _run(self, args: Dict[str, Any], *, configured: Optional[str] = None) -> Dict[str, Any]:
        from tools import video_generation_tool
        import hermes_cli.plugins as plugins_module

        saved = video_generation_tool._read_configured_video_provider
        video_generation_tool._read_configured_video_provider = lambda: configured  # type: ignore
        saved_discover = plugins_module._ensure_plugins_discovered
        plugins_module._ensure_plugins_discovered = lambda *_a, **_k: None  # type: ignore
        try:
            raw = video_generation_tool._handle_video_generate(args)
        finally:
            video_generation_tool._read_configured_video_provider = saved  # type: ignore
            plugins_module._ensure_plugins_discovered = saved_discover  # type: ignore
        return json.loads(raw)

    def test_no_provider_returns_clear_error(self):
        result = self._run({"prompt": "a dog"})
        assert result["success"] is False
        assert result["error_type"] == "no_provider_configured"

    def test_unknown_provider_returns_clear_error(self):
        result = self._run({"prompt": "a dog"}, configured="ghost")
        assert result["success"] is False
        assert result["error_type"] == "provider_not_registered"




    def test_upscale_in_schema_and_forwarded(self):
        """`upscale` is advertised per-capability by the dynamic builder
        (#95681 diet — static schema no longer carries it) and forwarded
        to providers when set, omitted (not None) when unset."""
        provider = _RecordingProvider()
        video_gen_registry.register_provider(provider)
        result = self._run({"prompt": "a dog", "upscale": True}, configured="fake")
        assert result["success"] is True
        assert provider.last_kwargs["upscale"] is True

        self._run({"prompt": "a dog"}, configured="fake")
        assert "upscale" not in provider.last_kwargs


def test_a_video_at_a_web_link_is_kept_locally_with_the_link_as_public_url(monkeypatch, tmp_path):
    """Crema plays a kept file in place (MEDIA:<path>); the link stays for edit/extend."""
    from tools import video_generation_tool as vgt

    kept = tmp_path / "video_1.mp4"
    calls = []

    def save(url, **_):
        calls.append(url)
        kept.write_bytes(b"mp4")
        return kept

    monkeypatch.setattr("agent.video_gen_provider.save_url_video", save)
    provider = _RecordingProvider()
    monkeypatch.setattr(vgt, "_resolve_active_provider", lambda: provider)
    result = json.loads(vgt._handle_video_generate({"prompt": "a cat"}))
    assert calls == ["https://example.com/v.mp4"]
    assert result["video"] == str(kept)
    assert result["public_url"] == "https://example.com/v.mp4"

