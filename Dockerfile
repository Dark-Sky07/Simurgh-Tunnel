# syntax=docker/dockerfile:1
#
# Simurgh Tunnel in a container.
#
# Two stages: the compiled engine is built from this checkout with GOPROXY=off
# (every Go dependency is vendored under go/third_party, so the build needs no
# network), then the Python engine and that binary are installed into a slim
# runtime.  The Python side has no dependencies to install either: its TOML
# reader is bundled.
#
#   docker build -t simurgh .
#   docker run --rm simurgh --version
#   docker run -d --name simurgh-exit -v simurgh-exit:/etc/simurgh \
#          -p 443:443 -p 19443:19443 simurgh exit
#
# Configuration lives in the /etc/simurgh volume and is written by `simurgh`
# itself: `docker run --rm -v simurgh-exit:/etc/simurgh simurgh init --role exit`.
# `docker-compose.yml` wires a relay and an exit together on one machine.

FROM golang:1.27-bookworm AS engine

WORKDIR /src

COPY go/ ./go/

# GOFLAGS is cleared on purpose: an inherited -mod=vendor or -mod=mod would
# fight the vendored tree, and GOPROXY=off makes an accidental network build
# fail loudly instead of silently.
RUN cd go \
 && CGO_ENABLED=0 GOFLAGS= GOPROXY=off GOTOOLCHAIN=local \
    go build -trimpath -ldflags "-s -w" -o /out/simurgh-go ./cmd/simurgh \
 && /out/simurgh-go version

FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="Simurgh Tunnel" \
      org.opencontainers.image.description="Transparent Iran <-> foreign TCP/UDP tunnel for VPN panels" \
      org.opencontainers.image.source="https://github.com/Dark-Sky07/Simurgh-Tunnel" \
      org.opencontainers.image.url="https://github.com/Dark-Sky07/Simurgh-Tunnel" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SIMURGH_HOME=/etc/simurgh

# ca-certificates: the tls/wss carriers verify real certificates, and
# `simurgh cert` / `simurgh update` fetch over HTTPS.  curl: acme.sh -- the
# ACME client `simurgh cert` drives first -- is itself a shell script that
# curl downloads, and the exit serves the decoy site with plain files.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

COPY simurgh/ /usr/lib/simurgh/simurgh/
COPY packaging/simurgh-launcher.py.in /tmp/simurgh-launcher.py
RUN sed "s|@LIBDIR@|/usr/lib/simurgh|" /tmp/simurgh-launcher.py > /usr/bin/simurgh \
 && chmod 0755 /usr/bin/simurgh \
 && rm /tmp/simurgh-launcher.py

COPY --from=engine /out/simurgh-go /usr/bin/simurgh-go

COPY packaging/docker/entrypoint.sh /usr/local/bin/simurgh-entrypoint
RUN chmod 0755 /usr/local/bin/simurgh-entrypoint \
 && mkdir -p /etc/simurgh && chmod 0700 /etc/simurgh

VOLUME ["/etc/simurgh"]
# 8787 the panel, 19443 the reverse-mode [tunnel] default, 443 and 8443 the
# mappings the examples use, 8808 the localhost-only speed endpoint.
EXPOSE 8787 19443 443 8443 8808

ENTRYPOINT ["/usr/local/bin/simurgh-entrypoint"]
CMD ["relay"]
