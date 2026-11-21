# The service image: the broker, the runner, the gateway and the control plane, from
# one install, with the control plane's web client built in.
#
# Build from the repository root, with `make image-service`, or:
#   docker build -f images/service/Dockerfile -t maji-service .
#
# Dependencies come from uv.lock and web/package-lock.json, so the image runs exactly
# what the tests ran.
#

# The web client, built here so nothing about it depends on the machine building the
# image. Its types are generated from api/openapi.yaml as it builds, so a client that
# has drifted from the contract fails the build.
#
FROM node:22-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d AS web
WORKDIR /web
COPY web/package.json web/package-lock.json web/.npmrc ./
RUN npm ci
COPY api/openapi.yaml /api/openapi.yaml
COPY web/ ./
RUN npm run build

FROM python:3.14-slim@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d

# ghcr.io lists a published image with the repository its `source` label names.
#
LABEL org.opencontainers.image.source=https://github.com/scanner/maji-mcp
LABEL org.opencontainers.image.description="maji-mcp's services: the broker, the runner, the gateway and the control plane"
LABEL org.opencontainers.image.licenses=BSD-3-Clause

COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /bin/uv

# The services run as user 10002, never as the executor's 10001. The directories that
# hold sockets, the run log, the skill library and the keys you connect services
# with belong to it, so a named volume mounted over them starts out owned by it too.
# The keys' folder is the service user's alone.
#
RUN groupadd --gid 10002 maji-service \
    && useradd --uid 10002 --gid 10002 --no-create-home maji-service \
    && mkdir -p /run/maji /run/maji-control /run/maji-runner \
        /run/maji-plane-broker /run/maji-plane-gateway \
        /var/lib/maji /var/lib/maji-control /var/lib/maji-library \
        /var/lib/maji-credentials \
    && chown 10002:10002 /run/maji /run/maji-control /run/maji-runner \
        /run/maji-plane-broker /run/maji-plane-gateway \
        /var/lib/maji /var/lib/maji-control /var/lib/maji-library \
        /var/lib/maji-credentials \
    && chmod 700 /var/lib/maji-credentials

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable && rm -rf /root/.cache
COPY --from=web /web/dist /app/web

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10002:10002
