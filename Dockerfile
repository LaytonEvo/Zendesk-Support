FROM python:3.11-slim

WORKDIR /app

# Install dependencies first so code edits don't invalidate the layer.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# The Railway volume mounts here; the corpus must live on it to survive deploys.
ENV CORPUS_DB=/data/corpus.sqlite3
ENV PYTHONUNBUFFERED=1

# Railway injects $PORT. Shell form so it expands at runtime.
CMD uvicorn evogolf_support.api.app:app --host 0.0.0.0 --port ${PORT:-8000}
