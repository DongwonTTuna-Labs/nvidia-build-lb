# syntax=docker/dockerfile:1.18@sha256:dabfc0969b935b2080555ace70ee69a5261af8a8f1b4df97b9e7fbcf6722eddf

FROM postgres@sha256:c7526c0f6c3f30260a563d7bcf8ad778effac59a44f8ffa86678c35418338609
RUN apk add --no-cache --upgrade \
        libcrypto3=3.5.7-r0 \
        libssl3=3.5.7-r0 \
        libxml2=2.13.9-r1 \
        libcap-ng=0.8.5-r0 \
        setpriv=2.41.4-r0 \
    && rm /usr/local/bin/gosu \
    && test ! -e /usr/local/bin/gosu \
    && test "$(readlink /bin/setpriv 2>/dev/null || true)" != /bin/busybox \
    && command -v setpriv >/dev/null \
    && install -d -o 0 -g 0 -m 0700 /run/canonical-secrets
COPY docker/postgres-prestart.sh /usr/local/bin/nblb-postgres-prestart
COPY docker/healthcheck.sh /usr/local/bin/nblb-healthcheck
RUN chmod 0555 /usr/local/bin/nblb-postgres-prestart /usr/local/bin/nblb-healthcheck
#trivy:ignore:DS-0002 -- bounded root prestart immediately execs UID 70 without capabilities.
ENTRYPOINT ["/usr/local/bin/nblb-postgres-prestart"]
