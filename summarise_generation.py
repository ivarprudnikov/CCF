# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the Apache 2.0 License.
"""
Experiment only: summarises results/{main,1b}[_r2]_priv_1k_<size>.json, written
by run_matrix.sh, as the Markdown tables in SNAPSHOT_GENERATION_MEMORY_TEST.md.
"""

import json
import os
import sys

SIZES = ["64m", "128m", "256m", "512m", "1g", "2g"]
RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def load(build, size, rep=""):
    path = os.path.join(RESULTS, f"{build}{rep}_priv_1k_{size}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    rows = []
    for size in SIZES:
        m, b = load("main", size), load("1b", size)
        assert (
            m["snapshot_bytes"] == b["snapshot_bytes"]
            or abs(m["snapshot_mib"] - b["snapshot_mib"]) < 0.5
        ), size
        rows.append((m, b))

    print(
        "| Snapshot (MiB) | Before, main / 1b (MiB) | Peak, main (MiB) "
        "| Peak, 1b (MiB) | Extra, main (MiB) | Extra, 1b (MiB) "
        "| Peak saved (MiB) | Time, main / 1b (s) |"
    )
    print("| --: | --: | --: | --: | --: | --: | --: | --: |")
    for m, b in rows:
        gm, gb = m["primary_snapshot_generation"], b["primary_snapshot_generation"]
        assert gm["peak_from_hwm"] and gb["peak_from_hwm"]
        saved = gm["peak_rss_mib"] - gb["peak_rss_mib"]
        print(
            f"| {m['snapshot_mib']:.1f} "
            f"| {gm['before_rss_mib']:.0f} / {gb['before_rss_mib']:.0f} "
            f"| {gm['peak_rss_mib']:.1f} ({gm['peak_x_snapshot']:.2f} x S) "
            f"| {gb['peak_rss_mib']:.1f} ({gb['peak_x_snapshot']:.2f} x S) "
            f"| {gm['extra_rss_mib']:.1f} ({gm['extra_x_snapshot']:.2f} x S) "
            f"| {gb['extra_rss_mib']:.1f} ({gb['extra_x_snapshot']:.2f} x S) "
            f"| {saved:.1f} ({100 * saved / gm['peak_rss_mib']:.0f}%) "
            f"| {gm['trigger_to_file_written_s']} / {gb['trigger_to_file_written_s']} |"
        )

    print()
    print(
        "| Snapshot (MiB) | Recovery peak, main (MiB) | Recovery peak, 1b (MiB) "
        "| Excluding LazyFree, main / 1b (MiB) |"
    )
    print("| --: | --: | --: | --: |")
    for m, b in rows:
        print(
            f"| {m['snapshot_mib']:.1f} | {m['recovery_hwm_mib']:.1f} "
            f"| {b['recovery_hwm_mib']:.1f} "
            f"| {m['recovery_peak_rss_minus_lazyfree_mib']:.1f} / "
            f"{b['recovery_peak_rss_minus_lazyfree_mib']:.1f} |"
        )

    print()
    worst = {}
    for size in SIZES:
        for build in ("main", "1b"):
            try:
                r1, r2 = load(build, size), load(build, size, "_r2")
            except FileNotFoundError:
                print(f"No repeat for {build} {size}", file=sys.stderr)
                continue
            for key in ("peak_rss_mib", "extra_rss_mib"):
                a = r1["primary_snapshot_generation"][key]
                c = r2["primary_snapshot_generation"][key]
                diff = abs(a - c) / a * 100
                if diff > worst.get((build, key), (0, None))[0]:
                    worst[(build, key)] = (diff, size)
    for (build, key), (diff, size) in sorted(worst.items()):
        print(f"Repeat pass: largest difference in {build} {key}: {diff:.1f}% ({size})")


if __name__ == "__main__":
    main()
