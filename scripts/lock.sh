#!/usr/bin/env bash
# Regenerate requirements.lock from requirements.txt.
#
# Resolves into a throwaway virtualenv rather than freezing the one you develop
# in: your working venv also holds pytest, ruff and their trees, and a lock
# that carries test tooling into the production image is worse than no lock.
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3.11}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "resolving requirements.txt with $PYTHON ..."
"$PYTHON" -m venv "$TMP/venv"
"$TMP/venv/bin/pip" install --quiet --upgrade pip
"$TMP/venv/bin/pip" install --quiet -r requirements.txt

{
  cat <<'HEADER'
# Generated. Do not edit by hand.
#
# The full runtime tree, transitive dependencies included, pinned to exact
# versions. `requirements.txt` states which libraries this service depends on
# and the floor for each; this file states what actually gets installed, so
# two builds of the same commit install the same code. Without it, a
# transitive release between builds silently changes what ships.
#
# Regenerate after editing requirements.txt:
#
#   make lock
#
# and run the suite before committing the result -- a lock bump is a code
# change, and the tests are what say whether it is a safe one.
#
# Resolved on Python 3.11; the images run 3.12. Version-conditional
# packages (async-timeout, needed only below 3.11.3) are harmless there.
HEADER
  echo
  "$TMP/venv/bin/pip" freeze --exclude-editable \
    | grep -viE '^(pip|setuptools|wheel)==' \
    | sort -f
} > requirements.lock

echo "wrote requirements.lock ($(grep -cv '^#\|^$' requirements.lock) pinned packages)"
echo
echo "next: pip install -r requirements-dev.txt && pytest -q"
