"""Meaning search for the knowledge notebook (agent/knowledge_store.py): a small Korean embedding model
run on the user's PC with onnxruntime, tokenizers and numpy — no network, no model API.

The model is Crema's int8 build of exp-models/dragonkue-KoEn-E5-Tiny (Apache-2.0; see the Crema repo's
scripts/embedding-model/build.py): mean pooling over the last hidden state, then L2 normalization;
queries take the prefix "query: ", passages "passage: ". Crema names its folder (model.onnx,
tokenizer.json) in CREMA_EMBED_MODEL. Without it, or when it cannot be loaded, get_embedder() returns
None and the notebook searches by words only.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Stored with every vector: a different model means the vectors are computed again.
MODEL_ID = "koen-e5-tiny-int8@292c09c"
DIM = 384
MAX_TOKENS = 512
BATCH = 4  # small batches: less padding and a lower memory peak (measured: 16 -> 437MB, 4 -> 238MB)

_lock = threading.Lock()
_embedder: "Optional[Embedder]" = None
_failed = False


class Embedder:
    def __init__(self, directory: Path) -> None:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.np = np
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tokenizer.enable_truncation(MAX_TOKENS)
        self.tokenizer.enable_padding()
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 2) // 2))
        self.session = ort.InferenceSession(str(directory / "model.onnx"), options, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}

    def encode(self, texts: List[str], kind: str = "passage"):
        """Unit vectors (n, DIM) float32 for ``texts``; kind is "query" or "passage"."""
        np = self.np
        prefix = "query: " if kind == "query" else "passage: "
        out = []
        for start in range(0, len(texts), BATCH):
            batch = self.tokenizer.encode_batch([prefix + t for t in texts[start:start + BATCH]])
            ids = np.array([e.ids for e in batch], dtype=np.int64)
            mask = np.array([e.attention_mask for e in batch], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.inputs:
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = self.session.run(None, feeds)[0]
            weights = mask[..., None].astype(np.float32)
            pooled = (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-9, None)
            pooled /= np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)
            out.append(pooled.astype(np.float32))
        return np.concatenate(out) if out else np.zeros((0, DIM), dtype=np.float32)


def get_embedder() -> Optional[Embedder]:
    """The loaded model, loading it on first use; None when it is not there or does not load."""
    global _embedder, _failed
    if _embedder is not None or _failed:
        return _embedder
    with _lock:
        if _embedder is None and not _failed:
            folder = os.getenv("CREMA_EMBED_MODEL", "")
            try:
                if not folder or not (Path(folder) / "model.onnx").exists():
                    raise FileNotFoundError(folder or "CREMA_EMBED_MODEL is not set")
                _embedder = Embedder(Path(folder))
            except Exception as exc:
                _failed = True
                logger.info("knowledge meaning search off: %s", exc)
    return _embedder


def available() -> bool:
    """Whether meaning search is on, without loading the model."""
    return _embedder is not None or (not _failed and (Path(os.getenv("CREMA_EMBED_MODEL", "") or ".") / "model.onnx").is_file())


def set_embedder(embedder) -> None:
    """Tests: use ``embedder`` (anything with encode(texts, kind) giving unit vectors); None resets."""
    global _embedder, _failed
    _embedder, _failed = embedder, False
