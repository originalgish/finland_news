# Finland Today — Implementation Plan

A self-hosted, single-user Finnish news aggregator: pull RSS from Finnish outlets, dedupe and cluster stories, translate and tag them with an LLM, browse them through saved views, and read a daily digest.

This plan reflects the decisions made so far:

- **Muting is out.** No `mute_rules`, no read-time hiding. Noise is controlled by saved views (include tags / include sources).
- **Single user.** No accounts; access is protected at the proxy (Caddy basic auth or Tailscale).
- **Store titles, short summaries and links only** — never full article text.
- **Worker and web app never talk directly.** Postgres is the only link between them.

---

## 0. Decisions at a glance

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | |
| Web | Flask + Jinja2 + HTMX, served by gunicorn | Server-rendered, no JS build |
| CSS | Tailwind standalone CLI → compiled `static/app.css` | No Node, no CDN script in prod |
| DB | PostgreSQL 16 + pgvector (`pgvector/pgvector:pg16` image) | |
| DB access | psycopg 3 + plain SQL | Keep it boring; no ORM |
| Migrations | Numbered `.sql` files + tiny runner (`schema_migrations` table) | Swap for Alembic only if it starts to hurt |
| HTTP client | httpx (async) | Conditional requests, timeouts, retries |
| Feed parsing | feedparser | |
| HTML cleanup | `selectolax` or `bleach`/`html.unescape` | Strip tags/entities in normalize |
| Embeddings | `fastembed`, local multilingual model on CPU | Embed the **Finnish original** |
| LLM | Claude Haiku 5.5 (`claude-haiku-5-5`) via `anthropic` SDK | Translation, tags, borderline cluster checks, digest text |
| Full-text search | Postgres `finnish` text search config | Stemming handles inflection (*vaalit / vaaleissa*) |
| Scheduler | supercronic in the worker container | |
| Proxy / TLS | Caddy | Only public-facing service |
| Hosting | Hetzner VPS, 2 vCPU / 4 GB | Docker Compose |
| Monitoring | healthchecks.io pings | |
| Backups | nightly `pg_dump` → Hetzner Storage Box; weekly snapshots | |

---

## 1. Repository layout

```
finland-today/
├── docker-compose.yml
├── docker-compose.override.yml      # local dev: ports, bind mounts, no Caddy
├── Caddyfile
├── .env.example
├── Makefile                         # dev shortcuts
├── pyproject.toml
├── migrations/
│   ├── 001_init.sql
│   └── 002_seed_sources.sql
├── app/                             # shared code (config, db, models)
│   ├── __init__.py
│   ├── config.py                    # env parsing (pydantic-settings or plain os.environ)
│   ├── db.py                        # connection pool, helpers
│   └── migrate.py                   # migration runner
├── worker/
│   ├── __main__.py                  # CLI: python -m worker <stage|run>
│   ├── fetch.py
│   ├── normalize.py
│   ├── dedupe.py
│   ├── embed.py
│   ├── cluster.py
│   ├── enrich.py                    # translation + LLM tags
│   ├── tagging.py                   # feed-category + keyword-rule tags
│   ├── digest.py
│   ├── llm.py                       # Claude client wrapper, batching, JSON parsing
│   ├── health.py                    # healthchecks.io pings
│   └── crontab
├── web/
│   ├── __init__.py                  # Flask app factory
│   ├── routes/
│   │   ├── feed.py
│   │   ├── clusters.py
│   │   ├── tags.py
│   │   ├── views.py
│   │   ├── sources.py
│   │   ├── search.py
│   │   └── digest.py
│   ├── queries.py                   # read-side SQL
│   ├── templates/
│   │   ├── base.html
│   │   ├── feed.html
│   │   ├── partials/                # HTMX fragments
│   │   └── ...
│   └── static/
│       ├── htmx.min.js              # vendored
│       ├── input.css
│       └── app.css                  # built by Tailwind CLI
├── docker/
│   ├── web.Dockerfile
│   └── worker.Dockerfile
├── scripts/
│   ├── backup.sh
│   └── deploy.sh
└── tests/
    ├── fixtures/feeds/              # saved real feed XML samples
    ├── conftest.py
    └── test_*.py
```

