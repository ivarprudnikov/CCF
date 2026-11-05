# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the Apache 2.0 License.
"""
Experiment-only: measure node memory (RSS) while a primary generates a large
snapshot, and through each phase of a disaster recovery from it, using the
basic sample app.

Populates the KV with --state-mb of data (private and/or public), triggers a
snapshot while sampling the primary's /proc/<pid>/status, stops the service,
then recovers it while sampling the recovering node. Phase boundaries are
taken from node log markers. Results are written as JSON to --output.
"""

import datetime
import itertools
import json
import os
import random
import string
import threading
import time

import ccf.ledger
import infra.clients
import infra.e2e_args
import infra.network
from loguru import logger as LOG

MEM_FIELDS = ("VmRSS", "VmHWM", "RssAnon", "RssFile", "RssShmem")

PHASE_MARKERS = [
    ("snapshot_loaded", "Found latest local snapshot file"),
    ("snapshot_deserialise_start", "Deserialising snapshot (size"),
    ("snapshot_deserialised", "Snapshot successfully deserialised"),
    ("public_ledger_start", "Starting to read public ledger"),
    ("public_ledger_end", "End of public ledger recovery"),
    ("private_recovery_start", "Beginning private recovery"),
    ("private_store_ready", "Recovery store successfully setup"),
    ("private_recovery_end", "Try end private recovery"),
    ("snapshot_generated", "Generated snapshot ["),
    ("snapshot_written", "New snapshot file written"),
]


def read_mem_kb(pid, with_smaps=False):
    result = {}
    with open(f"/proc/{pid}/status", encoding="utf-8") as f:
        for line in f:
            key, _, value = line.partition(":")
            if key in MEM_FIELDS:
                result[key] = int(value.split()[0])
    if with_smaps:
        # MADV_FREE'd pages still count in RSS until reclaimed
        with open(f"/proc/{pid}/smaps_rollup", encoding="utf-8") as f:
            for line in f:
                key, _, value = line.partition(":")
                if key == "LazyFree":
                    result[key] = int(value.split()[0])
    return result


class MemorySampler(threading.Thread):
    """Waits for a new process running `binary_name`, then samples its memory."""

    def __init__(self, binary_name, exclude_pids, interval_s=0.005):
        super().__init__(daemon=True)
        self.binary_name = binary_name
        self.exclude_pids = set(exclude_pids)
        self.interval_s = interval_s
        self.samples = []
        self.pid = None
        self.stop_event = threading.Event()

    def _find_pid(self):
        for entry in os.listdir("/proc"):
            if not entry.isdigit() or int(entry) in self.exclude_pids:
                continue
            try:
                exe = os.readlink(f"/proc/{entry}/exe")
            except OSError:
                continue
            if os.path.basename(exe) == self.binary_name:
                return int(entry)
        return None

    def run(self):
        while not self.stop_event.is_set() and self.pid is None:
            self.pid = self._find_pid()
            if self.pid is None:
                time.sleep(0.001)
        lazy_free = 0
        n = 0
        while not self.stop_event.is_set():
            try:
                mem = read_mem_kb(self.pid, with_smaps=(n % 4 == 0))
            except (FileNotFoundError, ProcessLookupError):
                break
            lazy_free = mem.setdefault("LazyFree", lazy_free)
            n += 1
            self.samples.append((time.time(), mem))
            time.sleep(self.interval_s)

    def stop(self):
        self.stop_event.set()
        self.join()


def pids_running(binary_name):
    pids = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            if os.path.basename(os.readlink(f"/proc/{entry}/exe")) == binary_name:
                pids.add(int(entry))
        except OSError:
            continue
    return pids


def random_value(size):
    return "".join(random.choices(string.ascii_letters + string.digits, k=size))


