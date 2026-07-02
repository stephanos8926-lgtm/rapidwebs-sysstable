.PHONY: install test lint format check clean

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

publish: build
	source .venv/bin/activate && twine upload dist/*

.PHONY: docker-build docker-run
docker-build:
	docker build -t rapidwebs-sysstable .

docker-run:
	docker run --rm -it \
		-v /proc:/proc:ro \
		-v /sys:/sys:ro \
		--pid=host \
		rapidwebs-sysstable status

# Systemd service management
.PHONY: service-install service-enable service-status service-stop
service-install:
	cp docs/sysstable.service ~/.config/systemd/user/
	systemctl --user daemon-reload

service-enable: service-install
	systemctl --user enable --now sysstable

service-status:
	systemctl --user status sysstable

service-stop:
	systemctl --user stop sysstable
