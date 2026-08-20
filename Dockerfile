FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends gnupg sqlite3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home app

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY prvaptmirror ./prvaptmirror
COPY scripts ./scripts
RUN pip install --no-cache-dir . && chmod +x /app/scripts/*.sh

USER 1000:1000
ENV PRVAPT_DATA_DIR=/var/lib/prvaptmirror
EXPOSE 8000
CMD ["uvicorn", "prvaptmirror.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
