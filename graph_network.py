#!/usr/bin/env python3
"""
graph_network.py

Reads the network/communication-protocol comparison CSVs written by
Parallel_processes/network_parallel.py (Results/network_results_<env>.csv)
and the curated hop-count CDF reference files it keeps
(Results/network_runs/hopcdf/<broadcast_protocol>_<shard_comm_protocol>.csv),
and writes protocol-comparison plots to network_graphs_<env>/.

Deliberately separate from graph.py: this file only ever answers "which
communication protocol is fastest / cheapest", not the architecture
comparison (non-sharded vs NEAR vs MEMO) that graph.py's memo_* functions
cover. Run once per network condition, same as graph.py's memo_* graphs:

    python graph_network.py --network_csv network_results_local.csv  --network_out network_graphs_local  --no_show
    python graph_network.py --network_csv network_results_usa.csv    --network_out network_graphs_usa    --no_show
    python graph_network.py --network_csv network_results_global.csv --network_out network_graphs_global --no_show
"""

import argparse
import os
import pandas as pd
import matplotlib.pyplot as plt

from graph import safe_mkdir, ensure_numeric, savefig, _pick_col

PROTOCOL_COLORS = {
    "gossip":    "#1f77b4",
    "flood":     "#d62728",
    "plumtree":  "#2ca02c",
    "gossipsub": "#ff7f0e",
}

SHARD_COMM_STYLES = {
    "kademlia": "-",
    "direct":   "--",
}


def _load_network_csv(csv_path: str):
    if not os.path.exists(csv_path):
        print(f"[skip] network CSV not found: {csv_path}")
        return None

    df = pd.read_csv(csv_path, engine="python", on_bad_lines="warn")
    df.columns = [str(c).strip() for c in df.columns]

    required = {
        "shards":       ["shards"],
        "block_size":   ["block size", "block_size", "blocksize"],
        "abt":          ["average block time", "avg block time", "avg_block_time"],
        "tps":          ["tps"],
        "messages":     ["messages"],
        "bprot":        ["broadcast_protocol"],
        "scomm":        ["shard_comm_protocol"],
    }
    cols = {k: _pick_col(df, cands) for k, cands in required.items()}
    missing = [k for k, c in cols.items() if c is None]
    if missing:
        print(f"[skip] network CSV missing required columns: {missing}")
        return None

    df = ensure_numeric(df, [cols["shards"], cols["block_size"], cols["abt"],
                              cols["tps"], cols["messages"]])
    df["broadcast_cpu_seconds"] = pd.to_numeric(
        df.get("broadcast_cpu_seconds"), errors="coerce") if "broadcast_cpu_seconds" in df.columns else float("nan")
    for hop_col in ("hop_p50", "hop_p90", "hop_p99", "hop_max"):
        df[hop_col] = pd.to_numeric(
            df.get(hop_col), errors="coerce") if hop_col in df.columns else float("nan")

    df = df.dropna(subset=[cols["shards"], cols["block_size"], cols["abt"], cols["tps"], cols["messages"]]).copy()
    if df.empty:
        return None

    df["shards_int"]     = df[cols["shards"]].astype(int)
    df["block_size_int"] = df[cols["block_size"]].astype(int)
    df["abt_val"]        = df[cols["abt"]].astype(float)
    df["tps_val"]        = df[cols["tps"]].astype(float)
    df["messages_val"]   = df[cols["messages"]].astype(float)
    df["broadcast_protocol"]  = df[cols["bprot"]].astype(str)
    df["shard_comm_protocol"] = df[cols["scomm"]].astype(str)
    return df


def _x_positions(shards_sorted):
    return {s: i * 2 for i, s in enumerate(shards_sorted)}


