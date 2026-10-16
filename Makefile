# orca-creality-cfs-sync: OrcaSlicer plugin that publishes the Creality CFS as Moonraker lane data
#
# `make` or `make help` lists the targets. This Makefile is a facade: each
# target delegates to the real tool. A new rule needs a `## description` on
# the same line to show up in help.

SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
MAKEFLAGS += --warn-undefined-variables --no-builtin-rules --no-print-directory
.DEFAULT_GOAL := help
.DELETE_ON_ERROR:

# ---------------------------------------------------------------------------
# Variables (?= allows overriding: `make check RUFF=ruff`)
# ---------------------------------------------------------------------------

APP    ?= $(notdir $(CURDIR))
PYTHON ?= python3
RUFF   ?= .venv/bin/ruff

# Colours only when stdout is a terminal (pipes and CI stay clean)
BOLD  :=
CYAN  :=
GREEN :=
RESET :=
ifneq ($(shell [ -t 1 ] && echo tty),)
  BOLD  := $(shell tput bold 2>/dev/null)
  CYAN  := $(shell tput setaf 6 2>/dev/null)
  GREEN := $(shell tput setaf 2 2>/dev/null)
  RESET := $(shell tput sgr0 2>/dev/null)
endif

##@ General

.PHONY: help
help: ## List the available targets
	@awk 'BEGIN { FS = ":.*##"; printf "\n$(BOLD)$(APP)$(RESET)\n\nUsage: make $(CYAN)<target>$(RESET)\n" } \
	  /^##@/ { printf "\n$(BOLD)%s$(RESET)\n", substr($$0, 5) } \
	  /^[a-zA-Z0-9_.\/-]+:.*?##/ { printf "  $(CYAN)%-18s$(RESET) %s\n", $$1, $$2 } \
	  END { printf "\n" }' $(MAKEFILE_LIST)

.PHONY: setup
setup: ## Create .venv with ruff (the plugin and the tests only need the stdlib)
	@echo "$(GREEN)▸ setup$(RESET)"
	$(PYTHON) -m venv .venv
	.venv/bin/pip install --quiet ruff

##@ Quality

.PHONY: lint
lint: ## Static lint with ruff, changes nothing
	@echo "$(GREEN)▸ lint$(RESET)"
	$(RUFF) check .

.PHONY: test
test: ## Run the tests against a fake Moonraker, no printer needed
	@echo "$(GREEN)▸ test$(RESET)"
	$(PYTHON) -I tests/test_creality_cfs_sync.py

.PHONY: check
check: lint test ## Everything CI runs: lint and test
	@echo "$(GREEN)✓ check ok$(RESET)"
