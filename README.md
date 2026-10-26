# Finland Today

A self-hosted, single-user Finnish news aggregator. It pulls RSS feeds from Finnish outlets, removes duplicates, groups stories about the same event, translates and tags them with an LLM, and lets you read them through saved views and a daily digest.

The full design and build order are in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## Status

- [x] Phase 0: project setup and initial schema
- [x] Seed: five outlets, starter tags, category mappings
- [x] Phase 1: fetch (`make run-worker STAGE=fetch`)
- [x] Phase 2: normalize and exact dedupe
- [x] Phase 3: embeddings (`make run-worker STAGE=embed`)
- [x] Phase 4: clustering (`make run-worker STAGE=cluster`). Pairs with cosine 0.80 or more merge. Borderline pairs (0.60 to 0.80) go to Claude's same-event check, or start new clusters when no API key is set. The hidden page `/admin/clusters` lists recent multi-article stories with each member's similarity to the cluster centre, and splits a wrongly merged article out into a story of its own.
- [x] Phase 5: enrichment and tags (`make run-worker STAGE=tag`, `make run-worker STAGE=enrich`). The first live run translated all articles and wrote all cluster headlines. Tags carry definitions in the prompt, and every article now gets at least one tag and an English summary wherever the Finnish one has a summary. Crime tags fell from 29 to 21, but two accidents are still tagged crime.
- [x] Phase 6: web app. The feed with saved views, cluster pages, tag and keyword-rule management, source health, and search. Writes are HTMX requests, so a plain cross-site form post is refused.
- [ ] Phase 7 onward: see the plan

Open the app at `http://localhost:5000`, or `http://localhost:8080` when `WEB_HOST_PORT` is set to 8080. The feed, tags, views, sources and search all update in place with HTMX, so switching a view or adding a tag does not reload the page.

The web app has no login. Put it behind Caddy basic auth or Tailscale before it leaves your machine. Styling is hand-written in `web/static/app.css`. Tailwind is not used yet, so `make css` does nothing useful.

Today the fetch stage downloads the five feeds, stores new and changed articles by canonical URL, and records each attempt in `fetch_log`. The embed stage gives each article a 384-dimension vector of its title and summary, using a multilingual model baked into the worker image. The cluster stage groups articles about the same event. Its thresholds were calibrated on hand-labelled pairs in [tests/fixtures/cluster_calibration.csv](tests/fixtures/cluster_calibration.csv). Re-check them if the embedding model changes.

The tag stage adds tags from feed categories and keyword rules, with no LLM. It tags each article once (`tagged_at`), so a tag you remove in the web app stays removed. A new keyword rule reaches older articles through the 30-day backfill when you save it. The enrich stage uses Claude (`LLM_MODEL`, default `claude-haiku-5-5`) to translate each article into English, add up to three tags from the starter list, and write one English headline per cluster. The cluster stage also asks Claude to confirm borderline pairs when a key is set. Without a key, enrich stops and cluster starts new clusters for borderline pairs.

## Quick start

You need Docker, [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv can install it).

```bash
cp .env.example .env        # set POSTGRES_PASSWORD, and use the same password in DATABASE_URL
make install                # creates .venv with dev dependencies
make up                     # starts Postgres 16 with pgvector
make migrate                # applies migrations/*.sql
make psql                   # open a psql shell on the dev database
```

`make up` starts the database and the web app. The worker runs one stage at a time with `make run-worker STAGE=...`, so it has no long-running container yet.

## Commands

| Command | What it does |
|---|---|
| `make install` | Create `.venv` and install the project with dev dependencies |
| `make up` / `make down` | Start or stop the dev database and the web app |
| `make migrate` | Apply pending migrations (safe to re-run) |
| `make psql` | Open a psql shell on the dev database |
| `make test` | Run the test suite |
| `make lint` | Run ruff |
| `make css` | Build Tailwind CSS (needs `web/static/input.css`, phase 6) |
| `make run-worker STAGE=fetch` | Download feeds and store new articles (phases 1-2) |
| `make run-worker STAGE=embed` | Embed every article that has no vector yet (phase 3) |
| `make run-worker STAGE=cluster` | Group new articles into events (phase 4). Uses Claude for borderline pairs when `ANTHROPIC_API_KEY` is set |
| `make run-worker STAGE=tag` | Add tags from feed categories and keyword rules to new articles (phase 5, no LLM) |
| `make run-worker STAGE=enrich` | Translate and tag new articles, and write cluster headlines with Claude (phase 5). Needs `ANTHROPIC_API_KEY` |

The migration database test is skipped unless `TEST_DATABASE_URL` is set. It creates and drops its own database, so the role needs `CREATEDB`. Use the port from `DB_HOST_PORT`:

```bash
TEST_DATABASE_URL=postgresql://ft:change-me@localhost:5432/ft make test
```

## Configuration

Settings come from `.env`. [.env.example](.env.example) lists them all.

- `DB_HOST_PORT` sets the host port for the dev database (default `5432`). Change it if something else already uses that port.
- `WEB_HOST_PORT` sets the host port for the dev web app (default `5000`). On macOS the AirPlay Receiver already uses 5000, so set it to `8080` there.
- `COMPOSE_PROFILES=prod` on the server starts Caddy. Leave it unset locally.
- `EMBED_MODEL` and `EMBED_DIM` pick the embedding model. `EMBED_DIM` must equal the `vector(384)` size in the schema. Changing the model means a new migration and re-embedding every article.
- `EMBED_CACHE_DIR` is where the model weights are stored. The worker image sets it to `/opt/models`, so containers never download the model at start. Leave it unset elsewhere.
- `ANTHROPIC_API_KEY` enables the LLM steps: enrich, and the same-event check in cluster. Keep the key in `.env`, which is never committed.
- `LLM_MODEL` picks the Claude model. The default is `claude-haiku-5-5`.
- `LLM_MAX_ARTICLES_PER_RUN` caps how many articles one enrich run sends to the model (default 500). Unfinished articles are picked up by the next run.

## Layout

```
app/          shared config and the migration runner (python -m app.migrate)
migrations/   numbered SQL files, applied in order
worker/       fetch, normalize, dedupe, embed, cluster, tagging, enrich (phases 1-5); digest (later)
web/          Flask + HTMX app: routes/, queries.py (read SQL), templates/, static/ (phase 6)
docker/       Dockerfiles for the web and worker images
tests/        pytest suite
```

Store only titles, short summaries and links. Full article text is never stored.

The worker and the web app never talk to each other directly. Postgres is the only link between them.