---

## 2. Data model

### 2.1 Schema (`migrations/001_init.sql`)

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- optional, fuzzy title search

-- Sources --------------------------------------------------------------
CREATE TABLE sources (
    id                    serial PRIMARY KEY,
    name                  text NOT NULL,
    feed_url              text NOT NULL UNIQUE,
    language              text NOT NULL DEFAULT 'fi',
    enabled               boolean NOT NULL DEFAULT true,
    -- fetch state
    etag                  text,
    last_modified         text,
    body_hash             text,
    last_fetched_at       timestamptz,
    last_status           int,
    consecutive_failures  int NOT NULL DEFAULT 0,
    disabled_reason       text
);

CREATE TABLE fetch_log (
    id           bigserial PRIMARY KEY,
    source_id    int NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    started_at   timestamptz NOT NULL DEFAULT now(),
    duration_ms  int,
    status_code  int,
    outcome      text NOT NULL,          -- ok | not_modified | unchanged | empty | error
    items_found  int NOT NULL DEFAULT 0,
    items_new    int NOT NULL DEFAULT 0,
    error        text
);
CREATE INDEX ON fetch_log (source_id, started_at DESC);

-- Clusters -------------------------------------------------------------
CREATE TABLE clusters (
    id             bigserial PRIMARY KEY,
    headline       text,                 -- LLM English headline (nullable until enriched)
    first_seen     timestamptz NOT NULL DEFAULT now(),
    last_updated   timestamptz NOT NULL DEFAULT now(),
    article_count  int NOT NULL DEFAULT 0,
    source_count   int NOT NULL DEFAULT 0,
    centroid       vector(384)           -- dimension must match embedding model
);
CREATE INDEX ON clusters (last_updated DESC);

-- Articles -------------------------------------------------------------
CREATE TABLE articles (
    id              bigserial PRIMARY KEY,
    source_id       int NOT NULL REFERENCES sources(id),
    cluster_id      bigint REFERENCES clusters(id) ON DELETE SET NULL,
    guid            text,
    url             text NOT NULL,       -- as received
    url_canonical   text NOT NULL UNIQUE,
    title           text NOT NULL,
    summary         text,                -- cleaned teaser, truncated (~500 chars)
    title_en        text,
    summary_en      text,
    feed_categories text[] NOT NULL DEFAULT '{}',
    published_at    timestamptz NOT NULL,
    fetched_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    embedding       vector(384),
    -- pipeline progress markers (NULL = stage still pending)
    embedded_at     timestamptz,
    clustered_at    timestamptz,
    enriched_at     timestamptz,
    search_tsv      tsvector GENERATED ALWAYS AS (
                        setweight(to_tsvector('finnish', coalesce(title,'')), 'A') ||
                        setweight(to_tsvector('finnish', coalesce(summary,'')), 'B')
                    ) STORED,
    search_tsv_en   tsvector GENERATED ALWAYS AS (
                        to_tsvector('english', coalesce(title_en,'') || ' ' || coalesce(summary_en,''))
                    ) STORED
);
CREATE UNIQUE INDEX ON articles (source_id, guid) WHERE guid IS NOT NULL;
CREATE INDEX ON articles (published_at DESC);
CREATE INDEX ON articles (cluster_id);
CREATE INDEX ON articles USING gin (search_tsv);
CREATE INDEX ON articles USING gin (search_tsv_en);
CREATE INDEX ON articles USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON articles (embedded_at) WHERE embedded_at IS NULL;
CREATE INDEX ON articles (enriched_at) WHERE enriched_at IS NULL;