# ----------------------------
# Protocol comparison: one metric vs shards, one line per broadcast_protocol
# (min/mean over block size + block time + shard_comm_protocol, filterable)
# ----------------------------
def _plot_metric_by_protocol(df, value_col: str, agg: str, ylabel: str,
                              title: str, out_name: str, outdir: str, show: bool,
                              log_y: bool = False, shard_comm_protocol: str = None):
    sub = df if shard_comm_protocol is None else df[df["shard_comm_protocol"] == shard_comm_protocol]
    if sub.empty:
        print(f"[skip] no rows for {out_name} (shard_comm_protocol={shard_comm_protocol})")
        return

    grouped = (
        sub.groupby(["broadcast_protocol", "shards_int"])[value_col]
        .agg(agg)
        .reset_index()
        .sort_values(["broadcast_protocol", "shards_int"])
    )

    all_shards = sorted(grouped["shards_int"].unique().tolist())
    x_positions = _x_positions(all_shards)

    fig, ax = plt.subplots(figsize=(12, 6))
    for bprot in sorted(grouped["broadcast_protocol"].unique().tolist()):
        row = grouped[grouped["broadcast_protocol"] == bprot].sort_values("shards_int")
        x_vals = [x_positions[s] for s in row["shards_int"]]
        color = PROTOCOL_COLORS.get(bprot, None)
        ax.plot(x_vals, row[value_col], marker="o", linewidth=2, markersize=5,
                label=bprot, color=color)

    ax.set_xlabel("Number of Shards", fontsize=13)
    ax.set_ylabel(ylabel, fontsize=13)
    ax.set_title(title, fontsize=14)
    ax.set_xticks([x_positions[s] for s in all_shards])
    ax.set_xticklabels([str(s) for s in all_shards], rotation=45, ha="right")
    if log_y:
        ax.set_yscale("log")
    ax.legend(title="Broadcast Protocol", fontsize=9, title_fontsize=10)
    ax.grid(True, linestyle="--", alpha=0.4)
    fig.tight_layout()

    savefig(outdir, out_name)
    print(f"[done] {os.path.join(outdir, out_name)}")
    if show:
        plt.show()
    else:
        plt.close(fig)


def _plot_metric_by_protocol_bar(df, value_col: str, agg: str, ylabel: str,
                                  title: str, out_name: str, outdir: str, show: bool,
                                  log_y: bool = False):
    grouped = (
        df.groupby(["broadcast_protocol", "shards_int"])[value_col]
        .agg(agg)
        .reset_index()
    )
    all_shards = sorted(grouped["shards_int"].unique().tolist())
    protocols = sorted(grouped["broadcast_protocol"].unique().tolist())

    group_width = 0.8
    bar_width = group_width / len(protocols)
    x = list(range(len(all_shards)))

    fig, ax = plt.subplots(figsize=(12, 6))
    for i, bprot in enumerate(protocols):
        row = grouped[grouped["broadcast_protocol"] == bprot].set_index("shards_int")
        y_vals = [row.loc[s, value_col] if s in row.index else 0 for s in all_shards]
        offset = (i - (len(protocols) - 1) / 2) * bar_width
        x_vals = [xi + offset for xi in x]
        ax.bar(x_vals, y_vals, width=bar_width * 0.95,
               label=bprot, color=PROTOCOL_COLORS.get(bprot))

    ax.set_xlabel("Number of Shards", fontsize=13)
    ax.set_ylabel(ylabel, fontsize=13)
    ax.set_title(title, fontsize=14)
    ax.set_xticks(x)
    ax.set_xticklabels([str(s) for s in all_shards], rotation=45, ha="right")
    if log_y:
        ax.set_yscale("log")
    ax.legend(title="Broadcast Protocol", fontsize=9, title_fontsize=10)
    ax.grid(True, axis="y", linestyle="--", alpha=0.4)
    fig.tight_layout()

    savefig(outdir, out_name)
    print(f"[done] {os.path.join(outdir, out_name)}")
    if show:
        plt.show()
    else:
        plt.close(fig)


def run_protocol_tps_vs_shards(network_csv: str, outdir: str, show: bool):
    df = _load_network_csv(network_csv)
    if df is None:
        print(f"[skip] tps-vs-shards: no usable data in {network_csv}")
        return
    _plot_metric_by_protocol_bar(
        df, "tps_val", "max", "Max TPS",
        "Broadcast Protocol Comparison: TPS vs Number of Shards",
        "network_tps_vs_shards.png", outdir, show,
    )


def run_protocol_blocktime_vs_shards(network_csv: str, outdir: str, show: bool, block_size: int = None):
    df = _load_network_csv(network_csv)
    if df is None:
        print(f"[skip] blocktime-vs-shards: no usable data in {network_csv}")
        return
    if block_size is not None:
        df = df[df["block_size_int"] == block_size]
        if df.empty:
            print(f"[skip] block size {block_size} not found for blocktime-vs-shards plot")
            return
    suffix = f" (Block Size = {block_size:,})" if block_size is not None else " (All Block Sizes — Min)"
    out_name = f"network_blocktime_vs_shards_bs{block_size}.png" if block_size is not None else "network_blocktime_vs_shards.png"
    _plot_metric_by_protocol(
        df, "abt_val", "min", "Min Actual Block Time (s)",
        f"Broadcast Protocol Comparison: Min Block Time vs Number of Shards{suffix}",
        out_name, outdir, show, log_y=True,
    )


