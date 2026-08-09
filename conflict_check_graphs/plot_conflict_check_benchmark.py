#!/usr/bin/env python3
"""
plot_conflict_check_benchmark.py

Reads ../conflict_check_benchmark_c.csv (written by conflict_check_benchmark.c)
and writes three plots into this folder:

  1. runtime_vs_n_threads<T>.png  - ms vs n (log-log), one line per algorithm,
                                     for a single --threads run
  2. speedup_vs_n_threads<T>.png  - algorithmic + parallel speedup vs n,
                                     for the same --threads run
  3. parallel_scaling_n<N>.png    - hash/sort parallel runtime AND speedup vs
                                     thread count, at a single n (compares
                                     every --threads run in the CSV for that n)

The CSV can contain rows written before the parallel columns existed (an
older, shorter schema) if you re-ran the benchmark without regenerating a
fresh file - those rows are dropped with a warning rather than crashing.

Run from this folder:
    python plot_conflict_check_benchmark.py
    python plot_conflict_check_benchmark.py --threads 8 --n 524288 --no_show
"""

import argparse
import os

import matplotlib.pyplot as plt
import pandas as pd

ALGO_COLORS = {
    "hash_set":    "#1f77b4",
    "sort_scan":   "#d62728",
    "radix_sort":  "#2ca02c",
    "hash_set||":  "#17becf",
    "sort_scan||": "#ff7f0e",
}


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
    if "threads" not in df.columns:
        print(f"[skip] {csv_path} has no 'threads' column - "
              f"re-run conflict_check_benchmark to regenerate it")
        return None

    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    before = len(df)
    df = df.dropna(subset=["n", "threads"]).copy()
    dropped = before - len(df)
    if dropped:
        print(f"[info] dropped {dropped} row(s) with no threads value "
              f"(written before the parallel columns existed)")

    df["n"] = df["n"].astype(int)
    df["threads"] = df["threads"].astype(int)
    return df.sort_values(["threads", "n"])


def plot_runtime_vs_n(df: pd.DataFrame, threads: int, outdir: str, show: bool):
    sub = df[df["threads"] == threads].sort_values("n")
    if sub.empty:
        print(f"[skip] no rows for threads={threads}")
        return

    fig, ax = plt.subplots(figsize=(10, 6))
    series = [
        ("hash_set_parallel_ms",  "hash_set||"),
        ("sort_scan_parallel_ms", "sort_scan||"),
    ]
    for col, label in series:
        if col in sub.columns:
            ax.plot(sub["n"], sub[col], "-", marker="o", linewidth=2,
                     markersize=5, label=label, color=ALGO_COLORS.get(label))

    ax.set_xscale("log", base=2)
    ax.set_yscale("log")
    ax.set_xlabel("n (transactions per block)", fontsize=13)
    ax.set_ylabel("Time (ms)", fontsize=13)
    ax.set_title(f"Conflict-check runtime vs n (threads={threads})", fontsize=14)
    ax.legend(fontsize=9)
    ax.grid(True, which="both", linestyle="--", alpha=0.4)

    out_name = f"runtime_vs_n_threads{threads}.png"
    savefig(outdir, out_name)
    print(f"[done] {os.path.join(outdir, out_name)}")
    plt.show() if show else plt.close(fig)


def plot_speedup_vs_n(df: pd.DataFrame, threads: int, outdir: str, show: bool):
    sub = df[df["threads"] == threads].sort_values("n")
    if sub.empty:
        print(f"[skip] no rows for threads={threads}")
        return

    fig, ax = plt.subplots(figsize=(10, 6))
    series = [
        ("hash_parallel_speedup", "hash_set / hash_set||"),
        ("sort_parallel_speedup", "sort_scan / sort_scan||"),
    ]
    for col, label in series:
        if col in sub.columns:
            ax.plot(sub["n"], sub[col], marker="o", linewidth=2,
                     markersize=5, label=label)

    ax.axhline(1.0, linestyle="--", color="gray", alpha=0.6)
    ax.set_xscale("log", base=2)
    ax.set_xlabel("n (transactions per block)", fontsize=13)
    ax.set_ylabel("Speedup (x)", fontsize=13)
    ax.set_title(f"Conflict-check speedup vs n (threads={threads})", fontsize=14)
    ax.legend(fontsize=9)
    ax.grid(True, which="both", linestyle="--", alpha=0.4)

    out_name = f"speedup_vs_n_threads{threads}.png"
    savefig(outdir, out_name)
    print(f"[done] {os.path.join(outdir, out_name)}")
    plt.show() if show else plt.close(fig)


def plot_runtime_vs_n_algorithms(df: pd.DataFrame, outdir: str, show: bool):
    """hash_set vs sort_scan vs radix_sort only - these don't use threads,
    so average across every --threads run in the CSV to cut sampling noise
    rather than picking one arbitrary run as a baseline."""
    sub = df.groupby("n", as_index=False)[
        ["hash_set_ms", "sort_scan_ms", "radix_sort_ms"]
    ].mean().sort_values("n")

    fig, ax = plt.subplots(figsize=(10, 6))
    series = [
        ("hash_set_ms",   "hash_set",   ALGO_COLORS["hash_set"]),
        ("sort_scan_ms",  "sort_scan",  ALGO_COLORS["sort_scan"]),
        ("radix_sort_ms", "radix_sort", ALGO_COLORS["radix_sort"]),
    ]
    for col, label, color in series:
        ax.plot(sub["n"], sub[col], "-", marker="o", linewidth=2,
                 markersize=5, label=label, color=color)

    ax.set_xscale("log", base=2)
    ax.set_yscale("log")
    ax.set_xlabel("n (transactions per block)", fontsize=13)
    ax.set_ylabel("Time (ms)", fontsize=13)
    ax.set_title("Conflict-check runtime vs n (sequential algorithms)", fontsize=14)
    ax.legend(fontsize=9)
    ax.grid(True, which="both", linestyle="--", alpha=0.4)

    out_name = "runtime_vs_n_algorithms.png"
    savefig(outdir, out_name)
    print(f"[done] {os.path.join(outdir, out_name)}")
    plt.show() if show else plt.close(fig)


