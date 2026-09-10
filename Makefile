.PHONY: install setup test coverage lint start stop status clean help \
        e2e-up e2e-down e2e-test e2e-logs e2e-reset e2e-shell e2e-acg \
        e2e-dump e2e-probe e2e-probe-mm

RUNTIME_DIR := $(HOME)/.agentcoop
CONFIG      := $(RUNTIME_DIR)/config.yaml

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n\nTargets:\n"} \
	     /^[a-zA-Z0-9_-]+:.*?##/ { printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

install: ## Install dependencies (uv sync)
	uv sync

setup: ## Run the interactive setup wizard (idempotent — skips if config exists)
	uv run coop onboard --repo-path "$(CURDIR)"

test: ## Run test suite
	uv run pytest tests/ -v --tb=short

coverage: ## Run the test suite with branch coverage and print the gaps
	uv run pytest tests/unit tests/integration -q --timeout=120 \
		--cov=gateway --cov-branch --cov-report=term-missing:skip-covered

lint: ## Run ruff check (if installed)
	@if command -v ruff >/dev/null 2>&1; then \
	    ruff check gateway/ tests/; \
	elif uv run ruff --version >/dev/null 2>&1; then \
	    uv run ruff check gateway/ tests/; \
	else \
	    echo "ruff not installed — skipping lint"; \
	fi

start: ## Start daemon
	uv run coop start

stop: ## Stop daemon
	uv run coop stop

status: ## Show daemon status
	uv run coop status

clean: ## Remove __pycache__, .coverage, dist/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true
	rm -f .coverage coverage.json .coverage.*
	rm -rf dist/ build/ *.egg-info

# =============================================================================
# E2E Test Targets
# Requires: Docker, ANTHROPIC_API_KEY (for Claude Code)
# =============================================================================

E2E_COMPOSE := tests/e2e/docker-compose.yml
E2E_RC_URL  := http://localhost:3100
E2E_MM_URL  := http://localhost:8065

# Both platforms must be bootstrapped BEFORE AgentCoop starts, and the ordering is
# not something compose can express. `depends_on: service_healthy` orders
# CONTAINERS; it says nothing about whether the `acg_bot` ACCOUNT exists yet.
# Each connector logs in as that account at startup, and a connector that
# cannot connect is FATAL to the whole daemon — the failure is re-raised out
# of the settle phase and the process exits 1. So an unseeded platform does not
# degrade to "that platform's tests time out": it takes the gateway down,
# every test with it, and under `restart: unless-stopped` it becomes a crash
# loop. That is what the running-state guard in e2e-test reports.
e2e-up: ## Start RC + Mattermost + AgentCoop for E2E tests (idempotent)
	@echo "==> Starting MongoDB + Rocket.Chat ..."
	docker compose -f $(E2E_COMPOSE) up -d mongodb rocketchat
	@echo "==> Starting Postgres + Mattermost ..."
	docker compose -f $(E2E_COMPOSE) up -d postgres mattermost
	@echo "==> Running RC setup (creating RC accounts) ..."
	uv run python tests/e2e/setup.py --rc-url $(E2E_RC_URL)
	@echo "==> Running MM setup (creating MM team + accounts) ..."
	uv run python tests/e2e/mm_setup.py --mm-url $(E2E_MM_URL)
	@echo "==> Starting AgentCoop ..."
	docker compose -f $(E2E_COMPOSE) up -d acg
	@echo "==> Done. Run 'make e2e-test' to execute the test suite."

e2e-test: ## Run E2E tests (requires e2e-up first, needs CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY)
	@docker container inspect acg-e2e >/dev/null 2>&1 || { \
	    echo "ERROR: container 'acg-e2e' is not up — run 'make e2e-up' first."; \
	    echo "       'make e2e-test' only runs the suite; it starts nothing."; \
	    exit 1; }
	@# "Exists" is not "running": `docker container inspect` succeeds for an
	@# exited or restarting container, and `acg` carries restart:
	@# unless-stopped — so a crash-loop (an unseeded Mattermost makes connect()
	@# fatal) would otherwise be reported below as a credentials problem,
	@# sending the operator to fix the wrong thing.
	@#
	@# `.State.Status`, not `.State.Running`: a crash-looping container is
	@# INTERMITTENTLY Running=true — the restart backoff starts at 100ms and
	@# each doomed boot spends seconds up while it tries to log in — so a
	@# Running check races the very case this guard is for. Status reads
	@# `restarting` throughout.
	@test "$$(docker container inspect -f '{{.State.Status}}' acg-e2e 2>/dev/null)" = "running" || { \
	    echo "ERROR: container 'acg-e2e' exists but is not running (state: $$(docker container inspect -f '{{.State.Status}}' acg-e2e 2>/dev/null))."; \
	    echo "       This is a startup failure, not a config or token problem."; \
	    echo "       'docker logs acg-e2e' has the reason; 'make e2e-dump'"; \
	    echo "       writes everything to ./e2e-logs."; \
	    exit 1; }
	@# The credentials that matter are the ones INSIDE the container, not the
	@# ones in this shell: compose bakes environment in at container CREATION,
	@# so a stack brought up without a token stays without one however the run
	@# is invoked. The previous check read THIS shell, found a token there and
	@# said nothing, while every Claude test timed out for eight minutes and
	@# reported "no matching post" as if delivery were broken.
	@#
	@# Stated as what it observed, not as a verdict: the compose also mounts
	@# ~/.claude, so a host with a logged-in CLI may supply credentials by a
	@# path this cannot see. Hence the override — a check that blocks a working
	@# setup is worse than the silence it replaced.
	@# Truthy values only. `test -n` accepted ANY non-empty value, so
	@# E2E_SKIP_CRED_CHECK=0 silently skipped the check — and the message below
	@# advertises the flag as 0/1, which is exactly what makes "=0" a natural
	@# thing to type when turning it back on.
	@case "$(E2E_SKIP_CRED_CHECK)" in 1|true|yes|on) exit 0;; esac; \
	docker exec acg-e2e sh -c 'test -n "$$CLAUDE_CODE_OAUTH_TOKEN$$ANTHROPIC_API_KEY"' || { \
	    echo "ERROR: neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY is"; \
	    echo "       set inside the running acg-e2e container."; \
	    echo "       Exporting them now does not help: the container was created"; \
	    echo "       without them, and compose fixes environment at creation."; \
	    echo "       Recreate just this container — the platforms and their"; \
	    echo "       seeded accounts survive, so this costs seconds, not a"; \
	    echo "       Rocket.Chat boot:"; \
	    echo "         CLAUDE_CODE_OAUTH_TOKEN=... docker compose -f $(E2E_COMPOSE) \\"; \
	    echo "             up -d --build --force-recreate acg"; \
	    echo "       (or ANTHROPIC_API_KEY=...). Reach for 'make e2e-down' only"; \
	    echo "       if you also want the platform data gone — it takes -v."; \
	    echo "       Without credentials every Claude test fails as a 120s"; \
	    echo "       timeout, which reads like a delivery bug."; \
	    echo "       If your credentials come from the ~/.claude mount instead,"; \
	    echo "       re-run with E2E_SKIP_CRED_CHECK=1."; \
	    exit 1; }
	uv run pytest tests/e2e/ -v -s --timeout=180 \
	    --ignore=tests/unit --ignore=tests/integration

