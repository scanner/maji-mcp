# maji-mcp's tasks, in three groups: running maji-mcp, developing it, and debugging a
# running stack. `make` on its own lists every target.
#
# `make help` prints each `##@ ` line as a heading and each target with the text after
# `## ` on its line, in the order of this file, so a target added with that text is
# listed under the heading above it.
#
# Docker commands go to the Docker CLI's current context (Rancher Desktop, Docker
# Desktop, Colima, or a daemon on this machine) unless DOCKER_HOST is set. The Docker
# SDK for Python, which the Docker tests use, does not read contexts, so the context's
# address is exported as DOCKER_HOST for every command.

ifndef DOCKER_HOST
DOCKER_HOST := $(shell docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null)
endif
ifneq ($(DOCKER_HOST),)
export DOCKER_HOST
endif

# Settings, each changed on the command line: `make up GATEWAY_PORT=9001`.
#
PRINCIPAL ?= claude-code
GATEWAY_PORT ?= 8765
CONTROL_PORT ?= 8767
BRIDGE_PORT ?= 8766
BRIDGE_ID ?= emacs-org
SCOPE ?= user
EMACS_ORG_MCP ?= $(HOME)/src/emacs-org-mcp
BACKUPS ?= $(HOME)/.local/share/maji-mcp/backups
DEMO_DIR ?= $(HOME)/.cache/maji-mcp/demo
DEMO_PORT ?= 8768
DESKTOP_CONFIG ?= $(HOME)/Library/Application Support/Claude/claude_desktop_config.json
STATE ?= $(HOME)/.local/state/maji-mcp

# The secrets directory is outside the project, in a hidden folder of your home
# directory. The runner never mounts a hidden folder at the top of your home
# directory, and refuses any folder that holds the secrets directory, so with the
# secrets there a script can be granted this whole checkout as one path.
#
SECRETS ?= $(HOME)/.config/maji-mcp/secrets

# The two images, and the tag they are built and run under. The stack runs `latest`.
# The Docker tests build and run `test`, so a test run never replaces the images a
# stack on this machine is using: the runner starts every executor from the runtime
# image's tag, so a rebuilt `latest` would change the running stack's next script.
#
TAG ?= latest
TEST_TAG ?= test
SERVICE_IMAGE ?= maji-service:$(TAG)
RUNTIME_IMAGE ?= maji-python-runtime:$(TAG)

# What `docker compose` reads from its environment (compose.yaml).
#
STACK = MAJI_SECRETS=$(SECRETS) MAJI_SECRETS_DIR=$(abspath $(SECRETS)) \
	MAJI_GATEWAY_PORT=$(GATEWAY_PORT) MAJI_CONTROL_PORT=$(CONTROL_PORT) \
	MAJI_SERVICE_IMAGE=$(SERVICE_IMAGE) MAJI_RUNTIME_IMAGE=$(RUNTIME_IMAGE)
COMPOSE = $(STACK) docker compose

# The runner joins the group that owns the Docker socket, to start executors without
# running as root. The group is the one a container sees on the socket, which differs
# from one Docker to another, so a container of the service image reads the group
# from the socket. `$(call socket_gid,<image>)` is shell text that gives
# MAJI_DOCKER_GID when it is set, and the socket's group otherwise.
#
socket_gid = $${MAJI_DOCKER_GID:-$$(docker run --rm --entrypoint stat \
	-v /var/run/docker.sock:/var/run/docker.sock $(1) -c %g /var/run/docker.sock)}

.PHONY: help up down setup token connect-claude-code connect-claude-desktop \
	reconnect-claude-code bridge bridge-stop bridge-fg bridge-logs \
	backup restore demo install check \
	format test test-docker test-local test-browser test-all web web-check images \
	image-service image-runtime runtime-lock clean status logs restart shell \
	library-use

.DEFAULT_GOAL := help

help:  ## List every command (`make` on its own does the same)
	@awk 'BEGIN {FS = ":.*## "} \
	  /^##@ / {printf "\n\033[1m%s\033[0m\n", substr($$0, 5); next} \
	  /^[A-Za-z0-9_-]+:.*## / {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}' \
	  $(MAKEFILE_LIST)
	@printf '\nSettings go on the command line, such as PRINCIPAL=, GATEWAY_PORT=,\n'
	@printf 'CONTROL_PORT= and SECRETS=. docs/using.md lists them all.\n'
	@printf 'Secrets directory: %s\n' "$(SECRETS)"

