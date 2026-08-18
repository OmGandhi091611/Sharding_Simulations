#!/usr/bin/env python3
"""
Parallel runner for the neighbors/fanout sweep.

Same grid-and-worker-pool shape as network_parallel.py, but every axis that
script sweeps (shards, block size, block time, protocols, conflict-check
method) is instead pinned to a single fixed value, and the one thing varied
is --neighbors / --gossip_fanout, moved together as a single combined axis
(not a cross product) since fanout must not exceed the neighbor count.

Fixed config: 512 shards, block size 524288, block time 0.146484375 (the
smallest/fastest entry in network_parallel.py's BLOCK_TIMES sweep), 1000
nodes, 1000 miners, broadcast_protocol=gossip (the only protocol with a
--gossip_fanout knob), shard_comm_protocol=direct, conflict_check_method=
nonce_hash_set, sig_scheme=ed25519.

    NETWORK_ENV=local  python Parallel_processes/fanout_neighbors_parallel.py
    NETWORK_ENV=usa    python Parallel_processes/fanout_neighbors_parallel.py
    NETWORK_ENV=global python Parallel_processes/fanout_neighbors_parallel.py

NETWORK_ENV=custom (the default) skips the rtt_ms/control_bw_mbps override
and falls back to whatever is currently in memo_config/base.json.

Per-run CSVs -> Results/network_runs/ (one row each, no contention).
Final CSV    -> Results/fanout_neighbors_results_<NETWORK_ENV>.csv
"""

import subprocess
import sys
import os
import threading
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

# rtt_ms sourced from real measurements (see README.md Network Condition
# Variants section for citations); control_bw_mbps is an engineering
# assumption, not independently sourced.
NETWORK_ENV_PARAMS = {
    "local":  {"rtt_ms": 0.3, "control_bw_mbps": 10000},
    "usa":    {"rtt_ms": 60,  "control_bw_mbps": 1000},
    "global": {"rtt_ms": 180, "control_bw_mbps": 50},
}

# ------------------------------------------------------------------ #
# Fixed config
# ------------------------------------------------------------------ #
SHARDS      = 512
BLOCK_SIZE  = 524288
BLOCK_TIME  = 0.146484375   # smallest entry in network_parallel.py's BLOCK_TIMES

TOTAL_NODES  = 1000
TOTAL_MINERS = 1000

SIG_SCHEME          = "ed25519"
BROADCAST_PROTOCOL  = "gossip"   # only protocol with a --gossip_fanout knob
SHARD_COMM_PROTOCOL = "direct"
CONFLICT_CHECK_METHOD = "nonce_hash_set"

# ------------------------------------------------------------------ #
# GRID — swept axis. neighbors and fanout move together (same value),
# not a cross product, since fanout must never exceed neighbor count.
# ------------------------------------------------------------------ #
NEIGHBOR_FANOUT_VALUES = [2, 10, 20, 50, 100, 150, 200, 250, 300, 350, 400, 450, 500]

# Number of repeat runs (distinct --seed values) per grid config, for
# computing mean/std/95% CI in the aggregation step.
REPEATS = 5

# One neighbors/fanout value whose full hop-count CDF is kept (mid-size,
# representative point).
REFERENCE_HOPCDF_VALUE = 100

# ------------------------------------------------------------------ #
# Worker pool limit
# ------------------------------------------------------------------ #
MAX_WORKERS = int(os.environ.get(
    "GRID_WORKERS",
    max(1, multiprocessing.cpu_count() - 2)
))


