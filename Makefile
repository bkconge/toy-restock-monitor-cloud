.PHONY: test lint-workflows

test:
	.venv/bin/python -m unittest discover tests/

lint-workflows:
	@command -v actionlint >/dev/null || { echo "install actionlint: brew install actionlint"; exit 1; }
	actionlint .github/workflows/*.yml
