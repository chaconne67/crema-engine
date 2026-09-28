"""Meaning search quality of the knowledge notebook with the bundled model, on a made-up Korean
workbook (tests/fixtures/knowledge_eval_ko.json: 220 pages of an imaginary small company, 180 questions
asked in the answer's words, in other words, or mixed). Needs the model: CI downloads Crema's release
asset (crema .github/workflows/engine-tests.yml) and names its folder in CREMA_TEST_EMBED_MODEL. The
floors sit a little under the rates measured when VECTOR_WEIGHT was chosen (Crema
docs/Crema-기억-3단계-뜻검색-계획-2026-09-28.md)."""
import json
import os
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest

from agent import knowledge_embed
from agent.knowledge_store import KnowledgeStore

REPO = Path(__file__).resolve().parent.parent.parent
SRC = REPO / "native" / "fts5_cjk" / "fts5_cjk.c"
WORKBOOK = json.loads((REPO / "tests" / "fixtures" / "knowledge_eval_ko.json").read_text(encoding="utf-8"))

# (kind, rank) -> lowest share of questions answered within that rank
FLOORS = {("all", 1): 0.66, ("all", 3): 0.82, ("same", 3): 0.96, ("other", 3): 0.68, ("mixed", 3): 0.90}


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    model = os.getenv("CREMA_TEST_EMBED_MODEL", "")
    if not model or not (Path(model) / "model.onnx").is_file():
        pytest.skip("no embedding model (CREMA_TEST_EMBED_MODEL)")
    if shutil.which("gcc") is None or not SRC.exists():
        pytest.skip("no C toolchain / tokenizer source")
    so = tmp_path_factory.mktemp("cjk") / "libfts5_cjk.so"
    subprocess.run(["gcc", "-shared", "-fPIC", "-O2", f"-I{SRC.parent / 'vendor'}", str(SRC), "-o", str(so)],
                   check=True, capture_output=True)
    mp = pytest.MonkeyPatch()
    mp.setenv("HERMES_FTS5_CJK_SO", str(so))
    knowledge_embed.set_embedder(knowledge_embed.Embedder(Path(model)))
    KnowledgeStore._shared.clear()
    s = KnowledgeStore.open(tmp_path_factory.mktemp("km") / "knowledge.db")
    for page in WORKBOOK["pages"]:
        s.write(page["slug"], title=page["title"], body=page["body"], sources=["test"])
    yield s
    KnowledgeStore._shared.clear()
    knowledge_embed.set_embedder(None)
    mp.undo()


def test_questions_in_other_words_find_their_page(store):
    assert store.vector_status()["vectors"] == store.vector_status()["chunks"]
    found = defaultdict(lambda: [0, 0, 0])
    for question in WORKBOOK["questions"]:
        slugs = [r["slug"] for r in store.search(question["queries"])]
        for kind in (question["kind"], "all"):
            found[kind][0] += 1
            found[kind][1] += slugs[:1] == [question["answer"]]
            found[kind][2] += question["answer"] in slugs[:3]
    rates = {(kind, rank): hits / n for kind, (n, top1, top3) in found.items() for rank, hits in ((1, top1), (3, top3))}
    print({f"{kind} top{rank}": f"{rate:.0%}" for (kind, rank), rate in sorted(rates.items())})
    for key, floor in FLOORS.items():
        assert rates[key] >= floor, f"{key[0]} top-{key[1]}: {rates[key]:.0%} < {floor:.0%}"
