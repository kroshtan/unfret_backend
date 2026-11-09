.PHONY: help install lint audit test test-unit test-integration db-up db-down run docker-up clean corpus opinions split label reports eval bump
.DEFAULT_GOAL := help

TEST_DB_CONTAINER = veridex-test-postgres
TEST_DB_URI = postgresql://veridex:veridex@localhost:55432/veridex

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
	uvx pip-audit -r .audit-requirements.txt -r .audit-requirements-mcp.txt --no-deps --disable-pip; status=$$?; \
		rm -f .audit-requirements.txt .audit-requirements-mcp.txt; exit $$status

test-unit:  ## Run unit tests (no external services needed)
	uv run pytest tests/unit

test-integration: db-up  ## Run integration tests against a throwaway Postgres container
	VERIDEX_TEST_POSTGRES_URI=$(TEST_DB_URI) uv run pytest tests/integration

test: db-up  ## Run the full test suite
	VERIDEX_TEST_POSTGRES_URI=$(TEST_DB_URI) uv run pytest

corpus:  ## Build the eval corpus from public datasets (into VERIDEX_CORPUS_DIR)
	uv run python -m evals.sources.beyondphish
	uv run python -m evals.sources.amazon2023
	uv run python -m evals.sources.mendeley

opinions:  ## Get the LLM second opinion on every corpus page and rebuild the review queue
	uv run python -m evals.second_opinion

split:  ## Assign the fixed train/test split (rule-only labels never go to test)
	uv run python -m evals.split

label:  ## Work through the review queue, one keypress per page
	uv run python -m evals.label

reports:  ## Label reported results from the live service into the corpus (UNFRET_URL, UNFRET_ADMIN_TOKEN[_FILE])
	uv run python -m evals.reports

eval:  ## Score the pipeline on the corpus (EVAL_ARGS="--limit 200 --replay")
	uv run python -m evals.run $(EVAL_ARGS)

db-up:  ## Start the test Postgres container (port 55432)
	@docker inspect $(TEST_DB_CONTAINER) >/dev/null 2>&1 || docker run -d --rm --name $(TEST_DB_CONTAINER) \
		-e POSTGRES_USER=veridex -e POSTGRES_PASSWORD=veridex -e POSTGRES_DB=veridex -p 55432:5432 postgres:17-alpine >/dev/null
	@until docker exec $(TEST_DB_CONTAINER) pg_isready -U veridex -q; do sleep 1; done

db-down:  ## Stop the test Postgres container
	-docker stop $(TEST_DB_CONTAINER)

run:  ## Run the API locally with auto-reload (needs Postgres, see docker-up)
	uv run uvicorn veridex.main:app --reload --port 8080

docker-up:  ## Build and start the API + Postgres with docker compose
	docker compose up --build

bump:  ## Cut a release: VERSION=x.y.z sets the version and dates the CHANGELOG (no commit, tag or deploy)
	@test -n "$(VERSION)" || (echo "usage: make bump VERSION=x.y.z" && exit 1)
	uv run scripts/bump_version.py $(VERSION)
	uv lock

clean:  ## Remove build, test and cache artifacts
	rm -rf build/ dist/ *.egg-info .coverage coverage.xml htmlcov/ .pytest_cache/ .mypy_cache/ .ruff_cache/
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
