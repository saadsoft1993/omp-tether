# Tasks for this repository. Needs Bun (1.4.4 or newer), Python 3.10 or newer, ruff for `lint`, and omp for the
# integration test and the gate. Point OMP_BIN and BUN_BIN at specific binaries if PATH has the wrong ones.
BUN    ?= $(or $(BUN_BIN),bun)
PYTHON ?= python3
export OMP_BIN BUN_BIN
export PYTHONDONTWRITEBYTECODE = 1

PLUGIN := plugins/omp-session-control
UNIT_TESTS := $(wildcard $(PLUGIN)/tests/unit/*.test.ts)
SERVICES := plugins/omp-services
SERVICES_UNIT_TESTS := $(wildcard $(SERVICES)/tests/unit/*.test.ts)
RELAY := plugins/omp-private-relay
DASH := plugins/omp-dashboard
MEMORY := plugins/omp-memory
UI_BUN := $(or $(BUN_BIN),bun)
# The omp checkout the relay's web client is built from (at a release tag); OMP_SRC is the checkout whose types are used by typecheck.
WEB_OMP_SRC ?= $(OMP_SRC)
SHELL_SCRIPTS := $(RELAY)/bin/omp-private-relay $(wildcard $(RELAY)/scripts/*.sh) $(SERVICES)/bin/omp-services $(SERVICES)/install.sh $(DASH)/bin/omp-dashboard $(MEMORY)/bin/omp-memory $(MEMORY)/scripts/*.sh scripts/*.sh tools/public-tree-check/*.sh


.PHONY: test-memory memory-vendor-check test-dashboard test-dashboard-mutation test-dashboard-conformance test-ui ui-install build-ui verify-ui typecheck-ui help check check-contrib test test-unit test-tools test-services test-relay test-mutation test-conformance test-integration gate gate-selftest lint lint-shell shellcheck typecheck release-check release-check-core release-check-relay verify-web export-gate public-check public-selftest bun-version

help:
	@echo "make check             everything below, in order (check-contrib, then the maintainer's publication gate)"
	@echo "make check-contrib     everything except the publication gate: what a contributor runs before a pull request"
	@echo "make test-unit         unit suite, conformance suite against the extension, crash tests (no omp needed)"
	@echo "make test-relay        omp-private-relay: real relay processes on free ports, fake omp, no network (no omp needed)"
	@echo "make test-mutation     revert each guard in a copy of omp-private-relay: its tests must fail on every copy"
	@echo "make release-check     the publishing gate: release-check-core (check with the type check required, then export-gate) and release-check-relay"
	@echo "make release-check-relay  verify-web plus the relay suite with the archive and omp-services schemas required (WEB_OMP_SRC OMP_TAG ASSET SERVICES_SCHEMAS; WEB_OMP_SRC defaults to OMP_SRC)"
	@echo "make verify-web        rebuild the relay web client from an omp tag and compare with its pin (WEB_OMP_SRC=<checkout> OMP_TAG=vX.Y.Z; needs network once)"
	@echo "make test-conformance  pairing-store/1 conformance suite against the relay's reader and writer"
	@echo "make test-tools        tests of the publication checker/exporter/gate, the load gate, the test kit"
	@echo "make test-integration  real omp TUI in a scratch HOME, fake model, scratch relay on a random port"
	@echo "make gate              install every plugin from the local catalog and load it headless"
	@echo "make gate-selftest     prove the gate rejects a throwing, a silent and a missing extension"
	@echo "make lint              ruff check and format check, shellcheck on the shell scripts"
	@echo "make typecheck         type-check against omp's types (needs OMP_SRC=<omp checkout>)"
	@echo "make export-gate       load-test a fresh export of the committed candidate"
	@echo "make public-check      allowlist, identity, wording and secret checks on the tree and a fresh export"
	@echo "make public-selftest   check the checker"
	@echo "make test-services     omp-services: extension unit tests, Python suite, health/1 conformance suite and its self-test"
	@echo "make test-memory      omp-memory: the bundled importer's Bun tests from the plugin copy (installs the npm library into a scratch dir once), the Python suite, and the vendor drift check"
	@echo "make memory-vendor-check  compare plugins/omp-memory/vendor with the source tree it was copied from (OMP_MEMORY_SOURCE_DIR; skipped when unset)"
	@echo "make shellcheck        alias of lint-shell"

check: check-contrib public-check

# Everything except the publication gate. The gate (public-check) requires every commit to carry the maintainer's identity and
# a clean working tree, so the maintainer runs it before a release; contributors run check-contrib.
check-contrib: bun-version lint $(if $(OMP_SRC),typecheck) test-unit test-relay test-mutation test-conformance test-dashboard test-dashboard-conformance test-dashboard-mutation test-memory test-ui verify-ui typecheck-ui test-tools test-services test-integration gate gate-selftest public-selftest

test: test-unit

# Bun's mock.module is process-wide, so each unit file runs in its own process.
test-unit: bun-version
	@set -e; for f in $(UNIT_TESTS); do echo "== $$f"; (cd $(PLUGIN) && $(BUN) test $${f#$(PLUGIN)/}); done

# Each relay test starts its own relay processes on free ports; the files share nothing, so one Bun process runs them all.
test-relay: bun-version
	cd $(RELAY) && $(BUN) test tests/unit

test-mutation: bun-version
	cd $(RELAY) && $(BUN) test tests/mutation

# The gate for publishing the relay's web client: the exact archive is verified against a fresh build from the tag, the whole
# relay suite runs with the archive and omp-services' schemas REQUIRED (a missing artifact fails instead of skipping), and the
# versions and hashes tested are printed. The browser run against real omp is a documented manual step (README, "Develop").
release-check-relay:
	@test -n "$(WEB_OMP_SRC)" -a -n "$(OMP_TAG)" -a -n "$(ASSET)" -a -n "$(SERVICES_SCHEMAS)" || { echo "usage: make release-check-relay WEB_OMP_SRC=<omp checkout at the tag> OMP_TAG=vX.Y.Z ASSET=<omp-private-relay-web-X.Y.tar> SERVICES_SCHEMAS=<omp-services schemas dir>"; exit 2; }
	$(RELAY)/scripts/verify-web.sh --omp-src "$(WEB_OMP_SRC)" --tag "$(OMP_TAG)" --omp-bin "$$(command -v $(or $(OMP_BIN),omp))"
	cd $(RELAY) && OMP_PRIVATE_RELAY_REQUIRE_ASSET=1 OMP_PRIVATE_RELAY_TEST_ASSET="$(abspath $(ASSET))" OMP_PRIVATE_RELAY_REQUIRE_SERVICES_SCHEMAS=1 OMP_SERVICES_SCHEMAS="$(abspath $(SERVICES_SCHEMAS))" $(BUN) test tests/unit
	@echo "release-check-relay: asset $$(shasum -a 256 "$(ASSET)" | cut -d' ' -f1), omp $$(env -u BUN_BE_BUN $$(command -v $(or $(OMP_BIN),omp)) --version), bun $$($(BUN) --version), tag $(OMP_TAG)"

# Not part of `check` (it builds from an omp checkout and needs the network once); the release of the web client requires it.
verify-web:
	@test -n "$(WEB_OMP_SRC)" -a -n "$(OMP_TAG)" || { echo "usage: make verify-web WEB_OMP_SRC=<omp checkout with tags> OMP_TAG=vX.Y.Z"; exit 2; }
	$(RELAY)/scripts/verify-web.sh --omp-src "$(WEB_OMP_SRC)" --tag "$(OMP_TAG)" --omp-bin "$$(command -v $(or $(OMP_BIN),omp))"

test-conformance: bun-version
	cd $(RELAY) && $(BUN) tests/conformance/pairing-store-suite.ts \
	  --reader-json '["$(shell command -v $(BUN))","tests/conformance/adapter.ts"]' \
	  --writer-json '["$(shell command -v $(BUN))","tests/conformance/adapter.ts"]'

test-integration: bun-version
	$(PYTHON) -m unittest discover -s $(PLUGIN)/tests/integration -v
	$(PYTHON) -m unittest discover -s $(SERVICES)/tests/integration -v
	$(PYTHON) -m unittest discover -s $(DASH)/tests/integration -v

gate:
	$(PYTHON) tools/extension-load-gate/gate.py

gate-selftest:
	$(PYTHON) tools/extension-load-gate/gate.py --selftest

lint:
	ruff check .
	ruff format --check .
	$(MAKE) lint-shell

lint-shell:
	@command -v shellcheck >/dev/null || { echo "error: shellcheck is required for lint (brew install shellcheck / apt install shellcheck)"; exit 1; }
	shellcheck $(SHELL_SCRIPTS)

typecheck:
	scripts/typecheck.py

# What a release needs: everything in `check`, with the type check required, not skipped (`check` skips it when OMP_SRC
# is not set, so that the other checks stay runnable on a machine without an omp checkout).
release-check-core:
	@test -n "$(OMP_SRC)" || { echo "error: release-check needs OMP_SRC=<omp checkout with dependencies installed>" >&2; exit 2; }
	$(MAKE) check
	$(MAKE) export-gate

# The whole publishing gate: `check` with the type check required, the export gate, and the relay's web-client gate
# (which needs OMP_TAG, ASSET and SERVICES_SCHEMAS as well; see release-check-relay).
release-check: release-check-core release-check-relay

# The extension-load gate on the exact export of the committed candidate (not the working tree).
export-gate:
	scripts/export-gate.sh

# Tests of the tools themselves: publication checker, exporter and gate, extension-load gate, test kit, type-check wrapper.
test-tools:
	$(PYTHON) -m unittest discover -s tools/public-tree-check -p 'test_*.py'
	$(PYTHON) -m unittest discover -s tools/extension-load-gate -p 'test_*.py'
	$(PYTHON) -m unittest discover -s tools/testkit -p 'test_*.py'
	$(PYTHON) -m unittest discover -s scripts -p 'test_*.py'

public-selftest:
	tools/public-tree-check/selftest.sh

public-check:
	scripts/check-public.sh

test-services: bun-version
	@set -e; for f in $(SERVICES_UNIT_TESTS); do echo "== $$f"; (cd $(SERVICES) && $(BUN) test $${f#$(SERVICES)/}); done
	$(PYTHON) -m unittest discover -s $(SERVICES)/tests/python -v
	$(PYTHON) $(SERVICES)/tests/conformance/health_1_suite.py --impl "$(CURDIR)/$(SERVICES)/bin/omp-services health --json --no-actions --file"
	$(PYTHON) $(SERVICES)/tests/conformance/health_1_suite.py --selftest

shellcheck: lint-shell

bun-version:
	@v=$$($(BUN) --version); \
	if [ "$$(printf '%s\n1.4.4\n' "$$v" | sort -V | head -n1)" != "1.4.4" ]; then \
	  echo "error: Bun $$v is older than 1.4.4 (set BUN_BIN)"; exit 1; fi

# omp-dashboard: the Python suite (scratch HOME, free ports, no network; the cross-implementation tests run the relay's adapter on Bun)
test-dashboard: bun-version
	cd $(DASH)/tests/python && PYTHONPATH="$(CURDIR)/$(DASH)/python" BUN_BIN="$(shell command -v $(BUN))" $(PYTHON) -m unittest discover -p 'test_*.py' -v

# pairing-store/1 conformance suite (kept with omp-private-relay) against the dashboard's reader and writer
test-dashboard-conformance: bun-version
	cd $(RELAY) && $(BUN) tests/conformance/pairing-store-suite.ts \
	  --reader-json '["$(PYTHON)","$(CURDIR)/$(DASH)/tests/conformance/adapter.py"]' --writer-json '["$(PYTHON)","$(CURDIR)/$(DASH)/tests/conformance/adapter.py"]'

# The page needs its packages once (network): `make ui-install`. The rest runs offline.
ui-install:
	cd $(DASH)/ui && $(UI_BUN) install --frozen-lockfile

test-ui:
	@test -d $(DASH)/ui/node_modules || { echo "error: run 'make ui-install' once (it downloads the page's build packages)"; exit 1; }
	cd $(DASH)/ui && $(UI_BUN) test tests

typecheck-ui:
	@test -d $(DASH)/ui/node_modules || { echo "error: run 'make ui-install' once"; exit 1; }
	cd $(DASH)/ui && node_modules/.bin/tsc --noEmit -p tsconfig.json

build-ui:
	cd $(DASH)/ui && $(UI_BUN) build.ts

# Rebuild the page from its sources with the Bun version recorded in ui-build.json and compare with the committed files.
verify-ui:
	@test -d $(DASH)/ui/node_modules || { echo "error: run 'make ui-install' once"; exit 1; }
	cd $(DASH)/ui && $(UI_BUN) build.ts --verify

# Each security guard of the dashboard is switched off in a copy; the test module that guards it must fail.
test-dashboard-mutation:
	$(PYTHON) $(DASH)/tests/mutation/run_mutations.py

# omp-memory: the importer's own Bun tests, run from the plugin's copy (they build scratch banks with the published Mnemopi
# library, @oh-my-pi/pi-mnemopi at omp's version, which the first run installs into a scratch directory: network once), then the
# Python suite (scratch HOME, fake banks, no omp needed; the drift test against the source tree is skipped with a message when
# that checkout is not named). Point BUN_BIN at the Bun 1.4.4 or newer to use.
test-memory: bun-version
	@scratch=$$(mktemp -d "$${TMPDIR:-/tmp}/omp-memory-test-home.XXXXXX") && trap 'rm -rf "$$scratch"' EXIT && \
	  (cd $(MEMORY)/vendor/memory_import && HOME="$$scratch" $(BUN) test) && \
	  (cd $(MEMORY)/tests/python && HOME="$$scratch" $(PYTHON) -m unittest discover -p 'test_*.py' -v)

memory-vendor-check:
	@d="$$OMP_MEMORY_SOURCE_DIR"; \
	if [ -n "$$d" ] && [ -d "$$d" ]; then $(MEMORY)/scripts/sync-vendor.sh --check; \
	else echo "skipped: OMP_MEMORY_SOURCE_DIR does not name the source tree (set it to compare)"; fi
