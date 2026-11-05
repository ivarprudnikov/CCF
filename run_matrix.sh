#!/bin/bash
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the Apache 2.0 License.
#
# Experiment only: reads "label state_mb value_size domain [rewrite_fraction]"
# lines from stdin and runs run_drmem.sh for each, printing a one-line summary
# per run. The workspace of a successful run is removed, to save disk space.
set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
while read -r label mb vs dom rewrite; do
  [[ -z "$label" || "$label" == \#* ]] && continue
  rm -rf "$ROOT/${BUILD_DIR:-build}/workspace/${label}"*
  "$ROOT/run_drmem.sh" "$label" --state-mb "$mb" --value-size "$vs" \
    --domain "$dom" --rewrite-fraction "${rewrite:-0}" > "/tmp/$label.log" 2>&1
  rc=$?
  summary=$(grep -o 'RESULT .* | written to' "/tmp/$label.log")
  echo "$label rc=$rc $summary"
  if [[ $rc -eq 0 ]]; then
    rm -rf "$ROOT/${BUILD_DIR:-build}/workspace/${label}"*
  fi
done
