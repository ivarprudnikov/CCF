#!/bin/bash
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the Apache 2.0 License.
#
# Experiment only: runs the snapshot generation experiment for main and for
# step 1b, alternating between the builds at each size so that both see the
# same machine state. Each argument is <size-label>:<state-mb>, e.g. 64m:45.
# Labels are prefixed with $REP if set (e.g. REP=_r2 for a second pass).
# Records have $VALUE_SIZE-byte values (default 1024), tagged $VALUE_TAG in
# the labels (default 1k).
# Results are written to results/ in each tree.
set -o pipefail

MAIN_TREE=${MAIN_TREE:-/home/ubuntu/CCF-worktree-dr-memory-main}
CHANGE_TREE=${CHANGE_TREE:-/home/ubuntu/CCF-worktree-dr-memory-1b}
VALUE_SIZE=${VALUE_SIZE:-1024}
VALUE_TAG=${VALUE_TAG:-1k}
run() {
  echo "$2 $3 $VALUE_SIZE private" | (cd "$1" && BUILD_DIR=build ./run_matrix.sh)
}
for spec in "$@"; do
  size=${spec%%:*}
  mb=${spec##*:}
  run "$MAIN_TREE" "main${REP}_priv_${VALUE_TAG}_${size}" "$mb"
  run "$CHANGE_TREE" "1b${REP}_priv_${VALUE_TAG}_${size}" "$mb"
done
