.PHONY: help install lint audit android-release affiliates bonus-codes retire-passes test test-unit test-integration test-expansion e2e e2e-base db-up db-down run docker-up clean corpus eu-shops balance new-classes markets opinions split label reports eval accuracy golden golden-set train model-release bump vat-rates-check safety-gate safety-gate-check
.DEFAULT_GOAL := help

TEST_DB_CONTAINER = unfret-test-postgres
TEST_DB_URI = postgresql://unfret:unfret@localhost:55432/unfret

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## Install the package with dev dependencies and pre-commit hooks
	uv sync --all-groups
	uv run pre-commit install

lint:  ## Run all pre-commit hooks (ruff, mypy, pydoclint, ...) on every file
	uv run pre-commit run --all-files

audit:  ## Check the locked dependencies for known vulnerabilities (pip-audit)
	uv export --frozen --all-groups --no-hashes --no-emit-project > .audit-requirements.txt
	uv export --project mcp --frozen --all-groups --no-hashes --no-emit-project > .audit-requirements-mcp.txt
	uv export --project clients/python --frozen --all-groups --no-hashes --no-emit-project > .audit-requirements-client.txt
	@# One run per environment: both lock the MCP SDK and its dependencies, and pip-audit refuses a package twice.
	status=0; \
		uvx pip-audit -r .audit-requirements.txt --no-deps --disable-pip || status=1; \
		uvx pip-audit -r .audit-requirements-mcp.txt --no-deps --disable-pip || status=1; \
		uvx pip-audit -r .audit-requirements-client.txt --no-deps --disable-pip || status=1; \
		rm -f .audit-requirements.txt .audit-requirements-mcp.txt .audit-requirements-client.txt; exit $$status

vat-rates-check:  ## Compare the vendored EU VAT rates and Cloudflare ranges with their sources (monthly; exit 1 on change)
	@status=0; \
		uv run scripts/update_vat_rates.py --check || status=1; \
		uv run scripts/update_cloudflare_ips.py --check || status=1; \
		exit $$status

safety-gate:  ## Add the EU Safety Gate's new weekly reports to the vendored index (review the diff, then commit)
	uv run scripts/update_safety_gate.py

safety-gate-check:  ## Exit 1 when the Commission published Safety Gate reports the vendored index lacks (monthly)
	uv run scripts/update_safety_gate.py --check

test-unit:  ## Run unit tests (no external services needed)
	uv run pytest tests/unit

test-integration: db-up  ## Run integration tests against a throwaway Postgres container
	UNFRET_TEST_POSTGRES_URI=$(TEST_DB_URI) uv run pytest tests/integration

test: db-up  ## Run the full test suite, then the unit tests again with the expansion classes on
	UNFRET_TEST_POSTGRES_URI=$(TEST_DB_URI) uv run pytest
	$(MAKE) test-expansion

test-expansion:  ## Unit tests with UNFRET_EXPANSION_CLASSES on (product results must not change)
	UNFRET_EXPANSION_CLASSES=true uv run pytest tests/unit --no-cov -q

e2e:  ## End-to-end sweep against dev (devnet; spends the e2e payer's test money): UNFRET_E2E_* (README)
	uv run pytest -m e2e tests/e2e -x --no-cov -p no:cacheprovider -v

e2e-base:  ## x402 on Base against Base Sepolia (testnet only; skips without UNFRET_E2E_BASE_* settings)
	uv run pytest -m e2e_base tests/e2e_base --no-cov -p no:cacheprovider -v

corpus:  ## Build the eval corpus from public datasets (into UNFRET_CORPUS_DIR)
	uv run python -m evals.sources.beyondphish
	uv run python -m evals.sources.amazon2023
	uv run python -m evals.sources.mendeley

eu-shops:  ## Add legitimate EU shops to the corpus, fetched live (robots.txt respected; no LLM calls)
	uv run python -m evals.sources.eu_shops

balance:  ## Add archived product pages of established US shops (legit) and ČOI-warned shops (scam), strict second opinion, split
	uv run python -m evals.sources.us_shops
	uv run python -m evals.sources.coi_shops
	for d in us-established-shops coi-risky-eshops; do uv run python -m evals.second_opinion --dataset $$d; done
	uv run python -m evals.split

new-classes:  ## Collect labelled p2p and service pages (pattern sources, synthetic, ACM, OSM), then second opinion and split
	uv run python -m evals.sources.patterns
	uv run python -m evals.sources.p2p_ads
	uv run python -m evals.sources.service_pages
	uv run python -m evals.sources.acm_traders
	uv run python -m evals.sources.osm_trades
	for c in p2p_listing service_site service_listing; do uv run python -m evals.second_opinion --class $$c; done
	uv run python -m evals.split

