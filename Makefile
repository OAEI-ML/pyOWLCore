.PHONY: check check-full test audit

check:
	python tools/check.py

check-full:
	python tools/check.py --full

test:
	python -m unittest discover -s tests/foundation -v

audit:
	python -m tools.audit.check_all
