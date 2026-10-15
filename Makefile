ADDON   := plugin.video.epgstation
VERSION := $(shell sed -n 's/.*<addon[^>]* version="\([^"]*\)".*/\1/p' $(ADDON)/addon.xml)
# Local settings (backlog 019): addresses and private literals live in an untracked KEY=value file outside the
# tree (keys: local.env.example). Command-line variables still win over it.
LOCAL_ENV ?= $(HOME)/.config/epgstation-kodi/local.env
-include $(LOCAL_ENV)
# KODI_HOST as given (command line, environment or local.env), before the default: deploy and smoke refuse
# without one (023).
KODI_HOST_GIVEN := $(KODI_HOST)
KODI_HOST    ?= localhost
EPGS_DEV_URL ?= http://$(KODI_HOST):8888
# No default production URL: snapshot-prod refuses without it, --dev captures need it to refuse it.
EPGS_PROD_URL ?=
# Private deny lists of the public audit; empty when the file does not exist (CI, a fresh clone).
PUBLIC_AUDIT_DENY_FILE  ?= $(wildcard $(HOME)/.config/epgstation-kodi/prod_fingerprints.txt)
PUBLIC_AUDIT_ALLOW_FILE ?= $(wildcard $(HOME)/.config/epgstation-kodi/fingerprint_allow_private.txt)
export LOCAL_ENV KODI_HOST KODI_SSH_USER EPGS_DEV_URL EPGS_PROD_URL SCREENSHOT_SSH SCREENSHOT_VM PUBLIC_DIR \
	PUBLIC_GIT_NAME PUBLIC_GIT_EMAIL PUBLIC_AUDIT_DENY PUBLIC_AUDIT_DENY_FILE PUBLIC_AUDIT_ALLOW_FILE
# Fixtures come from the dev EPGStation's free channels only (backlog 020); --dev refuses EPGS_PROD_URL.
# Broadcast stills never stay in the tree: every capture with thumbnails is followed by the placeholders (019).
PLACEHOLDERS = uv run --with pillow python tools/make_placeholder_thumbs.py
SNAP_DEV = python3 tools/snapshot_api.py --dev --prod-url "$(EPGS_PROD_URL)" $(EPGS_DEV_URL)
# Private GET-only capture of production, outside every work tree, never committed (backlog 020 U8).
PROD_SNAPSHOT_DIR ?= $(HOME)/.cache/epgstation-kodi/prod-snapshot
PROD_FINGERPRINTS ?= $(HOME)/.config/epgstation-kodi/prod_fingerprints.txt
# Kodi repository (backlog 010). The base URL is baked into the repository addon at build time; since 1.1.0
# (backlog 019) every build points at GitHub Pages, the only host since the LAN repository was retired (022).
REPO_ID       := repository.kodistation
REPO_VERSION  := $(shell sed -n 's/.*<addon[^>]* version="\([^"]*\)".*/\1/p' $(REPO_ID)/addon.xml)
PUBLIC_REPO_BASE_URL := https://myfeethurt.github.io/kodistation/
REPO_BASE_URL ?= $(PUBLIC_REPO_BASE_URL)
REPO_DIR      ?= dist/repo
ZIPS := dist/$(ADDON)-$(VERSION).zip dist/$(REPO_ID)-$(REPO_VERSION).zip
# Demo video: raw clips and step logs are private, outside the tree. demo-shoot films one clip on the Kodi in
# local.env (KODI_HOST, KODI_SSH_USER; GBM, JSON-RPC and EventServer on); FREE_ONLY=<EPGStation URL> keeps its paid
# channels off camera, SHOOT_ARGS passes more (--forbid TITLE, --server, --episode, --channel, --recording).
DEMO_RAW ?= $(HOME)/.cache/epgstation-kodi/demo

.PHONY: help lint test check build zip repo release deploy smoke screenshot ci audit-public export-public export-pages-init snapshot-prod snapshot-dev snapshot-dev2 snapshot-schedules snapshot-live snapshot-reserves snapshot-stream fake fake-prod clean demo-shoot demo-edit demo-check

