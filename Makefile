SHELL := /bin/sh
.DEFAULT_GOAL := help

IMAGE_DIGEST ?=
POSTGRES_IMAGE_DIGEST ?=
SOURCE_MANIFEST ?=
MODE ?=

contract-red: EVIDENCE_DIR ?= .omo/evidence/task-1-nvidia-build-lb
test-vault-auth: EVIDENCE_DIR ?= .omo/evidence/task-2-nvidia-build-lb
test-nvidia-routing: EVIDENCE_DIR ?= .omo/evidence/task-3-nvidia-build-lb
test-ui-fake: EVIDENCE_DIR ?= .omo/evidence/task-4-nvidia-build-lb
test-api: EVIDENCE_DIR ?= .omo/evidence/task-5-nvidia-build-lb
build-candidate: EVIDENCE_DIR ?= .omo/evidence/task-6a-nvidia-build-lb
test-browser-prod: EVIDENCE_DIR ?= .omo/evidence/task-6b-nvidia-build-lb
verify-local scan-release: EVIDENCE_DIR ?= .omo/evidence/task-7-nvidia-build-lb
smoke-live: EVIDENCE_DIR ?= .omo/evidence/task-10-nvidia-build-lb
smoke-hermes: EVIDENCE_DIR ?= .omo/evidence/task-11b-nvidia-build-lb

.PHONY: help contract-red test-vault-auth test-nvidia-routing test-ui-fake test-api
.PHONY: build-candidate test-browser-prod verify-local scan-release smoke-live smoke-hermes

help:
	@printf '%s\n' \
		'nvidia-build-lb verification targets' \
		'' \
		'  contract-red       Todo 1 intentional-red verifier' \
		'  test-vault-auth    Todo 2 pytest marker: vault_auth' \
		'  test-nvidia-routing Todo 3 pytest marker: nvidia_routing' \
		'  test-ui-fake       Todo 4 pytest marker: ui_fake' \
		'  test-api           Todo 5 pytest marker: api' \
		'  build-candidate    Todo 6A candidate image and compose gate' \
		'  test-browser-prod  Todo 6B browser gate; requires app/PG digests and source manifest' \
		'  verify-local       Todo 7 local gate; requires app/PG digests and source manifest' \
		'  scan-release       Todo 7 scans; requires app/PG digests and source manifest' \
		'  smoke-live         Todo 10A/10B script; requires MODE and IMAGE_DIGEST (reserved)' \
		'  smoke-hermes       Todo 11B script; requires IMAGE_DIGEST (reserved)' \
		'' \
		'Build candidate.json records IMAGE_DIGEST and POSTGRES_IMAGE_DIGEST.' \
		'Reuse those values and the same source-manifest.json for 6B and 7.' \
		'' \
		'All targets accept EVIDENCE_DIR and use their own task-scoped default.'

contract-red:
	uv run python tests/contracts/verify_intentional_red.py --evidence-dir "$(EVIDENCE_DIR)"

test-vault-auth:
	EVIDENCE_DIR="$(EVIDENCE_DIR)" uv run pytest -m vault_auth -q

test-nvidia-routing:
	EVIDENCE_DIR="$(EVIDENCE_DIR)" uv run pytest -m nvidia_routing -q

test-ui-fake:
	EVIDENCE_DIR="$(EVIDENCE_DIR)" uv run pytest -m ui_fake -q

test-api:
	EVIDENCE_DIR="$(EVIDENCE_DIR)" uv run pytest -m api -q

build-candidate:
	@test -x scripts/qa/build-candidate.sh || { printf '%s\n' 'UNIMPLEMENTED[78]: Todo 6A owns scripts/qa/build-candidate.sh'; exit 78; }
	EVIDENCE_DIR="$(EVIDENCE_DIR)" scripts/qa/build-candidate.sh

