# Crema engine

The Crema engine is a fork of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
(MIT, see `LICENSE`). Crema bundles the engine source exported by Git.
Files keep their upstream paths and names so upstream changes can be compared and taken file by file.

- Upstream base: the commit pointed to by the `hermes-upstream` branch.
- Crema app: https://github.com/chaconne67/crema

## Rules for the Crema branch

- Keep only Hermes commits on `hermes-upstream` so Git can identify Crema's changes reliably.
- Keep upstream files in Git. Exclude unused files from the bundle or disable their features to avoid deletion conflicts when merging Hermes updates.
- When modifying an upstream file or adding a file, record what changed and why in `CREMA_CHANGES.tsv` and run `python scripts/check_crema_changes.py`. The record gives the next merge a reason to keep or reconsider each change.
- Add Crema features in new files wherever practical to reduce overlap with future Hermes changes.

### Feature inventory

`CREMA_FEATURES.tsv` records user- and operator-visible differences from the Hermes baseline, with
the behavior, evidence, verification steps and implementation status for each feature. Update it
when changing engine behavior or bundle exclusions and when reconciling a new Hermes baseline.
Run each relevant row's verification on the updated engine. The inventory check validates links and
paths. It does not execute the feature checks.

- Link changed files to the feature IDs in `CREMA_CHANGES.tsv` (`features`, comma-separated). Use
  `non-feature` with a `non_feature_reason` for maintenance-only changes.
- In `.gitattributes`, a `# crema-feature <id>` line assigns the consecutive `export-ignore` rules
  below it to that feature. For a test-only rule, use `# crema-feature non-feature <reason>`.
- The feature inventory's `tests` and `inspect` columns list comma-separated repository paths.
  `inspect` can name a code location as `path::symbol`. A dash marks an empty reference list.

## How Crema runs it

`python crema_engine.py` with the assembled `CREMA_ENGINE_INPUT`, `HERMES_HOME` (Crema's own engine folder, never the user's own Hermes)
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

Both PC listeners bind 127.0.0.1. Krema and Jabis run `hermes gateway run` with their own
assembled engine input. The app repository assembles the theme definitions in
`services/{crema,dev,fc}/service.yaml`. Krema selects its developer or staff settings for each
profile. The assembled input supplies chat and scheduled toolsets, plugins and hook order.

## What Crema offers

