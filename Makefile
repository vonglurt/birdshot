# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
#
# birdshot -- IMX477 bird and sky capture for the Raspberry Pi CM4.
#
# The compiled 2.0 line under native/ owns the front door: `make run`,
# `doctor`, `selftest`, `info` and `dist` all mean the native binary. The
# 1.x Python line lives on, deprecated, under prototype/ -- every target
# that touches it is prefixed `prototype-` (see prototype/README.md).
#
# Most of this Makefile runs on the Mac you develop on. The one target that
# cannot is `prototype-selftest`, which needs the camera; it is driven over
# SSH.

SHELL := /usr/bin/env bash
NATIVE_BUILD := native/build
QT_PREFIX := $(shell brew --prefix qt 2>/dev/null)
PY_SRC := $(shell find prototype/src -name '*.py' 2>/dev/null)
BIN_SRC := prototype/bin/birdshot-cli prototype/bin/birdshot-gui prototype/bin/birdshot-wallpaper
SH_SRC := prototype/sync.sh prototype/install.sh $(wildcard prototype/mac/*.sh) .githooks/pre-commit

.DEFAULT_GOAL := help
.PHONY: help check lint sanitise audit-history vendor-check hooks \
        build run doctor selftest info dist \
        prototype prototype-debug prototype-doctor prototype-deps \
        prototype-dist prototype-selftest prototype-info clean

help:
	@echo 'birdshot -- make targets'
	@echo
	@echo '  make check          the pre-push gate: build + selftest + lint + sanitise + vendor-check'
	@echo '  make build          compile the native line (cmake, Release)'
	@echo '  make run            build + open the GUI (Qt front end; browser viewfinder without Qt)'
	@echo '  make doctor         native doctor: deps, cameras, storage'
	@echo '  make selftest       the native selftest, 30 checks, no hardware needed'
	@echo '  make info           native info: backend, site, storage'
	@echo '  make dist           stage the native binary into dist/'
	@echo
	@echo '  make lint           byte-compile the prototype, parse every shell script'
	@echo '  make sanitise       scan tracked files for un-publishable strings'
	@echo '  make audit-history  scan every blob in every commit (slower, thorough)'
	@echo '  make vendor-check   verify vendor/ manifests against their hashes'
	@echo '  make hooks          install the sanitisation pre-commit hook'
	@echo
	@echo '  the deprecated 1.x Python line, in prototype/:'
	@echo '  make prototype           launch the Python GUI from this checkout'
	@echo '  make prototype-debug     Python GUI with profiling, Qt logging, dev mode'
	@echo '  make prototype-doctor    check this machine for the Python line'
	@echo '  make prototype-deps      list third-party imports and external binaries'
	@echo '  make prototype-dist      build the sdist + wheel the 1.x channels consume'
	@echo '  make prototype-selftest  on-camera selftest on the Pi (needs hardware)'
	@echo '  make prototype-info      camera, modes, storage and calibration, from the Pi'

# ---------------------------------------------------------------- the gate --
# Compiled-first: the gate will not pass unless the native line builds and
# its selftest is green, on this machine, right now.
check: build selftest lint sanitise vendor-check
	@echo
	@echo 'check: PASS'

lint:
	@echo '== byte-compiling the Python prototype =='
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

# ------------------------------------------------------- the native line --
# Needs only cmake and a C++17 compiler; the build directory is reused
# across runs.
build:
	@cmake -S native -B $(NATIVE_BUILD) -DCMAKE_BUILD_TYPE=Release $(if $(QT_PREFIX),-DCMAKE_PREFIX_PATH=$(QT_PREFIX))
	@cmake --build $(NATIVE_BUILD) --config Release

# Bare `make run` launches the GUI, as it always has: the Qt front end when
# Qt was found at build time, else the browser viewfinder. Any other command
# rides ARGS: `make run ARGS='plan --days 3'`.
run: build
	@if [ -n "$(ARGS)" ]; then $(NATIVE_BUILD)/birdshot $(ARGS); \
	elif [ -x $(NATIVE_BUILD)/birdshot-gui ]; then $(NATIVE_BUILD)/birdshot-gui; \
	else $(NATIVE_BUILD)/birdshot gui; fi

doctor: build
	@$(NATIVE_BUILD)/birdshot doctor

selftest: build
	@$(NATIVE_BUILD)/birdshot selftest

info: build
	@$(NATIVE_BUILD)/birdshot info

# One binary is the distribution (docs/PACKAGING.md is the 1.x story; the
# native channels live in native/packaging/). CI builds the per-platform
# release artifacts; this stages this machine's.
dist: check
	@mkdir -p dist
	@cp $(NATIVE_BUILD)/birdshot dist/
	@ls -la dist

# ---------------------------------------------- the 1.x Python prototype --
# Deprecated: bug fixes only, no new features. See prototype/README.md.
prototype-doctor:
	@./prototype/bin/birdshot-cli doctor

# Launch the Python GUI from the checkout. On a machine without the camera
# stack (a Mac today) this fails fast and points at prototype-doctor instead
# of a bare traceback.
prototype:
	@python3 prototype/bin/birdshot-gui || \
	  { st=$$?; echo; \
	    echo "launch failed -- 'make prototype-doctor' lists what this machine is missing"; \
	    exit $$st; }

# Same launch, loud: BIRDSHOT_PROFILE turns on the engine's per-frame timing
# report, -X dev enables Python's debug checks and full warnings, and the Qt
# platform layer logs what it is doing. Windowed, so the terminal stays
# visible next to it.
prototype-debug:
	@BIRDSHOT_PROFILE=1 QT_LOGGING_RULES='qt.qpa.*=true' \
	  python3 -X dev -W default prototype/bin/birdshot-gui --no-maximize || \
	  { st=$$?; echo; \
	    echo "launch failed -- 'make prototype-doctor' lists what this machine is missing"; \
	    exit $$st; }

# Drift detector for THIRD-PARTY.md: anything listed here needs a row there.
prototype-deps:
	@echo '== third-party Python imports =='
	@grep -rhoE '^[[:space:]]*(import|from) (numpy|picamera2|simplejpeg|piexif|PyQt5)' \
	  --include='*.py' prototype/src prototype/bin | awk '{print $$2}' | sort | uniq -c | sed 's/^/   /'
	@echo '== external binaries invoked =='
	@grep -rhoE '"(ffmpeg|ffprobe|rsync|ssh|xdg-open|pcmanfm|feh|gio|exiftool)"' \
	  --include='*.py' prototype/src prototype/bin | tr -d '"' | sort -u | sed 's/^/   /'
	@echo '== GPL surface: files importing PyQt5 =='
	@grep -rl PyQt5 --include='*.py' prototype/src prototype/bin | sed 's/^/   /'
	@echo '   (everything outside prototype/src/birdshot/gui/ must be a docstring or a'
	@echo '    function-local import -- see THIRD-PARTY.md)'

# The canonical 1.x build (docs/PACKAGING.md, Phase 1): one sdist + wheel
# that the 1.x packaging/ channels consume. Needs `pip install build`.
prototype-dist: check
	@python3 -m build prototype --outdir prototype/dist
	@ls -la prototype/dist

# ------------------------------------------------------------ the hardware --
prototype-selftest:
	@./prototype/sync.sh selftest

prototype-info:
	@./prototype/sync.sh info

clean:
	@rm -rf $(NATIVE_BUILD) dist
	@find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@find . -name '*.pyc' -delete 2>/dev/null || true
	@rm -f .audit-blobs
	@echo 'cleaned'
