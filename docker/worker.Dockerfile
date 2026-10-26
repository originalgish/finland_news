FROM python:3.12-slim

ARG TARGETARCH
ARG SUPERCRONIC_VERSION=v0.2.51

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EMBED_CACHE_DIR=/opt/models

WORKDIR /app

# supercronic runs the schedule in worker/crontab. The checksums are for the release
# binaries of SUPERCRONIC_VERSION, so update them together with the version.
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) sha256=cc232108374e8e2b537045f2d5eaa1229f619db314c3ba6a14d38b35dc0aa123 ;; \
        arm64) sha256=4ae22576a78223d8ccdf5f3b3660a5cbb5288416bf0ca96afc4a36d195932b94 ;; \
        *) echo "no supercronic build for ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    curl -fsSL -o /usr/local/bin/supercronic \
        "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH}"; \
    echo "${sha256}  /usr/local/bin/supercronic" | sha256sum -c -; \
    chmod +x /usr/local/bin/supercronic; \
    apt-get purge -y curl; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*

COPY . .
# Editable install keeps app/ and migrations/ resolving from /app.
RUN pip install -e .

# Bake the embedding model into the image so containers never download it at start.
RUN python -c "from app.config import DEFAULT_EMBED_MODEL; from worker.embed import load_model; load_model(DEFAULT_EMBED_MODEL, '/opt/models')"
