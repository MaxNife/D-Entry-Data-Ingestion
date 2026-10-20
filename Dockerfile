FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Third-party dependencies first, so they cache independently of our source.
# --no-install-project skips building this package (which needs README.md + src/).
COPY pyproject.toml uv.lock ./
# --extra sqlserver: the pure-wheel SQL Server driver, for SQL Server connections.
RUN uv sync --frozen --no-dev --extra sqlserver --no-install-project

# Now the project itself: the editable build needs README.md and src/ present.
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra sqlserver

COPY contracts ./contracts
COPY alembic ./alembic
COPY alembic.ini ./
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "ingest.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
