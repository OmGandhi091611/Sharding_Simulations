#!/usr/bin/env python3
"""
Parallel simulation runner with grid generation.

Generates all combinations of shards x block_size x block_time and runs
them in parallel with a worker pool. Each sim writes to its own CSV under
Results/runs/ - no file contention. C merger combines everything at the end.

Usage:
    python run_parallel.py
    GRID_WORKERS=32 python run_parallel.py   # override worker count
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
ROOT        = Path(__file__).resolve().parent.parent
SIM_SCRIPT  = str(ROOT / "simulation.py")
BASE_CONFIG = str(ROOT / "memo_config/base.json")
LOG_DIR     = str(ROOT / "memo_logs")
RESULTS_DIR = str(ROOT / "Results")
RUNS_DIR    = os.path.join(RESULTS_DIR, "runs")
MERGE_BIN   = str(ROOT / "merge_results")

# ------------------------------------------------------------------ #
# GRID — edit these three lists to define your parameter space
# ------------------------------------------------------------------ #
SHARD_COUNTS = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]

# Signature schemes to sweep — comment out any you don't want to run
SIG_SCHEMES = [
    "ed25519",
    "dilithium2",
    "falcon512",
    "sphincs_sha2_128s",
]

BLOCK_SIZES = [
    1024,
    2048,
    4096,
    8192,
    16384,
    32768,
    65536,
    131072,
    262144,
    524288,
]

BLOCK_TIMES = [
    1200,
    600,
    300,
    150,
    75,
    37.5,
    18.75,
    9.375,
    4.6875,
    2.34375,
    1.171875,
    0.5859375,
    0.29296875,
    0.146484375,
    0.0732421875,
    0.03662109375,
    0.01831054688,
    0.009155273438,
    0.004577636719,
    0.002288818359,
    0.00114440918
]


# ------------------------------------------------------------------ #
# Worker pool limit
# Leave 2 cores free for OS + merger overhead.
# Override: GRID_WORKERS=32 python run_parallel.py
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
    for shards, blocksize, blocktime, sig in itertools.product(
            SHARD_COUNTS, BLOCK_SIZES, BLOCK_TIMES, SIG_SCHEMES):

        nodes        = {128: 256, 256: 512, 512: 1000}.get(shards, 100)
        miners       = nodes
        neighbors    = min(50, nodes - 1)
        transactions = blocksize * 200
        wallets      = transactions

        name = f"s{shards}_bs{blocksize}_bt{blocktime:.5f}_{sig}".replace(".", "p")
        combos.append({
            "name":            name,
            "shards":          shards,
            "total_blocksize": blocksize,
            "blocktime":       blocktime,
            "nodes":           nodes,
            "miners":          miners,
            "neighbors":       neighbors,
            "transactions":    transactions,
            "wallets":         wallets,
            "sig_scheme":      sig,
        })
    return combos


# ------------------------------------------------------------------ #
# Process launch
# ------------------------------------------------------------------ #

def launch_sim(combo: Dict[str, Any]) -> subprocess.Popen:
    """
    Launch one simulation. Base JSON supplies common params.
    Varying params (shards, blocksize, blocktime) are passed as CLI args
    so they always win over anything in the base JSON.
    """
    name = combo["name"]

    cmd = [sys.executable, "-u", SIM_SCRIPT]

    # Base config for common params (nodes, miners, hashrate, rtt_ms, etc.)
    if os.path.exists(BASE_CONFIG):
        cmd += ["--config", BASE_CONFIG]

    # Output routing - unique per run, no contention
    cmd += ["--results_csv", f"{name}.csv", "--results_dir", RUNS_DIR]

    # Varying params as CLI args (win over base JSON)
    cmd += ["--shards",          str(combo["shards"])]
    cmd += ["--total-blocksize", str(combo["total_blocksize"])]
    cmd += ["--blocktime",       str(combo["blocktime"])]
    cmd += ["--nodes",           str(combo["nodes"])]
    cmd += ["--miners",          str(combo["miners"])]
    cmd += ["--neighbors",       str(combo["neighbors"])]
    cmd += ["--transactions",    str(combo["transactions"])]
    cmd += ["--wallets",         str(combo["wallets"])]
    cmd += ["--blocks",          "200"]
    cmd += ["--sig_scheme",      combo["sig_scheme"]]

    # Suppress per-block prints - too noisy for hundreds of runs
    cmd += ["--quiet_blocks"]

    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def stream_to_log(proc: subprocess.Popen, log_path: str):
    """Drain stdout to log file only (no terminal spam for large grids)."""
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
        """Called under self._lock."""
        pct = 100.0 * self._done / self._total if self._total else 0
        print(f"[{self._done:>4}/{self._total}  {pct:5.1f}%]  {name}  {status}",
              flush=True)


# ------------------------------------------------------------------ #
# Merge
# ------------------------------------------------------------------ #

def merge_run_csvs_by_scheme(runs_dir: str):
    for scheme in SIG_SCHEMES:
        final_csv = os.path.join(RESULTS_DIR, f"memo_results_{scheme}.csv")
        csvs = sorted(Path(runs_dir).glob(f"*_{scheme}.csv"))
        if not csvs:
            print(f"[merge] No per-run CSVs found for {scheme} - skipping.")
            continue

        if not os.path.exists(MERGE_BIN):
            print(f"[merge] '{MERGE_BIN}' not found - using Python fallback. "
                  "Compile with: gcc -O2 -fopenmp merge_results.c -o merge_results")
            _python_fallback_merge(csvs, final_csv)
        else:
            cmd = [MERGE_BIN, final_csv] + [str(p) for p in csvs]
            print(f"[merge] Merging {len(csvs)} files -> {final_csv}")
            subprocess.run(cmd)

        print(f"[merge] Done -> {final_csv}")


def _python_fallback_merge(csvs, final_csv):
    import csv
    all_rows, header = [], []
    for p in csvs:
        try:
            with open(p, newline="") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames:
                    for field in reader.fieldnames:
                        if field not in header:
                            header.append(field)
                for row in reader:
                    all_rows.append(row)
        except Exception as e:
            print(f"[merge-py] Skipping {p}: {e}")

    os.makedirs(os.path.dirname(final_csv), exist_ok=True)
    with open(final_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for row in all_rows:
            writer.writerow({k: row.get(k, "") for k in header})
    print(f"[merge-py] {len(all_rows)} rows -> {final_csv}")


# ------------------------------------------------------------------ #
# Main
# ------------------------------------------------------------------ #

def main():
    os.makedirs(LOG_DIR,      exist_ok=True)
    os.makedirs(RUNS_DIR,     exist_ok=True)
    os.makedirs(RESULTS_DIR,  exist_ok=True)

    combos = build_grid()
    total  = len(combos)

    print(f"Parallel grid runner")
    print(f"  Shards      : {SHARD_COUNTS}")
    print(f"  Block sizes : {BLOCK_SIZES}")
    print(f"  Block times : {BLOCK_TIMES}")
    print(f"  Sig schemes : {SIG_SCHEMES}")
    print(f"  Total runs  : {total}")
    print(f"  Workers     : {MAX_WORKERS}  (set GRID_WORKERS=N to override)")
    print(f"  Base config : {BASE_CONFIG}")
    print(f"  Logs        : {LOG_DIR}/")
    print(f"  Per-run CSVs: {RUNS_DIR}/")
    print(f"  Final CSVs  : " + ", ".join(f"memo_results_{s}.csv" for s in SIG_SCHEMES))
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
    merge_run_csvs_by_scheme(RUNS_DIR)
    print(f"\nDone. Results -> Results/memo_results_<scheme>.csv")


if __name__ == "__main__":
    main()