FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EMBED_CACHE_DIR=/opt/models

WORKDIR /app

COPY . .
# Editable install keeps app/ and migrations/ resolving from /app.
RUN pip install -e .

# Bake the embedding model into the image so containers never download it at start.
RUN python -c "from app.config import DEFAULT_EMBED_MODEL; from worker.embed import load_model; load_model(DEFAULT_EMBED_MODEL, '/opt/models')"