-- Tags -----------------------------------------------------------------
CREATE TABLE tags (
    id      serial PRIMARY KEY,
    name    text NOT NULL UNIQUE,        -- e.g. 'politics', 'economy', 'vaasa'
    weight  real NOT NULL DEFAULT 1.0,   -- used by digest ranking
    llm_assignable boolean NOT NULL DEFAULT true  -- include in LLM's allowed list
);

CREATE TYPE tag_origin AS ENUM ('feed', 'rule', 'llm', 'manual');

CREATE TABLE article_tags (
    article_id  bigint NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    tag_id      int    NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    origin      tag_origin NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (article_id, tag_id)
);
CREATE INDEX ON article_tags (tag_id);

-- Keyword → tag rules (managed from the tag manager UI)
CREATE TABLE tag_rules (
    id       serial PRIMARY KEY,
    tag_id   int NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    query    text NOT NULL,              -- websearch_to_tsquery('finnish', query)
    enabled  boolean NOT NULL DEFAULT true
);

-- Map raw feed categories to tags (e.g. 'Urheilu' → sports)
CREATE TABLE feed_category_map (
    source_id  int REFERENCES sources(id) ON DELETE CASCADE,  -- NULL = any source
    category   text NOT NULL,
    tag_id     int NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    UNIQUE (source_id, category)
);

-- Saved views ----------------------------------------------------------
CREATE TABLE views (
    id               serial PRIMARY KEY,
    name             text NOT NULL UNIQUE,
    include_tags     int[] NOT NULL DEFAULT '{}',   -- empty = all tags
    include_sources  int[] NOT NULL DEFAULT '{}',   -- empty = all sources
    is_default       boolean NOT NULL DEFAULT false,
    position         int NOT NULL DEFAULT 0
);

