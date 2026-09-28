"""Crema's knowledge: what the agent learned while working — problems and their fixes, the user's
corrections, decisions and why, ways of working, what it looked up — as pages in knowledge.db next to
state.db. The method is GBrain's (github.com/garrytan/gbrain, MIT, e78f1c3 v0.59.0.0): slugged pages
with a compiled body and a dated timeline, whole-page writes that keep the previous version, content
hashes, soft delete, CJK-aware chunks, links extracted by rule, and hybrid ranking with reciprocal rank
fusion (src/core/search/hybrid.ts). Crema stores it in SQLite and ranks two keyword indexes (the
cjk_unicode61 tokenizer and trigram) where GBrain adds a vector index.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# Kinds of page, by slug prefix as in GBrain (the prefix is what classifies a page).
TYPES = ("project", "incident", "feedback", "reference", "decision")
AUTHORITIES = ("user_said", "agent_observed", "standing_instruction")
STATUSES = ("active", "needs_review", "superseded")

RRF_K = 60  # GBrain RRF_K
TITLE_BOOST = 1.25  # GBrain title phrase match
PURGE_AFTER_S = 72 * 3600  # GBrain soft delete window
STALE_AFTER_S = 90 * 86400  # not confirmed for this long -> needs review
CHUNK_WORDS, CHUNK_OVERLAP = 300, 50  # GBrain chunkers/recursive.ts
_STATUS_WEIGHT = {"active": 1.0, "needs_review": 0.8, "superseded": 0.5}

_CJK = re.compile(r"[ᄀ-ᇿ぀-ヿ㄰-㆏㐀-䶿一-鿿가-힯]")
_WIKI_LINK = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_TOKEN = re.compile(r"[\wᄀ-ᇿ㄰-㆏가-힯]+", re.UNICODE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
  id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE, type TEXT NOT NULL, title TEXT NOT NULL,
  body TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}', content_hash TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, confirmed_at REAL NOT NULL,
  last_retrieved_at REAL, deleted_at REAL);
CREATE TABLE IF NOT EXISTS page_versions (
  id INTEGER PRIMARY KEY, page_id INTEGER NOT NULL, title TEXT, type TEXT, body TEXT, meta TEXT,
  snapshot_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS timeline (
  id INTEGER PRIMARY KEY, page_id INTEGER NOT NULL, date TEXT NOT NULL, summary TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '', UNIQUE(page_id, date, summary));
CREATE TABLE IF NOT EXISTS links (
  from_id INTEGER NOT NULL, to_slug TEXT NOT NULL, UNIQUE(from_id, to_slug));
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY, page_id INTEGER NOT NULL, idx INTEGER NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS distilled (session_id TEXT PRIMARY KEY, messages INTEGER NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS maintenance (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def slugify(value: str) -> str:
    """GBrain's slugifySegment: accents off, Hangul kept (NFD then NFC), lower case, spaces to '-',
    '/' kept as the path separator."""
    parts = []
    for segment in str(value).strip().strip("/").split("/"):
        decomposed = unicodedata.normalize("NFD", segment)
        kept = "".join(c for c in decomposed if not (unicodedata.combining(c) and not _CJK.match(c)))
        segment = unicodedata.normalize("NFC", kept).lower()
        segment = re.sub(r"[^\w㄰-㆏가-힯-]+", "-", segment).strip("-")
        if segment:
            parts.append(segment)
    return "/".join(parts)


def _cjk_heavy(text: str) -> bool:
    letters = [c for c in text if not c.isspace()]
    return bool(letters) and sum(1 for c in letters if _CJK.match(c)) / len(letters) >= 0.3


def chunk_text(text: str, words: int = CHUNK_WORDS, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """GBrain's recursive chunker in short: paragraphs, then lines, then sentences, packed to about
    ``words`` words with ``overlap`` carried over; CJK-heavy text counts characters as words."""
    text = text.strip()
    if not text:
        return []
    cjk = _cjk_heavy(text)
    size = (lambda s: len(re.sub(r"\s", "", s))) if cjk else (lambda s: len(s.split()))
    pieces: List[str] = []
    for para in re.split(r"\n\s*\n", text):
        if size(para) <= words:
            pieces.append(para)
            continue
        for sentence in re.split(r"(?<=[.!?。！？\n])\s+", para):
            if size(sentence) <= words:
                pieces.append(sentence)
            elif cjk:  # no break left: cut by characters, else by words (GBrain's last resort)
                pieces.extend(sentence[i:i + words] for i in range(0, len(sentence), words))
            else:
                tokens = sentence.split()
                pieces.extend(" ".join(tokens[i:i + words]) for i in range(0, len(tokens), words))
    chunks: List[str] = []
    current: List[str] = []
    for piece in pieces:
        if current and size(" ".join(current + [piece])) > words:
            chunks.append("\n".join(current))
            tail = "\n".join(current)
            keep = tail[-overlap:] if cjk else " ".join(tail.split()[-overlap:])
            current = [keep] if keep else []
        current.append(piece)
    if current:
        chunks.append("\n".join(current))
    return [c.strip() for c in chunks if c.strip()]


def extract_links(body: str) -> List[str]:
    """Links by rule, no LLM (GBrain link-extraction.ts): [[slug]] and [text](slug) that are not URLs."""
    found = []
    for target in _WIKI_LINK.findall(body) + _MD_LINK.findall(body):
        target = target.strip()
        if target and "://" not in target and not target.startswith(("#", "mailto:")):
            slug = slugify(target)
            if slug and slug not in found:
                found.append(slug)
    return found


def _content_hash(title: str, type_: str, body: str, meta: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps([title, type_, body, meta], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _match_query(terms: Iterable[str]) -> str:
    """FTS5 query: every term quoted (no operators from user text), any of them may match."""
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


class KnowledgeStore:
    """One connection per database file, shared across the process under a re-entrant lock
    (the pattern of upstream's plugins/memory/holographic store): each chat's agent and the
    settings API use the same file."""

    _shared: Dict[str, "KnowledgeStore"] = {}
    _shared_lock = threading.Lock()

    @classmethod
    def open(cls, path: Optional[Path] = None) -> "KnowledgeStore":
        if path is None:
            from hermes_constants import get_hermes_home
            path = get_hermes_home() / "knowledge.db"
        key = str(Path(path).resolve())
        with cls._shared_lock:
            store = cls._shared.get(key)
            if store is None:
                store = cls._shared[key] = cls(Path(path))
            return store

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        try:
            from hermes_state_fts import load_fts5_cjk_extension
            self.cjk = load_fts5_cjk_extension(self.conn)
        except Exception:
            self.cjk = False
        self.conn.executescript(_SCHEMA)
        word = "cjk_unicode61" if self.cjk else "unicode61"
        existing = self.conn.execute("SELECT sql FROM sqlite_master WHERE name = 'chunks_word'").fetchone()
        if existing and word not in existing[0]:
            # The tokenizer became available (or went away): rebuild the word index with it.
            self.conn.execute("DROP TABLE chunks_word")
        self.conn.executescript(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_word USING fts5(text, content='chunks', content_rowid='id', tokenize='{word}');
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_tri USING fts5(text, content='chunks', content_rowid='id', tokenize='trigram');
        """)
        if not existing or word not in existing[0]:
            self.conn.execute("INSERT INTO chunks_word(chunks_word) VALUES ('rebuild')")

    # -- writing -------------------------------------------------------------------------

    def write(self, slug: str, *, title: str, body: str, type_: Optional[str] = None,
              sources: Optional[List[str]] = None, authority: str = "agent_observed",
              status: str = "active", tags: Optional[List[str]] = None) -> Dict[str, Any]:
        """Create or replace a whole page (GBrain put_page). The same content again is skipped; a
        replaced page keeps its previous version. ``sources`` say where it came from (a chat, a
        file, an address) and are required."""
        slug = slugify(slug)
        if "/" not in slug or slug.split("/", 1)[0] not in TYPES:
            raise ValueError(f"slug must start with one of {', '.join(t + '/' for t in TYPES)}")
        type_ = type_ or slug.split("/", 1)[0]
        if type_ not in TYPES:
            raise ValueError(f"type must be one of {', '.join(TYPES)}")
        if authority not in AUTHORITIES:
            raise ValueError(f"authority must be one of {', '.join(AUTHORITIES)}")
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        title, body = (title or "").strip(), (body or "").strip()
        sources = [s.strip() for s in (sources or []) if str(s).strip()]
        if not title or not body:
            raise ValueError("title and body are required")
        if not sources:
            raise ValueError("sources are required: where this was learned (a chat, a file, an address)")
        now = time.time()
        with self.lock:
            row = self.conn.execute("SELECT * FROM pages WHERE slug = ?", (slug,)).fetchone()
            meta = json.loads(row["meta"]) if row else {}
            meta.update({"authority": authority, "status": status, "tags": sorted(set(tags or meta.get("tags", [])))})
            meta["sources"] = list(dict.fromkeys(meta.get("sources", []) + sources))[-20:]
            digest = _content_hash(title, type_, body, {k: meta[k] for k in ("authority", "status")})
            if row and row["content_hash"] == digest and row["deleted_at"] is None:
                self.conn.execute("UPDATE pages SET confirmed_at = ?, meta = ? WHERE id = ?",
                                  (now, json.dumps(meta, ensure_ascii=False), row["id"]))
                return {"slug": slug, "result": "unchanged", "version": None}
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                version = None
                if row:
                    version = self.conn.execute(
                        "INSERT INTO page_versions (page_id, title, type, body, meta, snapshot_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (row["id"], row["title"], row["type"], row["body"], row["meta"], now)).lastrowid
                    self.conn.execute(
                        "UPDATE pages SET type = ?, title = ?, body = ?, meta = ?, content_hash = ?, updated_at = ?,"
                        " confirmed_at = ?, deleted_at = NULL WHERE id = ?",
                        (type_, title, body, json.dumps(meta, ensure_ascii=False), digest, now, now, row["id"]))
                    page_id = row["id"]
                else:
                    page_id = self.conn.execute(
                        "INSERT INTO pages (slug, type, title, body, meta, content_hash, created_at, updated_at, confirmed_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (slug, type_, title, body, json.dumps(meta, ensure_ascii=False), digest, now, now, now)).lastrowid
                self._index(page_id, title, body)
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        return {"slug": slug, "result": "updated" if row else "created", "version": version}

    def _index(self, page_id: int, title: str, body: str) -> None:
        old = self.conn.execute("SELECT id, text FROM chunks WHERE page_id = ?", (page_id,)).fetchall()
        for chunk in old:
            for table in ("chunks_word", "chunks_tri"):
                self.conn.execute(f"INSERT INTO {table}({table}, rowid, text) VALUES ('delete', ?, ?)", (chunk["id"], chunk["text"]))
        self.conn.execute("DELETE FROM chunks WHERE page_id = ?", (page_id,))
        timeline = "\n".join(f"{r['date']} {r['summary']}" for r in self.conn.execute(
            "SELECT date, summary FROM timeline WHERE page_id = ? ORDER BY date", (page_id,)))
        for idx, text in enumerate(chunk_text(f"{title}\n\n{body}") + chunk_text(timeline)):
            chunk_id = self.conn.execute("INSERT INTO chunks (page_id, idx, text) VALUES (?, ?, ?)", (page_id, idx, text)).lastrowid
            for table in ("chunks_word", "chunks_tri"):
                self.conn.execute(f"INSERT INTO {table}(rowid, text) VALUES (?, ?)", (chunk_id, text))
        self.conn.execute("DELETE FROM links WHERE from_id = ?", (page_id,))
        for target in extract_links(body):
            self.conn.execute("INSERT OR IGNORE INTO links (from_id, to_slug) VALUES (?, ?)", (page_id, target))

    def add_timeline(self, slug: str, summary: str, *, date: Optional[str] = None, source: str = "") -> Dict[str, Any]:
        """One dated line under a page (GBrain timeline); the same line twice is kept once."""
        slug, summary = slugify(slug), (summary or "").strip()
        if not summary:
            raise ValueError("summary is required")
        date = date or time.strftime("%Y-%m-%d")
        with self.lock:
            row = self._live(slug)
            added = self.conn.execute(
                "INSERT OR IGNORE INTO timeline (page_id, date, summary, source) VALUES (?, ?, ?, ?)",
                (row["id"], date, summary, source)).rowcount
            if added:
                self.conn.execute("UPDATE pages SET updated_at = ?, confirmed_at = ? WHERE id = ?", (time.time(), time.time(), row["id"]))
                self._index(row["id"], row["title"], row["body"])
        return {"slug": slug, "result": "added" if added else "unchanged"}

    def set_status(self, slug: str, status: str) -> Dict[str, Any]:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        with self.lock:
            row = self._live(slug)
            meta = json.loads(row["meta"])
            meta["status"] = status
            self.conn.execute("UPDATE pages SET meta = ?, confirmed_at = ? WHERE id = ?",
                              (json.dumps(meta, ensure_ascii=False), time.time(), row["id"]))
        return {"slug": row["slug"], "status": status}

    def delete(self, slug: str) -> Dict[str, Any]:
        """Soft delete: gone from search and lists, restorable for 72 hours (GBrain)."""
        with self.lock:
            row = self._live(slug)
            self.conn.execute("UPDATE pages SET deleted_at = ? WHERE id = ?", (time.time(), row["id"]))
        return {"slug": row["slug"], "result": "deleted"}

    def undo(self, slug: str) -> Dict[str, Any]:
        """Back one step: a deleted page comes back; a replaced page returns to its previous version;
        a page with no previous version is deleted (it was just created)."""
        slug = slugify(slug)
        with self.lock:
            row = self.conn.execute("SELECT * FROM pages WHERE slug = ?", (slug,)).fetchone()
            if row is None:
                raise ValueError(f"no page {slug}")
            if row["deleted_at"] is not None:
                self.conn.execute("UPDATE pages SET deleted_at = NULL WHERE id = ?", (row["id"],))
                return {"slug": slug, "result": "restored"}
            prev = self.conn.execute(
                "SELECT * FROM page_versions WHERE page_id = ? ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
            if prev is None:
                self.conn.execute("UPDATE pages SET deleted_at = ? WHERE id = ?", (time.time(), row["id"]))
                return {"slug": slug, "result": "deleted"}
            self.conn.execute("DELETE FROM page_versions WHERE id = ?", (prev["id"],))
            meta = json.loads(prev["meta"] or "{}")
            self.conn.execute(
                "UPDATE pages SET title = ?, type = ?, body = ?, meta = ?, content_hash = ?, updated_at = ? WHERE id = ?",
                (prev["title"], prev["type"], prev["body"], prev["meta"],
                 _content_hash(prev["title"], prev["type"], prev["body"], {k: meta.get(k) for k in ("authority", "status")}),
                 time.time(), row["id"]))
            self._index(row["id"], prev["title"], prev["body"])
        return {"slug": slug, "result": "reverted"}

    def _live(self, slug: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM pages WHERE slug = ? AND deleted_at IS NULL", (slugify(slug),)).fetchone()
        if row is None:
            raise ValueError(f"no page {slugify(slug)}")
        return row

    # -- reading -------------------------------------------------------------------------

    def _page(self, row: sqlite3.Row, *, full: bool) -> Dict[str, Any]:
        meta = json.loads(row["meta"])
        page = {
            "slug": row["slug"], "type": row["type"], "title": row["title"], "status": meta.get("status", "active"),
            "authority": meta.get("authority"), "sources": meta.get("sources", []), "tags": meta.get("tags", []),
            "updated": time.strftime("%Y-%m-%d", time.localtime(row["updated_at"])),
            "confirmed": time.strftime("%Y-%m-%d", time.localtime(row["confirmed_at"])),
        }
        if full:
            page["body"] = row["body"]
            page["timeline"] = [dict(r) for r in self.conn.execute(
                "SELECT date, summary, source FROM timeline WHERE page_id = ? ORDER BY date DESC", (row["id"],))]
            page["links"] = [r[0] for r in self.conn.execute("SELECT to_slug FROM links WHERE from_id = ?", (row["id"],))]
            page["backlinks"] = [r[0] for r in self.conn.execute(
                "SELECT p.slug FROM links l JOIN pages p ON p.id = l.from_id WHERE l.to_slug = ? AND p.deleted_at IS NULL",
                (row["slug"],))]
        return page

    def get(self, slug: str) -> Dict[str, Any]:
        with self.lock:
            row = self._live(slug)
            self.conn.execute("UPDATE pages SET last_retrieved_at = ? WHERE id = ?", (time.time(), row["id"]))
            return self._page(row, full=True)

    def list(self, type_: Optional[str] = None, include_deleted: bool = False) -> List[Dict[str, Any]]:
        where, args = ["1=1"], []
        if type_:
            where.append("type = ?")
            args.append(type_)
        if not include_deleted:
            where.append("deleted_at IS NULL")
        with self.lock:
            return [self._page(r, full=False) for r in self.conn.execute(
                f"SELECT * FROM pages WHERE {' AND '.join(where)} ORDER BY updated_at DESC", args)]

    def search(self, queries: List[str], limit: int = 8, type_: Optional[str] = None) -> List[Dict[str, Any]]:
        """GBrain's hybrid ranking over keyword lists: per query a word list (cjk_unicode61 BM25) and a
        substring list (trigram BM25), fused by reciprocal rank (k=60); then title-phrase, backlink and
        status weights; an exact slug or title goes first. Best chunk per page."""
        queries = [q.strip() for q in queries if q and q.strip()][:3]
        if not queries:
            return []
        with self.lock:
            fused: Dict[int, float] = {}
            best: Dict[int, tuple] = {}
            for query in queries:
                terms = _TOKEN.findall(query.lower())
                if not terms:
                    continue
                lists = [("chunks_word", _match_query(terms))]
                long_terms = [t for t in terms if len(t) >= 3]
                if long_terms:
                    lists.append(("chunks_tri", _match_query(long_terms)))
                for table, match in lists:
                    try:
                        rows = self.conn.execute(
                            f"SELECT c.id, c.page_id, snippet({table}, 0, '[', ']', '…', 16) AS snip, bm25({table}) AS rank"
                            f" FROM {table} JOIN chunks c ON c.id = {table}.rowid JOIN pages p ON p.id = c.page_id"
                            f" WHERE {table} MATCH ? AND p.deleted_at IS NULL {'AND p.type = ?' if type_ else ''}"
                            f" ORDER BY rank LIMIT 60", [match] + ([type_] if type_ else [])).fetchall()
                    except sqlite3.OperationalError:
                        continue
                    seen_pages = set()
                    for position, row in enumerate(rows):
                        if row["page_id"] in seen_pages:
                            continue
                        seen_pages.add(row["page_id"])
                        fused[row["page_id"]] = fused.get(row["page_id"], 0.0) + 1.0 / (RRF_K + position + 1)
                        if row["page_id"] not in best:
                            best[row["page_id"]] = (row["snip"], table)
            # Exact slug or title: first, as GBrain's exact-match promotion.
            exact = set()
            for query in queries:
                for row in self.conn.execute(
                        "SELECT id FROM pages WHERE deleted_at IS NULL AND (slug = ? OR lower(title) = lower(?))",
                        (slugify(query), query.strip())):
                    exact.add(row["id"])
                    fused.setdefault(row["id"], 0.0)
            if not fused:
                return []
            top = max(fused.values()) or 1.0
            results = []
            for page_id, score in fused.items():
                row = self.conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
                meta = json.loads(row["meta"])
                score = score / top
                title = row["title"].lower()
                if any(q.lower() in title for q in queries):
                    score *= TITLE_BOOST
                backlinks = self.conn.execute("SELECT COUNT(*) FROM links WHERE to_slug = ?", (row["slug"],)).fetchone()[0]
                score *= 1 + 0.05 * math.log(1 + backlinks)
                score *= _STATUS_WEIGHT.get(meta.get("status", "active"), 1.0)
                evidence = "exact" if page_id in exact else ("title" if any(q.lower() in title for q in queries)
                                                            else "keyword")
                page = self._page(row, full=False)
                page.update({"score": round(score + (10 if page_id in exact else 0), 4), "evidence": evidence,
                             "snippet": (best.get(page_id) or (row["body"][:160],))[0]})
                results.append(page)
            results.sort(key=lambda p: p["score"], reverse=True)
            results = results[:limit]
            # One step along the links of the top results, so related pages are in view.
            shown = {p["slug"] for p in results}
            for page in results[:3]:
                row = self._live(page["slug"])
                related = [r[0] for r in self.conn.execute(
                    "SELECT to_slug FROM links WHERE from_id = ? UNION SELECT p.slug FROM links l JOIN pages p ON p.id = l.from_id"
                    " WHERE l.to_slug = ? AND p.deleted_at IS NULL", (row["id"], row["slug"]))]
                page["related"] = [s for s in related if s not in shown][:5]
            return results

    # -- maintenance ---------------------------------------------------------------------

    def maintain(self, review: bool = True) -> Dict[str, int]:
        """The rule steps of GBrain's dream cycle, no AI: purge pages deleted over 72 hours ago and,
        with ``review``, mark pages unconfirmed for 90 days as needing review; count broken links and
        orphans. The purge always runs: a delete is restorable for 72 hours, not forever."""
        now = time.time()
        with self.lock:
            purged = [r[0] for r in self.conn.execute(
                "SELECT id FROM pages WHERE deleted_at IS NOT NULL AND deleted_at < ?", (now - PURGE_AFTER_S,))]
            for page_id in purged:
                for chunk in self.conn.execute("SELECT id, text FROM chunks WHERE page_id = ?", (page_id,)).fetchall():
                    for table in ("chunks_word", "chunks_tri"):
                        self.conn.execute(f"INSERT INTO {table}({table}, rowid, text) VALUES ('delete', ?, ?)", (chunk["id"], chunk["text"]))
                for table in ("chunks", "timeline", "page_versions"):
                    self.conn.execute(f"DELETE FROM {table} WHERE page_id = ?", (page_id,))
                self.conn.execute("DELETE FROM links WHERE from_id = ?", (page_id,))
                self.conn.execute("DELETE FROM pages WHERE id = ?", (page_id,))
            stale = 0
            for row in (self.conn.execute(
                    "SELECT id, meta FROM pages WHERE deleted_at IS NULL AND confirmed_at < ?",
                    (now - STALE_AFTER_S,)).fetchall() if review else []):
                meta = json.loads(row["meta"])
                if meta.get("status") == "active":
                    meta["status"] = "needs_review"
                    self.conn.execute("UPDATE pages SET meta = ? WHERE id = ?", (json.dumps(meta, ensure_ascii=False), row["id"]))
                    stale += 1
            broken = self.conn.execute(
                "SELECT COUNT(*) FROM links WHERE to_slug NOT IN (SELECT slug FROM pages WHERE deleted_at IS NULL)").fetchone()[0]
            orphans = self.conn.execute(
                "SELECT COUNT(*) FROM pages p WHERE deleted_at IS NULL AND NOT EXISTS (SELECT 1 FROM links WHERE from_id = p.id)"
                " AND NOT EXISTS (SELECT 1 FROM links WHERE to_slug = p.slug)").fetchone()[0]
            self.conn.execute("INSERT OR REPLACE INTO maintenance (key, value) VALUES ('last_rules', ?)", (str(now),))
        return {"purged": len(purged), "needs_review": stale, "broken_links": broken, "orphans": orphans}

    def distilled_count(self, session_id: str) -> int:
        with self.lock:
            row = self.conn.execute("SELECT messages FROM distilled WHERE session_id = ?", (session_id,)).fetchone()
        return row[0] if row else 0

    def maintenance_value(self, key: str) -> Optional[Any]:
        with self.lock:
            row = self.conn.execute("SELECT value FROM maintenance WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_maintenance_value(self, key: str, value: Any) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO maintenance (key, value) VALUES (?, ?)", (key, json.dumps(value)))

    def mark_distilled(self, session_id: str, messages: int) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO distilled (session_id, messages, at) VALUES (?, ?, ?)",
                              (session_id, messages, time.time()))

    def changed_since(self, since: float) -> List[Dict[str, Any]]:
        with self.lock:
            return [self._page(r, full=False) for r in self.conn.execute(
                "SELECT * FROM pages WHERE deleted_at IS NULL AND updated_at >= ? ORDER BY updated_at", (since,))]
