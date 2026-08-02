ARG ADMIN_WEB_BASE_IMAGE=debian:bookworm-slim
FROM ${ADMIN_WEB_BASE_IMAGE} AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        python3 \
        python3-argon2 \
        python3-flask \
        python3-gunicorn \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin admin-web

WORKDIR /app

COPY --chown=admin-web:admin-web containers/admin-web/app.py containers/admin-web/manage.py containers/admin-web/wsgi.py /app/
COPY --chown=admin-web:admin-web containers/admin-web/templates /app/templates
COPY --chown=admin-web:admin-web containers/admin-web/static /app/static

USER 10001:10001

EXPOSE 8081

FROM base AS test

COPY --chown=admin-web:admin-web containers/admin-web/tests /app/tests

CMD ["python3", "-m", "unittest", "discover", "-s", "/app/tests", "-v"]

FROM base AS runtime

CMD ["python3", "-m", "gunicorn", "--bind=0.0.0.0:8081", "--workers=1", "--threads=4", "--timeout=30", "--access-logfile=-", "--error-logfile=-", "wsgi:app"]