##@ Running maji-mcp

up: images setup  ## Build, add missing secrets, start the stack, open passkey setup
	MAJI_DOCKER_GID="$(call socket_gid,$(SERVICE_IMAGE))" $(COMPOSE) up -d
	@$(STACK) uv run maji-setup --secrets $(SECRETS) setup-page --port $(CONTROL_PORT)

down:  ## Stop the stack, keeping its volumes
	$(COMPOSE) down

setup:  ## Create any missing secrets, keeping each one that exists
	uv run maji-setup --secrets $(SECRETS) init --principal $(PRINCIPAL)

token:  ## Replace PRINCIPAL's gateway token only (see reconnect-claude-code)
	uv run maji-setup --secrets $(SECRETS) token $(PRINCIPAL)

# Connecting an app. Each app has a target named for it, `connect-<app>`, so another
# app gets a target of its own beside these. An app without a target connects to
# http://127.0.0.1:GATEWAY_PORT/mcp with PRINCIPAL's token as a bearer token
# (docs/using.md).
#
# Claude Code runs bin/maji-headers each time it connects. The helper reads
# PRINCIPAL's token from its file, so the token is never copied into Claude Code's
# configuration, and names a new session: Claude Code speaks an MCP revision without
# sessions of its own. An existing `maji` server is left alone.
#
connect-claude-code:  ## Add maji to Claude Code (SCOPE=local for this project alone)
	@test -f $(SECRETS)/$(PRINCIPAL).token || { \
	  echo "No $(SECRETS)/$(PRINCIPAL).token: $(PRINCIPAL)'s token was made by hand."; \
	  echo "'make reconnect-claude-code' replaces it with one kept there, and connects"; \
	  echo "with that."; \
	  exit 1; }
	@if claude mcp get maji >/dev/null 2>&1; then \
	  echo "Claude Code already has a server named maji. If it doesn't connect,"; \
	  echo "'make reconnect-claude-code' replaces it."; \
	  exit 1; fi
	@entry=$$(uv run maji-setup --secrets $(SECRETS) claude-code-json \
	  --principal $(PRINCIPAL) --port $(GATEWAY_PORT) \
	  --helper $(abspath bin/maji-headers)) && \
	  claude mcp add-json --scope $(SCOPE) maji "$$entry"

# Claude Desktop's chats reach local MCP servers only as commands it starts, so it
# starts maji-relay (`src/maji_mcp/host/relay.py`), which reads PRINCIPAL's token from
# its file for each request and passes each call on to the gateway. A token replaced
# by `make reconnect-claude-code` is used from the next request on, with nothing to
# change here. Desktop keeps its own settings in the same file, so the file is changed
# only while Desktop is closed. REPLACE=1 puts this entry in place of a different
# `maji` already there, as after the secrets directory or the checkout has moved.
#
connect-claude-desktop:  ## Add maji to Claude Desktop's chats (with Desktop closed)
	@test -f $(SECRETS)/$(PRINCIPAL).token || { \
	  echo "No $(SECRETS)/$(PRINCIPAL).token: $(PRINCIPAL)'s token was made by hand."; \
	  echo "'make reconnect-claude-code' replaces it with one kept there."; \
	  exit 1; }
	@if pgrep -xq Claude; then \
	  echo "Quit Claude Desktop first: it keeps its own settings in the file maji is"; \
	  echo "added to. Then run 'make connect-claude-desktop' again, and open Desktop."; \
	  exit 1; fi
	@uv run maji-setup --secrets $(SECRETS) connect-claude-desktop \
	  --config "$(DESKTOP_CONFIG)" --principal $(PRINCIPAL) --port $(GATEWAY_PORT) \
	  $(if $(REPLACE),--replace)

