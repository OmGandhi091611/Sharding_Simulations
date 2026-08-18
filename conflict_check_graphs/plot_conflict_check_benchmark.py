#!/usr/bin/env python3
"""
plot_conflict_check_benchmark.py

Reads ../conflict_check_benchmark_c.csv (written by conflict_check_benchmark.c)
and writes one grouped bar chart per timed phase into this folder:

  alloc_ms_by_algorithm.png    - allocation phase (hash_set, nonce_hash_set only)
  process_ms_by_algorithm.png  - hashing/processing phase (hash_set, nonce_hash_set only)
  sort_ms_by_algorithm.png     - sort phase (sort_scan, nonce_sort_scan only)
  scan_ms_by_algorithm.png     - adjacent-pair scan phase (sort_scan, nonce_sort_scan only)
  total_ms_by_algorithm.png    - total time, all 5 algorithms

The CSV is long-format: one row per (algorithm, run, n), 10 runs per
(algorithm, n). alloc_ms/process_ms only apply to hash_set/nonce_hash_set;
sort_ms/scan_ms only apply to sort_scan/nonce_sort_scan; radix_sort has
neither pair (only total_ms). Non-applicable columns are 0 for a given
algorithm's rows, so each per-phase chart only plots the algorithms that
actually measure that phase - bars are grouped by n (averaged over the 10
runs) with one bar per algorithm within each group.

Run from this folder:
    python plot_conflict_check_benchmark.py
    python plot_conflict_check_benchmark.py --no_show
"""

import argparse
import os

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ALGO_COLORS = {
    "hash_set":         "#1f77b4",
    "nonce_hash_set":   "#17becf",
    "sort_scan":        "#d62728",
    "nonce_sort_scan":  "#ff7f0e",
    "radix_sort":       "#2ca02c",
}

ALGO_ORDER = ["hash_set", "nonce_hash_set", "sort_scan", "nonce_sort_scan", "radix_sort"]

PHASES = [
    ("alloc_ms",   "Allocation time by algorithm"),
    ("process_ms", "Hash/process time by algorithm"),
    ("sort_ms",    "Sort time by algorithm"),
    ("scan_ms",    "Scan time by algorithm"),
    ("total_ms",   "Total time by algorithm"),
]


def safe_mkdir(path: str):
    os.makedirs(path, exist_ok=True)


def savefig(outdir: str, name: str, dpi: int = 300) -> str:
    safe_mkdir(outdir)
    path = os.path.join(outdir, name)
    plt.tight_layout()
    plt.savefig(path, dpi=dpi, bbox_inches="tight")
    return path


def load_csv(csv_path: str):
    if not os.path.exists(csv_path):
        print(f"[skip] CSV not found: {csv_path}")
        return None

    df = pd.read_csv(csv_path, engine="python", on_bad_lines="warn")
    df.columns = [str(c).strip() for c in df.columns]

    required = {"algorithm", "run", "n", "alloc_ms", "process_ms", "sort_ms", "scan_ms", "total_ms"}
    missing = required - set(df.columns)
    if missing:
        print(f"[skip] {csv_path} is missing columns {sorted(missing)} - "
              f"re-run conflict_check_benchmark to regenerate it")
        return None

    for c in ["run", "n", "alloc_ms", "process_ms", "sort_ms", "scan_ms", "total_ms"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["algorithm", "n"]).copy()
    df["n"] = df["n"].astype(int)
    return df.sort_values(["n", "algorithm"])


def plot_phase_by_algorithm(df: pd.DataFrame, col: str, title: str, outdir: str, show: bool):
    means = df.groupby(["algorithm", "n"], as_index=False)[col].mean()

    algos = [a for a in ALGO_ORDER if means.loc[means["algorithm"] == a, col].sum() > 0]
    if not algos:
        print(f"[skip] no algorithm has nonzero {col}")
        return

    n_values = sorted(means["n"].unique())
    x = np.arange(len(n_values))
    width = 0.8 / len(algos)

    fig, ax = plt.subplots(figsize=(16, 6))
    for i, algo in enumerate(algos):
        sub = means[means["algorithm"] == algo].set_index("n").reindex(n_values)
        offset = (i - (len(algos) - 1) / 2) * width
        ax.bar(x + offset, sub[col].values, width, label=algo, color=ALGO_COLORS.get(algo))

    ax.set_xticks(x)
    ax.set_xticklabels([str(n) for n in n_values], rotation=45, ha="right")
    ax.set_yscale("log")
    ax.set_xlabel("n (transactions per block)", fontsize=13)
    ax.set_ylabel("Time (ms, mean of 10 runs)", fontsize=13)
    ax.set_title(title, fontsize=14)
    ax.legend(fontsize=9)
    ax.grid(True, which="both", axis="y", linestyle="--", alpha=0.4)

    out_name = f"{col}_by_algorithm.png"
    savefig(outdir, out_name)
    print(f"[done] {os.path.join(outdir, out_name)}")
    plt.show() if show else plt.close(fig)


def main():
    ap = argparse.ArgumentParser(
        description="Plot conflict_check_benchmark_c.csv results as grouped bar charts."
    )
    ap.add_argument("--csv", default="../conflict_check_benchmark_c.csv")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--no_show", action="store_true", help="Don't show plots, only save them")
    args = ap.parse_args()
    show = not args.no_show

    df = load_csv(args.csv)
    if df is None:
        return

    for col, title in PHASES:
        plot_phase_by_algorithm(df, col, title, args.outdir, show)


if __name__ == "__main__":
    main()
