"""The Anthropic model picker.

Upstream merged the curated ``_PROVIDER_MODELS["anthropic"]`` list ahead of the live
``/v1/models`` catalog, so a model the curated list lacked (claude-opus-5-5) sank to the
bottom and dotted aliases (claude-fable-5.1) appeared beside their live id. Crema lists the
account's live catalog alone, strongest first; the curated list only when it cannot be fetched.
"""

from unittest.mock import patch

from hermes_cli import models as M


def test_anthropic_lists_the_live_catalog_strongest_first():
    """Crema: the account's live /v1/models list is the picker, with no curated entries mixed in
    (a new model needs no catalog edit), ordered by family (Fable, Opus, Sonnet, Haiku) and then
    the higher version first."""
    live = [
        "claude-opus-5-5", "claude-fable-5-1", "claude-opus-5", "claude-sonnet-5",
        "claude-opus-4-5-20251101", "claude-haiku-4-5-20251001", "claude-future-9-99",
    ]
    with patch.object(M, "_fetch_anthropic_models", return_value=live):
        result = M.provider_model_ids("anthropic")

    assert result == [
        "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-opus-4-5-20251101",
        "claude-sonnet-5", "claude-haiku-4-5-20251001", "claude-future-9-99",
    ]


def test_anthropic_falls_back_to_curated_when_live_unavailable():
    """No creds / live failure -> the curated list, in the same order."""
    with patch.object(M, "_fetch_anthropic_models", return_value=None):
        result = M.provider_model_ids("anthropic")

    assert result == sorted(M._PROVIDER_MODELS["anthropic"], key=M._anthropic_rank)
    assert result[:3] == ["claude-fable-5.1", "claude-fable-5", "claude-opus-5"]
