.PHONY: install test lint fmt run serve docker clean

install:
	python -m pip install -e ".[dev,web,office]"

test:
	pytest -q

lint:
	ruff check src tests

fmt:
	ruff check --fix src tests
	ruff format src tests

run:
	anyaclaw chat

serve:
	anyaclaw serve

docker:
	docker build -t anyaclaw .

demo:
	python examples/mock_openai_server.py

clean:
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov
