#!/usr/bin/env python3
"""
Convergence check for REPEATS in network_parallel.py.

Runs 1-2 representative configs at MAX_SEED distinct --seed values, then
looks at increasing prefixes of that seed pool (5, 10, 20, 30) to see how
the 95% CI half-width on tps shrinks as the repeat count N grows. Pick the
smallest N where the CI has stopped shrinking meaningfully and hard-code
it as REPEATS in network_parallel.py.

Network condition (rtt_ms/control_bw_mbps) comes from memo_config/base.json,
same as network_parallel.py — edit that file by hand for whichever
condition you're checking convergence under.

Usage:
    python Parallel_processes/network_convergence_check.py
    GRID_WORKERS=8 python Parallel_processes/network_convergence_check.py
"""

import subprocess
import sys
import os
import json
import math
import statistics
import threading
from pathlib import Path
from typing import Any, Dict, List

ROOT        = Path(__file__).resolve().parent.parent
SIM_SCRIPT  = str(ROOT / "simulation.py")
BASE_CONFIG = str(ROOT / "memo_config/base.json")
RESULTS_DIR = ROOT / "Results"
OUT_PNG     = RESULTS_DIR / "network_convergence_check.png"

# 1-2 representative configs to bracket variance across the grid: a
# small-shard point and a large-shard point. Block size/time match
# REFERENCE_HOPCDF_* in network_parallel.py so this check is representative
# of the actual main-sweep run.
REPRESENTATIVE_CONFIGS = [
    {"name": "s1",   "shards": 1,   "total_blocksize": 65536, "blocktime": 18.75},
    {"name": "s128", "shards": 128, "total_blocksize": 65536, "blocktime": 18.75},
]

BROADCAST_PROTOCOL  = "gossip"
SHARD_COMM_PROTOCOL = "direct"
SIG_SCHEME          = "ed25519"

TOTAL_NODES     = 1024
TOTAL_NEIGHBORS = 512
TOTAL_MINERS    = 1024

MAX_SEED    = 30           # size of the seed pool each config is run over
CHECKPOINTS = [5, 10, 20, 30]
METRIC      = "tps"        # field read from simulation.py's RESULT_JSON line

MAX_WORKERS = int(os.environ.get("GRID_WORKERS", max(1, (os.cpu_count() or 2) - 2)))

# 97.5th percentile of Student's t at df = N-1, for each N in CHECKPOINTS —
# small-sample CIs need t, not the N->inf normal approximation (1.96).
T_975 = {5: 2.776, 10: 2.262, 20: 2.093, 30: 2.045}

# Recommend the smallest checkpoint where every config's relative CI
# half-width falls at or under this threshold.
THRESHOLD_PCT = 5.0


def run_one(config: Dict[str, Any], seed: int) -> float:
    blocksize    = config["total_blocksize"]
    transactions = blocksize * 1000

    cmd = [sys.executable, "-u", SIM_SCRIPT]
    if os.path.exists(BASE_CONFIG):
        cmd += ["--config", BASE_CONFIG]
    cmd += [
        "--shards", str(config["shards"]),
        "--total-blocksize", str(blocksize),
        "--blocktime", str(config["blocktime"]),
        "--nodes", str(TOTAL_NODES),
        "--miners", str(TOTAL_MINERS),
        "--neighbors", str(TOTAL_NEIGHBORS),
        "--transactions", str(transactions),
        "--wallets", str(transactions),
        "--blocks", "1000",
        "--sig_scheme", SIG_SCHEME,
        "--broadcast_protocol", BROADCAST_PROTOCOL,
        "--shard_comm_protocol", SHARD_COMM_PROTOCOL,
        "--seed", str(seed),
        "--quiet_blocks",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"sim failed (config={config['name']} seed={seed}):\n"
            f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
        )
    for line in result.stdout.splitlines():
        if line.startswith("RESULT_JSON "):
            payload = json.loads(line[len("RESULT_JSON "):])
            return float(payload[METRIC])
    raise RuntimeError(f"No RESULT_JSON line found (config={config['name']} seed={seed})")


def run_all_seeds(config: Dict[str, Any]) -> List[float]:
    values: List[Any] = [None] * MAX_SEED
    sem    = threading.Semaphore(MAX_WORKERS)
    lock   = threading.Lock()
    errors: List[str] = []

    def worker(seed: int):
        sem.acquire()
        try:
            values[seed] = run_one(config, seed)
        except Exception as e:
            with lock:
                errors.append(str(e))
        finally:
            sem.release()

    threads = [threading.Thread(target=worker, args=(s,)) for s in range(MAX_SEED)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        raise RuntimeError("\n---\n".join(errors[:3]))
    return values


def ci_half_width(sample: List[float], n: int) -> float:
    sd = statistics.stdev(sample[:n])
    t  = T_975.get(n, 1.96)
    return t * sd / math.sqrt(n)


def main():
    RESULTS_DIR.mkdir(exist_ok=True)
    results: Dict[str, Dict[int, tuple]] = {}

    for config in REPRESENTATIVE_CONFIGS:
        print(f"[run] {config['name']}: shards={config['shards']} "
              f"blocksize={config['total_blocksize']} blocktime={config['blocktime']} "
              f"-> {MAX_SEED} seeded runs ({MAX_WORKERS} workers)")
        samples = run_all_seeds(config)

        rows = {}
        for n in CHECKPOINTS:
            mean = statistics.mean(samples[:n])
            hw   = ci_half_width(samples, n)
            rel  = 100.0 * hw / mean if mean else 0.0
            rows[n] = (mean, hw, rel)
            print(f"    N={n:>2}  mean_tps={mean:10.3f}  "
                  f"95% CI half-width={hw:8.3f}  (~{rel:5.2f}% of mean)")
        results[config["name"]] = rows

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(7, 5))
        for name, rows in results.items():
            ax.plot(CHECKPOINTS, [rows[n][2] for n in CHECKPOINTS],
                     marker="o", label=name)
        ax.set_xlabel("Repeats (N)")
        ax.set_ylabel("95% CI half-width (% of mean tps)")
        ax.set_title("Convergence of tps CI width vs. repeat count")
        ax.legend()
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(OUT_PNG, dpi=150)
        print(f"\n[plot] Wrote -> {OUT_PNG}")
    except ImportError:
        print("\n[plot] matplotlib not available, skipping plot")

    recommended = CHECKPOINTS[-1]
    for n in CHECKPOINTS:
        if all(rows[n][2] <= THRESHOLD_PCT for rows in results.values()):
            recommended = n
            break

    print(f"\n[recommendation] REPEATS = {recommended}  "
          f"(smallest N with 95% CI half-width <= {THRESHOLD_PCT}% of mean tps "
          f"for every representative config)")
    print("Hard-code this into REPEATS in Parallel_processes/network_parallel.py.")


if __name__ == "__main__":
    main()
