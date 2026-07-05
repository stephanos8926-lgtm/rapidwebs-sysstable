.PHONY: help install test test-all lint format format-check check clean build \
        plugin-install install-package install-systemd uninstall \
        deploy docker-build docker-run release

# ── Meta ──────────────────────────────────────────────────────────────────────

help: ## Show this help
	@printf '\033[36m%s\033[0m\n' 'rapidwebs-sysstable — Makefile'
	@printf '\033[90m%s\033[0m\n' '──────────────────────────────────────────────'
	@grep -Eh '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Development ───────────────────────────────────────────────────────────────

install: ## Install package in editable mode with dev deps
	uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"

install-package: ## Install the released package from PyPI
	pip install rw-sysstable

test: ## Run tests (short output)
	.venv/bin/pytest tests/ -v --tb=short

test-all: ## Run all tests (verbose)
	.venv/bin/pytest tests/ -v

lint: ## Run linter (ruff check)
	.venv/bin/ruff check src/ tests/

format: ## Auto-format code
	.venv/bin/ruff format src/ tests/

format-check: ## Check formatting without changing
	.venv/bin/ruff format src/ tests/ --check

check: format-check lint test ## Run all checks (format → lint → test)
	@echo "✅ All checks passed"

clean: ## Remove build artifacts and caches
	rm -rf build/ dist/ *.egg-info .pytest_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete

# ── Build & Release ───────────────────────────────────────────────────────────

build: ## Build wheel and sdist
	source .venv/bin/activate && python -m build

release: ## Tag version, build, and push to PyPI + GitHub
	@echo "📦 Running full release pipeline..."
	$(MAKE) check
	$(MAKE) build
	@echo "   Tagging v$(shell python3 -c "import sys; sys.path.insert(0,'src'); import sysstable; print(sysstable.__version__)")..."
	git tag v$(shell python3 -c "import sys; sys.path.insert(0,'src'); import sysstable; print(sysstable.__version__)")
	git push origin main --tags
	@echo "✅ Release triggered. GitHub Actions handles PyPI publish."

# ── Docker ────────────────────────────────────────────────────────────────────

docker-build: ## Build Docker image
	docker build -t rapidwebs-sysstable:latest .

docker-run: ## Run Docker container (daemon mode)
	docker compose up -d

docker-stop: ## Stop Docker container
	docker compose down

docker-logs: ## View Docker container logs
	docker compose logs -f

# ── Plugin ────────────────────────────────────────────────────────────────────

plugin-install: ## Install Hermes plugin to ~/.hermes/plugins/
	@echo "Installing Hermes plugin..."
	@chmod +x hermes-plugin/rapidwebs-sysstable/install.sh
	@./hermes-plugin/rapidwebs-sysstable/install.sh

# ── Systemd ───────────────────────────────────────────────────────────────────

install-systemd: ## Install systemd user service
	@echo "Installing systemd user service..."
	@mkdir -p ~/.config/systemd/user/
	@cp docs/sysstable.service ~/.config/systemd/user/sysstable.service
	@systemctl --user daemon-reload
	@echo "✅ Service installed. Enable with: systemctl --user enable --now sysstable"

uninstall: ## Remove sysstable artifacts (config, db, daemon, service)
	@echo "⚠️  This will stop the daemon and remove all data."
	@echo "   Run: sysstable uninstall"
	@echo "   Then: systemctl --user stop sysstable 2>/dev/null; \
	          rm -f ~/.config/systemd/user/sysstable.service; \
	          systemctl --user daemon-reload"
	@echo "   Then: rm -rf ~/.config/sysstable ~/.cache/sysstable"

# ── Deploy ────────────────────────────────────────────────────────────────────

deploy: ## Deploy to rw-server-01 (requires SSH access)
	@echo "🚀 Deploying to rw-server-01..."
	@read -p "Server hostname [rw-server-01]: " host; \
	 host=$${host:-rw-server-01}; \
	 echo "   Building wheel..."; \
	 source .venv/bin/activate && python -m build -q; \
	 wheel=$$(ls dist/rapidwebs_sysstable-*.whl | sort -V | tail -1); \
	 echo "   Uploading $$wheel..."; \
	 scp "$$wheel" "$$host:/tmp/"; \
	 ssh "$$host" "pip install /tmp/$$(basename $$wheel) && rm /tmp/$$(basename $$wheel)"; \
	 echo "✅ Deployed to $$host"