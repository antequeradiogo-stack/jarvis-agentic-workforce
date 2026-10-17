.PHONY: test test-fast demo

test:  ## full suite, network-blocked
	./scripts/test.sh -v

test-fast:  ## no offline wrapper
	PYTHONPATH=src python3 -m unittest discover -s tests

demo:  ## all demo scenarios, network-blocked
	./scripts/demo.sh