e2e-logs: ## Tail logs for all E2E containers
	docker compose -f $(E2E_COMPOSE) logs -f

e2e-shell: ## Shell into a running E2E container (S=acg|rocketchat|mongodb|mattermost|postgres, default acg)
	docker compose -f $(E2E_COMPOSE) exec $(or $(S),acg) bash

e2e-acg: ## Run an AgentCoop command inside the container (e.g. make e2e-acg C="list")
	@test -n "$(C)" || (echo "usage: make e2e-acg C=\"list\"" && exit 1)
	docker compose -f $(E2E_COMPOSE) exec acg AgentCoop $(C)

e2e-dump: ## Write full container logs + state to ./e2e-logs (same set CI uploads)
	@mkdir -p e2e-logs
	@for c in acg-e2e acg-e2e-rocketchat acg-e2e-mongodb \
	          acg-e2e-mattermost acg-e2e-postgres; do \
	    docker logs $$c > e2e-logs/$$c.log 2>&1 || true; \
	done
	@docker compose -f $(E2E_COMPOSE) ps > e2e-logs/ps.txt 2>&1 || true
	@docker compose -f $(E2E_COMPOSE) config > e2e-logs/resolved-compose.yml 2>&1 || true
	@docker exec acg-e2e sh -c 'cat /root/.agentcoop/gateway.log' \
	    > e2e-logs/acg-gateway.log 2>&1 || true
	@docker exec acg-e2e coop list --all > e2e-logs/acg-list.txt 2>&1 || true
	@echo "==> Wrote e2e-logs/ ($$(ls e2e-logs | wc -l | tr -d ' ') files)"

e2e-probe: ## Re-verify the RC platform behaviour design §6 depends on, against the running stack
	uv run python scripts/probe_a1_rc.py \
	    --url $(E2E_RC_URL) \
	    --probe-user test_user --probe-password test_user_e2e_2024 \
	    --admin-user admin --admin-password admin_e2e_2024 \
	    --member-room acg-e2e-claude --outside-room acg-e2e-outside

# The probe answers a different question from the E2E test, which is why both
# exist: this one asks what MATTERMOST does (no AgentCoop involved), and is what to
# reach for when a version bump makes the pin guard fail. The E2E test asks
# whether AgentCoop honours it. A probe pass with an E2E failure means the runtime
# regressed; both failing means the platform changed under us.
#
# NOTE: this drives the acg_bot ACCOUNT — the same one the connector uses —
# and its membership isolation case JOINS the bot to the outside channel and
# removes it again. Two residues are possible if it is killed partway, and
# `mm_setup.py` only clears the first:
#   * the bot left inside the outside channel — mm_setup removes it;
#   * a watcher RECORD for that channel, created because the join made the
#     channel deliver events and the deliberately-broad `acg-e2e-mm-*` glob
#     claims it. mm_setup does not touch records, so clear it by hand:
#     make e2e-acg C="expire 'mm-e2e:acg-e2e-mm-outside'"
# test_mm_membership_delivery.py checks both up front and names the remedy,
# rather than reporting the residue as this run's leak.
e2e-probe-mm: ## Re-verify the Mattermost platform behaviour design §6.2/§6.3 depends on
	uv run python scripts/probe_a2_mm.py \
	    --url $(E2E_MM_URL) --team acg-e2e \
	    --probe-user acg_bot --probe-password acg_bot_e2e_2024 \
	    --admin-user mmadmin --admin-password mmadmin_e2e_2024 \
	    --member-channel acg-e2e-mm-claude --outside-channel acg-e2e-mm-outside

e2e-down: ## Stop and remove all E2E containers and volumes
	docker compose -f $(E2E_COMPOSE) down -v

e2e-reset: e2e-down e2e-up ## Full reset: tear down, recreate, re-setup
