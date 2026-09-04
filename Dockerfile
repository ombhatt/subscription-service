# The API image. Also the jobs image: `docker run <image> reconcile` runs the
# reconciliation job with the same code and the same dependency tree the web
# process is running, which is the point of not having a separate one.
#
# Two stages so the compiler and the pip cache do not ship to production. The
# runtime stage receives a finished virtualenv and nothing else.

# ---------------------------------------------------------------- build ----
FROM python:3.12-slim-bookworm AS builder

# Everything here has a manylinux wheel, so no toolchain is installed on
# purpose: if a wheel ever goes missing the build fails loudly rather than
# quietly growing a compiler into the image.
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copied alone so the dependency layer is cached until the lock itself changes.
COPY requirements.lock ./
# --no-deps is deliberate: the lock is meant to be the complete transitive
# tree, so letting pip resolve again could pull in something the lock does not
# name. `pip check` then verifies that claim -- if the lock is missing a
# dependency, the build fails here instead of the container failing on import.
RUN pip install --no-deps -r requirements.lock && pip check

# -------------------------------------------------------------- runtime ----
FROM python:3.12-slim-bookworm AS runtime

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
