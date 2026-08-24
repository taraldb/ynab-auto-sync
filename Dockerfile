FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir --prefix=/install .


FROM node:20-slim AS frontend-builder

WORKDIR /frontend

# Copy the whole frontend/ directory (rather than package.json first for
# layer caching) so this works whether or not package-lock.json exists yet
# - npm ci requires a lockfile, npm install doesn't.
COPY frontend/ ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi \
    && npm run build


FROM python:3.12-slim AS runtime

RUN useradd --create-home --uid 1000 appuser

COPY --from=builder /install /usr/local
COPY --chown=appuser:appuser src /app/src
COPY --chown=appuser:appuser --from=frontend-builder /frontend/dist /app/frontend/dist

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

RUN mkdir -p /app/config /app/state && chown -R appuser:appuser /app

VOLUME ["/app/config", "/app/state"]

EXPOSE 8080

USER appuser

ENTRYPOINT ["python", "-m", "ynab_auto_sync"]