def populate(network, args):
    """Writes ~state_mb of records, returning the (endpoint, first, end) key
    index ranges written, in order."""
    primary, _ = network.find_primary()
    target_bytes = args.state_mb * 1024 * 1024
    batch_bytes = args.batch_kb * 1024
    per_batch = max(1, batch_bytes // (args.value_size + 16))
    endpoints = []
    if args.domain in ("private", "mixed"):
        endpoints.append("/app/records")
    if args.domain in ("public", "mixed"):
        endpoints.append("/app/public_records")
    written = 0
    key_idx = 0
    ranges = []
    # Reuse a pool of values so generation does not dominate the run time,
    # while keeping values distinct enough to defeat any compression.
    pool = [random_value(args.value_size) for _ in range(64)]
    with primary.client("user0") as c:
        while written < target_bytes:
            for endpoint in endpoints:
                body = {}
                first = key_idx
                for _ in range(per_batch):
                    body[f"k{key_idx:012d}"] = pool[key_idx % len(pool)]
                    key_idx += 1
                r = c.post(endpoint, body)
                assert r.status_code == 204, r
                ranges.append((endpoint, first, key_idx))
                written += per_batch * (args.value_size + 13)
    LOG.info(f"Wrote {key_idx} records, ~{written / 1024 / 1024:.1f} MiB")
    return ranges


def rewrite(network, args, ranges):
    """Overwrites the first rewrite_fraction of the records written by
    populate() with new values of the same size, so that the ledger after the
    snapshot updates existing state."""
    primary, _ = network.find_primary()
    total = sum(end - first for _, first, end in ranges)
    remaining = int(total * args.rewrite_fraction)
    pool = [random_value(args.value_size) for _ in range(64)]
    rewritten = 0
    r = None
    with primary.client("user0") as c:
        for endpoint, first, end in ranges:
            if remaining <= 0:
                break
            count = min(end - first, remaining)
            body = {
                f"k{idx:012d}": pool[(idx + 1) % len(pool)]
                for idx in range(first, first + count)
            }
            r = c.post(endpoint, body)
            assert r.status_code == 204, r
            remaining -= count
            rewritten += count
        if r is not None:
            c.wait_for_commit(r, timeout=120)
    LOG.info(f"Rewrote {rewritten} of {total} records after the snapshot")
    return rewritten


def wait_for_post_open_snapshot(node, timeout=300):
    with node.client() as c:
        r = c.get("/node/network").body.json()
    create_seqno = int(r["current_service_create_txid"].split(".")[1])
    end_time = time.time() + timeout
    with node.client("user0") as c:
        while time.time() < end_time:
            for snapshot in node.get_snapshots():
                idx = ccf.ledger.snapshot_index_from_filename(snapshot)[0]
                if idx > create_seqno:
                    LOG.info(f"Found post-open snapshot {snapshot}")
                    return snapshot
            # Small writes advance commit, so the snapshot can be persisted
            c.post("/app/records", {"tick": "x"})
            time.sleep(0.2)
    raise TimeoutError("No post-open snapshot")


def parse_log_markers(out_path):
    markers = []
    with open(out_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            for name, needle in PHASE_MARKERS:
                if needle in line:
                    ts = line.split(" ", 1)[0]
                    try:
                        t = datetime.datetime.fromisoformat(ts).timestamp()
                    except ValueError:
                        continue
                    markers.append((t, name, line.strip()[:200]))
    return markers


def summarise(samples, markers):
    """RSS at each marker, and peak RSS until the next marker."""
    points = [(samples[0][0], "process_start")] + [(t, name) for t, name, _ in markers]
    points.append((samples[-1][0] + 1, "end"))
    phases = []
    for (t0, n0), (t1, _) in itertools.pairwise(points):
        window = [m for t, m in samples if t0 <= t < t1]
        at = next((m for t, m in samples if t >= t0), samples[-1][1])
        phases.append(
            {
                "after": n0,
                "t": round(t0 - samples[0][0], 3),
                "duration_s": round(t1 - t0, 3),
                "rss_at_mib": round(at["VmRSS"] / 1024, 1),
                "peak_rss_mib": round(
                    max((m["VmRSS"] for m in window), default=at["VmRSS"]) / 1024,
                    1,
                ),
                "peak_rss_minus_lazyfree_mib": round(
                    max(
                        (m["VmRSS"] - m.get("LazyFree", 0) for m in window),
                        default=at["VmRSS"],
                    )
                    / 1024,
                    1,
                ),
            }
        )
    return phases


def generation_metrics(samples, before, after, t_trigger, t_end, snapshot_mib):
    """Memory of the primary while it generates the snapshot, from just before
    the snapshot is triggered until it is committed. VmHWM is exact: if it rose
    after the trigger, the peak since the trigger is the final VmHWM; otherwise
    the peak is taken from the samples. LazyFree (freed but not yet reclaimed
    by the kernel) is sampled less often, so the peak excluding it is a lower
    bound."""
    window = [m for t, m in samples if t_trigger <= t <= t_end] + [after]
    sampled_peak = max(m["VmRSS"] for m in window)
    hwm_rose = after["VmHWM"] > before["VmHWM"]
    peak = after["VmHWM"] if hwm_rose else sampled_peak
    before_live = before["VmRSS"] - before.get("LazyFree", 0)
    peak_live = max(m["VmRSS"] - m.get("LazyFree", 0) for m in window)

    def mib(kb):
        return round(kb / 1024, 1)

    return {
        "before_rss_mib": mib(before["VmRSS"]),
        "before_lazyfree_mib": mib(before.get("LazyFree", 0)),
        "before_hwm_mib": mib(before["VmHWM"]),
        "peak_rss_mib": mib(peak),
        "peak_from_hwm": hwm_rose,
        "peak_x_snapshot": round(peak / 1024 / snapshot_mib, 2),
        "extra_rss_mib": mib(peak - before["VmRSS"]),
        "extra_x_snapshot": round((peak - before["VmRSS"]) / 1024 / snapshot_mib, 2),
        "peak_rss_minus_lazyfree_mib": mib(peak_live),
        "extra_minus_lazyfree_mib": mib(peak_live - before_live),
        "extra_minus_lazyfree_x_snapshot": round(
            (peak_live - before_live) / 1024 / snapshot_mib, 2
        ),
        "after_rss_mib": mib(after["VmRSS"]),
        "samples": len(window) - 1,
    }


def raise_default_request_timeout(timeout_s):
    """Apply timeout_s to every client request that does not set its own."""
    original_call = infra.clients.CCFClient.call
    # call(path, body, http_verb, headers, timeout, ...)
    timeout_position = 4

    def call(self, *args, **kwargs):
        if len(args) <= timeout_position:
            kwargs.setdefault("timeout", timeout_s)
        return original_call(self, *args, **kwargs)

    infra.clients.CCFClient.call = call


def run(args):
    binary_name = os.path.basename(args.package)
    with infra.network.network(
        args.nodes, args.binary_dir, args.debug_nodes, pdb=args.pdb
    ) as network:
        network.start_and_open(args)
        primary, _ = network.find_primary()
        old_pids = pids_running(binary_name)
        if len(old_pids) != 1:
            raise RuntimeError(
                f"Expected one {binary_name} process (the primary), found "
                f"{sorted(old_pids)}: stop leftover nodes from earlier runs"
            )
        primary_sampler = MemorySampler(binary_name, set(), interval_s=0.02)
        primary_sampler.pid = next(iter(old_pids))
        primary_sampler.start()

        ranges = populate(network, args)
        before_snapshot = read_mem_kb(primary_sampler.pid, with_smaps=True)
        t_trigger = time.time()
        snapshot_trigger = primary.trigger_snapshot()
        primary.wait_for_snapshot(snapshot_trigger.seqno, timeout=300)
        t_committed = time.time()
        after_snapshot = read_mem_kb(primary_sampler.pid, with_smaps=True)
        snapshots_dir = network.get_committed_snapshots(primary, force_txs=False)
        rewritten = rewrite(network, args, ranges) if args.rewrite_fraction else 0
        snapshot_name = ccf.ledger.latest_snapshot(snapshots_dir)
        snapshot_size = os.path.getsize(os.path.join(snapshots_dir, snapshot_name))
        LOG.info(f"Snapshot {snapshot_name}: {snapshot_size / 1024 / 1024:.2f} MiB")
        primary_sampler.stop()
        primary_out, _ = primary.get_logs()
        written = [
            t
            for t, name, _ in parse_log_markers(primary_out)
            if name == "snapshot_written" and t >= t_trigger
        ]
        generation = generation_metrics(
            primary_sampler.samples,
            before_snapshot,
            after_snapshot,
            t_trigger,
            t_committed,
            snapshot_size / 1024 / 1024,
        )
        generation["trigger_to_file_written_s"] = (
            round(written[0] - t_trigger, 2) if written else None
        )
        generation["trigger_to_committed_s"] = round(t_committed - t_trigger, 2)
        LOG.info(f"Primary while generating the snapshot: {generation}")

        network.save_service_identity(args)
        network.stop_all_nodes()
        current_ledger_dir, committed_ledger_dirs = primary.get_ledger()

        sampler = MemorySampler(binary_name, pids_running(binary_name))
        sampler.start()
        t_start = time.time()
        recovered = infra.network.Network(
            args.nodes, args.binary_dir, args.debug_nodes, existing_network=network
        )
        try:
            recovered.start_in_recovery(
                args,
                ledger_dir=current_ledger_dir,
                committed_ledger_dirs=committed_ledger_dirs,
                snapshots_dir=snapshots_dir,
            )
            t_public = time.time()
            recovered.recover(args)
            t_open = time.time()
            new_primary, _ = recovered.find_primary()
            # Wait for the snapshot requested when the recovered service opened
            wait_for_post_open_snapshot(new_primary)
            time.sleep(args.settle_s)
            sampler.stop()

            out_path, _ = new_primary.get_logs()
            markers = parse_log_markers(out_path)
        except BaseException:
            # Otherwise a failed run leaves the recovered node running, and
            # later runs would sample it instead of their own nodes
            recovered.stop_all_nodes(
                skip_verification=True, check_file_invariants=False
            )
            raise
        recovered.stop_all_nodes()

    samples = sampler.samples
    peak = max(m["VmRSS"] for _, m in samples) / 1024
    baseline = samples[0][1]["VmRSS"] / 1024
    primary_peak = (
        max(m["VmRSS"] for _, m in primary_sampler.samples) / 1024
        if primary_sampler.samples
        else None
    )
    s_mib = snapshot_size / 1024 / 1024
    result = {
        "config": {
            "state_mb": args.state_mb,
            "value_size": args.value_size,
            "domain": args.domain,
            "rewrite_fraction": args.rewrite_fraction,
            "rewritten_records": rewritten,
        },
        "snapshot_mib": round(s_mib, 2),
        "snapshot_bytes": snapshot_size,
        "primary_snapshot_generation": generation,
        "recovery_first_sample_rss_mib": round(baseline, 1),
        "recovery_peak_rss_mib": round(peak, 1),
        "recovery_final_rss_mib": round(samples[-1][1]["VmRSS"] / 1024, 1),
        "recovery_peak_rss_minus_lazyfree_mib": round(
            max(m["VmRSS"] - m.get("LazyFree", 0) for _, m in samples) / 1024, 1
        ),
        "recovery_final_lazyfree_mib": round(
            samples[-1][1].get("LazyFree", 0) / 1024, 1
        ),
        "old_primary_peak_rss_mib": (
            round(primary_peak, 1) if primary_peak is not None else None
        ),
        # Kernel-maintained high-water marks, exact regardless of sampling
        "recovery_hwm_mib": round(max(m.get("VmHWM", 0) for _, m in samples) / 1024, 1),
        "old_primary_hwm_mib": round(
            max((m.get("VmHWM", 0) for _, m in primary_sampler.samples), default=0)
            / 1024,
            1,
        ),
        "old_primary_samples": len(primary_sampler.samples),
        "old_primary_timeline": [
            [round(t - primary_sampler.samples[0][0], 3), m["VmRSS"], m["VmHWM"]]
            for t, m in primary_sampler.samples[
                :: max(1, len(primary_sampler.samples) // 500)
            ]
        ],
        "wall_s": {
            "start_in_recovery": round(t_public - t_start, 1),
            "recover": round(t_open - t_public, 1),
        },
        "markers": [
            {"t": round(t - samples[0][0], 3), "name": name, "line": line}
            for t, name, line in markers
        ],
        "phases": summarise(samples, markers),
        "timeline": [
            [
                round(t - samples[0][0], 3),
                m["VmRSS"],
                m.get("RssAnon", 0),
                m.get("LazyFree", 0),
            ]
            for t, m in samples[:: max(1, len(samples) // 2000)]
        ],
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    LOG.success(
        f"RESULT snapshot {s_mib:.1f} MiB | primary generating it: "
        f"before {generation['before_rss_mib']} MiB, "
        f"peak {generation['peak_rss_mib']} MiB "
        f"({generation['peak_x_snapshot']}x snapshot), "
        f"extra {generation['extra_rss_mib']} MiB "
        f"({generation['extra_x_snapshot']}x), "
        f"extra excluding LazyFree {generation['extra_minus_lazyfree_mib']} MiB "
        f"({generation['extra_minus_lazyfree_x_snapshot']}x), "
        f"{generation['trigger_to_file_written_s']} s | "
        f"recovery peak RSS {peak:.1f} MiB ({peak / s_mib:.2f}x snapshot), "
        f"excluding LazyFree {result['recovery_peak_rss_minus_lazyfree_mib']} MiB | "
        f"written to {args.output}"
    )
    for p in result["phases"]:
        LOG.info(p)


if __name__ == "__main__":

    def add(parser):
        parser.add_argument("--state-mb", type=int, default=64)
        parser.add_argument("--value-size", type=int, default=1024)
        parser.add_argument("--batch-kb", type=int, default=2048)
        parser.add_argument(
            "--domain", choices=["private", "public", "mixed"], default="private"
        )
        parser.add_argument(
            "--rewrite-fraction",
            type=float,
            default=0.0,
            help="Fraction of records to overwrite after the snapshot",
        )
        parser.add_argument("--settle-s", type=float, default=2.0)
        parser.add_argument(
            "--request-timeout",
            type=int,
            default=None,
            help="Default client request timeout (s); recovering a large "
            "snapshot keeps the node busy for longer than the usual default",
        )
        parser.add_argument("--output", default="dr_memory_result.json")

    args = infra.e2e_args.cli_args(add)
    args.package = args.package or "samples/apps/basic/basic"
    args.nodes = infra.e2e_args.min_nodes(args, f=0)
    if args.request_timeout is not None:
        raise_default_request_timeout(args.request_timeout)
    run(args)
