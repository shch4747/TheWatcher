.PHONY: dev test lint typecheck check check-gowa check-lapis add-admin benchmark

dev: ## Run the app locally (main.py: FastAPI + scheduler tick loop)
	uv run python main.py

test: ## Run the test suite (no external services needed)
	uv run pytest -q

lint:
	uv run ruff check .

typecheck:
	uv run mypy shared
	uv run mypy agents/wa_agent
	uv run mypy agents/project_agent
	uv run mypy main.py

check: lint typecheck test ## Everything CI runs

check-gowa: ## Test the gowa connection in isolation
	uv run python scripts/check_gowa.py

check-lapis: ## Test the Lapis (or local vault) connection in isolation
	uv run python scripts/check_lapis.py

add-admin: ## make add-admin WA=919876543210@s.whatsapp.net
	uv run python scripts/add_bot_admin.py "$(WA)"

benchmark: ## Worker model benchmark (needs MODELS_API_KEY - real, billed calls)
	uv run python scripts/run_worker_benchmark.py
