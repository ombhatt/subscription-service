# The API image. Also the jobs image: `docker run <image> reconcile` runs the
# reconciliation job with the same code and the same dependency tree the web
# process is running, which is the point of not having a separate one.
#
# Two stages so uv and its cache do not ship to production. The runtime stage
# receives a finished virtualenv and nothing else.

# ---------------------------------------------------------------- build ----
FROM python:3.12-slim-bookworm AS builder

# The same uv version that wrote uv.lock (pyproject.toml's required-version).
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

# Everything here has a manylinux wheel, so no toolchain is installed on
# purpose: if a wheel ever goes missing the build fails loudly rather than
# quietly growing a compiler into the image. The base image's interpreter is
# the one the runtime stage has too, so uv must not download another.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy

WORKDIR /build
# Copied alone so the dependency layer is cached until the lock itself changes.
COPY pyproject.toml uv.lock ./
# --frozen installs exactly what uv.lock says, verifying every hash, and never
# re-resolves; CI's `uv sync --locked` is what fails if pyproject.toml has
# drifted from the lock. --no-dev leaves the test and lint tools out. `uv pip check` then confirms the tree is
# complete, so a missing dependency fails the build, not the container's import.
RUN uv sync --frozen --no-dev --python /usr/local/bin/python \
 && uv pip check --python /opt/venv/bin/python

# -------------------------------------------------------------- runtime ----
FROM python:3.12-slim-bookworm AS runtime

# Debian's security fixes, installed at build time rather than waiting for the
# base tag to be rebuilt upstream -- which happens on Docker's schedule, often
# days after Debian ships the fix. The nightly image scan found exactly that
# gap: two HIGH CVEs in libpcre2-8-0 with a fixed package already in
# bookworm-security while python:3.12-slim-bookworm still carried the old one.
#
# Runtime stage only; the builder's packages never ship. A cached copy of this
# layer stops receiving fixes, so release builds should pass --pull --no-cache
# (the scheduled scan in security.yml does).
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*

# Nothing here needs root. Running as one means a container escape starts with
# uid 0, and it makes read-only-rootfs deployments awkward for no benefit.
RUN useradd --create-home --uid 10001 app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENVIRONMENT=production

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app app ./app
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app docker-entrypoint.sh /usr/local/bin/entrypoint

USER app
EXPOSE 8000

# Liveness only -- deliberately /healthz and not /readyz. Docker restarts an
# unhealthy container, and restarting every instance because Postgres is down
# is a crash loop, not a recovery. Readiness is the load balancer's job.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request,sys;p=os.environ.get('PORT','8000');sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+p+'/healthz',timeout=4).status==200 else 1)"

ENTRYPOINT ["entrypoint"]
CMD ["api"]
