.PHONY: install test test-all lint format format-check check clean build plugin-install

install:
	uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"

test:
	.venv/bin/pytest tests/ -v --tb=short

test-all:
	.venv/bin/pytest tests/ -v

lint:
	.venv/bin/ruff check src/ tests/

format:
	.venv/bin/ruff format src/ tests/

format-check:
	.venv/bin/ruff format src/ tests/ --check

check: format-check lint test
	@echo "✅ All checks passed"

clean:
	rm -rf build/ dist/ *.egg-info .pytest_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete

build:
	source .venv/bin/activate && python -m build

plugin-install:
	@echo "Installing Hermes plugin..."
	@chmod +x hermes-plugin/rapidwebs-sysstable/install.sh
	@./hermes-plugin/rapidwebs-sysstable/install.sh