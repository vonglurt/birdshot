# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
#
# birdshot -- IMX477 bird and sky capture for the Raspberry Pi CM4.
#
# Most of this Makefile runs on the Mac you develop on. The one target that
# cannot is `selftest`, which needs the camera; it is driven over SSH.

SHELL := /usr/bin/env bash
PY_SRC := $(shell find src -name '*.py' 2>/dev/null)
BIN_SRC := bin/birdshot-cli bin/birdshot-gui bin/birdshot-wallpaper
SH_SRC := sync.sh install.sh $(wildcard mac/*.sh) .githooks/pre-commit

.DEFAULT_GOAL := help
.PHONY: help check lint sanitise audit-history deps vendor-check hooks dist doctor run rundebug selftest info clean

help:
	@echo 'birdshot -- make targets'
	@echo
	@echo '  make check          lint + sanitise + vendor-check  (the pre-push gate)'
	@echo '  make lint           byte-compile Python, parse every shell script'
	@echo '  make sanitise       scan tracked files for un-publishable strings'
	@echo '  make audit-history  scan every blob in every commit (slower, thorough)'
	@echo '  make deps           list third-party imports and external binaries'
	@echo '  make vendor-check   verify vendor/ manifests against their hashes'
	@echo '  make hooks          install the sanitisation pre-commit hook'
	@echo '  make dist           build the sdist + wheel every channel consumes'
	@echo '  make doctor         check this machine: deps, cameras, storage'
	@echo '  make run            launch the GUI from this checkout'
	@echo '  make rundebug       GUI with profiling, Qt logging and Python dev mode'
	@echo
	@echo '  make selftest       run the on-camera selftest on the Pi (needs hardware)'
	@echo '  make info           camera, modes, storage and calibration, from the Pi'

# ---------------------------------------------------------------- the gate --
check: lint sanitise vendor-check
	@echo
	@echo 'check: PASS'

lint:
	@echo '== byte-compiling Python =='
	@python3 -m py_compile $(PY_SRC) $(BIN_SRC)
	@echo '   ok: $(words $(PY_SRC)) modules, $(words $(BIN_SRC)) scripts'
	@echo '== parsing shell scripts =='
	@for f in $(SH_SRC); do bash -n "$$f" || exit 1; done
	@echo '   ok: $(words $(SH_SRC)) scripts'

# Same rules as .githooks/pre-commit, applied to the whole tracked tree rather
# than to staged content -- catches anything committed before the hook existed.
#
# The hook and this Makefile are exempt from their own scan by path: both have
# to spell the patterns in order to match them. Every other file, including the
# lab report that documents what was scrubbed, is scanned normally.
sanitise:
	@echo '== sanitisation scan (tracked files) =='
	@fail=0; \
	for f in $$(git ls-files); do \
	  case "$$f" in .githooks/pre-commit|Makefile) continue;; esac; \
	  grep -q 'birdshot-lint: allow-legacy-name' "$$f" 2>/dev/null && legacy_ok=1 || legacy_ok=0; \
	  if grep -qE '\b(192\.168\.[0-9]+\.[0-9]+|10\.[0-9]+\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+)\b' "$$f" 2>/dev/null; then \
	    echo "   $$f: private IP address"; fail=1; fi; \
	  if grep -oE '/home/[a-z_][a-z0-9_-]*' "$$f" 2>/dev/null | grep -qvE '^/home/pi$$'; then \
	    echo "   $$f: personal home path"; fail=1; fi; \
	  if grep -oE '/media/[a-z_][a-z0-9_-]*' "$$f" 2>/dev/null | grep -qvE '^/media/(pi|<user>)$$'; then \
	    echo "   $$f: personal media path"; fail=1; fi; \
	  if [ "$$legacy_ok" = 0 ] && grep -oE '\bpicam\b' "$$f" 2>/dev/null | grep -q .; then \
	    echo "   $$f: pre-rebrand name 'picam'"; fail=1; fi; \
	  if grep -qE 'BEGIN (RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY' "$$f" 2>/dev/null; then \
	    echo "   $$f: private key material"; fail=1; fi; \
	done; \
	if [ $$fail -ne 0 ]; then echo '   FAIL'; exit 1; fi; \
	echo '   ok: nothing un-publishable in $(shell git ls-files | wc -l | tr -d " ") tracked files'

# The tree can be clean while history is not. This reads every blob ever
# committed, which is what an attacker or an archivist would do.
audit-history:
	@echo '== auditing every blob in history =='
	@git rev-list --objects --all \
	  | git cat-file --batch-check='%(objecttype) %(objectname)' \
	  | awk '$$1=="blob"{print $$2}' | sort -u > .audit-blobs
	@fail=0; \
	for pat in '192\.168\.' '10\.[0-9]+\.[0-9]+\.[0-9]+' '/home/paul' '64mush' \
	           'BEGIN.*PRIVATE KEY'; do \
	  n=0; \
	  while read -r o; do \
	    git cat-file blob "$$o" 2>/dev/null | grep -qE "$$pat" && n=$$((n+1)); \
	  done < .audit-blobs; \
	  printf '   %-28s %s blob(s)\n' "$$pat" "$$n"; \
	  [ "$$n" != 0 ] && fail=1; \
	done; \
	rm -f .audit-blobs; \
	if [ $$fail -ne 0 ]; then echo '   FAIL -- history is not clean'; exit 1; fi; \
	echo '   ok: history carries no un-publishable content'
	@echo '== files ever committed, by type =='
	@git log --all --pretty=format: --name-only | sort -u | grep -v '^$$' \
	  | sed -E 's/.*\.//' | sort | uniq -c | sort -rn | sed 's/^/   /'

# Drift detector for THIRD-PARTY.md: anything listed here needs a row there.
deps:
	@echo '== third-party Python imports =='
	@grep -rhoE '^[[:space:]]*(import|from) (numpy|picamera2|simplejpeg|piexif|PyQt5)' \
	  --include='*.py' src bin | awk '{print $$2}' | sort | uniq -c | sed 's/^/   /'
	@echo '== external binaries invoked =='
	@grep -rhoE '"(ffmpeg|ffprobe|rsync|ssh|xdg-open|pcmanfm|feh|gio|exiftool)"' \
	  --include='*.py' src bin | tr -d '"' | sort -u | sed 's/^/   /'
	@echo '== GPL surface: files importing PyQt5 =='
	@grep -rl PyQt5 --include='*.py' src bin | sed 's/^/   /'
	@echo '   (everything outside src/birdshot/gui/ must be a docstring or a'
	@echo '    function-local import -- see THIRD-PARTY.md)'

vendor-check:
	@echo '== vendor/ manifests =='
	@if [ -z "$$(ls -A vendor 2>/dev/null | grep -v README.md)" ]; then \
	  echo '   ok: vendor/ holds no third-party code (see vendor/README.md)'; \
	else \
	  fail=0; \
	  for d in vendor/*/; do \
	    [ -d "$$d" ] || continue; \
	    for req in SOURCE.txt LICENSE MANIFEST.sha256; do \
	      [ -f "$$d$$req" ] || { echo "   $$d: missing $$req"; fail=1; }; \
	    done; \
	    if [ -f "$$d/MANIFEST.sha256" ]; then \
	      ( cd "$$d" && shasum -a 256 -c MANIFEST.sha256 --quiet ) \
	        || { echo "   $$d: hashes do not match"; fail=1; }; \
	    fi; \
	  done; \
	  [ $$fail -eq 0 ] || exit 1; \
	  echo '   ok'; \
	fi

hooks:
	@git config core.hooksPath .githooks
	@echo 'pre-commit sanitisation gate installed (core.hooksPath = .githooks)'

# The canonical build (docs/PACKAGING.md, Phase 1): one sdist + wheel that
# every packaging/ channel consumes. Needs `python3 -m pip install build`.
dist: check
	@python3 -m build
	@ls -la dist

doctor:
	@./bin/birdshot-cli doctor

# Launch the app from the checkout. On a machine without the camera stack
# (a Mac today -- the backend split in docs/ROADMAP.md is what changes that)
# this fails fast and points at doctor instead of a bare traceback.
run:
	@python3 bin/birdshot-gui || \
	  { st=$$?; echo; \
	    echo "launch failed -- 'make doctor' lists what this machine is missing"; \
	    exit $$st; }

# Same launch, loud: BIRDSHOT_PROFILE turns on the engine's per-frame timing
# report, -X dev enables Python's debug checks and full warnings, and the Qt
# platform layer logs what it is doing. Windowed, so the terminal stays
# visible next to it.
rundebug:
	@BIRDSHOT_PROFILE=1 QT_LOGGING_RULES='qt.qpa.*=true' \
	  python3 -X dev -W default bin/birdshot-gui --no-maximize || \
	  { st=$$?; echo; \
	    echo "launch failed -- 'make doctor' lists what this machine is missing"; \
	    exit $$st; }

# ------------------------------------------------------------ the hardware --
selftest:
	@./sync.sh selftest

info:
	@./sync.sh info

clean:
	@find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@find . -name '*.pyc' -delete 2>/dev/null || true
	@rm -f .audit-blobs
	@echo 'cleaned'
