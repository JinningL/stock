# Repository Guidelines

## Project Structure & Module Organization
`main.py` contains the stock monitor, email delivery, JSON state handling, and static report generation. Runtime data lives in `data/daily_history.json`; the generated site is `docs/index.html`. GitHub automation is defined in `.github/workflows/run.yml`. Treat `__pycache__/` and `.state/` as disposable local artifacts.

## Build, Test, and Development Commands
Create or reuse a local virtualenv, then install the single runtime dependency:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run locally with the same environment variables used in Actions:

```bash
EMAIL=you@gmail.com PASSWORD=app-password STOCK_SYMBOLS=QQQ,TSLA,CRCL python main.py
```

Useful checks:

```bash
python -m py_compile main.py   # syntax check
python main.py                 # fetch data, update JSON/report, send email if configured
```

## Coding Style & Naming Conventions
Follow existing Python style in `main.py`: 4-space indentation, `snake_case` for functions and variables, `UPPER_CASE` for module constants, and small helper functions for parsing or formatting. Prefer standard library utilities unless a third-party package is already required. Keep output files deterministic by preserving sorted JSON keys and stable HTML generation.

## Testing Guidelines
There is no formal test suite yet. For changes, at minimum run `python -m py_compile main.py` and a local `python main.py` pass with safe test credentials or mocked environment variables. If you add tests, place them under `tests/` and use `test_<feature>.py` naming so a future `pytest` setup is straightforward.

## Commit & Pull Request Guidelines
Recent history uses short, imperative commit subjects such as `Add static stock report page` and `Fix stock monitor defaults and workflow setup`. Keep commits focused and descriptive. PRs should summarize behavior changes, note any new environment variables or secrets, and include screenshots when `docs/index.html` output changes. Link the relevant issue when one exists.

## Security & Configuration Tips
Never commit real email credentials or generated local state from `.state/`. Keep GitHub secrets in repository settings, and document any new required secret in `README.md` and the workflow file together.
