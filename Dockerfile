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

# Set from CI via --build-arg to the pushed git tag (e.g. "v0.1.7"); "dev"
# is what a local `docker build` with no --build-arg gets, and what the
# GUI's footer falls back to displaying.
ARG APP_VERSION=dev
ARG BUILD_TIMESTAMP=unknown
ENV APP_VERSION=${APP_VERSION} \
    BUILD_TIMESTAMP=${BUILD_TIMESTAMP}

# Unraid runs Docker containers as nobody:users (uid 99, gid 100) by default
# - confirmed against Unraid's own docs/forums, not assumed. Used numerically
# rather than via useradd/groupadd: python:3.12-slim's base Debian image
# already reserves gid 100 for its own "users" group, so creating a NEW
# named account at that gid would collide. Docker's USER/--chown accept raw
# uid:gid with no /etc/passwd entry required, and this app never needs one
# (no home-directory-dependent behavior) - so this sidesteps the collision
# entirely rather than working around it.
COPY --from=builder /install /usr/local
COPY --chown=99:100 src /app/src
COPY --chown=99:100 --from=frontend-builder /frontend/dist /app/frontend/dist

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

RUN mkdir -p /app/config /app/state && chown -R 99:100 /app

VOLUME ["/app/config", "/app/state"]

EXPOSE 8080

USER 99:100

ENTRYPOINT ["python", "-m", "ynab_auto_sync"]
