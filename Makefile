.PHONY: test demo serve seed inspect clean

test:
	cd tests && python -m unittest discover -s . -p "test_*.py"

demo:
	python -m clawcrew.cli demo

seed:
	python -m clawcrew.cli seed

inspect:
	python -m clawcrew.cli inspect

serve:
	python -m clawcrew.cli serve

clean:
	rm -rf demo_data .pytest_cache **/__pycache__
