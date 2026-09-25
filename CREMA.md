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
- `settings`: the four dashboard routers Crema uses — `oauth`, `config_env`, `models`, `audio`
  (`hermes_cli/web_routers/`). Auth: `X-Hermes-Session-Token: <token>`.

Both bind 127.0.0.1 only.

## What Crema offers

| Area | Kept | How it is chosen |
|---|---|---|
| Tools | files, terminal/process, todo, web search/extract, image understanding | `hermes-api-server` in `toolsets.py` |
| Providers | ChatGPT (openai-codex), Claude (anthropic, claude-code), OpenAI, Gemini, OpenRouter API keys | provider plugins kept below; the Crema app lists only these |

## Changes from upstream

1. `crema_engine.py` — new: the launcher above.
2. `toolsets.py` — `hermes-api-server` lists Crema's tools instead of the full core set.
3. Removed (not used by the engine; nothing kept imports them):
   - top level: `apps/ website/` (except `website/static/api/model-catalog.json`) `ui-tui/ web/ skills/ optional-skills/
     optional-mcps/ plugin-catalog/ evals/ scripts/ docker/ nix/ native/ tests-js/ contributors/`,
     Docker/Nix/npm/lint files, translated READMEs, `batch_runner.py mini_swe_runner.py mcp_serve.py
     toolset_distributions.py trajectory_compressor.py setup-hermes.sh`
   - `plugins/`: `disk-cleanup google_meet hermes-achievements kanban security-guidance spotify observability
     teams_pipeline platforms image_gen video_gen cron_providers`, the memory providers under `plugins/memory/`,
     and every model provider except `openai-codex anthropic gemini openrouter custom`

Kept code still mentions many removed features (browser, memory, delegation, …) through imports that
are either lazy or guarded; those modules stay until the code that names them is gone.

## Adding a feature back

1. Restore the upstream files at the same paths: `git checkout a25cf4d77d -- <path>` (or from a newer
   upstream commit after comparing with `git diff a25cf4d77d upstream/main -- <path>`).
2. Turn it on where it is chosen (table above).
3. Add a Crema CI check for it and list the change here.

## Updating from upstream

Compare only the kept paths: `git diff a25cf4d77d <new upstream commit> -- $(git ls-files)`,
take the security and bug fixes that touch them, move the base commit above, and run the tests.
