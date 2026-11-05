# Snapshot generation memory test (step 1b)

This test measures the memory a CCF node uses while it generates a snapshot, with and without the change on branch `dr-memory-1b-snapshot-generation` (commit `e801e2824`, step 1b of the disaster recovery memory plan), for snapshots from 63 MiB to 2056 MiB holding records with 1 KiB values or with 1 MiB values.

## Result

- With the change, generating a snapshot of size `S` takes `1.0 x S` of extra memory (the snapshot itself) instead of `3.0 x S`, at every size tested, with either value size.
- For the largest snapshots, 2056 MiB (over 2 GiB), the node's peak fell from 9155 MiB to 5021 MiB with 1 KiB values, 45% lower, and from 9018 MiB to 4616 MiB with 1 MiB values, 49% lower.
  From 1 GiB upwards the peak is about `2.5 x S` instead of `4.5 x S` with 1 KiB values, and `2.0 x S` instead of `4.0 x S` with 1 MiB values, excluding memory freed but not yet reclaimed.
- Snapshots were also generated faster: 6-8 s instead of 11-13 s at 2056 MiB, from the trigger until the snapshot file was written.
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

The script `tests/dr_memory_experiment.py` was run once for each build, value size and snapshot size, alternating between the builds at each size, and the whole set was run twice.
Each run:

1. Starts a single-node service running the `basic` sample app, with one worker thread and automatic snapshots disabled (`snapshot_tx_interval` of 1,000,000).
2. Writes private records with 1 KiB or 1 MiB values to the app's `records` table through its `POST /app/records` endpoint, in requests of about 2 MiB (one record per request for 1 MiB values), until it has written the requested amount of data, chosen to give the target snapshot size.
3. Records the node's memory, triggers a snapshot with `POST /node/snapshot:create`, and waits until the snapshot is committed.
4. Stops the service and recovers it from that snapshot, sampling the recovering node's memory until the recovered service has opened and written its own snapshot.

The node's memory is read from `/proc/<pid>/status` every 20 ms:

