#!/usr/bin/env python3
"""
Parallel runner for the network/communication-protocol comparison.

Same grid-and-worker-pool shape as memo_parallel.py, but the swept axis is
broadcast_protocol x shard_comm_protocol (not signature scheme) — this sweep
answers "which protocol is fastest", not "which architecture is fastest".
Signature scheme is fixed to ed25519 so the grid stays about protocol/network
behavior, not signature cost.

Network condition (local / US WAN / global WAN) is NOT swept here — like
memo_parallel.py, that's controlled by rtt_ms/control_bw_mbps in
memo_config/base.json, edited by hand between runs. Set NETWORK_ENV to label
the output file for whichever condition base.json currently represents:

    NETWORK_ENV=local  python Parallel_processes/network_parallel.py
    NETWORK_ENV=usa    python Parallel_processes/network_parallel.py
    NETWORK_ENV=global python Parallel_processes/network_parallel.py

Merging always goes through the compiled C merger (./merge_results) — there
is no Python fallback here, unlike memo_parallel.py, since correctness of
this sweep depends on the merge being fast across a much larger grid.

Per-run CSVs -> Results/network_runs/ (one row each, no contention).
Final CSV    -> Results/network_results_<NETWORK_ENV>.csv

Hop-count CDFs: only a small reference subset of the grid (REFERENCE_HOPCDF,
one config per broadcast_protocol) gets its full hop-count CDF kept, at
Results/network_runs/hopcdf/<bprot>_<scomm>.csv — the CDF is a many-row
distribution and can't be merged the same way as the one-row-per-run results
(the C merger only ever keeps one data row per input file). Every other run
in the grid points its hop-cdf output at a shared throwaway file so
thousands of runs don't leave thousands of unused CDF files behind.
"""

import subprocess
import sys
import os
import threading
import itertools
import multiprocessing
import time
from pathlib import Path
from typing import Dict, Any, List

# ------------------------------------------------------------------ #
# Paths — all anchored to the project root (one level up from this script)
# ------------------------------------------------------------------ #
ROOT         = Path(__file__).resolve().parent.parent
SIM_SCRIPT   = str(ROOT / "simulation.py")
BASE_CONFIG  = str(ROOT / "memo_config/base.json")
LOG_DIR      = str(ROOT / "network_logs")
RESULTS_DIR  = str(ROOT / "Results")
RUNS_DIR     = os.path.join(RESULTS_DIR, "network_runs")
HOPCDF_DIR   = os.path.join(RUNS_DIR, "hopcdf")
THROWAWAY_HOPCDF = "_throwaway_hopcdf.csv"
MERGE_BIN    = str(ROOT / "merge_results")
AGGREGATE_BIN = str(ROOT / "aggregate_results")

NETWORK_ENV  = os.environ.get("NETWORK_ENV", "custom")

# ------------------------------------------------------------------ #
# GRID — edit these lists to define the parameter space.
# Full protocol sweep multiplies the grid size by
# len(BROADCAST_PROTOCOLS) * len(SHARD_COMM_PROTOCOLS) versus memo_parallel's
# single fixed combo — trim SHARD_COUNTS/BLOCK_SIZES/BLOCK_TIMES if this is
# too large for one run.
# ------------------------------------------------------------------ #
SHARD_COUNTS = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]

SIG_SCHEMES = ["ed25519"]

# Full multi-protocol comparison — kept here (unused) for the future
# protocol-comparison paper. This paper's main sweep fixes both axes to
# the fastest protocol found (see BROADCAST_PROTOCOLS / SHARD_COMM_PROTOCOLS
# below) and instead sweeps --seed for statistical repeats.
BROADCAST_PROTOCOLS_FULL   = ["gossip", "plumtree", "gossipsub"]
SHARD_COMM_PROTOCOLS_FULL  = ["kademlia", "direct"]

BROADCAST_PROTOCOLS = ["gossip"]

SHARD_COMM_PROTOCOLS = ["direct"]

# Number of repeat runs (distinct --seed values) per grid config, for
# computing mean/std/95% CI in the aggregation step. Tune by running
# network_convergence_check.py and picking the N where CI width stops
# shrinking meaningfully; hard-code the result here.
REPEATS = 10

BLOCK_SIZES = [
    1024, 2048, 4096, 8192, 16384, 32768,
    65536, 131072, 262144, 524288,
]

BLOCK_TIMES = [
    1200, 600, 300, 150, 75, 37.5, 18.75, 9.375,
    4.6875, 2.34375, 1.171875, 0.5859375, 0.29296875, 0.146484375,
]

