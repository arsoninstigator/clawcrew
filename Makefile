.PHONY: install test demo seed inspect serve vendor-audit check clean

install:
	pip install -e ".[dev]"

test:
	cd tests && python -m unittest discover -s . -p "test_*.py"

# what a fresh machine should do before anyone believes the repo works
check: vendor-audit test

demo:
	python -m clawcrew.cli demo

seed:
	python -m clawcrew.cli seed

inspect:
	python -m clawcrew.cli inspect

serve:
	python -m clawcrew.cli serve

# every vendored package must keep its upstream licence and a provenance record,
# otherwise the copy is unattributable and has no business being in the tree
vendor-audit:
	@for d in clawcrew/vendor/*/; do \
		test -f "$$d/LICENSE" || (echo "missing $$d/LICENSE"; exit 1); \
		test -f "$$d/PROVENANCE.md" || (echo "missing $$d/PROVENANCE.md"; exit 1); \
	done
	@echo "vendor audit ok"

clean:
	rm -rf demo_data .pytest_cache **/__pycache__
