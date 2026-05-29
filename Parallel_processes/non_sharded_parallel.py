#!/usr/bin/env python3
"""
Non-sharded parallel simulation runner with grid generation.

Generates all combinations of block_size x block_time and runs them
in parallel with a worker pool. Each sim writes to its own CSV under
Results/non_sharded_runs/ - no file contention. Python merger combines
everything at the end.

Usage:
    python non_sharded_parallel.py
    GRID_WORKERS=32 python non_sharded_parallel.py   # override worker count
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
BASE_CONFIG = str(ROOT / "non_sharded_config/base.json")
LOG_DIR     = str(ROOT / "non_sharded_logs")
RESULTS_DIR = str(ROOT / "Results")
RUNS_DIR    = os.path.join(RESULTS_DIR, "non_sharded_runs")
MERGE_BIN   = str(ROOT / "merge_results")

# ------------------------------------------------------------------ #
# GRID — edit these lists to define your parameter space
# ------------------------------------------------------------------ #
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
]

# ------------------------------------------------------------------ #
# Worker pool limit
# Leave 2 cores free for OS + merger overhead.
# Override: GRID_WORKERS=32 python non_sharded_parallel.py
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
    for blocksize, blocktime in itertools.product(BLOCK_SIZES, BLOCK_TIMES):
        name = f"ns_bs{blocksize}_bt{blocktime:.5f}".replace(".", "p")
        combos.append({
            "name":            name,
            "total_blocksize": blocksize,
            "blocktime":       blocktime,
        })
    return combos


# ------------------------------------------------------------------ #
# Process launch
# ------------------------------------------------------------------ #

def launch_sim(combo: Dict[str, Any]) -> subprocess.Popen:
    """
    Launch one simulation. Base JSON supplies common params.
    Varying params (blocksize, blocktime) are passed as CLI args
    so they always win over anything in the base JSON.
    """
    name = combo["name"]

    cmd = [sys.executable, "-u", SIM_SCRIPT]

    if os.path.exists(BASE_CONFIG):
        cmd += ["--config", BASE_CONFIG]

    # Output routing - unique per run, no contention
    cmd += ["--results_csv", f"{name}.csv", "--results_dir", RUNS_DIR]

    # Varying params as CLI args (win over base JSON)
    cmd += ["--total-blocksize", str(combo["total_blocksize"])]
    cmd += ["--blocktime",       str(combo["blocktime"])]
    cmd += ["--blocks",          "1000"]

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

def merge_run_csvs(runs_dir: str):
    final_csv = os.path.join(RESULTS_DIR, "non_sharded_results.csv")
    csvs = sorted(Path(runs_dir).glob("ns_*.csv"))
    if not csvs:
        print("[merge] No per-run CSVs found - skipping.")
        return

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
# Skip-completed filter
# ------------------------------------------------------------------ #

def load_merged_tps_keys(results_dir: str) -> set:
    """Read the final merged CSV once → set of (tps, avg_block_time) already merged."""
    import csv
    merged = set()
    final_csv = os.path.join(results_dir, "non_sharded_results.csv")
    if not os.path.exists(final_csv):
        return merged
    try:
        with open(final_csv, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    merged.add((row["tps"].strip(), row["average block time"].strip()))
                except KeyError:
                    continue
        print(f"[skip-check] {len(merged)} rows loaded from {os.path.basename(final_csv)}")
    except Exception as e:
        print(f"[skip-check] Could not read {final_csv}: {e}")
    return merged


def needs_run(combo: dict, runs_dir: str, merged_keys: set) -> bool:
    """A combo needs to run if:
    - its per-run CSV doesn't exist (never ran), OR
    - its per-run CSV result is already present in the merged CSV (stale)."""
    import csv
    per_run_csv = os.path.join(runs_dir, f"{combo['name']}.csv")
    if not os.path.exists(per_run_csv):
        return True
    try:
        with open(per_run_csv, newline="") as f:
            for row in csv.DictReader(f):
                key = (row["tps"].strip(), row["average block time"].strip())
                if key in merged_keys:
                    return True  # stale — same as what's already merged
                return False     # fresh result not yet in merged CSV, skip
    except Exception:
        pass
    return True  # unreadable per-run CSV, re-run to be safe


# ------------------------------------------------------------------ #
# Main
# ------------------------------------------------------------------ #

def main():
    os.makedirs(LOG_DIR,      exist_ok=True)
    os.makedirs(RUNS_DIR,     exist_ok=True)
    os.makedirs(RESULTS_DIR,  exist_ok=True)

    combos = build_grid()
    total  = len(combos)

    merged_keys = load_merged_tps_keys(RESULTS_DIR)
    if merged_keys:
        combos = [c for c in combos if needs_run(c, RUNS_DIR, merged_keys)]
        skipped = total - len(combos)
        print(f"[skip-check] {skipped} already-merged runs skipped, {len(combos)} remaining\n")

    print(f"Non-sharded parallel grid runner")
    print(f"  Block sizes : {BLOCK_SIZES}")
    print(f"  Block times : {BLOCK_TIMES}")
    print(f"  Total runs  : {total}")
    print(f"  Workers     : {MAX_WORKERS}  (set GRID_WORKERS=N to override)")
    print(f"  Base config : {BASE_CONFIG}")
    print(f"  Logs        : {LOG_DIR}/")
    print(f"  Per-run CSVs: {RUNS_DIR}/")
    print(f"  Final CSV   : Results/non_sharded_results.csv")
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
    merge_run_csvs(RUNS_DIR)
    print(f"\nDone. Results -> Results/non_sharded_results.csv")


if __name__ == "__main__":
    main()
