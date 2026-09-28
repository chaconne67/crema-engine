# Crema engine

This branch (`crema`) is the engine bundled inside the Crema desktop app: a pruned fork of
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT, see `LICENSE`).
Files keep their upstream paths and names so upstream changes can be compared and taken file by file.

- Upstream base: `a25cf4d77d46733767b91d5a410e701f4596541e` (hermes-agent 0.21.4)
- Crema app: https://github.com/chaconne67/crema

## How Crema runs it

`python crema_engine.py` with `HERMES_HOME` (Crema's own engine folder, never the user's own Hermes)
and `HERMES_DASHBOARD_SESSION_TOKEN` in the environment. It prints one line, `{"api": port, "settings": port}`:

- `api`: the Hermes run API (`gateway/platforms/api_server.py`) without the gateway — `/health`,
  `/v1/models`, `/v1/runs` (events, approval, stop), `/api/sessions`, `/api/model/options`.
  Auth: `Authorization: Bearer <token>`.
- `settings`: the dashboard routers Crema uses — `oauth`, `config_env`, `models`, `audio`
  (`hermes_cli/web_routers/`) — plus four routes of others: `/api/learning/graph` and
  `/api/learning/node` (what the agent remembers and the skills it learned, to read, edit and
  delete), `/api/sessions/search`, `/api/tools/toolsets/{name}/models`; and Crema's own
  `POST /api/crema/backup` (the engine folder into a .zip, without API keys and sign-in tokens),
  `/api/crema/knowledge` (the knowledge notebook: list, page, edit, delete, undo) and
  `POST /api/crema/distill` (a quiet chat's lessons into the notebook).
  Auth: `X-Hermes-Session-Token: <token>`.

Both bind 127.0.0.1 only.

## What Crema offers

| Area | Kept | How it is chosen |
|---|---|---|
| Tools | files, terminal/process, todo, web search/extract, image understanding, memory, past-chat search, skills (the agent writes and improves them) | `hermes-api-server` in `toolsets.py` |
| Knowledge notebook | pages of what the agent learned working with the user, searched before work and written after (the method of GBrain, below); a quiet chat is distilled into it, and once a day while Crema is quiet the notebook is tidied | `knowledge` in `toolsets.py` (a direct surface in `tools/tool_search.py`, so never deferred); `knowledge.search_mode` light/balanced/thorough, `knowledge.nightly` |
| Past-chat search in Korean/CJK | the `cjk_unicode61` tokenizer (`native/fts5_cjk/`), built by Crema as `fts5_cjk.dll` and named in `HERMES_FTS5_CJK_SO`; chats saved before it are indexed when the engine starts | `crema_engine.py` |
| Providers | every upstream provider that needs no extra package (Bedrock needs boto3 and Vertex google-auth, so both are left out) | provider plugins kept below; the Crema app hides only `moa` and `claude-code` |
| Logins | only those made in Crema: other programs' logins (Claude Code, Codex CLI, GitHub CLI) are never borrowed | `auth.adopt_external_logins` default |

## Changes from upstream

1. `crema_engine.py` — new: the launcher above (and indexing past chats for the CJK search at start).
2. `toolsets.py` — `hermes-api-server` lists Crema's tools instead of the full core set.
3. `hermes_cli/config_defaults.py` — `auth.adopt_external_logins` defaults to false: borrowing another
   program's rotating login can log that program out, and a Provider should appear only once added.
4. `hermes_cli/copilot_auth.py` — the `gh auth token` fallback follows `auth.adopt_external_logins` too.
5. `pyproject.toml` — the `crema` extra (see below).
6. `hermes_state.py` — a read-only attach also loads the CJK tokenizer and serves the CJK index when
   it is complete, as the writer does; without it the dashboard's prefix search ("세금*") found nothing.
7. `agent/learning_mutations.py` — a user's memory edit or delete holds the memory tool's file lock.
8. `hermes_cli/backup.py`, `hermes_cli/subcommands/backup.py` — `--no-secrets` leaves out `.env`,
   `auth.json` and the credential vault.
9. The knowledge notebook (Crema's; the method of GBrain, github.com/garrytan/gbrain, MIT, at
   `e78f1c3` v0.59.0.0 — page model, whole-page writes with versions, content hashes, soft delete,
   CJK-aware chunking, rule-based links, reciprocal rank fusion with title/backlink weights, the
   brain-first instructions; tracked in the Crema plans' `Crema-GBrain-추적.md`):
   - new: `agent/knowledge_store.py` (SQLite `knowledge.db`), `tools/knowledge_tool.py` (the tools and
     the always-on instructions), `tests/agent/test_knowledge_*.py`, `tests/tools/test_knowledge_tool.py`,
     `tests/test_crema_engine_knowledge.py`
   - `toolsets.py`: the `knowledge` toolset, in `hermes-api-server`; `tools/tool_search.py`: `knowledge` is a
     direct surface (never deferred behind tool_search)
   - `agent/system_prompt.py`: the notebook's instructions in the tool guidance when its tools are loaded
   - `tools/memory_tool.py`, `agent/background_review.py`: memories in the language the user writes in
   - `hermes_cli/config_defaults.py`: `memory.nudge_interval` 0 — a quiet chat's distillation reviews
     memory instead of every tenth turn
   - `crema_engine.py`: the notebook routes, `distill` (the background review on one chat, focused on
     the notebook, with the knowledge tools admitted through `extra_tools`) and the daily pass
   - meaning search (GBrain's vector index, run locally): `agent/knowledge_embed.py` (Crema's int8
     KoEn-E5-Tiny under `CREMA_EMBED_MODEL`, onnxruntime + tokenizers + numpy in the `crema` extra),
     chunk vectors in `knowledge.db`, a weighted meaning list in the fusion, words only without the
     model; `crema_engine.py` fills missing vectors at start; the quality floor
     `tests/agent/test_knowledge_meaning_quality.py` on `tests/fixtures/knowledge_eval_ko.json` runs when
     `CREMA_TEST_EMBED_MODEL` names the model (the Crema engine test workflow downloads it)
10. Removed (not used by the engine; nothing kept imports them):
   - top level: `apps/ website/` (except `website/static/api/model-catalog.json`) `ui-tui/ web/ skills/ optional-skills/
     optional-mcps/ plugin-catalog/ evals/ scripts/ docker/ nix/ native/ (except native/fts5_cjk/) tests-js/ contributors/`,
     Docker/Nix/npm/lint files, translated READMEs, `batch_runner.py mini_swe_runner.py mcp_serve.py
     toolset_distributions.py trajectory_compressor.py setup-hermes.sh`
   - `tools/`: `xai_video_tools.py video_generation_tool.py` (their `plugins/video_gen/` was removed)
   - `plugins/`: `disk-cleanup google_meet hermes-achievements kanban security-guidance spotify observability
     teams_pipeline platforms image_gen video_gen cron_providers`, the memory providers under `plugins/memory/`,
     and the model providers `bedrock vertex` (they need packages the bundle does not carry; the other
     providers were cut at first and restored on 2026-09-26)
   - `scripts/` except `run_tests_parallel.py`, upstream's per-file test runner the tests rely on for
     isolation: run the tests with `python scripts/run_tests_parallel.py`, not bare pytest
   - the test files of removed features (every test in them failed only here, or their subject is a
     removed feature). The tests of kept files that still check a removed feature are listed in the
     Crema repo's `scripts/engine-tests/known-pruned-failures.txt`, which the engine test comparison
     (`.github/workflows/engine-tests.yml` there) reports apart from new failures

Kept code still mentions many removed features (browser, memory, delegation, …) through imports that
are either lazy or guarded; those modules stay until the code that names them is gone.

Known harmless log: the first Anthropic call logs "boto3 lazy install did not complete" once — the
Anthropic adapter imports `agent/bedrock_adapter.py`, which asks for boto3, and runtime installs are off.

## Adding a feature back

1. Restore the upstream files at the same paths: `git checkout a25cf4d77d -- <path>` (or from a newer
   upstream commit after comparing with `git diff a25cf4d77d upstream/main -- <path>`).
2. Turn it on where it is chosen (table above).
3. Add a Crema CI check for it and list the change here.

## Updating from upstream

Compare only the kept paths: `git diff a25cf4d77d <new upstream commit> -- $(git ls-files)`,
take the security and bug fixes that touch them, move the base commit above, and run the tests.