def run_protocol_messages_vs_shards(network_csv: str, outdir: str, show: bool):
    df = _load_network_csv(network_csv)
    if df is None:
        print(f"[skip] messages-vs-shards: no usable data in {network_csv}")
        return
    _plot_metric_by_protocol_bar(
        df, "messages_val", "mean", "Messages",
        "Broadcast Protocol Comparison: Messages vs Number of Shards",
        "network_messages_vs_shards.png", outdir, show, log_y=True,
    )


def run_protocol_rounds_vs_shards(network_csv: str, outdir: str, show: bool, percentile: str = "hop_p50"):
    df = _load_network_csv(network_csv)
    if df is None or df[percentile].isna().all():
        print(f"[skip] rounds-vs-shards: no usable {percentile} data in {network_csv}")
        return
    df = df.dropna(subset=[percentile])
    label = {"hop_p50": "Median", "hop_p90": "P90", "hop_p99": "P99", "hop_max": "Max"}[percentile]
    _plot_metric_by_protocol_bar(
        df, percentile, "mean", f"{label} Rounds to Inform Network (per block)",
        f"Broadcast Protocol Comparison: {label} Broadcast Rounds vs Number of Shards",
        f"network_rounds_vs_shards_{percentile}.png", outdir, show,
    )


def run_protocol_broadcast_cpu_vs_shards(network_csv: str, outdir: str, show: bool):
    df = _load_network_csv(network_csv)
    if df is None or df["broadcast_cpu_seconds"].isna().all():
        print(f"[skip] broadcast-cpu-vs-shards: no usable broadcast_cpu_seconds data in {network_csv}")
        return
    df = df.dropna(subset=["broadcast_cpu_seconds"])
    _plot_metric_by_protocol(
        df, "broadcast_cpu_seconds", "mean", "Broadcast CPU-seconds (network-wide, per run)",
        "Broadcast Protocol Comparison: Aggregate Broadcast CPU Cost vs Number of Shards",
        "network_broadcast_cpu_vs_shards.png", outdir, show, log_y=True,
    )


# ----------------------------
# Shard-communication comparison: kademlia vs direct, one metric vs shards
# ----------------------------
def run_shard_comm_comparison(network_csv: str, outdir: str, show: bool,
                               metric: str = "abt_val", ylabel: str = "Min Actual Block Time (s)"):
    df = _load_network_csv(network_csv)
    if df is None:
        print(f"[skip] shard-comm comparison: no usable data in {network_csv}")
        return

    agg_fn = "min" if metric == "abt_val" else "mean"
    grouped = (
        df.groupby(["shard_comm_protocol", "shards_int"])[metric]
        .agg(agg_fn)
        .reset_index()
        .sort_values(["shard_comm_protocol", "shards_int"])
    )

    all_shards = sorted(grouped["shards_int"].unique().tolist())
    x_positions = _x_positions(all_shards)

    fig, ax = plt.subplots(figsize=(12, 6))
    for scomm in sorted(grouped["shard_comm_protocol"].unique().tolist()):
        row = grouped[grouped["shard_comm_protocol"] == scomm].sort_values("shards_int")
        x_vals = [x_positions[s] for s in row["shards_int"]]
        ax.plot(x_vals, row[metric], marker="o", linewidth=2, markersize=5, label=scomm)

    ax.set_xlabel("Number of Shards", fontsize=13)
    ax.set_ylabel(ylabel, fontsize=13)
    ax.set_title(f"Shard Communication Protocol Comparison: {ylabel} vs Number of Shards", fontsize=14)
    ax.set_xticks([x_positions[s] for s in all_shards])
    ax.set_xticklabels([str(s) for s in all_shards], rotation=45, ha="right")
    ax.set_yscale("log")
    ax.legend(title="Shard Comm Protocol", fontsize=9, title_fontsize=10)
    ax.grid(True, linestyle="--", alpha=0.4)
    fig.tight_layout()

    out_name = f"network_shard_comm_{metric}.png"
    savefig(outdir, out_name)
    print(f"[done] {os.path.join(outdir, out_name)}")
    if show:
        plt.show()
    else:
        plt.close(fig)