test-browser-prod:
	@test -n "$(IMAGE_DIGEST)" || { printf '%s\n' 'INPUT[64]: IMAGE_DIGEST is required'; exit 64; }
	@test -n "$(POSTGRES_IMAGE_DIGEST)" || { printf '%s\n' 'INPUT[64]: POSTGRES_IMAGE_DIGEST is required'; exit 64; }
	@test -n "$(SOURCE_MANIFEST)" || { printf '%s\n' 'INPUT[64]: SOURCE_MANIFEST is required'; exit 64; }
	@test -x scripts/qa/test-browser-prod.sh || { printf '%s\n' 'UNIMPLEMENTED[78]: Todo 6B owns scripts/qa/test-browser-prod.sh'; exit 78; }
	IMAGE_DIGEST="$(IMAGE_DIGEST)" POSTGRES_IMAGE_DIGEST="$(POSTGRES_IMAGE_DIGEST)" SOURCE_MANIFEST="$(SOURCE_MANIFEST)" EVIDENCE_DIR="$(EVIDENCE_DIR)" scripts/qa/test-browser-prod.sh

verify-local:
	@test -n "$(IMAGE_DIGEST)" || { printf '%s\n' 'INPUT[64]: IMAGE_DIGEST is required'; exit 64; }
	@test -n "$(POSTGRES_IMAGE_DIGEST)" || { printf '%s\n' 'INPUT[64]: POSTGRES_IMAGE_DIGEST is required'; exit 64; }
	@test -n "$(SOURCE_MANIFEST)" || { printf '%s\n' 'INPUT[64]: SOURCE_MANIFEST is required'; exit 64; }
	@test -x scripts/qa/verify-local.sh || { printf '%s\n' 'UNIMPLEMENTED[78]: Todo 7 owns scripts/qa/verify-local.sh'; exit 78; }
	IMAGE_DIGEST="$(IMAGE_DIGEST)" POSTGRES_IMAGE_DIGEST="$(POSTGRES_IMAGE_DIGEST)" SOURCE_MANIFEST="$(SOURCE_MANIFEST)" EVIDENCE_DIR="$(EVIDENCE_DIR)" scripts/qa/verify-local.sh

scan-release:
	@test -n "$(IMAGE_DIGEST)" || { printf '%s\n' 'INPUT[64]: IMAGE_DIGEST is required'; exit 64; }
	@test -n "$(POSTGRES_IMAGE_DIGEST)" || { printf '%s\n' 'INPUT[64]: POSTGRES_IMAGE_DIGEST is required'; exit 64; }
	@test -n "$(SOURCE_MANIFEST)" || { printf '%s\n' 'INPUT[64]: SOURCE_MANIFEST is required'; exit 64; }
	@test -x scripts/qa/scan-release.sh || { printf '%s\n' 'UNIMPLEMENTED[78]: Todo 7 owns scripts/qa/scan-release.sh'; exit 78; }
	IMAGE_DIGEST="$(IMAGE_DIGEST)" POSTGRES_IMAGE_DIGEST="$(POSTGRES_IMAGE_DIGEST)" SOURCE_MANIFEST="$(SOURCE_MANIFEST)" EVIDENCE_DIR="$(EVIDENCE_DIR)" scripts/qa/scan-release.sh

smoke-live:
	@case "$(MODE)" in one-key|two-key) ;; *) printf '%s\n' 'INPUT[64]: MODE must be one-key or two-key'; exit 64 ;; esac
	@test -n "$(IMAGE_DIGEST)" || { printf '%s\n' 'INPUT[64]: IMAGE_DIGEST is required'; exit 64; }
	@test -x scripts/qa/smoke-live.sh || { printf '%s\n' 'UNIMPLEMENTED[78]: Todo 10A/10B owns scripts/qa/smoke-live.sh'; exit 78; }
	MODE="$(MODE)" IMAGE_DIGEST="$(IMAGE_DIGEST)" EVIDENCE_DIR="$(EVIDENCE_DIR)" scripts/qa/smoke-live.sh

smoke-hermes:
	@test -n "$(IMAGE_DIGEST)" || { printf '%s\n' 'INPUT[64]: IMAGE_DIGEST is required'; exit 64; }
	@test -x scripts/qa/smoke-hermes.sh || { printf '%s\n' 'UNIMPLEMENTED[78]: Todo 11B owns scripts/qa/smoke-hermes.sh'; exit 78; }
	IMAGE_DIGEST="$(IMAGE_DIGEST)" EVIDENCE_DIR="$(EVIDENCE_DIR)" scripts/qa/smoke-hermes.sh
