.PHONY: setup serve demo run test lint clean

setup:
	python3 -m venv --system-site-packages .venv
	.venv/bin/python -m pip install -q polars fastapi "uvicorn[standard]" pytest

serve:
	.venv/bin/python -m app.cli serve --port 8404

demo:
	.venv/bin/python -m app.cli demo

run:
	.venv/bin/python -m app.cli run --run-id cli_run
	.venv/bin/python -m app.cli report cli_run

test:
	.venv/bin/python -m pytest tests/test_pipeline.py -q
	node --test tests/core.test.js

lint:
	.venv/bin/python -m pyflakes app/*.py tests/*.py || true

clean:
	rm -rf data/app.db data/app.db-* data/datasets data/runs .pytest_cache
