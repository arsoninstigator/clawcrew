.PHONY: install test demo seed inspect serve vendor-audit check clean

install:
	pip install -e ".[dev]"

test:
	cd clawcrew/tests && python -m unittest discover -s . -p "test_*.py"

# what a fresh machine should do before anyone believes the repo works
check: vendor-audit test

demo:
	python -m core.cli demo

seed:
	python -m core.cli seed

inspect:
	python -m core.cli inspect

serve:
	python -m core.cli serve

# every vendored package must keep its upstream licence and a provenance record,
# otherwise the copy is unattributable and has no business being in the tree
vendor-audit:
	@checked=0; for d in vendor/*/; do \
		case "$$(basename $$d)" in __pycache__) continue;; esac; \
		test -f "$$d/LICENSE" || { echo "missing $$d/LICENSE"; exit 1; }; \
		test -f "$$d/PROVENANCE.md" || { echo "missing $$d/PROVENANCE.md"; exit 1; }; \
		checked=$$((checked+1)); \
	done; echo "vendor audit ok ($$checked packages)"

clean:
	rm -rf demo_data .pytest_cache **/__pycache__

ddl:
	python scripts/export_ddl.py > clawcrew/docs/schema.postgres.sql
