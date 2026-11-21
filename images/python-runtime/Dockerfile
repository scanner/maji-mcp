# The Python runtime image: where agent-written scripts run.
#
# Build from the repository root, with `make image-runtime`, or:
#   docker build -f images/python-runtime/Dockerfile -t maji-python-runtime .
#
# The base is pinned by digest, because the digest feeds the promotion signature and the
# build cache key (docs/design/planned/languages.md). Update the digest deliberately.
#
FROM python:3.14-slim@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d

# ghcr.io lists a published image with the repository its `source` label names.
#
LABEL org.opencontainers.image.source=https://github.com/scanner/maji-mcp
LABEL org.opencontainers.image.description="maji-mcp's Python runtime, where agent-written scripts run"
LABEL org.opencontainers.image.licenses=BSD-3-Clause

# A fixed non-root user. The runner also passes --user, so this is the fallback, not
# the control.
#
RUN groupadd --gid 10001 maji && useradd --uid 10001 --gid 10001 --no-create-home maji

# Git, for scripts that read the repositories mounted under /mnt, read-only. Debian's
# package, from Debian's own archive.
#
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY images/python-runtime/gitconfig /etc/gitconfig

# The libraries a script can import beyond the standard library (requirements.in),
# installed from PyPI only as the lock pins them: every file has to match a hash in
# it, and nothing is built from source.
#
COPY images/python-runtime/requirements.txt /opt/maji/requirements.txt
RUN pip install --no-cache-dir --root-user-action=ignore --disable-pip-version-check \
        --index-url https://pypi.org/simple --require-hashes --only-binary=:all: \
        --no-deps -r /opt/maji/requirements.txt

COPY src/maji_mcp/executor/maji.py /opt/maji/maji.py
COPY src/maji_mcp/executor/run.py /opt/maji/run.py

# A mounted repository's own config can name commands for git to run, such as a
# filesystem monitor or hooks. Settings in GIT_CONFIG_* outrank a repository's config,
# so every git process here gets these: the stub client's helpers, GitPython, and git
# run straight from a script.
#
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GIT_OPTIONAL_LOCKS=0 \
    GIT_TERMINAL_PROMPT=0 \
    GIT_CONFIG_COUNT=4 \
    GIT_CONFIG_KEY_0=core.fsmonitor \
    GIT_CONFIG_VALUE_0=false \
    GIT_CONFIG_KEY_1=core.hooksPath \
    GIT_CONFIG_VALUE_1=/dev/null \
    GIT_CONFIG_KEY_2=safe.directory \
    GIT_CONFIG_VALUE_2=* \
    GIT_CONFIG_KEY_3=core.pager \
    GIT_CONFIG_VALUE_3=cat \
    HOME=/work

USER 10001:10001
WORKDIR /work
ENTRYPOINT ["python", "-I", "/opt/maji/run.py"]