help:            ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

lint:            ## ruff
	uv run ruff check .

test:            ## unit + fake-server integration tests
	uv run pytest

build:           ## clean copies of both addons in dist/ (no caches; repository addon rendered for REPO_BASE_URL)
	rm -rf dist/$(ADDON) && mkdir -p dist
	rsync -a --exclude __pycache__ --exclude '*.pyc' $(ADDON) dist/
	python3 tools/build_repo.py render --base-url $(REPO_BASE_URL) --src $(REPO_ID) --out dist/$(REPO_ID)

check: build     ## kodi-addon-checker on both built addons (Kodi repo indexes fetched with retries and cached a day: tools/addon_check.py)
	python3 tools/build_repo.py check-repo-addon --src $(REPO_ID)
	cd dist && for a in $(ADDON) $(REPO_ID); do \
		timeout 600 uv run python ../tools/addon_check.py --branch omega $$a || exit 1; \
	done

zip: build       ## reproducible installable zips of both addons in dist/, checked for development files
	python3 tools/build_repo.py zip --out dist/$(ADDON)-$(VERSION).zip dist/$(ADDON)
	python3 tools/build_repo.py zip --out dist/$(REPO_ID)-$(REPO_VERSION).zip dist/$(REPO_ID)
	python3 tools/build_repo.py check-zip --git-root . $(ZIPS)
	@for z in $(ZIPS); do python3 -m zipfile -l $$z | grep -E '(^|/)(tests|tools|dist|__pycache__)/|\.pyc( |$$)' && \
		{ echo "development files in $$z" >&2; exit 1; }; done; true

repo: zip        ## publish both zips into the Kodi repository directory REPO_DIR (default dist/repo)
	python3 tools/build_repo.py publish --repo-dir $(REPO_DIR) $(ZIPS)

release:         ## cut a release: make release VERSION=x.y.z (bump, <news>, commit, tag; never pushes)
	@[ "$(origin VERSION)" = "command line" ] || { echo "usage: make release VERSION=x.y.z" >&2; exit 1; }
	RELEASE_TRAILER="$(RELEASE_TRAILER)" tools/release.sh $(VERSION)

deploy: build    ## install on the dev VM Kodi
	KODI_HOST="$(KODI_HOST_GIVEN)" tools/deploy.sh "$(KODI_HOST_GIVEN)"

smoke:           ## end-to-end check through Kodi JSON-RPC on the dev VM
	uv run python tools/kodi_smoke.py --host "$(KODI_HOST_GIVEN)"

screenshot:      ## smoke + screenshot of the recordings list (dist/screenshot.png)
	uv run python tools/kodi_smoke.py --host "$(KODI_HOST_GIVEN)" --screenshot dist/screenshot.png

ci: lint test check audit-public  ## everything that runs without the VM

audit-public:    ## the public export of the working tree (git archive, export-ignore) passes tools/public_audit.py (019)
	python3 tools/public_audit.py --worktree .

export-pages-init: ## go-live G4: orphan gh-pages commit (empty index.html) in PUBLIC_DIR by the public identity (never pushes)
	python3 tools/export_public.py --init-pages

export-public:   ## one commit of HEAD into PUBLIC_DIR (never pushes): make export-public MESSAGE="..." [TAG=1] [INIT=1]
	@[ -n "$(MESSAGE)" ] || { echo 'usage: make export-public MESSAGE="KODIstation x.y.z" [TAG=1] [INIT=1]' >&2; exit 1; }
	python3 tools/export_public.py --message "$(MESSAGE)" $(if $(filter 1,$(TAG)),--tag) $(if $(filter 1,$(INIT)),--init)

