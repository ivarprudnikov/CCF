#!/bin/bash
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the Apache 2.0 License.
#
# Experiment only: runs tests/dr_memory_experiment.py once against the build in
# $BUILD_DIR (default: build), writing results/<label>.json.
# Usage: run_drmem.sh <label> [dr_memory_experiment.py args...]
set -eo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$ROOT/${BUILD_DIR:-build}"
# shellcheck disable=SC1091
source "$ROOT/env/bin/activate"
export PYTHONPATH="$ROOT/tests"

LABEL=$1
shift
C="$ROOT/samples/constitutions/default"
mkdir -p "$ROOT/results"
python3 "$ROOT/tests/dr_memory_experiment.py" -b . --label "$LABEL" \
  --log-level info --worker-threads 1 \
  --constitution "$C/actions.js" --constitution "$C/validate.js" \
  --constitution "$C/resolve.js" --constitution "$C/apply.js" \
  --package samples/apps/basic/basic --snapshot-tx-interval 1000000 \
  --max-http-body-size 16777216 --ledger-chunk-bytes 5MB \
  --ledger-recovery-timeout 600 --request-timeout 300 \
  --output "$ROOT/results/$LABEL.json" "$@"
