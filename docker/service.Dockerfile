FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN pip install --no-cache-dir uv && useradd --create-home --uid 10001 hyperextract
COPY pyproject.toml uv.lock README.md ./
COPY hyperextract ./hyperextract
COPY alembic.ini ./
RUN uv sync --frozen --extra service --extra graph-rag --no-dev
RUN mkdir -p /exchange && chown -R hyperextract:hyperextract /app /exchange
USER hyperextract
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "he-api"]
