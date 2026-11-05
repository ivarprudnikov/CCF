# Snapshot generation memory test (step 1b)

This test measures the memory a CCF node uses while it generates a snapshot, with and without the change on branch `dr-memory-1b-snapshot-generation` (commit `e801e2824`, step 1b of the disaster recovery memory plan), for snapshots from 63 MiB to 2056 MiB.

## Result

- With the change, generating a snapshot of size `S` takes `1.0 x S` of extra memory (the snapshot itself) instead of `3.0 x S`, at every size tested.
- For the largest snapshot, 2056 MiB (2,155,349,339 bytes, over 2 GiB), the node's peak fell from 9155 MiB to 5021 MiB, 45% lower.
  From 1 GiB upwards the peak is about `2.5 x S` instead of `4.5 x S`.
- Snapshots were also generated faster: 6-8 s instead of 12-13 s at 2056 MiB, from the trigger until the snapshot file was written.
- Every snapshot was then used to recover the service, which succeeded with both builds at every size.
- The recovery peak, excluding memory freed but not yet reclaimed, did not change: it is reached while the recovering node loads the snapshot, which this change does not modify (steps 1a and 1c of the plan do).

## What was compared

| Build  | Code                                                                            |
| ------ | ------------------------------------------------------------------------------- |
| `main` | microsoft/CCF `main` at `9e5082345` (5 November 2026)                           |
| 1b     | `main` plus this change only, built from `924ccb06d` (same tree as `e801e2824`) |

Both were built the same way, in the same container (Azure Linux 3.0, clang 18.1.8), with the default CMake configuration (`RelWithDebInfo`, snmalloc allocator).
Both ran the unmodified `basic` sample app on the virtual platform, on a machine with 8 vCPUs and 27 GiB of memory.

## How the test was carried out

The script `tests/dr_memory_experiment.py` was run once for each build and snapshot size, alternating between the builds at each size, and the whole set was run twice.
Each run:

1. Starts a single-node service running the `basic` sample app, with one worker thread and automatic snapshots disabled (`snapshot_tx_interval` of 1,000,000).
2. Writes private records with 1 KiB values to the app's `records` table through its `POST /app/records` endpoint, in requests of about 2 MiB, until it has written the requested amount of data, chosen to give the target snapshot size.
3. Records the node's memory, triggers a snapshot with `POST /node/snapshot:create`, and waits until the snapshot is committed.
4. Stops the service and recovers it from that snapshot, sampling the recovering node's memory until the recovered service has opened and written its own snapshot.

The node's memory is read from `/proc/<pid>/status` every 20 ms:

- Before: resident memory (`VmRSS`) just before the snapshot is triggered, mostly the key-value state (`1.4 x S` at 2056 MiB).
- Peak: the kernel's resident memory high-water mark (`VmHWM`) once the snapshot is committed.
  It is exact regardless of sampling, and in every run it was higher than just before the trigger, so it was reached while the snapshot was generated.
- Extra: peak minus before, the memory needed to generate the snapshot.
- Time: from the trigger to the node's `New snapshot file written` log line.

The allocator returns freed memory to the kernel lazily (`MADV_FREE`), so it can count as resident until the kernel reclaims it.
The script also samples this memory (`LazyFree` in `/proc/<pid>/smaps_rollup`); excluding it changes the extra memory by at most `0.15 x S` in any run, so it does not explain the difference.

To reproduce, inside the build container, build each tree (this branch, and `dr-memory-main-baseline`, which is `main` plus the same scripts), then run both with `run_1b_comparison.sh` from this tree.
`run_drmem.sh` expects a Python virtual environment in each tree's `env`, with `tests/requirements.txt` and the `python/` package installed.

```bash
cmake -S . -B build -GNinja && ninja -C build basic    # in each tree
./run_1b_comparison.sh 64m:45 128m:92 256m:186 512m:373 1g:748 2g:1504
REP=_r2 ./run_1b_comparison.sh 64m:45 128m:92 256m:186 512m:373 1g:748 2g:1504
cp ../CCF-worktree-dr-memory-main/results/main_*.json results/
python3 summarise_generation.py
```

Each argument is a label and the amount of data to write (`--state-mb`), chosen to give snapshots of about 64 MiB to 2 GiB; each run's results are written to `results/<label>.json` in the tree of the build it ran, and `summarise_generation.py` prints the tables below from them.

## Results

Memory of the node while it generates a snapshot, first pass:

| Snapshot (MiB) | Before, main / 1b (MiB) |  Peak, main (MiB) |    Peak, 1b (MiB) | Extra, main (MiB) |   Extra, 1b (MiB) | Peak saved (MiB) | Time, main / 1b (s) |
| -------------: | ----------------------: | ----------------: | ----------------: | ----------------: | ----------------: | ---------------: | ------------------: |
|           62.7 |               154 / 157 |  343.2 (5.47 x S) |  220.0 (3.51 x S) |  189.2 (3.02 x S) |   62.8 (1.00 x S) |      123.2 (36%) |         0.36 / 0.33 |
|          128.0 |               242 / 243 |  627.1 (4.90 x S) |  371.1 (2.90 x S) |  384.6 (3.00 x S) |  128.3 (1.00 x S) |      256.0 (41%) |         0.78 / 0.42 |
|          256.0 |               434 / 427 | 1203.7 (4.70 x S) |  687.5 (2.69 x S) |  769.9 (3.01 x S) |  260.6 (1.02 x S) |      516.2 (43%) |         1.35 / 0.83 |
|          511.9 |               785 / 781 | 2330.9 (4.55 x S) | 1301.7 (2.54 x S) | 1545.8 (3.02 x S) |  521.1 (1.02 x S) |     1029.2 (44%) |         2.57 / 1.49 |
|         1023.7 |             1498 / 1501 | 4589.3 (4.48 x S) | 2542.7 (2.48 x S) | 3091.4 (3.02 x S) | 1042.1 (1.02 x S) |     2046.6 (45%) |         4.95 / 3.97 |
|         2055.5 |             2941 / 2927 | 9154.6 (4.45 x S) | 5021.4 (2.44 x S) | 6213.3 (3.02 x S) | 2094.4 (1.02 x S) |     4133.2 (45%) |        12.85 / 7.76 |

The second pass reproduced every peak within 3.2% and every extra within 1.6% of the first, and within 1.5% from 256 MiB upwards.

Peak memory of the node recovering from that snapshot, first pass:

| Snapshot (MiB) | Recovery peak, main (MiB) | Recovery peak, 1b (MiB) | Excluding LazyFree, main / 1b (MiB) |
| -------------: | ------------------------: | ----------------------: | ----------------------------------: |
|           62.7 |                     367.2 |                   367.0 |                       303.3 / 300.2 |
|          128.0 |                     848.8 |                   720.6 |                       590.7 / 590.0 |
|          256.0 |                    1412.6 |                  1569.7 |                     1152.3 / 1130.9 |
|          511.9 |                    2797.1 |                  2797.3 |                     2276.6 / 2270.5 |
|         1023.7 |                    5564.2 |                  5565.3 |                     4523.4 / 4516.8 |
|         2055.5 |                   11148.6 |                 11149.0 |                     9058.3 / 9058.1 |

Excluding memory freed but not yet reclaimed, the recovery peaks of the two builds are within 2% of each other at every size, in both passes.
Including it, the change lowered the recovery peak at 128 MiB in both passes (849 to 721 MiB), and raised it at 256 MiB in the first pass only.
During recovery at 2056 MiB, generating the snapshot when the recovered service opens peaked at about 9058 MiB on `main` and 5029 MiB with the change, excluding that memory, but loading the snapshot beforehand reaches 9058 MiB with both builds.

## Scope

- Only private records with 1 KiB values were tested, on a single node.
  Public tables take the same serialisation path, without encryption.
  The size of the key-value state relative to the snapshot (`1.4 x S` here) depends on the size of the values, while the extra memory to generate a snapshot was proportional to the snapshot's size at every size tested.
- The primary generates a snapshot every `snapshot_tx_interval` transactions, so the saving applies to every service with snapshots, not only during disaster recovery.

## Other checks on the change

- Unit tests pass: `kv_test`, including the two new tests (the snapshot written in place is byte-identical to the previous serialisation, and finalising it is one-shot), `encryptor_test`, `snapshot_test`, `snapshotter_test`, `history_test`, `historical_queries_test` and `ledger_secrets_test`.
- End-to-end tests pass: `recovery_test`, `recovery_stale_snapshot_join_test`, `recovery_intermediate_snapshot_join_test` and `recovery_snapshot_endorsements_test`.
- clang-tidy reports nothing for `src/node/recovery_decision_protocol.cpp`, which includes every changed header, and nothing new in the test file; clang-format and the changelog, ASCII and disallowed-comment checks pass.
- Not run: the full CI suite.
