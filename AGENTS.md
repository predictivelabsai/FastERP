# Repository Guidelines

## Project Structure & Module Organization

FastERP is a Python 3.12, server-rendered FastHTML application. `web_app.py` is
the entry point and owns routes, authentication, and startup. Database schema,
queries, and transactional helpers live in `db.py`; `seed.py` rebuilds the
deterministic synthetic SQLite dataset. Keep presentation code under `web/`:
`layout.py` defines the shared shell and CSS, `views.py` renders ERP screens,
and `ai.py` handles grounded chat and slash commands. Migration utilities and
demo tooling belong in `scripts/`; roadmap and demo assets live in `docs/`.
Do not commit `.env`, `fasterp.sqlite`, caches, or virtual environments.

## Build, Test, and Development Commands

Create a local environment and run the app:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.sample .env
.venv/bin/python web_app.py
```

The server is available at `http://localhost:5011`. Run
`.venv/bin/python seed.py` to discard local database contents and recreate the
synthetic demo data. Use `docker compose up --build` for the containerized
setup. Before submitting Python changes, run
`.venv/bin/python -m compileall web_app.py db.py seed.py web scripts`.

## Coding Style & Naming Conventions

Follow the existing PEP 8-oriented style: four-space indentation,
`snake_case` functions and variables, `UPPER_SNAKE_CASE` constants, and short
module docstrings. Keep route handlers thin; put persistence and business
transactions in `db.py`, and reusable FastHTML component trees in `web/`.
Prefer parameterized SQL (`WHERE id=?`) over interpolated values. No formatter
or linter is currently configured, so match adjacent code and keep imports
clear and grouped.

## Testing Guidelines

There is currently no automated test suite or coverage threshold. Validate
changes by reseeding, starting the server, and exercising affected workflows
in the browser: login, sales orders, invoices/payments, stock, purchasing, and
AI slash commands as applicable. New tests should use `pytest`, live under
`tests/`, and follow `test_<feature>.py` with `test_<behavior>()` names.

## Commit & Pull Request Guidelines

Recent history uses concise, imperative, feature-level subjects, for example
`Add buying side ...` and `Make order-to-cash transactional`. Keep commits
focused and explain schema or data-flow effects in the body when needed. Pull
requests should summarize behavior, list verification steps, link relevant
issues, and include screenshots or a short recording for UI changes. Call out
new environment variables, migrations, and any demo-data changes explicitly.
