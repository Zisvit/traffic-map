PY := .venv/bin/python
PIP := .venv/bin/pip

venv:
	python3 -m venv .venv
	$(PIP) install -U pip
	$(PIP) install -e '.[dev]'

assets:
	$(PY) scripts/fetch_assets.py

test:
	.venv/bin/pytest

run:
	$(PY) -m trafficmap

snapshot:
	$(PY) -m trafficmap --mock tests/fixtures/mock.jsonl --snapshot /tmp/tmap.txt --frames 30

.PHONY: venv assets test run snapshot