snapshot-prod:   ## private GET-only capture of production into PROD_SNAPSHOT_DIR (outside the tree) + PROD_FINGERPRINTS; OPENAPI=1 also refreshes docs/api/openapi.json
	@[ -n "$(EPGS_PROD_URL)" ] || { echo "EPGS_PROD_URL is not set (local.env)" >&2; exit 1; }
	python3 tools/snapshot_api.py $(EPGS_PROD_URL) $(PROD_SNAPSHOT_DIR) --reserves-too --fingerprints $(PROD_FINGERPRINTS) \
		$(if $(filter 1,$(OPENAPI)),--openapi docs/api/openapi.json)

snapshot-dev:    ## refresh tests/fixtures/dev in place: its own recordings only (the ids tests pin; never dev2's), stale files removed
	$(SNAP_DEV) tests/fixtures/dev --only-ids-of tests/fixtures/dev --exclude-ids-of tests/fixtures/dev2
	$(PLACEHOLDERS) tests/fixtures/dev

snapshot-dev2:   ## refresh tests/fixtures/dev2: the newest <= 100 free dev recordings not in dev, drop logs, reservations
	rm -rf tests/fixtures/dev2
	$(SNAP_DEV) tests/fixtures/dev2 --exclude-ids-of tests/fixtures/dev --newest 100 --reserves-too
	$(PLACEHOLDERS) tests/fixtures/dev2

snapshot-schedules: ## refresh tests/fixtures/dev/schedule_texts.json (4 days of free dev guide texts, GET only)
	$(SNAP_DEV) tests/fixtures/dev --schedules-only 4

snapshot-live:   ## refresh tests/fixtures/live (free channels, schedules now..+6h, guide.json; shared by dev and dev2; GET only, no streams)
	$(SNAP_DEV) tests/fixtures/live --live

snapshot-reserves: ## refresh the reservation fixtures of dev and dev2 (backlog 007; GET only, <= 60 KB each)
	$(SNAP_DEV) tests/fixtures/dev --reserves
	$(SNAP_DEV) tests/fixtures/dev2 --reserves

snapshot-stream: ## refresh tests/fixtures/streams (011): ONE dev recorded HLS stream (<= 30 s, always DELETEd; refuses production)
	python3 tools/dev/snapshot_stream.py

fake:            ## serve the dev2 fixtures as a fake EPGStation on :8889
	python3 tools/fake_epgstation.py tests/fixtures/dev2 --port 8889

fake-prod:       ## serve the private prod snapshot (make snapshot-prod) on :8889, for manual checks only
	python3 tools/fake_epgstation.py $(PROD_SNAPSHOT_DIR) --port 8889

demo-shoot:      ## film one demo clip: make demo-shoot CLIP=firstrun|library|live|record|stream NAME=F|L|V|R|S
	@[ -n "$(KODI_HOST_GIVEN)" ] && [ -n "$(CLIP)" ] && [ -n "$(NAME)" ] || \
		{ echo "usage: make demo-shoot CLIP=... NAME=... (KODI_HOST in local.env)" >&2; exit 1; }
	mkdir -p -m 700 $(DEMO_RAW)
	python3 tools/demo/shoot.py $(CLIP) --name $(NAME) --out $(DEMO_RAW) --kodi $(KODI_HOST_GIVEN) \
		--ssh "$${KODI_SSH_USER:+$$KODI_SSH_USER@}$(KODI_HOST_GIVEN)" $(if $(FREE_ONLY),--free-only $(FREE_ONLY)) $(SHOOT_ARGS)

demo-edit:       ## build media/ from the clips in DEMO_RAW and tools/demo/cuts.json
	python3 tools/demo/edit.py --raw $(DEMO_RAW) --out media --work $(DEMO_RAW)

demo-check:      ## check the media files: no metadata, no text chunks/comments, no private literal, size/length limits
	python3 tools/demo/check_media.py media/*.mp4 media/*.gif media/*.png

clean:
	rm -rf dist .pytest_cache .ruff_cache
	find . -name __pycache__ -prune -exec rm -rf {} +
