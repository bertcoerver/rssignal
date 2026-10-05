.PHONY: release clean build upload addon-release signal-cli-bump

release: clean build upload

# Builds and publishes the Home Assistant add-on on GitHub Actions; Home
# Assistant then offers it as an update. See .github/workflows/addon.yml.
addon-release:
	@test -n "$(VERSION)" || { echo "usage: make addon-release VERSION=x.y.z"; exit 1; }
	gh workflow run addon.yml --ref main -f version=$(VERSION)
	@echo "Started. Follow it with: gh run watch"

# Points addon/Dockerfile at the newest signal-cli, or at SIGNAL_CLI=x.y.z, and
# at the libsignal and Java that release needs. Edits the file and nothing else: commit,
# push, then addon-release. See addon/scripts/bump-signal-cli.sh.
signal-cli-bump:
	addon/scripts/bump-signal-cli.sh $(SIGNAL_CLI)

clean:
	rm -rf dist/ build/ src/*.egg-info

build:
	python -m build

upload:
	twine upload dist/*