# For a `maji` that doesn't connect, as when its token was replaced or made by hand.
# PRINCIPAL gets a new token, and any client still holding the old one stops working
# with it. The gateway reads token hashes only at start, so it is restarted. Claude
# Code can keep a server in each of its scopes, so `maji` is removed from each one
# it's in before it is added again.
#
reconnect-claude-code: token  ## When maji won't connect: a new token, and maji added again
	$(COMPOSE) restart gateway
	@for scope in local project user; do \
	  if claude mcp remove maji -s $$scope >/dev/null 2>&1; then \
	    echo "removed maji from Claude Code's $$scope scope"; fi; \
	done
	@$(MAKE) --no-print-directory connect-claude-code

# The bridge serves a stdio MCP server that acts as you, such as emacs-org-mcp, over
# HTTP on 127.0.0.1 behind a token, so the broker can call it (docs/design/broker.md).
# It runs on your machine, as you, for as long as the broker is to reach that server,
# so `make bridge` starts it in the background: its pid and what it writes are kept
# in STATE, and `make bridge-stop` stops it.
#
# BRIDGE_SERVER is the server's command. By default it is emacs-org-mcp
# (https://github.com/scanner/emacs-org-mcp), from a checkout at EMACS_ORG_MCP, and
# `make bridge` says where to get one when the checkout is missing. A BRIDGE_SERVER
# given on the command line is started as it is.
#
# emacs-org-mcp is given its settings as flags: started without them, it ignores its
# environment variables. ORG_DIR serves another org directory, such as a copy. The
# server is started without this project's VIRTUAL_ENV, which its own uv would warn
# about on every start.
#
# The broker connects to its servers as it starts, so a bridge started while the
# stack is up is followed by a restart of the broker.
#
BRIDGE_SERVER ?= env -u VIRTUAL_ENV uv --directory $(EMACS_ORG_MCP) run server.py \
	$(if $(ORG_DIR),--org-dir=$(ORG_DIR))
BRIDGE = .venv/bin/maji-bridge --id $(BRIDGE_ID) --port $(BRIDGE_PORT) \
	--token-file $(SECRETS)/$(BRIDGE_ID).token -- $(BRIDGE_SERVER)
BRIDGE_PID = $(STATE)/bridge-$(BRIDGE_ID).pid
BRIDGE_LOG = $(STATE)/bridge-$(BRIDGE_ID).log

# True when the pid file names a running bridge, and leaves its pid in `$pid`. A pid
# that has since gone to another program doesn't count. A stopped bridge's pid file
# is emptied, which reads the same as no file.
#
BRIDGE_RUNNING = { pid=$$(cat "$(BRIDGE_PID)" 2>/dev/null); [ -n "$$pid" ] \
	&& ps -o command= -p "$$pid" 2>/dev/null \
	| grep -q -- "maji-bridge --id $(BRIDGE_ID) "; }

bridge: setup  ## Start the bridge to emacs-org-mcp, in the background
	@$(if $(filter file,$(origin BRIDGE_SERVER)),test -f "$(EMACS_ORG_MCP)/server.py" || { \
	  echo "bridge: no emacs-org-mcp checkout at $(EMACS_ORG_MCP)."; \
	  echo "Clone https://github.com/scanner/emacs-org-mcp there. For a checkout"; \
	  echo "somewhere else: make bridge EMACS_ORG_MCP=<path>"; exit 1; })
	@mkdir -p "$(STATE)" && chmod 700 "$(STATE)"
	@if $(BRIDGE_RUNNING); then \
	  echo "bridge: already running (pid $$pid); 'make bridge-stop' stops it"; \
	  exit 0; fi; \
	(umask 077; nohup $(BRIDGE) >> "$(BRIDGE_LOG)" 2>&1 < /dev/null & \
	  echo $$! > "$(BRIDGE_PID)"); \
	pid=$$(cat "$(BRIDGE_PID)"); tries=0; \
	until [ "$$(curl -s -o /dev/null -m 1 -w '%{http_code}' -X POST \
	    http://127.0.0.1:$(BRIDGE_PORT)/mcp)" = 401 ] && kill -0 $$pid 2>/dev/null; do \
	  tries=$$((tries + 1)); \
	  if ! kill -0 $$pid 2>/dev/null || [ $$tries -gt 150 ]; then \
	    echo "bridge: didn't start. The end of $(BRIDGE_LOG):"; \
	    tail -n 15 "$(BRIDGE_LOG)"; \
	    kill $$pid 2>/dev/null; : > "$(BRIDGE_PID)"; exit 1; fi; \
	  sleep 0.2; done; \
	echo "bridge: serving $(BRIDGE_ID) on 127.0.0.1:$(BRIDGE_PORT), in the background (pid $$pid)"; \
	echo "bridge: 'make bridge-stop' stops it, and 'make bridge-logs' shows what it wrote"; \
	if [ -n "$$($(COMPOSE) ps --status running -q broker 2>/dev/null)" ]; then \
	  echo "bridge: restarting the broker, which connects to the bridge as it starts"; \
	  $(COMPOSE) restart broker; fi