-- Digests --------------------------------------------------------------
CREATE TABLE digests (
    date         date PRIMARY KEY,
    html         text NOT NULL,
    items        jsonb NOT NULL,          -- [{cluster_id, headline, blurb, links:[...]}] for RSS output
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Migration bookkeeping --------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
```

**Notes**
- `vector(384)` assumes a 384-dim model. Set it to whatever model you pick (see §6) **before** running the first migration — changing it later means re-embedding.
- Additions relative to the earlier sketch: `fetch_log`, fetch-state columns on `sources`, `tag_rules`, `feed_category_map`, `tags.weight`, pipeline marker columns, `digests.items`. Each one is there to support something already agreed (source health page, keyword rules, digest ranking, RSS digest output).
- Future, if wanted: `views.exclude_tags int[]` for "everything except sports". Not in v1.

### 2.2 Seed (`migrations/002_seed_sources.sql`)

Seed Yle, HS, IS, Iltalehti and Kauppalehti. **Verify each feed URL in a browser first** — publishers move feeds. Also seed a starter tag set (politics, economy, business, sports, culture, crime, health, tech, world, local, weather) and a few `feed_category_map` rows once you've seen real category values in the fixtures.

---

## 3. Phase 0 — Project setup (≈1 h)

**Tasks**
1. `git init`, `pyproject.toml` with deps: `flask`, `gunicorn`, `psycopg[binary,pool]`, `pgvector`, `httpx`, `feedparser`, `selectolax`, `fastembed`, `anthropic`, `python-dotenv`; dev: `pytest`, `respx`, `ruff`.
2. `.env.example`:
   ```
   DATABASE_URL=postgresql://ft:ft@db:5432/ft
   POSTGRES_PASSWORD=change-me
   ANTHROPIC_API_KEY=
   LLM_MODEL=claude-haiku-5-5
   EMBED_MODEL=<chosen fastembed model>
   EMBED_DIM=384
   HC_FETCH_URL=            # healthchecks.io ping URLs
   HC_DIGEST_URL=
   TZ=Europe/Helsinki
   USER_AGENT=FinlandToday/0.1 (+https://your-domain/about)
   ```
3. `docker-compose.yml` with `db`, `worker`, `web`, `caddy` (see §11). `docker-compose.override.yml` for local dev exposes `db:5432` and `web:5000`, bind-mounts the code, skips Caddy.
4. `app/migrate.py`: read `migrations/*.sql` in order, skip versions already in `schema_migrations`, apply each in a transaction.
5. `Makefile`: `up`, `down`, `migrate`, `psql`, `test`, `lint`, `css`, `run-worker STAGE=...`.

**Done when** `make up && make migrate` creates the schema and `make psql` shows the tables.

---

## 4. Phase 1 — Fetch (≈2 h)

**Module:** `worker/fetch.py`. One async function per source, all sources run concurrently with a small semaphore (e.g. 5).

**Per-source flow**
1. Build headers: `User-Agent` from config; `If-None-Match: etag`, `If-Modified-Since: last_modified` when present.
2. `httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5), follow_redirects=False)`.
3. Handle response:

| Response | Action |
|---|---|
| `304` | outcome `not_modified`, done |
| `200`, body hash == `body_hash` | outcome `unchanged`, done |
| `200`, new body | parse with feedparser, save new `etag` / `last_modified` / `body_hash` |
| `301` / `308` | update `sources.feed_url`, retry once with new URL |
| `302` / `307` | follow once, don't update stored URL |
| timeout, connect error, `5xx`, `429` | retry up to 3× with exponential backoff + jitter; honor `Retry-After` |
| `404` / `410` | no retry; increment failures |
| other `4xx` | no retry; log |

4. Treat as **failure** (not success): feedparser `bozo` with zero entries, or zero items when the source normally has many. Never interpret an empty feed as "everything deleted".
5. On success reset `consecutive_failures = 0`; on failure increment it. At ≥ 5, set `enabled = false`, `disabled_reason`, and surface it on the source-health page.
6. Write one `fetch_log` row per source per run.
7. Return parsed entries to the next stage (in-process; no queue needed).

**Tests**
- `respx` mocks: 304 path, unchanged-hash path, 301 updates URL, 5xx retries then succeeds, 404 increments failures, 5 failures disables source.
- Fixture XML from each real source in `tests/fixtures/feeds/`.

**Done when** `python -m worker fetch` hits all seeded sources, populates `fetch_log`, and a second immediate run is mostly `not_modified`/`unchanged`.

---

## 5. Phase 2 — Normalize + exact dedupe (≈2 h)

**Module:** `worker/normalize.py`, `worker/dedupe.py`.

**Normalize each entry into a dict:**
- `url_canonical`: lower-case scheme/host, strip `utm_*`, `fbclid`, `gclid`, other tracking params, strip fragments, strip trailing slash, unwrap AMP paths (`/amp/`, `?amp`). Keep a per-source hook for odd cases.
- `guid`: entry id if present and not equal to the URL of another item.
- `title`: HTML-unescape, collapse whitespace.
- `summary`: strip HTML tags and entities, collapse whitespace, truncate to ~500 chars at a word boundary. Never store full article bodies.
- `published_at`: parse with timezone; if missing → `fetched_at`; if naive → assume `Europe/Helsinki`; if in the future → clamp to `now()`. Store in UTC.
- `feed_categories`: list of raw category strings.

**Exact dedupe (upsert):**
```sql
INSERT INTO articles (source_id, guid, url, url_canonical, title, summary,
                      feed_categories, published_at)
VALUES (...)
ON CONFLICT (url_canonical) DO UPDATE
   SET title = EXCLUDED.title,
       summary = EXCLUDED.summary,
       updated_at = now(),
       -- headline changed → re-translate later
       enriched_at = CASE WHEN articles.title IS DISTINCT FROM EXCLUDED.title
                          THEN NULL ELSE articles.enriched_at END
RETURNING id, (xmax = 0) AS inserted;
```
- If no canonical-URL match but `(source_id, guid)` matches an existing row → treat as an edit of that row (URL changed).
- If neither GUID nor stable URL: fall back to `sha256(title + published_at)` as `guid`.
- Count `items_new` from `inserted = true` and update the `fetch_log` row.

**Tests**
- Canonicalization table test (tracking params, AMP, case, trailing slash).
- Date edge cases: missing, naive, future.
- Edited headline updates the row instead of inserting, and clears `enriched_at`.
- Summary stripping on real fixture HTML.

**Done when** re-running the pipeline over the same fixture feeds inserts zero new rows.

---

## 6. Phase 3 — Embeddings (≈1 h)

**Module:** `worker/embed.py`.

1. Pick the model. In a Python shell, list `fastembed`'s supported text models and choose a **multilingual** one that handles Finnish (a multilingual MiniLM or multilingual-e5 variant). Record its dimension in `EMBED_DIM` and in the migration.
   - If you pick an e5 model, prefix inputs with `"passage: "` as that family expects.
2. Bake the model into the worker image at build time (run a one-line download in the Dockerfile) so the container doesn't download it on every start.
3. Select `WHERE embedded_at IS NULL ORDER BY id LIMIT 256`, embed `title + "\n" + summary` (Finnish original), write `embedding`, set `embedded_at = now()`. Loop until none left.
4. Also re-embed when a title edit cleared `enriched_at`? Optional; v1 can skip.

**Done when** all articles have embeddings and a nearest-neighbour query returns sensible matches:
```sql
SELECT title FROM articles
ORDER BY embedding <=> (SELECT embedding FROM articles WHERE id = :id)
LIMIT 10;
```

---

## 7. Phase 4 — Story clustering (≈3 h)

**Module:** `worker/cluster.py`.

**Algorithm (incremental, per new article):**
1. Candidate search: nearest articles from the last **72 h** that already have a `cluster_id`:
   ```sql
   SELECT id, cluster_id, source_id, 1 - (embedding <=> :emb) AS sim
   FROM articles
   WHERE cluster_id IS NOT NULL
     AND published_at > :pub - interval '72 hours'
   ORDER BY embedding <=> :emb
   LIMIT 10;
   ```
2. Decide using the best match's similarity `sim` (thresholds are **starting values to tune**):
   - `sim ≥ 0.95` **and same source** → near-duplicate (republished/updated item). Attach to the same cluster; don't increase `source_count`.
   - `sim ≥ 0.85` → join that cluster.
   - `0.75 ≤ sim < 0.85` → **borderline**: ask the LLM "same news event? yes/no" with both titles+summaries (batched; see §8). Join on yes, otherwise new cluster.
   - `sim < 0.75` → create a new cluster.
3. On join: update `article_count`, recompute `source_count` (distinct sources), update `centroid` as running mean, set `last_updated = now()`, and **null the cluster headline** so it gets regenerated.
4. Set `clustered_at = now()`.

**Tuning workflow** (do this before trusting the thresholds):
- After a couple of days of data, export ~100 random article pairs across similarity bands to CSV, label same/different by hand, pick thresholds that minimize wrong merges. Wrong merges are worse than missed merges.
- Add a hidden admin page `/admin/clusters` listing clusters with members and their similarities, plus a "split article out" button (sets `cluster_id` NULL, re-runs cluster for that article with a flag forcing a new cluster).

**Tests**
- Synthetic embeddings to unit-test the decision logic (thresholds, same-source near-dup).
- Integration test with fixture articles about one obvious shared story across 3 sources → one cluster, `source_count = 3`.

**Done when** the same story from Yle, IS and Iltalehti lands in one cluster on real data.

---

## 8. Phase 5 — Enrichment: translation + tags (≈3 h)

### 8.1 Rule-based tags (no LLM) — `worker/tagging.py`
1. **Feed tags:** for each new article, map `feed_categories` through `feed_category_map` → `article_tags` with origin `feed`.
2. **Keyword rules:** for each enabled `tag_rules` row:
   ```sql
   INSERT INTO article_tags (article_id, tag_id, origin)
   SELECT a.id, :tag_id, 'rule'
   FROM articles a
   WHERE a.id = ANY(:new_ids)
     AND a.search_tsv @@ websearch_to_tsquery('finnish', :query)
   ON CONFLICT DO NOTHING;
   ```
   When a rule is created/edited in the UI, also backfill it over the last 30 days.

### 8.2 LLM enrichment — `worker/llm.py`, `worker/enrich.py`
- Select `WHERE enriched_at IS NULL`, batches of ~20 articles per request.
- One request per batch returns, per article: `title_en`, `summary_en` (1–2 sentences), `tags` (chosen **only** from the allowed list of `tags WHERE llm_assignable`).
- Force structured JSON via tool use (a single `record_enrichment` tool with a JSON schema), so parsing is reliable.
- Put the static instructions + tag list first in the system prompt and enable prompt caching on it.
- Validate: drop unknown tags, ensure every article id came back; retry the missing ones once alone.
- Write `title_en`, `summary_en`, `enriched_at`; insert `article_tags` with origin `llm`. **Never overwrite or delete `manual` tags.**
- Borderline cluster checks (§7) go through the same wrapper as a separate small batched call.
- Cost guard: config `LLM_MAX_ARTICLES_PER_RUN` (e.g. 500); log token usage per run to stdout.
- Optional later: switch to the Message Batches API for cheaper, non-urgent runs.

### 8.3 Cluster headlines
- For clusters with `headline IS NULL` that have ≥ 1 enriched article: send member titles (English) → get one neutral English headline. Batch ~20 clusters per call.

**Tests**
- Mock the Anthropic client; test batching, JSON validation, unknown-tag filtering, manual-tag preservation, missing-item retry.

**Done when** new articles get English titles/summaries and 1–3 sensible tags, and clusters get headlines.

---

## 9. Phase 6 — Web app (≈4 h)

Flask app factory in `web/__init__.py`; read-side SQL in `web/queries.py`. All writes are small HTMX form posts returning a partial.

### 9.1 Routes

| Route | Purpose |
|---|---|
| `GET /` | Feed for the active view (default view or `?view=<id>`), clusters ordered by `last_updated DESC`, paginated (infinite scroll via `hx-get` on last row) |
| `GET /clusters/<id>` | Cluster detail: headline, all member articles grouped by source, original Finnish + English, tags |
| `POST /articles/<id>/tags` | Add manual tag → returns tag chip partial |
| `DELETE /articles/<id>/tags/<tag_id>` | Remove tag (any origin) → returns partial |
| `GET /tags` | Tag manager: list tags, weights, `llm_assignable`, keyword rules, counts |
| `POST /tags`, `PATCH /tags/<id>`, `DELETE /tags/<id>` | CRUD |
| `POST /tags/<id>/rules`, `DELETE /rules/<id>` | Keyword rules; show live preview count of matches over last 7 days before saving |
| `GET /views`, `POST /views`, `PATCH /views/<id>`, `DELETE /views/<id>` | Saved views (include tags / include sources, default flag, order) |
| `GET /search?q=` | Full-text over Finnish (`search_tsv`) and English (`search_tsv_en`), ranked with `ts_rank` |
| `GET /sources` | Source health: last fetch, last status, consecutive failures, items/day, recent `fetch_log`; enable/disable toggle |
| `GET /digest`, `GET /digest/<date>` | Stored digest HTML |
| `GET /digest.xml` | RSS of recent digests (built from `digests.items`) |
| `GET /healthz` | DB ping, for Caddy/uptime checks |

### 9.2 Feed query (view filter)
```sql
SELECT c.id, c.headline, c.last_updated, c.source_count, ...
FROM clusters c
WHERE EXISTS (
  SELECT 1 FROM articles a
  LEFT JOIN article_tags t ON t.article_id = a.id
  WHERE a.cluster_id = c.id
    AND (cardinality(:sources) = 0 OR a.source_id = ANY(:sources))
    AND (cardinality(:tags)    = 0 OR t.tag_id    = ANY(:tags))
)
ORDER BY c.last_updated DESC
LIMIT 30 OFFSET :offset;
```

### 9.3 UI details
- Base layout: top bar with view switcher (tabs), search box, links to Digest / Tags / Sources.
- Cluster card: English headline, "N sources" badge, source favicons/initials, time ago, tag chips, expand → member articles (HTMX `hx-get` partial).
- Toggle Finnish/English display per user preference (cookie).
- Mobile-first; Tailwind standalone CLI builds `app.css` (`make css`); vendor `htmx.min.js` into `static/`.

**Done when** you can switch views, open a cluster, add/remove a tag, create a keyword rule, and see source health — all without full page reloads.

---

## 10. Phase 7 — Daily digest (≈2 h)

**Module:** `worker/digest.py`, run once each morning (e.g. 07:00 Helsinki).

1. Candidate clusters: `last_updated` within the last 24 h.
2. Score each cluster:
   ```
   score = 2.0 * source_count
         + 1.0 * recency          -- exp decay, half-life ~8 h from last_updated
         + 1.0 * max(tag.weight)  -- across cluster's tags (default 1.0)
   ```
   Weights are config constants; tune after a week.
3. Take top 5–10. For each, one LLM call (batched) producing a headline + 1–2 sentence English blurb from member titles/summaries.
4. Render `templates/digest.html` → store `html` and `items` in `digests` (upsert on `date`, so a re-run replaces it).
5. Ping `HC_DIGEST_URL`.
6. Optional later: email via SMTP (Postmark/Mailgun/Gmail app password) using the same HTML.

**Done when** `/digest` shows today's digest with links to original articles and `/digest.xml` validates in an RSS reader.

---

## 11. Phase 8 — Worker orchestration & scheduling (≈1 h)

**CLI** (`python -m worker ...`):
- `fetch`, `embed`, `cluster`, `enrich`, `digest` — run one stage.
- `run` — `fetch → normalize/dedupe → tagging(rules/feed) → embed → cluster → enrich → headlines`. Each stage only processes rows whose marker is NULL, so any stage can crash and the next run picks up where it left off.
- Use a Postgres advisory lock (`pg_try_advisory_lock`) at the start of `run` so two runs never overlap.
- Ping `HC_FETCH_URL` only if ≥ 60 % of enabled sources succeeded; ping `/fail` otherwise.

**`worker/crontab`** (supercronic, container `TZ=Europe/Helsinki`):
```
30 6,18 * * *   python -m worker run
0  7    * * *   python -m worker digest
15 3    * * *   python -m worker maintenance   # prune fetch_log > 90 days, VACUUM ANALYZE
```
Two fetches a day was the agreed polite default. Bump to every 2–3 h later if you want fresher news — conditional requests make that cheap.

---

## 12. Phase 9 — Deployment on Hetzner (≈2 h)

### 12.1 `docker-compose.yml` (production)
```yaml
services:
  db:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: ft
      POSTGRES_DB: ft
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ft"]
      interval: 10s
    restart: unless-stopped

  worker:
    build: { context: ., dockerfile: docker/worker.Dockerfile }
    env_file: .env
    depends_on: { db: { condition: service_healthy } }
    command: ["supercronic", "/app/worker/crontab"]
    restart: unless-stopped

  web:
    build: { context: ., dockerfile: docker/web.Dockerfile }
    env_file: .env
    depends_on: { db: { condition: service_healthy } }
    command: ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "web:create_app()"]
    restart: unless-stopped

  caddy:
    image: caddy:2
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
    depends_on: [web]
    restart: unless-stopped

volumes:
  pgdata:
  caddy_data:
```
`db` is not exposed on the host. Only Caddy publishes ports.

### 12.2 `Caddyfile`
```
news.example.com {
    basic_auth {
        denis <bcrypt hash from `caddy hash-password`>
    }
    encode zstd gzip
    reverse_proxy web:8000
}
```
Alternative: skip basic auth and serve only on the Tailscale interface.

### 12.3 Server setup checklist
1. Create a CX22-class VPS (2 vCPU / 4 GB), Ubuntu LTS, add SSH key.
2. Create non-root user, disable password SSH and root login.
3. Hetzner Cloud Firewall: allow 22 (optionally restrict to your IP), 80, 443 only.
4. Install Docker Engine + compose plugin; enable unattended-upgrades.
5. Point DNS A/AAAA record at the server.
6. `git clone`, create `.env` (not in git), `docker compose up -d --build`, `docker compose run --rm web python -m app.migrate`.
7. Deploys: `scripts/deploy.sh` = `git pull && docker compose up -d --build && docker compose run --rm web python -m app.migrate`.

---

## 13. Phase 10 — Ops: backups & monitoring (≈1 h)

- **Backups** (`scripts/backup.sh`, host cron at 02:30):
  `docker compose exec -T db pg_dump -U ft -Fc ft > /backups/ft-$(date +%F).dump`, then `rsync`/`borg` to a Hetzner Storage Box. Keep 14 daily + 8 weekly. Tags, tag rules and views are the irreplaceable data; articles can be re-fetched.
- **Weekly server snapshots** in the Hetzner console (or backups add-on).
- **Restore drill:** restore one dump into a scratch container once, so you know it works.
- **Monitoring:** healthchecks.io checks for `run` (expected every 12 h, grace 1 h), `digest` (daily), and `backup` (daily). Optional: uptime check on `/healthz`.
- **Logs:** `docker compose logs`; set Docker's json-file log rotation (`max-size: 10m`, `max-file: 3`).

---

## 14. Testing strategy

- **Unit:** canonicalization, date parsing, summary cleaning, retry classification, cluster decision logic, digest scoring, LLM response validation.
- **Integration:** pytest fixture that starts a throwaway Postgres (the same `pgvector/pgvector` image via compose profile or `testcontainers`), applies migrations, runs the pipeline over `tests/fixtures/feeds/*.xml` with the LLM mocked.
- **Fixtures:** save one real feed response from each source on day one (`curl -o`). These catch publisher quirks better than anything synthetic.
- **CI (optional):** GitHub Actions running `ruff` + `pytest`.

---

## 15. Build order & milestones

| # | Phase | Est. | Milestone you can see |
|---|---|---|---|
| 0 | Setup | 1 h | Schema exists |
| 1 | Fetch | 2 h | `fetch_log` fills; second run mostly 304s |
| 2 | Normalize + dedupe | 2 h | Clean rows; re-runs insert nothing |
| 3 | Embeddings | 1 h | Nearest-neighbour query looks right |
| 4 | Clustering | 3 h | Same story from 3 outlets = 1 cluster |
| 5 | Enrichment | 3 h | English titles, tags, cluster headlines |
| 6 | Web app | 4 h | Browse, views, tags, sources page |
| 7 | Digest | 2 h | `/digest` + RSS |
| 8 | Scheduling | 1 h | Runs on its own |
| 9 | Deploy | 2 h | Live on HTTPS behind auth |
| 10 | Ops | 1 h | Backups + alerts verified |

≈ 22 h total. A sensible first "usable" cut is phases 0–2 + a bare feed page (titles + links, no clustering) — about 6 h — then layer the rest on top.

**Suggested order tweak:** deploy early (right after phase 2 with a minimal page) so the server collects real data while you build clustering and enrichment. You'll need a few days of real articles to tune clustering thresholds anyway.

---

## 16. Open questions to settle as you go

1. Exact feed URLs per outlet, and whether to use several section feeds per outlet (e.g. Yle main + Yle regional for Ostrobothnia) or one main feed each.
2. Final embedding model and dimension (decide before the first migration).
3. Fetch frequency: keep 2×/day or go to every 2–3 h.
4. Digest delivery: page + RSS only, or also email.
5. Access: Caddy basic auth vs Tailscale-only.
6. Whether to add `views.exclude_tags` after living with include-only views for a while.
