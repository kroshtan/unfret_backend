# CLAUDE.md

Guidance for Claude Code working in this repository.

## Autonomy

Claude has standing permission to **commit, push, open pull requests and merge** without asking first, **as long as
CI passes**. Never merge with failing or skipped checks; fix the cause instead. Work on a branch, never commit
directly to `main`. Merging to `main` deploys to **dev** (dev.unfret.kroshtan.com, devnet) automatically.

**Production is different:** Claude must **never** trigger the `Deploy production` workflow, create a release tag
meant for it, or run `kamal … -d production`, without the user's explicit go for **that specific release**. A
general permission to merge or deploy does not cover production (README, "Deploying").

Remotes: `origin` is the Forgejo forge at git.kroshtan.com (primary, CI runs there); `github` is the public mirror.

## Project

FastAPI + LangGraph service that scores e-commerce pages for scam, dropshipping and dark-pattern signals. See
`README.md` for the architecture and `CHANGELOG.md` for what changed.

## Commands

```bash
make install   # uv sync + pre-commit hooks
make lint      # ruff, ruff format, mypy (strict), pydoclint
make test      # unit + integration (throwaway Postgres container on port 55432)
make audit     # pip-audit over the locked dependencies
```

## Conventions

- Python 3.14, uv, strict mypy, sphinx-style docstrings checked by pydoclint.
- Settings come from `UNFRET_*` environment variables (`src/unfret/config.py`); never commit secrets or `.env`.
- Outbound requests to user-supplied URLs go through `unfret.net.fetch_public`.
- Don't log or persist analysed URLs or anything that links a payer to what they checked.