# ------------------------------------------------------------------ #
# Build combination list
# ------------------------------------------------------------------ #
def build_grid() -> List[Dict[str, Any]]:
    combos = []
    for value in NEIGHBOR_FANOUT_VALUES:
        for seed in range(REPEATS):
            transactions = BLOCK_SIZE * 1000
            wallets = transactions

            name = (f"fanout_v{value}_bt{BLOCK_TIME:.5f}_seed{seed}").replace(".", "p")

            is_reference = (value == REFERENCE_HOPCDF_VALUE and seed == 0)

            combos.append({
                "name":                 name,
                "shards":               SHARDS,
                "total_blocksize":      BLOCK_SIZE,
                "blocktime":            BLOCK_TIME,
                "nodes":                TOTAL_NODES,
                "miners":               TOTAL_MINERS,
                "neighbors":            value,
                "gossip_fanout":        value,
                "transactions":         transactions,
                "wallets":              wallets,
                "sig_scheme":           SIG_SCHEME,
                "broadcast_protocol":   BROADCAST_PROTOCOL,
                "shard_comm_protocol":  SHARD_COMM_PROTOCOL,
                "conflict_check_method": CONFLICT_CHECK_METHOD,
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

    env_params = NETWORK_ENV_PARAMS.get(NETWORK_ENV)
    if env_params:
        cmd += ["--rtt_ms", str(env_params["rtt_ms"])]
        cmd += ["--control_bw_mbps", str(env_params["control_bw_mbps"])]

    cmd += ["--results_csv", f"{name}.csv", "--results_dir", RUNS_DIR]

    if combo["is_reference"]:
        hop_name = f"hopcdf/fanout_{combo['neighbors']}.csv"
    else:
        hop_name = THROWAWAY_HOPCDF
    cmd += ["--hop_cdf_csv", hop_name]

    cmd += ["--shards",          str(combo["shards"])]
    cmd += ["--total-blocksize", str(combo["total_blocksize"])]
    cmd += ["--blocktime",       str(combo["blocktime"])]
    cmd += ["--nodes",           str(combo["nodes"])]
    cmd += ["--miners",          str(combo["miners"])]
    cmd += ["--neighbors",       str(combo["neighbors"])]
    cmd += ["--gossip_fanout",   str(combo["gossip_fanout"])]
    cmd += ["--transactions",    str(combo["transactions"])]
    cmd += ["--wallets",         str(combo["wallets"])]
    cmd += ["--blocks",          "1000"]
    cmd += ["--sig_scheme",      combo["sig_scheme"]]
    cmd += ["--broadcast_protocol",  combo["broadcast_protocol"]]
    cmd += ["--shard_comm_protocol", combo["shard_comm_protocol"]]
    cmd += ["--seed",            str(combo["seed"])]

    cmd += ["--conflict_check_method", combo["conflict_check_method"]]

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
    final_csv = os.path.join(RESULTS_DIR, f"fanout_neighbors_results_{network_env}.csv")
    csvs = sorted(Path(runs_dir).glob("fanout_*.csv"))
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
    merged_csv = os.path.join(results_dir, f"fanout_neighbors_results_{network_env}.csv")
    agg_csv    = os.path.join(results_dir, f"fanout_neighbors_results_{network_env}_agg.csv")

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
# Skip-completed filter (same dedup key shape as network_parallel.py, plus
# neighbors on top since that's the swept axis here)
# ------------------------------------------------------------------ #
def _row_key(row: dict) -> tuple:
    return (
        row.get("shards", "").strip(),
        row.get("block size", "").strip(),
        row.get("blocktime in configuration file", "").strip(),
        row.get("neighbors", "").strip(),
        row.get("seed", "").strip(),
        row.get("tps", "").strip(),
        row.get("average block time", "").strip(),
        row.get("broadcast_protocol", "").strip(),
        row.get("shard_comm_protocol", "").strip(),
        row.get("verify_mode", "").strip(),
        row.get("conflict_check", "").strip(),
    )


def load_merged_tps_keys(results_dir: str, network_env: str) -> set:
    import csv
    merged = set()
    final_csv = os.path.join(results_dir, f"fanout_neighbors_results_{network_env}.csv")
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

    env_params = NETWORK_ENV_PARAMS.get(NETWORK_ENV)
    env_desc = (
        f"rtt_ms={env_params['rtt_ms']}, control_bw_mbps={env_params['control_bw_mbps']} (overridden on CLI)"
        if env_params else "not overridden — using memo_config/base.json as-is"
    )

    print("Neighbors/fanout sweep — parallel grid runner")
    print(f"  Network env : {NETWORK_ENV}  (set NETWORK_ENV=local|usa|global to label output)")
    print(f"  Net params  : {env_desc}")
    print(f"  Shards      : {SHARDS} (fixed)")
    print(f"  Block size  : {BLOCK_SIZE} (fixed)")
    print(f"  Block time  : {BLOCK_TIME} (fixed)")
    print(f"  Nodes/Miners: {TOTAL_NODES} / {TOTAL_MINERS} (fixed)")
    print(f"  Neighbors=Fanout: {NEIGHBOR_FANOUT_VALUES} (swept together)")
    print(f"  Repeats     : {REPEATS} seeds/config (0..{REPEATS - 1})")
    print(f"  Sig scheme  : {SIG_SCHEME} (fixed)")
    print(f"  Conflict chk: {CONFLICT_CHECK_METHOD} (fixed)")
    print(f"  Broadcast   : {BROADCAST_PROTOCOL} (fixed)")
    print(f"  Shard comm  : {SHARD_COMM_PROTOCOL} (fixed)")
    print(f"  Total runs  : {total}")
    print(f"  Workers     : {MAX_WORKERS}  (set GRID_WORKERS=N to override)")
    print(f"  Base config : {BASE_CONFIG}")
    print(f"  Logs        : {LOG_DIR}/")
    print(f"  Per-run CSVs: {RUNS_DIR}/")
    print(f"  Hop-CDF ref : {HOPCDF_DIR}/ (neighbors=fanout={REFERENCE_HOPCDF_VALUE})")
    print(f"  Final CSV   : fanout_neighbors_results_{NETWORK_ENV}.csv")
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
    print(f"\nDone. Results   -> Results/fanout_neighbors_results_{NETWORK_ENV}.csv")
    print(f"Aggregated       -> Results/fanout_neighbors_results_{NETWORK_ENV}_agg.csv")
    print(f"Hop-count CDF references -> {HOPCDF_DIR}/fanout_<value>.csv")


if __name__ == "__main__":
    main()