bridge-stop:  ## Stop the bridge
	@if ! $(BRIDGE_RUNNING); then echo "bridge: not running"; exit 0; fi; \
	kill $$pid; tries=0; \
	while kill -0 $$pid 2>/dev/null && [ $$tries -lt 50 ]; do \
	  tries=$$((tries + 1)); sleep 0.1; done; \
	: > "$(BRIDGE_PID)"; echo "bridge: stopped"

bridge-fg: setup  ## Run the bridge in the foreground, to watch it (Ctrl-C stops it)
	$(BRIDGE)

bridge-logs:  ## Show what the bridge wrote (TAIL=50, FOLLOW=1)
	@tail -n $(or $(TAIL),50) $(if $(FOLLOW),-f) "$(BRIDGE_LOG)"

# A backup holds the queue's facts and diffs, which can include work data, so it goes
# into a directory only you can read, outside the repository and any synced folder.
# The copy is taken while the control plane runs, written under a `.partial` name and
# renamed once it is complete. Restoring stops the control plane first, and refuses a
# backup that is damaged or belongs to another instance.
#
backup:  ## Copy the control plane's database into BACKUPS
	@mkdir -p $(BACKUPS) && chmod 700 $(BACKUPS)
	@f=$(BACKUPS)/control-$$(date +%Y%m%d-%H%M%S).db; \
	  if (umask 077; $(COMPOSE) exec -T control maji-control backup \
	    > $$f.partial); then mv $$f.partial $$f && echo "backup: $$f"; \
	  else echo "backup: failed; $$f.partial holds what was copied"; exit 1; fi

restore:  ## Put a backup back: make restore FROM=<file>
	@test -n "$(FROM)" || { echo "make restore FROM=<backup file>"; exit 1; }
	$(COMPOSE) stop control
	@$(COMPOSE) run --rm -T --no-deps control maji-control restore \
	  < $(FROM); status=$$?; $(COMPOSE) start control; exit $$status

# A control plane of its own, with a database whose instance is `demo`, seeded with
# sample requests; nothing in it is ever applied. RESET=1 starts its queue afresh and
# keeps its passkey; RESET=all removes that too. It opens the setup page for a passkey
# of its own, named "maji-mcp on demo".
#
demo: web  ## Try the queue's web client on a demo queue (RESET=1 empties it)
	uv run maji-control demo --dir $(DEMO_DIR) --port $(DEMO_PORT) \
	  --web-dir web/dist $(if $(RESET),--reset $(filter all,$(RESET)))

##@ Developing maji-mcp

install:  ## Install the Python and web client dependencies, as locked
	uv sync
	cd web && npm ci

check:  ## Lint, and check formatting and types
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy

format:  ## Fix what lint can fix, and format the code
	uv run ruff check --fix src tests
	uv run ruff format src tests

test:  ## Run the tests that need no Docker, local checkouts or browser
	uv run pytest

# The web client: its types come from api/openapi.yaml on every build and check, so
# a client that drifts from the contract fails here.
#
web:  ## Build the web client into web/dist
	cd web && npm ci && npm run build

web-check:  ## Check the web client's types, and run its tests
	cd web && npm ci && npm run typecheck && npm test

# Drives the built web client in your installed Chrome, with a virtual passkey.
#
test-browser: web  ## Drive the built web client in Chrome, with a virtual passkey
	uv run pytest -m browser

