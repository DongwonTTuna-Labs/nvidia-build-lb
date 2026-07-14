# syntax=docker/dockerfile:1.18@sha256:dabfc0969b935b2080555ace70ee69a5261af8a8f1b4df97b9e7fbcf6722eddf

FROM ghcr.io/astral-sh/uv:0.11.24@sha256:99ea34acedc870ba4ad11a1f540a1c04267c9f30aadc465a94406f52dfda2c36 AS uv

FROM python:3.13.14-alpine3.23@sha256:9fdbf2e3e82628351513560b121e2ee6ce31cac212be9e070c5a5e2769fb5e76 AS builder
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=0 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    SOURCE_DATE_EPOCH=0 \
    PYTHONHASHSEED=0
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable
RUN find .venv -type d -name __pycache__ -prune -exec rm -rf {} + \
    && find .venv -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete \
    && ! find .venv -type f \( -name '*.pyc' -o -name '*.pyo' \) -print -quit | grep -q . \
    && dist_info=$(printf '%s' .venv/lib/python3.13/site-packages/nvidia_build_lb-*.dist-info) \
    && test -f "$dist_info/uv_cache.json" \
    && rm "$dist_info/uv_cache.json" \
    && sed -i '\#/uv_cache.json,#d' "$dist_info/RECORD" \
    && ! grep -F '/uv_cache.json,' "$dist_info/RECORD"

FROM python:3.13.14-alpine3.23@sha256:9fdbf2e3e82628351513560b121e2ee6ce31cac212be9e070c5a5e2769fb5e76 AS runtime
ENV PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NVIDIA_BUILD_LB_STAGE=production \
    NVIDIA_BUILD_LB_LOG_LEVEL=INFO
RUN /sbin/apk add --no-cache libcap-ng=0.8.5-r0 setpriv=2.41.4-r0 \
    && test "$(readlink /bin/setpriv 2>/dev/null || true)" != /bin/busybox \
    && command -v setpriv >/dev/null \
    && install -d -o 65532 -g 65532 -m 0555 /app \
    && install -d -o 0 -g 0 -m 0700 /run/canonical-secrets \
    && install -d -o 0 -g 0 -m 0700 /run/qa-canonical
WORKDIR /app
COPY --from=builder --chown=65532:65532 /build/.venv /app/.venv
COPY --chown=65532:65532 alembic.ini /app/alembic.ini
COPY --chown=65532:65532 migrations /app/migrations
COPY docker/app-entrypoint.sh /usr/local/bin/nblb-app-entrypoint
COPY docker/healthcheck.sh /usr/local/bin/nblb-healthcheck
RUN chmod 0555 /usr/local/bin/nblb-app-entrypoint /usr/local/bin/nblb-healthcheck \
    && find /app -xdev -type d -exec chmod 0555 {} + \
    && find /app -xdev -type f -exec chmod 0444 {} + \
    && chmod 0555 /app/.venv/bin/*
HEALTHCHECK --interval=2s --timeout=2s --start-period=10s --retries=10 \
  CMD ["/bin/setpriv", "--reuid=65532", "--regid=65532", "--clear-groups", "--inh-caps=-all", "--ambient-caps=-all", "--bounding-set=-all", "--no-new-privs", "/usr/local/bin/nblb-healthcheck", "65532", "65532", "app"]
#trivy:ignore:DS-0002 -- bounded root prestart immediately execs UID 65532 without capabilities.
ENTRYPOINT ["/usr/local/bin/nblb-app-entrypoint"]
