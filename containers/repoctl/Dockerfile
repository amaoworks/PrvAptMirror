ARG REPOCTL_BASE_IMAGE=debian:bookworm-slim
FROM ${REPOCTL_BASE_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
ARG REPOCTL_APT_MIRROR=

RUN if [ -n "${REPOCTL_APT_MIRROR}" ]; then \
        sed -i "s|http://deb.debian.org|${REPOCTL_APT_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        aptly \
        ca-certificates \
        gnupg \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY containers/repoctl/repoctl /usr/local/bin/repoctl
COPY containers/repo-worker/worker /usr/local/bin/repo-worker

RUN chmod 0755 /usr/local/bin/repoctl /usr/local/bin/repo-worker

ENV APTLY_CONFIG=/etc/aptly.conf \
    GNUPGHOME=/var/lib/prvaptmirror/gnupg

ENTRYPOINT ["/usr/local/bin/repoctl"]
CMD ["help"]