# ----------------------------
# Hop-count CDF overlay — reads the curated reference files directly
# (Results/network_runs/hopcdf/<broadcast_protocol>_<shard_comm_protocol>.csv),
# one line per broadcast_protocol. These are NOT merged across the grid
# (many-row distributions don't fit the one-row-per-file C merger), so this
# only compares the fixed reference config network_parallel.py kept.
# ----------------------------
def run_hop_cdf_overlay(hopcdf_dir: str, outdir: str, show: bool):
    if not os.path.isdir(hopcdf_dir):
        print(f"[skip] hop-cdf reference dir not found: {hopcdf_dir}")
        return

    files = sorted(f for f in os.listdir(hopcdf_dir) if f.endswith(".csv") and not f.startswith("."))
    if not files:
        print(f"[skip] no hop-cdf reference files in {hopcdf_dir}")
        return

    fig, ax = plt.subplots(figsize=(9, 6))
    plotted = 0
    for fname in files:
        label = fname[:-4]  # "<broadcast_protocol>_<shard_comm_protocol>"
        bprot = label.split("_")[0]
        path = os.path.join(hopcdf_dir, fname)
        df = pd.read_csv(path)
        if "hop_count" not in df.columns or "cumulative_fraction" not in df.columns:
            print(f"[skip] {path} missing hop_count/cumulative_fraction columns")
            continue
        df = ensure_numeric(df, ["hop_count", "cumulative_fraction"]).dropna().sort_values("hop_count")
        if df.empty:
            continue
        ax.step(df["hop_count"], df["cumulative_fraction"], where="post",
                linewidth=2, label=label, color=PROTOCOL_COLORS.get(bprot))
        plotted += 1

    if plotted == 0:
        print("[skip] no usable hop-cdf reference files to plot")
        plt.close(fig)
        return

    ax.axhline(0.5, linestyle="--", alpha=0.3, color="gray")
    ax.axhline(0.9, linestyle="--", alpha=0.3, color="gray")
    ax.set_xlabel("Hop count", fontsize=13)
    ax.set_ylabel("Fraction of network informed", fontsize=13)
    ax.set_title("Hop-Count CDF by Broadcast Protocol (reference config)", fontsize=13)
    ax.set_ylim(0, 1.02)
    ax.set_xlim(left=0)
    ax.legend(title="Protocol", fontsize=9, title_fontsize=10)
    ax.grid(True, linestyle="--", alpha=0.35)
    fig.tight_layout()

    savefig(outdir, "network_hop_cdf_overlay.png")
    print(f"[done] {os.path.join(outdir, 'network_hop_cdf_overlay.png')}")
    if show:
        plt.show()
    else:
        plt.close(fig)


# ----------------------------
# Per-block rounds/messages evolution — reads the per-block logs written by
# simulation.py's --per_block_csv (Results/network_runs/perblock/
# <broadcast_protocol>_<shard_comm_protocol>.csv), one line per broadcast_protocol.
# Not part of the merged grid results (one row per block, not per run) — shows
# how a protocol's rounds/messages change block-by-block over a single run,
# e.g. Plumtree's tree pruning down from a flood after the first few blocks.
# ----------------------------
def run_rounds_messages_per_block(perblock_dir: str, outdir: str, show: bool):
    if not os.path.isdir(perblock_dir):
        print(f"[skip] per-block dir not found: {perblock_dir}")
        return

    files = sorted(f for f in os.listdir(perblock_dir) if f.endswith(".csv") and not f.startswith("."))
    if not files:
        print(f"[skip] no per-block log files in {perblock_dir}")
        return

    series = {}
    for fname in files:
        label = fname[:-4]
        bprot = label.split("_")[0]
        path = os.path.join(perblock_dir, fname)
        df = pd.read_csv(path)
        if "block" not in df.columns or "rounds" not in df.columns or "messages" not in df.columns:
            print(f"[skip] {path} missing block/rounds/messages columns")
            continue
        df = ensure_numeric(df, ["block", "rounds", "messages"]).dropna().sort_values("block")
        if df.empty:
            continue
        series[label] = (bprot, df)

    if not series:
        print("[skip] no usable per-block log files to plot")
        return

    for metric, ylabel, log_y, out_name in [
        ("rounds", "Rounds to Inform Network", False, "network_rounds_per_block.png"),
        ("messages", "Messages", True, "network_messages_per_block.png"),
    ]:
        fig, ax = plt.subplots(figsize=(12, 6))
        for label, (bprot, df) in series.items():
            ax.plot(df["block"], df[metric], linewidth=1.5, label=label,
                    color=PROTOCOL_COLORS.get(bprot))
        ax.set_xlabel("Block number", fontsize=13)
        ax.set_ylabel(ylabel, fontsize=13)
        ax.set_title(f"{ylabel} vs Block Number (per-block, reference config)", fontsize=14)
        if log_y:
            ax.set_yscale("log")
        ax.legend(title="Protocol", fontsize=9, title_fontsize=10)
        ax.grid(True, linestyle="--", alpha=0.4)
        fig.tight_layout()

        savefig(outdir, out_name)
        print(f"[done] {os.path.join(outdir, out_name)}")
        if show:
            plt.show()
        else:
            plt.close(fig)


