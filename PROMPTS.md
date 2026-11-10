# PROMPTS.md

This file records the main AI prompts used during development.

## 1. Project implementation

> Read the attached assignment specification and build the HTTP API it describes. Follow the contract and all three parts: member creation and phone lookup, plans and invoice scheduling, and gateway reconciliation. Include the required Dockerfile, tests, `NOTES.md`, and this prompt log. Keep the implementation straightforward and make sure it handles validation, exact minor-unit money calculations, deterministic reconciliation, and concurrent requests correctly. Do not add comments to the source code. Ask me only if an essential requirement is unclear.

## 2. Fixing the Docker / Python 3.11 build error

When building and running the project in Docker (Python 3.11 base image), the tests failed. I reported the error back and asked for it to be fixed for Python 3.11 rather than changing the Python version.

**Outcome:** The cause was a workaround from the first version: `from __future__ import annotations` in `app/main.py`, which broke how the framework resolved type hints at runtime. Python 3.11 supports `str | None` and `int | None` natively, so the import was removed. I then rebuilt the virtual environment and re-ran the suite locally before continuing:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-test.txt
python -m pytest -q
python --version
```

Result: `16 passed` on Python 3.11.x. I did not push until the tests passed on my machine.

## 3. Freeing port 8080 for the container

> Check which process is running on port 8080 and kill it.

**Outcome:** An earlier local run of the server was still holding port 8080, which blocked the Docker container from binding to it. Freed it with:

```bash
lsof -i :8080
kill -9 $(lsof -ti :8080)
lsof -i :8080
```

No output from the last command confirmed the port was free, and the container started normally.

## Use of AI

AI assistance was used to generate the initial implementation from the supplied specification and to diagnose the issues above. The full conversation is linked at the top of this file. I reviewed the changes, ran the test suite locally on Python 3.11, and verified the Docker setup myself before submitting.
