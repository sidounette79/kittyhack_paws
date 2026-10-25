#!/bin/sh
# run_tests.sh — run the kittyhack-remote test suite without needing a full
# local dev environment (heavy ML/cv2 deps, Python 3.11 specifically).
#
# 24.10: the production `kittyhack-remote:latest` image already has every
# production dependency installed - reuse it as a base instead of building
# a separate dev image that could drift out of sync. Mount the REAL, live
# source tree (same one `docker-compose`/the running container uses) over
# /app so this always tests actual current code, not whatever was baked
# into the image at its last build (which may be stale, and whose
# .dockerignore excludes tests/ entirely - that's why running this against
# the image's own /app silently finds zero tests).
#
# Usage (from OLDMEDIA, where the image actually lives):
#   ./run_tests.sh                          # full suite
#   ./run_tests.sh tests/unit/test_backend_loop_coverage.py   # one file
#   ./run_tests.sh -k magnet                # by keyword
set -e
cd "$(dirname "$0")"

sudo /usr/local/bin/docker run --rm \
	-v "$(pwd):/app" \
	-v "$(pwd)/../data:/data" \
	--entrypoint "" \
	kittyhack-remote:latest \
	sh -c 'pip install -q pytest pytest-timeout pytest-cov 2>&1 | grep -v "already satisfied" || true; python3 -m pytest tests/unit/ "$@"' -- "$@"
