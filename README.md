# Finland Today

A self-hosted, single-user Finnish news aggregator. It pulls RSS feeds from Finnish outlets, removes duplicates, groups stories about the same event, translates and tags them with an LLM, and lets you read them through saved views and a daily digest.

The full design and build order are in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## Status

- [x] Phase 0: project setup and initial schema
- [x] Seed: five outlets, starter tags, category mappings
- [x] Phase 1: fetch (`make run-worker STAGE=fetch`)
- [x] Phase 2: normalize and exact dedupe
- [x] Phase 3: embeddings (`make run-worker STAGE=embed`)
- [x] Phase 4: clustering (`make run-worker STAGE=cluster`). Pairs with cosine 0.80 or more merge. Borderline pairs (0.60 to 0.80) start new clusters until the Phase 5 same-event check exists.
- [ ] Phase 5 onward: see the plan

The web app and the later worker stages are not written yet. Today the fetch stage downloads the five feeds, stores new and changed articles by canonical URL, and records each attempt in `fetch_log`. The embed stage gives each article a 384-dimension vector of its title and summary, using a multilingual model baked into the worker image. The cluster stage groups articles about the same event. Its thresholds were calibrated on hand-labelled pairs in [tests/fixtures/cluster_calibration.csv](tests/fixtures/cluster_calibration.csv). Re-check them if the embedding model changes.

## Quick start

You need Docker, [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv can install it).

```bash
cp .env.example .env        # set POSTGRES_PASSWORD, and use the same password in DATABASE_URL
make install                # creates .venv with dev dependencies
make up                     # starts Postgres 16 with pgvector
make migrate                # applies migrations/*.sql
make psql                   # open a psql shell on the dev database
```

`make up` starts only the database. The web and worker services need code from later phases before they can run.

## Commands

| Command | What it does |
|---|---|
| `make install` | Create `.venv` and install the project with dev dependencies |
| `make up` / `make down` | Start or stop the dev database |
| `make migrate` | Apply pending migrations (safe to re-run) |
| `make psql` | Open a psql shell on the dev database |
| `make test` | Run the test suite |
| `make lint` | Run ruff |
| `make css` | Build Tailwind CSS (needs `web/static/input.css`, phase 6) |
| `make run-worker STAGE=fetch` | Download feeds and store new articles (phases 1-2) |
| `make run-worker STAGE=embed` | Embed every article that has no vector yet (phase 3) |
| `make run-worker STAGE=cluster` | Group new articles into events (phase 4) |

The migration database test is skipped unless `TEST_DATABASE_URL` is set. It creates and drops its own database, so the role needs `CREATEDB`. Use the port from `DB_HOST_PORT`:

```bash
TEST_DATABASE_URL=postgresql://ft:change-me@localhost:5432/ft make test
```

## Configuration

Settings come from `.env`. [.env.example](.env.example) lists them all.

- `DB_HOST_PORT` sets the host port for the dev database (default `5432`). Change it if something else already uses that port.
- `COMPOSE_PROFILES=prod` on the server starts Caddy. Leave it unset locally.
- `EMBED_MODEL` and `EMBED_DIM` pick the embedding model. `EMBED_DIM` must equal the `vector(384)` size in the schema. Changing the model means a new migration and re-embedding every article.
- `EMBED_CACHE_DIR` is where the model weights are stored. The worker image sets it to `/opt/models`, so containers never download the model at start. Leave it unset elsewhere.

## Layout

```
app/          shared config and the migration runner (python -m app.migrate)
migrations/   numbered SQL files, applied in order
worker/       fetch, normalize, dedupe, embed, cluster (phases 1-4); enrich, digest (later)
web/          Flask + HTMX app (phase 6)
docker/       Dockerfiles for the web and worker images
tests/        pytest suite
```

Store only titles, short summaries and links. Full article text is never stored.

The worker and the web app never talk to each other directly. Postgres is the only link between them.
