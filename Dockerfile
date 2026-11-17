# syntax=docker/dockerfile:1
# API image for the text-to-SQL service (FastAPI + LangGraph).
#
#   docker build -t text2sql-api .
#   docker build -t text2sql-api --build-arg EXTRAS=service,postgresql,mysql,vector .
#
# The vector extra (sentence-transformers, chromadb, pinecone) is left out by default: it
# adds several GB (PyTorch). Without it the schema linker passes the full schema to the
# planner instead of a retrieved subset.
FROM python:3.12-slim

ARG EXTRAS=service,postgresql,mysql

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

# The code reads config/*.yaml relative to the source tree, so the project is installed in
# editable mode from /app rather than copied into site-packages. All drivers ship as
# wheels (psycopg[binary], psycopg2-binary, mysql-connector-python): no compiler needed.
COPY . .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -e ".[${EXTRAS}]"

# Run as an unprivileged user. It owns only the directory for local state (.t2s holds the
# SQLite metadata/checkpoint files when no external database is configured) and its home
# (model and Chroma caches when the vector extra is installed).
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && mkdir -p /app/.t2s \
    && chown app:app /app/.t2s
USER app

EXPOSE 8000

# python:slim has no curl; use the interpreter that is already there.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"]

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