- Before: resident memory (`VmRSS`) just before the snapshot is triggered, mostly the key-value state (at 2056 MiB, `1.4 x S` with 1 KiB values and `1.0 x S` with 1 MiB values, excluding memory freed but not yet reclaimed).
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
export VALUE_SIZE=1048576 VALUE_TAG=1m
./run_1b_comparison.sh 64m:48 128m:96 256m:192 512m:384 1g:768 2g:1542
REP=_r2 ./run_1b_comparison.sh 64m:48 128m:96 256m:192 512m:384 1g:768 2g:1542
cp ../CCF-worktree-dr-memory-main/results/main_*.json results/
python3 summarise_generation.py 1k && python3 summarise_generation.py 1m
```

Each argument is a label and the amount of data to write (`--state-mb`), chosen to give snapshots of about 64 MiB to 2 GiB; each run's results are written to `results/<label>.json` in the tree of the build it ran, and `summarise_generation.py` prints the tables below from them.
The app stores values base64-encoded, so with 1 MiB values each record takes 1,398,145 bytes of the snapshot, and the amount of data to write is the number of records.

## Results with 1 KiB values

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

## Results with 1 MiB values

Memory of the node while it generates a snapshot, first pass:

| Snapshot (MiB) | Before, main / 1b (MiB) |  Peak, main (MiB) |    Peak, 1b (MiB) | Extra, main (MiB) |   Extra, 1b (MiB) | Peak saved (MiB) | Time, main / 1b (s) |
| -------------: | ----------------------: | ----------------: | ----------------: | ----------------: | ----------------: | ---------------: | ------------------: |
|           64.1 |               131 / 131 |  323.4 (5.05 x S) |  194.8 (3.04 x S) |  192.5 (3.00 x S) |   64.2 (1.00 x S) |      128.6 (40%) |         0.47 / 0.18 |
|          128.1 |               220 / 201 |  605.0 (4.72 x S) |  329.0 (2.57 x S) |  384.9 (3.00 x S) |  128.3 (1.00 x S) |      276.0 (46%) |         0.63 / 0.39 |
|          256.1 |               372 / 396 | 1141.7 (4.46 x S) |  652.1 (2.55 x S) |  769.6 (3.01 x S) |  256.6 (1.00 x S) |      489.6 (43%) |          1.3 / 0.76 |
|          512.1 |               732 / 706 | 2271.2 (4.44 x S) | 1219.1 (2.38 x S) | 1539.1 (3.01 x S) |  513.1 (1.00 x S) |     1052.1 (46%) |         2.49 / 1.15 |
|         1024.1 |             1147 / 1419 | 4225.4 (4.13 x S) | 2445.0 (2.39 x S) | 3078.2 (3.01 x S) | 1026.1 (1.00 x S) |     1780.4 (42%) |         5.87 / 2.95 |
|         2056.2 |             2838 / 2556 | 9018.1 (4.39 x S) | 4615.9 (2.24 x S) | 6180.3 (3.01 x S) | 2060.2 (1.00 x S) |     4402.2 (49%) |        11.72 / 6.06 |

The second pass reproduced the extra memory of every run to within 0.1 MiB.
With 1 MiB values, up to 734 MiB of the memory before the snapshot had been freed while the requests were processed but not yet reclaimed, an amount that varied between runs, so the peaks varied by up to 5.2% between passes.
Excluding that memory, the memory before the snapshot was the same for both builds in every run, within 3%, and the peak at 2056 MiB was 8280 MiB on `main` and 4160-4162 MiB with the change (`4.03 x S` and `2.02 x S`), in both passes.

Peak memory of the node recovering from that snapshot, first pass:

| Snapshot (MiB) | Recovery peak, main (MiB) | Recovery peak, 1b (MiB) | Excluding LazyFree, main / 1b (MiB) |
| -------------: | ------------------------: | ----------------------: | ----------------------------------: |
|           64.1 |                     350.6 |                   350.7 |                       286.2 / 286.6 |
|          128.1 |                     671.7 |                   671.8 |                       543.1 / 541.4 |
|          256.1 |                    1313.0 |                  1313.6 |                     1055.6 / 1056.3 |
|          512.1 |                    2596.5 |                  2596.0 |                     2083.1 / 2083.6 |
|         1024.1 |                    5162.7 |                  5162.4 |                     4137.2 / 4133.1 |
|         2056.2 |                   10120.7 |                  9981.8 |                     8284.7 / 8269.3 |

Excluding memory freed but not yet reclaimed, the recovery peaks of the two builds are within 0.3% of each other at every size, in both passes.

## Scope

- Only private records were tested, with 1 KiB and 1 MiB values, on a single node.
  Public tables take the same serialisation path, without encryption.
  The size of the key-value state relative to the snapshot (`1.4 x S` with 1 KiB values and `1.0 x S` with 1 MiB values) depends on the size of the values, while the extra memory to generate a snapshot was `3.0 x S` on `main` and `1.0 x S` with the change at every size, with either value size.
- The primary generates a snapshot every `snapshot_tx_interval` transactions, so the saving applies to every service with snapshots, not only during disaster recovery.

## Other checks on the change

- Unit tests pass: `kv_test`, including the two new tests (the snapshot written in place is byte-identical to the previous serialisation, and finalising it is one-shot), `encryptor_test`, `snapshot_test`, `snapshotter_test`, `history_test`, `historical_queries_test` and `ledger_secrets_test`.
- End-to-end tests pass: `recovery_test`, `recovery_stale_snapshot_join_test`, `recovery_intermediate_snapshot_join_test` and `recovery_snapshot_endorsements_test`.
- clang-tidy reports nothing for `src/node/recovery_decision_protocol.cpp`, which includes every changed header, and nothing new in the test file; clang-format and the changelog, ASCII and disallowed-comment checks pass.
- Not run: the full CI suite.