| Area | Kept | How it is chosen |
|---|---|---|
| Tools | files, terminal/process, todo, web search/extract, image understanding, memory, past-chat search, skills (the agent writes and improves them), a keyless backup ("백업해 줘"); image and video generation when the app's 부가 기능 연동 connects them | `hermes-api-server` in `toolsets.py`; the app adds `image_gen`/`video_gen` to `platform_toolsets.api_server` |
| Knowledge notebook | pages of what the agent learned working with the user, searched before work and written after (the method of GBrain, below); a quiet chat is distilled into it, and once a day while Crema is quiet the notebook is tidied | `knowledge` in `toolsets.py` (a direct surface in `tools/tool_search.py`, so never deferred); `knowledge.search_mode` light/balanced/thorough, `knowledge.nightly` |
| Past-chat search in Korean/CJK | the SQLite store uses word and trigram indexes with a SQL scan for short terms; startup prepares the indexes for chats saved by older releases | `crema_engine.py::index_past_chats`, `persistence/sqlite/conversation/_store/search.py` |
| Providers | the previous Crema provider set, with the new Hermes modules required by existing connections | provider profiles are discovered from the bundled paths and filtered by the application's connection settings |
| Logins | only those made in Crema: other programs' logins (Claude Code, Codex CLI, GitHub CLI) are never borrowed | `auth.adopt_external_logins` default |
| Shared files | chats that change the same file take turns: an order on the kanban board, a hand-over when the other chat is quiet and committed, else one judge call (the chat's model or `auxiliary.crema_turns`); a refused write waits for nothing, and the app starts the chat again | `agent/crema_file_turns.py`, `/api/crema/turns` |
| Approval words | the approval box explains held commands in Korean. The event translates recognized descriptions and shows a general warning for unknown ones. The original approval identifier stays in `pattern_key`. | `agent/crema_approval_words.py`, `gateway/platforms/api_server.py::_approval_request_event` |
| Messages between chats | a chat lists the user's other chats and sends one a message (the `crema_chats` tool): a replying chat reads it after its current tool step, an idle one when it next works, or now when the sender asks to wake it (the app starts it) | `crema_chats` in `toolsets.py`; `agent/crema_file_turns.py` notes, `/api/crema/chats` |
| Crema's manual | the built-in skill `crema-guide`: how Crema's screens, settings, engine, account and updates work, read by the agent when the user asks about Crema; each release ships its own, and an edited or deleted copy is put back at start | `skills/crema-guide/`, seeded by `crema_engine.py` |

## Changes from upstream

1. `crema_engine.py` — new: the launcher above (and indexing past chats for the CJK search at start).
2. `toolsets.py` — `hermes-api-server` lists Crema's tools instead of the full core set.
3. `hermes_cli/config_defaults.py` — `auth.adopt_external_logins` defaults to false: borrowing another
   program's rotating login can log that program out, and a Provider should appear only once added.
4. `plugins/model-providers/copilot/` — the GitHub CLI fallback and Copilot CLI model discovery follow
   `auth.adopt_external_logins`. Copilot ACP uses the same setting through its provider profile.
5. `pyproject.toml` — the `crema` extra (see below).
6. `persistence/` — the settings reader uses the same Korean search as the writer, including prefixes
   such as `세금*`. A word-index miss on one or two ASCII letters or digits, such as `5G`, retries
   at ASCII word boundaries. That retry and short CJK scans that replace the old bigram route
   omit tool output. Word-index hits and explicit tool-role searches can include tool output.
   Crema uses the storage interface for chat history, context estimates and scheduled chats.
7. `agent/learning_mutations.py` — Hermes now provides the shared lock for memory edits and deletes. Crema keeps a regression test for concurrent writes.
   Partial memory edits use Hermes's whitespace-preserving spans and reject overlapping matches.
8. `hermes_cli/backup.py`, `hermes_cli/subcommands/backup.py` — `--no-secrets` leaves out `.env`,
   `auth.json` and the credential vault. `tools/crema_backup_tool.py` (new, `backup` toolset in `hermes-api-server`)
   makes that backup when the user asks in the chat, in Documents\Crema 백업.
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
   - file/ pages (Crema's): a document or material the user has, with `meta.location` (a full path, or
     user@host:/path) — required for them, part of the content hash (a moved file is a new version), its
     file name indexed with the title; reads give `location` and, for a file on this computer, a file://
     `link` the app shows in its folder
   - the free plan (`crema.free`, set by the app from the account's plan; `tools/knowledge_tool.py` `plan_free`):
     the notebook stays readable — get, think, search by words — but `knowledge_write` refuses, a quiet chat is
     not distilled and search leaves out the meaning list (`KnowledgeStore.search(meaning=False)`)
10. Crema's working method and default persona:
   - `agent/prompt_builder.py` `CREMA_METHOD_GUIDANCE`: how the agent works (honesty, changing things safely,
     smallest implementation, one success path, problem solving, a message the user sends while it works —
     now, or after the current work when the user says so), in every prompt right after the identity,
     for every model, with or without tools; `agent/system_prompt.py` `_guidance_parts` puts it first. It
     replaces upstream `TASK_COMPLETION_GUIDANCE`, and its `agent.task_completion_guidance` switch is gone:
     the method is not the user's to turn off. How the agent speaks stays in SOUL.md, the persona the user edits.
   - `hermes_cli/default_soul.py`: the Crema default persona (seeded SOUL.md) with the answer style; an
     unedited earlier Crema default is upgraded in place
11. `hermes_cli/web_deps.py` `_server`: resolves a router helper's module through `importlib.import_module`
   instead of a plain `sys.modules` read, so a route called while another worker thread is still importing
   that module waits for it (the app's first `/api/env` after a start failed with "partially initialized
   module 'hermes_cli.web_server_messaging'"); test `tests/hermes_cli/test_web_deps_import_race.py`
12. Chats that change the same file take turns (Crema plan `Crema-대화협업-계획-2026-09-29.md`):
   - new: `agent/crema_file_turns.py` — the upstream kanban board (`hermes_cli/kanban_db.py`, kept whole; its
     plugin, dispatcher and worker tools stay unused) holds each chat's current work, the order between chats
     (links) and what happened (events); the rule runs in the `pre_tool_call` hook of `write_file`/`patch`,
     records written files in `post_tool_call` (after a terminal command: the files of the chat's folder whose content it changed in Git), tells a chat
     its notes in `pre_llm_call`; `tests/agent/test_crema_file_turns.py`
   - `crema_engine.py`: installs the hooks; `GET /api/crema/turns` (settle, then who waits, who the user must
     order, whose turn came), `POST /api/crema/turns/woken|order|release`
   - messages between chats (Crema plans `Crema-대화간-메시지-계획-2026-10-09.md`, Codex's way since
     `Crema-대화간-메시지-Codex방식-계획-2026-10-21.md`): new `tools/crema_chats_tool.py` (`crema_chats` toolset in
     `hermes-api-server`: list, send, read); a message waits in the notes of `agent/crema_file_turns.py` until the
     app hands it over (it starts an idle chat with it or puts it into a running reply); `POST /api/crema/chats`
     (the sidebar's chats, kept in `crema-chats.json`), `GET /api/crema/turns` also lists the messages to hand
     over, `POST /api/crema/turns/handed`
   - the files a reply wrote (Crema plan `Crema-파일-링크-계획-2026-10-09.md`): every write of `write_file`/`patch`
     (made or changed) and every file of the chat's folder whose content a terminal command changed in Git is a `crema_wrote` event;
     `GET /api/crema/files?session_id=&since=` lists those still there, for the app's line under the reply
13. Excluded from the Crema bundle because Crema does not use these Hermes features or development files.
   Git keeps the upstream copies to avoid deletion conflicts during merges. The `export-ignore` rules
   in `.gitattributes` specify what `git archive` leaves out.
   - top level: `apps/`, `website/` (except `website/static/api/model-catalog.json`), `ui-tui/`, `web/`,
     Hermes bundled skills (see item 14 for Crema's manual), `optional-skills/`, `optional-mcps/`,
     `plugin-catalog/`, `evals/`, `docker/`, `nix/`, `tests-js/`, `contributors/`,
     Docker/Nix/npm/lint files, translated READMEs, `batch_runner.py mini_swe_runner.py mcp_serve.py
     toolset_distributions.py trajectory_compressor.py setup-hermes.sh`
   - `tools/`: `xai_video_tools.py`
   - `plugins/`: `disk-cleanup google_meet hermes-achievements kanban security-guidance observability
     teams_pipeline platforms cron_providers`, the memory providers under `plugins/memory/`,
     and the model providers `bedrock vertex` (they need packages the bundle does not carry; the other
     providers were cut at first and restored on 2026-09-26)
   - Hermes release, install and development scripts. The bundled per-file runner
     `scripts/run_tests_parallel.py` retains its launch helpers and
     `scripts/ci/list_os_marked_tests.py` so the bundle can run its own test files.
   - Hermes v0.23.3's remote configuration plugin, standalone dashboard session-token
     provider, LM Studio, Solstice and Tencent TokenHub providers, and its `setup_beat`
     onboarding card tool. The Windows computer-use driver plugin and default delegation
     backend stay in the bundle because Hermes moved existing backends there.
   - Core modules imported by the Crema API, gateway or tool runtime stay in the bundle.
     This includes `tui_gateway` Python helpers used by settings routes and shared tool code.
     The theme input controls the tools sent to the model.
   - test files for excluded features, and files whose tests all failed only in Crema.
     Retained tests that still check an excluded feature are listed in the
     Crema repo's `scripts/engine-tests/known-pruned-failures.txt`, which the engine test comparison
     (`.github/workflows/engine-tests.yml` there) reports apart from new failures
   - brought back on 2026-09-27 (1a85b8bbf1): `tools/video_generation_tool.py` and the image/video backends
     Crema's connected Providers drive — `plugins/image_gen/` (openai-codex, openai, openrouter, xai) and
     `plugins/video_gen/` (openrouter, xai)
14. Crema's manual as a built-in skill (Crema plan `Crema-기본-설명서-스킬-계획-2026-10-11.md`):
   - new: `skills/crema-guide/` — `SKILL.md` (what Crema is, how to use the manual, which reference answers what)
     and `references/*.md` (screens, settings, models and connections, memory and the notebook, chats together,
     the engine, account/updates/data, troubleshooting), in Korean as the app is. A change to what users see or
     do in Crema (app or engine) updates it in the same work; the Crema plans' `crema/AGENTS.md` says so
   - `crema_engine.py`: `sync_skills()` (upstream `tools/skills_sync.py`, which only the gateway and the CLI
     called) at start, so the bundled skills reach `HERMES_HOME/skills`
   - `agent/skill_utils.py`: `crema-guide` in `ESSENTIAL_SKILLS` (never disabled or deleted by the agent)
   - `tools/skills_sync.py`: an essential skill's copy is compared with the bundled one on every sync and put
     back when edited or deleted — upstream keeps a user-modified bundled skill and stops updating it, which
     would freeze the manual at the first edit the agent makes; tests in `tests/tools/test_skills_sync.py`
15. (Removed 2026-11-13, Crema plan `Crema-음성입력-제거-계획-2026-11-13.md`: Thock and Crema are apart, so the
   `thock_heard` tool, its `thock` toolset and its place in `_DIRECT_SURFACE_TOOLSETS` are gone.)

16. Review prompts without Hermes' CLI (Crema plan `Crema-기본-설명서-스킬-계획-2026-10-11.md`):
   `agent/background_review.py`'s skill and combined review prompts name `crema-guide` as the bundled
   skill and reserve edits for agent-owned skills. Hub-installed, external, pinned, locked, and
   user-owned skills are protected by the review instructions. The skill tool blocks background
   writes to `crema-guide` even after the reviewer reads it.

17. Each run works in the folder Crema names: `POST /v1/runs` takes `cwd` (the chat's project folder, else the app's
   `Documents\Crema`; one that is not an existing absolute folder is a 400 `invalid_cwd`, never a fallback).
   `gateway/platforms/api_server_runs.py` binds it as the turn's session cwd (`_bind_api_server_session(cwd=)`
   in `gateway/platforms/api_server.py`, read by the system prompt's "Current working directory") and registers
   it as the session's terminal/file-tool cwd (`register_task_env_overrides`), so concurrent chats keep their own;
   tests in `tests/gateway/test_api_server_run_work_folder.py`

The prompt-cache duration defaults to five minutes. Marker caching stays enabled on supported
routes, and Crema skips extra model calls that would keep the cache warm, so `auto` stays at
five minutes. The Hermes request planner may split a system prompt with per-request instructions
into three blocks on some routes.

The server browser looks for a runnable `npx` in engine-managed npm, the legacy engine and
Homebrew locations, then the system PATH. Browser discovery, cache warming, and Chromium
installation and launch share the resolver. When the login vault is available, browser input
tools tell the model to call `browser_vault` through `tool_call`.

Messaging adapters remain outside the archive. Their declared secret environment variable names
stay in the child-process scrub policy. The full-checkout blocklist test checks the copy used
in the archive against the bundled platform manifests. Tests that need an omitted Telegram plugin, Vertex
profile, catalog generator, evaluation parser, or Desktop handoff run when the corresponding
files are available.

Running or testing the engine directly from a full Git checkout can discover components absent from
the bundle. Use an empty directory and run `git archive HEAD | tar -x -C <empty-directory>` to
prepare the bundled engine source before running or testing it.

- Spotify and more image/video backends load automatically. Platform adapters register lazily.
- Bedrock and Vertex register through model-provider discovery. Memory providers become discoverable.
- The xAI video edit and extend tools register. Exposure depends on toolset, provider and credentials.
- Hermes skills and catalogs become discoverable. The Hermes dashboard can discover Kanban and Achievements.

Kept code still mentions many unbundled features (browser, memory, delegation, …) through lazy or
guarded imports.

Known harmless log: the first Anthropic call logs "boto3 lazy install did not complete" once — the
Anthropic adapter imports `agent/bedrock_adapter.py`, which asks for boto3, and runtime installs are off.

## Adding a feature back

1. Remove the feature's `export-ignore` rules from `.gitattributes`. If a rule covers a whole
   directory, replace it with rules for the paths that should stay excluded.
2. Enable the feature in the relevant Crema configuration.
3. Test the feature from the exported source. Add a Crema CI check and record the change in
   `CREMA_CHANGES.tsv` and this document.

## Updating from upstream

The merge for Hermes v0.23.3 uses upstream commit `caafe5389ae585ef77387a962aee05b1bcf17029`
as its second parent. While the shared `hermes-upstream` branch still marks the old release,
point the local branch of that name at the new upstream commit in a separate shared clone.
Check out the update commit there and run `python3 scripts/check_crema_changes.py`.
Compare clean `git archive` bundles under the same theme inputs and run the linked feature
tests before accepting a new upstream base.