markets:  ## Per-market metrics (EU, US, other) of the shipped scorers; no LLM calls (MARKETS_ARGS="--train")
	uv run python -m evals.markets $(MARKETS_ARGS)

opinions:  ## Get the LLM second opinion on every corpus page and rebuild the review queue
	uv run python -m evals.second_opinion

split:  ## Assign the fixed train/test split (rule-only labels never go to test)
	uv run python -m evals.split

label:  ## Work through the review queue, one keypress per page
	uv run python -m evals.label

reports:  ## Label reported results into the corpus: confident proposals in bulk, then one by one (UNFRET_URL, UNFRET_ADMIN_TOKEN[_FILE]; REPORTS_ARGS="--min-confidence 0.9 --no-bulk --order confidence --agreement reporter")
	uv run python -m evals.reports $(REPORTS_ARGS)

codes:  ## Intro codes: ARGS="list | create CODE --uses N [--checks 5] | off CODE | on CODE | add CODE N | link CODE"
	uv run python -m unfret.credits.codes $(ARGS)

affiliates:  ## Affiliates: ARGS="list | create CODE --name NAME [--note T] | off|on CODE | note CODE T | stats-link|revoke CODE | report [--month M] | paid CODE [--note T] | reverse ID | delete CODE"
	uv run python -m unfret.credits.affiliates_cli affiliate $(ARGS)

bonus-codes:  ## Bonus codes: ARGS="list | create CODE --bonus PCT [--expires YYYY-MM-DD] [--uses N] [--affiliate CODE] | off|on CODE"
	uv run python -m unfret.credits.affiliates_cli bonus $(ARGS)

retire-passes:  ## Retire every pass made before a moment (dry run unless --confirm): ARGS="--before ISO [--keep ID | --keep-from FILE]... [--confirm]"
	uv run python -m unfret.admin_cli retire-passes $(ARGS)

eval:  ## Score the pipeline on the corpus (EVAL_ARGS="--limit 200 --replay")
	uv run python -m evals.run $(EVAL_ARGS)

train:  ## Retrain the scorer into the private model store <corpus>/models/<version>/, never src/ (TRAIN_ARGS="--ship C [--quick]")
	uv run python -m evals.train $(TRAIN_ARGS)

model-release:  ## Point the model store's current.json at retrained scorers (RELEASE_ARGS="--model V [--quick V]"; none: baseline)
	uv run python -m evals.release current $(RELEASE_ARGS)

golden-set:  ## Collect the golden set: product pages of well-known sellers from the corpus's private seed list (live, else archived)
	uv run python -m evals.sources.golden $(GOLDEN_SET_ARGS)

golden:  ## False alarms on the golden set, both tiers: no Serious concerns, under 5% Worth a look or worse (GOLDEN_ARGS="--replay --split test")
	uv run python -m evals.golden $(GOLDEN_ARGS)

android-release:  ## Put a signed Android release on /android (TAG=v0.1.0): checks the APK against its release.json, writes static/android-release.json
	uv run python -m unfret.android_release $(TAG)

accuracy:  ## Replay the corpus (recorded LLM answers only): the /accuracy page's numbers, or a retrained scorer's with UNFRET_MODEL_PATH (ACCURACY_ARGS="--reuse STAMP" | "--publish VERSION")
	uv run python -m evals.accuracy $(ACCURACY_ARGS)

db-up:  ## Start the test Postgres container (port 55432)
	@docker inspect $(TEST_DB_CONTAINER) >/dev/null 2>&1 || docker run -d --rm --name $(TEST_DB_CONTAINER) \
		-e POSTGRES_USER=unfret -e POSTGRES_PASSWORD=unfret -e POSTGRES_DB=unfret -p 55432:5432 postgres:17-alpine >/dev/null
	@until docker exec $(TEST_DB_CONTAINER) pg_isready -U unfret -q; do sleep 1; done

db-down:  ## Stop the test Postgres container
	-docker stop $(TEST_DB_CONTAINER)

run:  ## Run the API locally with auto-reload (needs Postgres, see docker-up)
	uv run uvicorn unfret.main:app --reload --port 8080

docker-up:  ## Build and start the API + Postgres with docker compose
	docker compose up --build

bump:  ## Cut a release: VERSION=x.y.z sets the version and dates the CHANGELOG (no commit, tag or deploy)
	@test -n "$(VERSION)" || (echo "usage: make bump VERSION=x.y.z" && exit 1)
	uv run scripts/bump_version.py $(VERSION)
	uv lock

clean:  ## Remove build, test and cache artifacts
	rm -rf build/ dist/ *.egg-info .coverage coverage.xml htmlcov/ .pytest_cache/ .mypy_cache/ .ruff_cache/
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
