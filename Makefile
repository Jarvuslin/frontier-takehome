# Safco Catalog Agent — convenience targets
# All targets work on Linux/macOS/WSL. Windows users: run the underlying
# `uv run ...` commands directly, or use Git Bash / WSL.

PY ?= 3.12

.PHONY: help install crawl crawl-gloves crawl-sutures discover report test fmt clean docker

help:
	@echo "Targets:"
	@echo "  install        - create venv via uv + install Playwright Chromium"
	@echo "  discover       - print discovered URL frontier without crawling"
	@echo "  crawl          - run both seed categories (capped via config)"
	@echo "  crawl-gloves   - crawl only the gloves seed"
	@echo "  crawl-sutures  - crawl only the sutures seed"
	@echo "  report         - regenerate data-quality report from latest run"
	@echo "  test           - run offline pytest suite (HTML fixtures, no network)"
	@echo "  clean          - wipe data/, debug/, logs/"
	@echo "  docker         - build the docker image"

install:
	uv sync --python $(PY)
	uv run python -m playwright install chromium

discover:
	uv run safco discover

crawl:
	uv run safco crawl

crawl-gloves:
	uv run safco crawl --seed gloves

crawl-sutures:
	uv run safco crawl --seed sutures-surgical-products

report:
	uv run safco report

test:
	uv run pytest -q

fmt:
	uv run python -m compileall -q src tests

clean:
	rm -rf data/products.db data/exports data/reports debug logs

docker:
	docker build -t safco-agent:latest .