def plot_speedup_vs_n_algorithms(df: pd.DataFrame, outdir: str, show: bool):
    """sort_scan/hash_set and sort_scan/radix_sort only, averaged across
    every --threads run for the same noise-reduction reason as above."""
    sub = df.groupby("n", as_index=False)[
        ["hash_set_ms", "sort_scan_ms", "radix_sort_ms"]
    ].mean().sort_values("n")
    sub["speedup"] = sub["sort_scan_ms"] / sub["hash_set_ms"]
    sub["radix_speedup"] = sub["sort_scan_ms"] / sub["radix_sort_ms"]

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.plot(sub["n"], sub["speedup"], marker="o", linewidth=2,
             markersize=5, label="sort_scan / hash_set")
    ax.plot(sub["n"], sub["radix_speedup"], marker="o", linewidth=2,
             markersize=5, label="sort_scan / radix_sort")

    ax.axhline(1.0, linestyle="--", color="gray", alpha=0.6)
    ax.set_xscale("log", base=2)
    ax.set_xlabel("n (transactions per block)", fontsize=13)
    ax.set_ylabel("Speedup (x)", fontsize=13)
    ax.set_title("Conflict-check speedup vs n (sequential algorithms)", fontsize=14)
    ax.legend(fontsize=9)
    ax.grid(True, which="both", linestyle="--", alpha=0.4)

    out_name = "speedup_vs_n_algorithms.png"
    savefig(outdir, out_name)
    print(f"[done] {os.path.join(outdir, out_name)}")
    plt.show() if show else plt.close(fig)


def plot_parallel_scaling(df: pd.DataFrame, n: int, outdir: str, show: bool):
    sub = df[df["n"] == n].sort_values("threads")
    if sub.empty:
        print(f"[skip] no rows for n={n}")
        return
    if sub["threads"].nunique() < 2:
        print(f"[skip] only one threads value present for n={n} - "
              f"nothing to compare (run the benchmark with a few "
              f"different --threads values first)")
        return

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

    ax1.plot(sub["threads"], sub["hash_set_parallel_ms"], marker="o",
              linewidth=2, label="hash_set||", color=ALGO_COLORS["hash_set||"])
    ax1.plot(sub["threads"], sub["sort_scan_parallel_ms"], marker="o",
              linewidth=2, label="sort_scan||", color=ALGO_COLORS["sort_scan||"])
    ax1.set_xlabel("Threads", fontsize=12)
    ax1.set_ylabel("Time (ms)", fontsize=12)
    ax1.set_title(f"Parallel runtime vs threads (n={n:,})", fontsize=13)
    ax1.legend(fontsize=9)
    ax1.grid(True, linestyle="--", alpha=0.4)

    ax2.plot(sub["threads"], sub["hash_parallel_speedup"], marker="o",
              linewidth=2, label="hash_set||", color=ALGO_COLORS["hash_set||"])
    ax2.plot(sub["threads"], sub["sort_parallel_speedup"], marker="o",
              linewidth=2, label="sort_scan||", color=ALGO_COLORS["sort_scan||"])
    min_threads = sub["threads"].min()
    ideal = sub["threads"] / min_threads
    ax2.plot(sub["threads"], ideal, linestyle=":", color="gray", label="ideal linear")
    ax2.set_xlabel("Threads", fontsize=12)
    ax2.set_ylabel("Speedup vs sequential (x)", fontsize=12)
    ax2.set_title(f"Parallel speedup vs threads (n={n:,})", fontsize=13)
    ax2.legend(fontsize=9)
    ax2.grid(True, linestyle="--", alpha=0.4)

    fig.tight_layout()
    out_name = f"parallel_scaling_n{n}.png"
    savefig(outdir, out_name)
    print(f"[done] {os.path.join(outdir, out_name)}")
    plt.show() if show else plt.close(fig)


def main():
    ap = argparse.ArgumentParser(
        description="Plot conflict_check_benchmark_c.csv results."
    )
    ap.add_argument("--csv", default="../conflict_check_benchmark_c.csv")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--no_show", action="store_true", help="Don't show plots, only save them")
    ap.add_argument("--threads", type=int, default=None,
                     help="Which --threads run to use for the runtime/speedup-vs-n "
                          "plots (default: largest threads value found in the CSV)")
    ap.add_argument("--n", type=int, default=None,
                     help="Which n to use for the parallel-scaling-vs-threads plot "
                          "(default: largest n found in the CSV)")
    args = ap.parse_args()
    show = not args.no_show

    df = load_csv(args.csv)
    if df is None:
        return

    threads = args.threads if args.threads is not None else int(df["threads"].max())
    n = args.n if args.n is not None else int(df["n"].max())

    plot_runtime_vs_n_algorithms(df, args.outdir, show)
    plot_speedup_vs_n_algorithms(df, args.outdir, show)
    plot_runtime_vs_n(df, threads, args.outdir, show)
    plot_speedup_vs_n(df, threads, args.outdir, show)
    plot_parallel_scaling(df, n, args.outdir, show)


if __name__ == "__main__":
    main()