# One (shards, blocksize, blocktime) config per broadcast_protocol whose full
# hop-count CDF is kept (against the first SHARD_COMM_PROTOCOLS entry only,
# to keep the reference set small). Pick a representative mid-size config.
REFERENCE_HOPCDF_SHARDS     = 16
REFERENCE_HOPCDF_BLOCKSIZE  = 65536
REFERENCE_HOPCDF_BLOCKTIME  = 18.75

# ------------------------------------------------------------------ #
# Worker pool limit
# ------------------------------------------------------------------ #
MAX_WORKERS = int(os.environ.get(
    "GRID_WORKERS",
    max(1, multiprocessing.cpu_count() - 2)
))

TOTAL_NODES     = 1024
TOTAL_NEIGHBORS = 512
TOTAL_MINERS    = 1024


# ------------------------------------------------------------------ #
# Build combination list
# ------------------------------------------------------------------ #
def build_grid() -> List[Dict[str, Any]]:
    combos = []
    for shards, blocksize, blocktime, sig, bprot, scomm, seed in itertools.product(
            SHARD_COUNTS, BLOCK_SIZES, BLOCK_TIMES, SIG_SCHEMES,
            BROADCAST_PROTOCOLS, SHARD_COMM_PROTOCOLS, range(REPEATS)):

        nodes        = TOTAL_NODES
        miners       = TOTAL_MINERS
        neighbors    = TOTAL_NEIGHBORS
        transactions = blocksize * 1000
        wallets      = transactions

        name = (f"net_s{shards}_bs{blocksize}_bt{blocktime:.5f}_"
                f"{bprot}_{scomm}_{sig}_seed{seed}").replace(".", "p")

        # Only seed 0 keeps the reference config's real hop-count CDF —
        # every other repeat of that same config would otherwise race to
        # write/clobber the same file, so it's pointed at the throwaway.
        is_reference = (
            shards == REFERENCE_HOPCDF_SHARDS
            and blocksize == REFERENCE_HOPCDF_BLOCKSIZE
            and blocktime == REFERENCE_HOPCDF_BLOCKTIME
            and scomm == SHARD_COMM_PROTOCOLS[0]
            and seed == 0
        )

        combos.append({
            "name":                 name,
            "shards":               shards,
            "total_blocksize":      blocksize,
            "blocktime":            blocktime,
            "nodes":                nodes,
            "miners":               miners,
            "neighbors":            neighbors,
            "transactions":         transactions,
            "wallets":              wallets,
            "sig_scheme":           sig,
            "broadcast_protocol":   bprot,
            "shard_comm_protocol":  scomm,
            "seed":                 seed,
            "is_reference":         is_reference,
        })
    return combos


# ------------------------------------------------------------------ #
# Process launch
# ------------------------------------------------------------------ #
def launch_sim(combo: Dict[str, Any]) -> subprocess.Popen:
    name = combo["name"]

    cmd = [sys.executable, "-u", SIM_SCRIPT]

    if os.path.exists(BASE_CONFIG):
        cmd += ["--config", BASE_CONFIG]

    cmd += ["--results_csv", f"{name}.csv", "--results_dir", RUNS_DIR]

    if combo["is_reference"]:
        hop_name = f"hopcdf/{combo['broadcast_protocol']}_{combo['shard_comm_protocol']}.csv"
    else:
        hop_name = THROWAWAY_HOPCDF
    cmd += ["--hop_cdf_csv", hop_name]

    cmd += ["--shards",          str(combo["shards"])]
    cmd += ["--total-blocksize", str(combo["total_blocksize"])]
    cmd += ["--blocktime",       str(combo["blocktime"])]
    cmd += ["--nodes",           str(combo["nodes"])]
    cmd += ["--miners",          str(combo["miners"])]
    cmd += ["--neighbors",       str(combo["neighbors"])]
    cmd += ["--transactions",    str(combo["transactions"])]
    cmd += ["--wallets",         str(combo["wallets"])]
    cmd += ["--blocks",          "1000"]
    cmd += ["--sig_scheme",      combo["sig_scheme"]]
    cmd += ["--broadcast_protocol",  combo["broadcast_protocol"]]
    cmd += ["--shard_comm_protocol", combo["shard_comm_protocol"]]
    cmd += ["--seed",            str(combo["seed"])]

    cmd += ["--quiet_blocks"]

    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def stream_to_log(proc: subprocess.Popen, log_path: str):
    with open(log_path, "w") as f:
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            f.write(line)
            f.flush()
    try:
        proc.stdout.close()
    except Exception:
        pass