# The tests start stacks of their own, with their own volumes, ports and secrets, from
# images built here under TEST_TAG (see TAG, above).
#
test-docker:  ## Build :test images, and run the tests that start their own stacks
	@$(MAKE) --no-print-directory images TAG=$(TEST_TAG)
	MAJI_DOCKER_GID="$(call socket_gid,maji-service:$(TEST_TAG))" \
	  MAJI_SERVICE_IMAGE=maji-service:$(TEST_TAG) \
	  MAJI_RUNTIME_IMAGE=maji-python-runtime:$(TEST_TAG) uv run pytest -m docker

test-local:  ## Run the tests that need local checkouts, such as emacs-org-mcp
	uv run pytest -m local

test-all: check test web-check test-browser test-docker  ## Run every check and test but test-local

images: image-service image-runtime  ## Build both images, as :latest (TAG= for another tag)

image-service:  ## Build the service image: broker, runner, gateway, control plane
	docker build -f images/service/Dockerfile -t $(SERVICE_IMAGE) .

image-runtime:  ## Build the Python runtime image, where scripts run
	docker build -f images/python-runtime/Dockerfile -t $(RUNTIME_IMAGE) .

# The runtime's libraries resolve from PyPI alone, whatever index uv or pip is set up
# to use here, and only from releases at least LOCK_AGE_DAYS old, so a release that is
# withdrawn soon after it appears never reaches the image. The lock takes only wheels,
# as the image installs only wheels, and holds a hash of every wheel for every
# platform; the image installs nothing without one.
#
LOCK_AGE_DAYS ?= 14

runtime-lock:  ## Pin the runtime's libraries, after changing its requirements.in
	env -u UV_INDEX_URL -u UV_EXTRA_INDEX_URL -u UV_INDEX -u UV_DEFAULT_INDEX \
	  uv pip compile --no-config --default-index https://pypi.org/simple \
	  --universal --python-version 3.14 --only-binary :all: --generate-hashes \
	  --exclude-newer $$(python3 -c "import datetime as d; print(d.date.today() - d.timedelta(days=$(LOCK_AGE_DAYS)))") \
	  --custom-compile-command "make runtime-lock" \
	  images/python-runtime/requirements.in -o images/python-runtime/requirements.txt

# Removes what a build or a test run left: never a stack's volumes, its secrets, or
# the `latest` images. An image is removed by its tag, so one that `latest` also names
# stays.
#
clean:  ## Remove build output, tool caches and the :test images
	rm -rf web/dist .pytest_cache .mypy_cache .ruff_cache
	-docker image rm maji-service:$(TEST_TAG) maji-python-runtime:$(TEST_TAG) 2>/dev/null

##@ Debugging a running stack

# SERVICE is one of broker, runner, gateway or control. Left out, `logs` and `restart`
# cover all four.
#
status:  ## Show the stack's containers and the control plane's health
	$(COMPOSE) ps
	@curl -fsS -m 3 http://localhost:$(CONTROL_PORT)/api/v0/health \
	  && echo || echo "control plane: no answer on port $(CONTROL_PORT)"
	@if $(BRIDGE_RUNNING); then \
	  echo "bridge: $(BRIDGE_ID) on 127.0.0.1:$(BRIDGE_PORT) (pid $$pid)"; \
	else echo "bridge: not running ('make bridge' starts it)"; fi

logs:  ## Show the last lines of the logs (SERVICE=, TAIL=200, FOLLOW=1)
	$(COMPOSE) logs --tail=$(or $(TAIL),200) $(if $(FOLLOW),--follow) $(SERVICE)

restart:  ## Restart the stack's services, or one: make restart SERVICE=broker
	$(COMPOSE) restart $(SERVICE)

library-use:  ## Count how scripts used each library they can import (DAYS=30)
	@$(COMPOSE) exec -T broker maji-library-use --run-log /var/lib/maji/runs.db \
	  --days $(or $(DAYS),30)

shell:  ## Open a shell in a service's container: make shell SERVICE=broker
	@test -n "$(SERVICE)" || { echo "make shell SERVICE=broker|runner|gateway|control"; exit 1; }
	$(COMPOSE) exec $(SERVICE) sh
