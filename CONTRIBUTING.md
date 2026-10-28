# Contributing

Thanks for your interest in Agentic NetOps.

## Development setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

On macOS/Linux:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Use `.env` only for local credentials. It is ignored by Git and must not be
committed.

## Tests

Run the offline suite before opening a pull request:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

or:

```bash
python -m pytest -q
```

The default suite uses fakes and mocks and does not contact network devices or
model providers. Live integration tests are opt-in and must remain disabled in
CI.

## Pull requests

Please keep changes focused and include tests for behavior changes. Before
submitting:

- run the offline test suite
- check for accidental secrets or local environment values
- update the README or docs when behavior or configuration changes
- avoid committing `.env`, credentials, generated artifacts, or local scratch
  files

## Code style

- Target Python 3.12+.
- Keep device access behind the connector abstraction.
- Keep API responses stable and credential-safe.
- Prefer deterministic diagnosis for behavior that must be explainable and
  testable.
- Keep optional AI/model integrations isolated from the core diagnosis path.
- Add type hints where they improve clarity and maintainability.

## Security

Do not report security vulnerabilities in public issues. See
[SECURITY.md](SECURITY.md).
