FROM debian:bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        aptly \
        ca-certificates \
        gnupg \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY containers/repoctl/repoctl /usr/local/bin/repoctl

RUN chmod 0755 /usr/local/bin/repoctl

ENV APTLY_CONFIG=/etc/aptly.conf \
    GNUPGHOME=/var/lib/prvaptmirror/gnupg

ENTRYPOINT ["/usr/local/bin/repoctl"]
CMD ["help"]
