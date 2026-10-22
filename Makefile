PY ?= .venv/bin/python

.PHONY: setup test demo chaos all clean
setup:
	python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

test:
	$(PY) -m pytest -q

demo:
	$(PY) -m orchestrator run core

chaos:
	$(PY) -m orchestrator run chaos

all: test
	$(PY) -m orchestrator run all --quiet

clean:
	rm -rf runs .pytest_cache
