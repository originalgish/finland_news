-- Initial schema. The vector dimension must match EMBED_DIM (384 = sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2).
-- Changing it later means re-embedding every article.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- fuzzy title search

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
    centroid       vector(384)
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
    id              serial PRIMARY KEY,
    name            text NOT NULL UNIQUE,        -- e.g. 'politics', 'economy', 'vaasa'
    weight          real NOT NULL DEFAULT 1.0,   -- used by digest ranking
    llm_assignable  boolean NOT NULL DEFAULT true  -- include in LLM's allowed list
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
