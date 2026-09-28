# Production container for LingYi memory + health server.
# This Dockerfile is intentionally minimal: stdlib Python only.
# If a deployment needs paramiko (real-SSH) it can pull the
# `lingyi-ssh` variant by changing the FROM tag.

FROM python:3.12-slim

# Avoid writing .pyc files; force UTF-8 everywhere.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Run as a non-root user.
RUN groupadd --system --gid 10001 lingyi && useradd --system --uid 10001 --gid lingyi --create-home --home-dir /var/lib/lingyi lingyi

WORKDIR /opt/lingyi
COPY pyproject.toml README.md LICENSE CHANGELOG.md ./
COPY src ./src
COPY production ./production
COPY scripts ./scripts
COPY docs ./docs

# Permissions: lingyi owns everything; data directory is writable.
RUN mkdir -p /var/lib/lingyi/data && chown -R lingyi:lingyi /var/lib/lingyi /opt/lingyi

USER 10001:10001

# Defaults. The platform'``s process supervisor overrides these via env.
ENV LINGYI_DB_PATH=/var/lib/lingyi/data/memory.db \
    LINGYI_HEALTH_HOST=0.0.0.0 \
    LINGYI_HEALTH_PORT=8080

EXPOSE 8080

# The healthcheck hits /healthz on the same port the server listens on.
HEALTHCHECK --interval=10s --timeout=2s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=1); sys.exit(0 if r.status==200 else 1)"

CMD ["python", "-c", "from production.runtime import main; main()"]