# ------------------------------------------------------------------ #
# Worker pool
# ------------------------------------------------------------------ #
class WorkerPool:
    def __init__(self, max_workers: int):
        self._sem    = threading.Semaphore(max_workers)
        self._lock   = threading.Lock()
        self._done   = 0
        self._failed: List[str] = []
        self._total  = 0

    def run_all(self, combos: List[Dict[str, Any]]) -> List[str]:
        self._total  = len(combos)
        self._done   = 0
        self._failed = []

        threads = [
            threading.Thread(target=self._run_one, args=(c,), daemon=True)
            for c in combos
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        return self._failed

    def _run_one(self, combo: Dict[str, Any]):
        name = combo["name"]
        self._sem.acquire()
        try:
            log_path = os.path.join(LOG_DIR, f"{name}.log")
            try:
                proc = launch_sim(combo)
            except Exception as e:
                with self._lock:
                    self._failed.append(name)
                    self._done += 1
                    self._log(name, f"LAUNCH ERROR: {e}")
                return

            log_t = threading.Thread(
                target=stream_to_log, args=(proc, log_path), daemon=True)
            log_t.start()

            ret = proc.wait()
            log_t.join()

            with self._lock:
                self._done += 1
                status = "OK" if ret == 0 else f"FAILED(exit={ret})"
                if ret != 0:
                    self._failed.append(name)
                self._log(name, status)
        finally:
            self._sem.release()

    def _log(self, name: str, status: str):
        pct = 100.0 * self._done / self._total if self._total else 0
        print(f"[{self._done:>4}/{self._total}  {pct:5.1f}%]  {name}  {status}",
              flush=True)


# ------------------------------------------------------------------ #
# Merge — always via the compiled C merger, no Python fallback
# ------------------------------------------------------------------ #
def merge_run_csvs(runs_dir: str, network_env: str):
    final_csv = os.path.join(RESULTS_DIR, f"network_results_{network_env}.csv")
    csvs = sorted(Path(runs_dir).glob("net_*.csv"))
    if not csvs:
        print("[merge] No per-run CSVs found - skipping.")
        return

    if not os.path.exists(MERGE_BIN):
        raise SystemExit(
            f"[merge] '{MERGE_BIN}' not found. Compile it first:\n"
            f"  gcc -O2 -fopenmp merge_results.c -o merge_results"
        )

    cmd = [MERGE_BIN, final_csv] + [str(p) for p in csvs]
    print(f"[merge] Merging {len(csvs)} files -> {final_csv}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise SystemExit(f"[merge] merge_results exited with code {result.returncode}")

    print(f"[merge] Done -> {final_csv}")


# ------------------------------------------------------------------ #
# Aggregate — collapses the merged per-(config,seed) CSV into one row per
# config (mean/std/95% CI across REPEATS seeds), via the compiled C tool.
# ------------------------------------------------------------------ #
def aggregate_merged_csv(results_dir: str, network_env: str):
    merged_csv = os.path.join(results_dir, f"network_results_{network_env}.csv")
    agg_csv    = os.path.join(results_dir, f"network_results_{network_env}_agg.csv")

    if not os.path.exists(merged_csv):
        print("[aggregate] No merged CSV found - skipping.")
        return

    if not os.path.exists(AGGREGATE_BIN):
        raise SystemExit(
            f"[aggregate] '{AGGREGATE_BIN}' not found. Compile it first:\n"
            f"  gcc -O2 aggregate_results.c -o aggregate_results -lm"
        )

    cmd = [AGGREGATE_BIN, merged_csv, agg_csv]
    print(f"[aggregate] Aggregating {merged_csv} -> {agg_csv}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise SystemExit(f"[aggregate] aggregate_results exited with code {result.returncode}")

    print(f"[aggregate] Done -> {agg_csv}")


# ------------------------------------------------------------------ #
# Skip-completed filter (same dedup key shape as memo_parallel.py, plus
# neighbors/shard-comm are already covered by broadcast/shard_comm fields)
# ------------------------------------------------------------------ #
def _row_key(row: dict) -> tuple:
    # Includes shard count / block size / block time / seed on top of the
    # original fields — those four alone risked false-duplicate collisions
    # (e.g. two different grid points that happen to land on the same tps).
    return (
        row.get("shards", "").strip(),
        row.get("block size", "").strip(),
        row.get("blocktime in configuration file", "").strip(),
        row.get("seed", "").strip(),
        row.get("tps", "").strip(),
        row.get("average block time", "").strip(),
        row.get("broadcast_protocol", "").strip(),
        row.get("shard_comm_protocol", "").strip(),
    )


def load_merged_tps_keys(results_dir: str, network_env: str) -> set:
    import csv
    merged = set()
    final_csv = os.path.join(results_dir, f"network_results_{network_env}.csv")
    if not os.path.exists(final_csv):
        return merged
    try:
        with open(final_csv, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    merged.add(_row_key(row))
                except KeyError:
                    continue
        print(f"[skip-check] {len(merged)} rows loaded from {os.path.basename(final_csv)}")
    except Exception as e:
        print(f"[skip-check] Could not read {final_csv}: {e}")
    return merged


def needs_run(combo: dict, runs_dir: str, merged_keys: set) -> bool:
    import csv
    per_run_csv = os.path.join(runs_dir, f"{combo['name']}.csv")
    if not os.path.exists(per_run_csv):
        return True
    try:
        with open(per_run_csv, newline="") as f:
            for row in csv.DictReader(f):
                key = _row_key(row)
                if key in merged_keys:
                    return True
                return False
    except Exception:
        pass
    return True


# ------------------------------------------------------------------ #
# Main
# ------------------------------------------------------------------ #
def main():
    os.makedirs(LOG_DIR,     exist_ok=True)
    os.makedirs(RUNS_DIR,    exist_ok=True)
    os.makedirs(HOPCDF_DIR,  exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)

    if not os.path.exists(MERGE_BIN):
        raise SystemExit(
            f"'{MERGE_BIN}' not found. Compile it first:\n"
            f"  gcc -O2 -fopenmp merge_results.c -o merge_results"
        )

    if not os.path.exists(AGGREGATE_BIN):
        raise SystemExit(
            f"'{AGGREGATE_BIN}' not found. Compile it first:\n"
            f"  gcc -O2 aggregate_results.c -o aggregate_results -lm"
        )

    combos = build_grid()
    total  = len(combos)

    merged_keys = load_merged_tps_keys(RESULTS_DIR, NETWORK_ENV)
    if merged_keys:
        combos = [c for c in combos if needs_run(c, RUNS_DIR, merged_keys)]
        skipped = total - len(combos)
        print(f"[skip-check] {skipped} fresh/already-merged runs skipped, {len(combos)} remaining\n")

    print("Network protocol comparison — parallel grid runner")
    print(f"  Network env : {NETWORK_ENV}  (set NETWORK_ENV=local|usa|global to label output)")
    print(f"  Shards      : {SHARD_COUNTS}")
    print(f"  Nodes/Miners: {TOTAL_NODES} / {TOTAL_MINERS} (fixed across all shard counts)")
    print(f"  Neighbors   : {TOTAL_NEIGHBORS}")
    print(f"  Block sizes : {BLOCK_SIZES}")
    print(f"  Block times : {BLOCK_TIMES}")
    print(f"  Repeats     : {REPEATS} seeds/config (0..{REPEATS - 1})")
    print(f"  Sig scheme  : {SIG_SCHEMES} (fixed)")
    print(f"  Broadcast   : {BROADCAST_PROTOCOLS}")
    print(f"  Shard comm  : {SHARD_COMM_PROTOCOLS}")
    print(f"  Total runs  : {total}")
    print(f"  Workers     : {MAX_WORKERS}  (set GRID_WORKERS=N to override)")
    print(f"  Base config : {BASE_CONFIG}")
    print(f"  Logs        : {LOG_DIR}/")
    print(f"  Per-run CSVs: {RUNS_DIR}/")
    print(f"  Hop-CDF ref : {HOPCDF_DIR}/ "
          f"(shards={REFERENCE_HOPCDF_SHARDS}, blocksize={REFERENCE_HOPCDF_BLOCKSIZE}, "
          f"blocktime={REFERENCE_HOPCDF_BLOCKTIME}, scomm={SHARD_COMM_PROTOCOLS[0]})")
    print(f"  Final CSV   : network_results_{NETWORK_ENV}.csv")
    print()

    if not os.path.exists(BASE_CONFIG):
        print(f"[warn] Base config '{BASE_CONFIG}' not found - "
              "all params must be covered by CLI defaults.\n")

    t0      = time.time()
    pool    = WorkerPool(MAX_WORKERS)
    failed  = pool.run_all(combos)
    elapsed = time.time() - t0

    print()
    print(f"All runs finished in {elapsed:.1f}s")
    print(f"  Succeeded : {total - len(failed)}")
    print(f"  Failed    : {len(failed)}")
    if failed:
        print(f"  Failed IDs: {failed[:20]}{'...' if len(failed) > 20 else ''}")

    print()
    merge_run_csvs(RUNS_DIR, NETWORK_ENV)
    aggregate_merged_csv(RESULTS_DIR, NETWORK_ENV)
    print(f"\nDone. Results   -> Results/network_results_{NETWORK_ENV}.csv")
    print(f"Aggregated       -> Results/network_results_{NETWORK_ENV}_agg.csv")
    print(f"Hop-count CDF references -> {HOPCDF_DIR}/<broadcast_protocol>_{SHARD_COMM_PROTOCOLS[0]}.csv")


if __name__ == "__main__":
    main()