# ----------------------------
# Main
# ----------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Read Results/network_results_<env>.csv and write protocol-comparison plots."
    )
    ap.add_argument("--no_show", action="store_true", help="Don't show plots, only save them")
    ap.add_argument("--results_dir", default="Results", help="Folder containing the network results CSV")
    ap.add_argument("--network_csv", default="network_results_local.csv")
    ap.add_argument("--network_out", default="network_graphs", help="Output folder for protocol comparison graphs")

    ap.add_argument("--hopcdf_dir", default=None,
                    help="Folder of curated hop-cdf reference CSVs "
                         "(default: <results_dir>/network_runs/hopcdf)")
    ap.add_argument("--hopcdf_out", default=None,
                    help="Output folder for the hop-cdf overlay plot (default: same as --network_out)")

    ap.add_argument("--perblock_dir", default=None,
                    help="Folder of per-block rounds/messages logs "
                         "(default: <results_dir>/network_runs/perblock)")
    ap.add_argument("--perblock_out", default=None,
                    help="Output folder for the per-block plots (default: same as --network_out)")

    ap.add_argument("--bt_blocksize", type=int, default=None,
                    help="If set, filter blocktime-vs-shards plot to this block size")

    ap.add_argument("--skip_tps", action="store_true")
    ap.add_argument("--skip_blocktime", action="store_true")
    ap.add_argument("--skip_messages", action="store_true")
    ap.add_argument("--skip_broadcast_cpu", action="store_true")
    ap.add_argument("--skip_shard_comm", action="store_true")
    ap.add_argument("--skip_hop_cdf", action="store_true")
    ap.add_argument("--skip_rounds", action="store_true")
    ap.add_argument("--rounds_percentile", default="hop_p50",
                    choices=["hop_p50", "hop_p90", "hop_p99", "hop_max"],
                    help="Which hop-count percentile to plot as 'rounds' vs shards")
    ap.add_argument("--skip_perblock", action="store_true")

    args = ap.parse_args()
    show = not args.no_show

    network_csv = os.path.join(args.results_dir, args.network_csv)
    hopcdf_dir  = args.hopcdf_dir or os.path.join(args.results_dir, "network_runs", "hopcdf")
    hopcdf_out  = args.hopcdf_out or args.network_out
    perblock_dir = args.perblock_dir or os.path.join(args.results_dir, "network_runs", "perblock")
    perblock_out = args.perblock_out or args.network_out

    if not args.skip_tps:
        run_protocol_tps_vs_shards(network_csv, args.network_out, show=show)
    if not args.skip_blocktime:
        run_protocol_blocktime_vs_shards(network_csv, args.network_out, show=show, block_size=args.bt_blocksize)
    if not args.skip_messages:
        run_protocol_messages_vs_shards(network_csv, args.network_out, show=show)
    if not args.skip_broadcast_cpu:
        run_protocol_broadcast_cpu_vs_shards(network_csv, args.network_out, show=show)
    if not args.skip_shard_comm:
        run_shard_comm_comparison(network_csv, args.network_out, show=show)
    if not args.skip_hop_cdf:
        run_hop_cdf_overlay(hopcdf_dir, hopcdf_out, show=show)
    if not args.skip_rounds:
        run_protocol_rounds_vs_shards(network_csv, args.network_out, show=show, percentile=args.rounds_percentile)
    if not args.skip_perblock:
        run_rounds_messages_per_block(perblock_dir, perblock_out, show=show)


if __name__ == "__main__":
    main()
