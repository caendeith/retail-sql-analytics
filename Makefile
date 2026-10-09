# ===========================================================================
#  OPTIONAL convenience wrapper.
#
#  `make` is NOT required and is NOT the primary run path -- the documented way
#  to run this project is plain:
#
#      python run_all.py
#
#  These targets exist only for people who already have make installed.
#  On Windows, use `python run_all.py` directly.
# ===========================================================================

PYTHON ?= python

.PHONY: help all install sample queries report verify clean clean-db docker-up docker-down

help:
	@echo "retail-sql-analytics -- optional make targets"
	@echo ""
	@echo "  make install     install pinned dependencies"
	@echo "  make all         full pipeline (same as: python run_all.py)"
	@echo "  make sample      full pipeline on the bundled sample CSV"
	@echo "  make queries     re-run only the SQL against an existing database"
	@echo "  make verify      print cleaning + headline numbers, no database"
	@echo "  make clean       remove caches and the embedded .pgdata directory"
	@echo ""
	@echo "Primary run path (no make needed):  $(PYTHON) run_all.py"

install:
	$(PYTHON) -m pip install -r requirements.txt

all:
	$(PYTHON) run_all.py

sample:
	$(PYTHON) run_all.py --sample --no-download

queries:
	$(PYTHON) run_queries.py

report:
	$(PYTHON) run_all.py

verify:
	$(PYTHON) scripts/verify_cleaning.py

docker-up:
	docker compose up -d

docker-down:
	docker compose down

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pgdata

clean-db: clean
