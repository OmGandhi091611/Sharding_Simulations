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
import numpy as np
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

# Bumped up across the board so nothing reads as too small in the paper —
# matches graph.py's scheme.
TITLE_FS        = 18
SUPTITLE_FS     = 20
LABEL_FS        = 16
TICK_FS         = 14
LEGEND_FS       = 13
LEGEND_TITLE_FS = 14


def _load_network_csv(csv_path: str, conflict_check: str = None):
    if not os.path.exists(csv_path):
        print(f"[skip] network CSV not found: {csv_path}")
        return None

    df = pd.read_csv(csv_path, engine="python", on_bad_lines="warn")
    df.columns = [str(c).strip() for c in df.columns]

    if conflict_check is not None and "conflict_check" in df.columns:
        df = df[df["conflict_check"] == conflict_check]
        if df.empty:
            print(f"[skip] no rows with conflict_check={conflict_check} in {csv_path}")
            return None

    required = {
        "shards":       ["shards"],
        "block_size":   ["block size", "block_size", "blocksize"],
        "abt":          ["average block time", "avg block time", "avg_block_time"],
        "tps":          ["tps"],
        "messages":     ["messages"],
        "bprot":        ["broadcast_protocol"],
        "scomm":        ["shard_comm_protocol"],
        "bt_cfg":       ["blocktime in configuration file", "configured blocktime",
                          "config blocktime", "blocktime", "block time"],
    }
    cols = {k: _pick_col(df, cands) for k, cands in required.items()}
    missing = [k for k, c in cols.items() if c is None]
    if missing:
        print(f"[skip] network CSV missing required columns: {missing}")
        return None

    df = ensure_numeric(df, [cols["shards"], cols["block_size"], cols["abt"],
                              cols["tps"], cols["messages"], cols["bt_cfg"]])
    df["broadcast_cpu_seconds"] = pd.to_numeric(
        df.get("broadcast_cpu_seconds"), errors="coerce") if "broadcast_cpu_seconds" in df.columns else float("nan")
    for hop_col in ("hop_p50", "hop_p90", "hop_p99", "hop_max"):
        df[hop_col] = pd.to_numeric(
            df.get(hop_col), errors="coerce") if hop_col in df.columns else float("nan")

    df = df.dropna(subset=[cols["shards"], cols["block_size"], cols["abt"],
                            cols["tps"], cols["messages"], cols["bt_cfg"]]).copy()
    if df.empty:
        return None

    df["shards_int"]     = df[cols["shards"]].astype(int)
    df["block_size_int"] = df[cols["block_size"]].astype(int)
    df["abt_val"]        = df[cols["abt"]].astype(float)
    df["tps_val"]        = df[cols["tps"]].astype(float)
    df["messages_val"]   = df[cols["messages"]].astype(float)
    df["broadcast_protocol"]  = df[cols["bprot"]].astype(str)
    df["shard_comm_protocol"] = df[cols["scomm"]].astype(str)
    df["blocktime_cfg"]  = df[cols["bt_cfg"]].astype(float).round(6)
    return df


def _x_positions(shards_sorted):
    return {s: i * 2 for i, s in enumerate(shards_sorted)}


# ----------------------------
# Neighbors/fanout sweep — reads the CSVs written by
# Parallel_processes/fanout_neighbors_parallel.py
# (Results/fanout_neighbors_results_<env>.csv). Shards/block size/block time/
# protocols are all fixed in that sweep; the only swept axis is
# neighbors == gossip_fanout, so these plots are single-series (no
# per-protocol grouping like the shard sweep above).
# ----------------------------
def _load_fanout_csv(csv_path: str):
    if not os.path.exists(csv_path):
        print(f"[skip] fanout/neighbors CSV not found: {csv_path}")
        return None

    df = pd.read_csv(csv_path, engine="python", on_bad_lines="warn")
    df.columns = [str(c).strip() for c in df.columns]

    required = {
        "neighbors": ["neighbors"],
        "tps":       ["tps"],
        "messages":  ["messages"],
    }
    cols = {k: _pick_col(df, cands) for k, cands in required.items()}
    missing = [k for k, c in cols.items() if c is None]
    if missing:
        print(f"[skip] fanout/neighbors CSV missing required columns: {missing}")
        return None

    df = ensure_numeric(df, [cols["neighbors"], cols["tps"], cols["messages"]])
    df = df.dropna(subset=[cols["neighbors"], cols["tps"], cols["messages"]]).copy()
    if df.empty:
        return None

    df["neighbors_int"] = df[cols["neighbors"]].astype(int)
    df["tps_val"]        = df[cols["tps"]].astype(float)
    df["messages_val"]   = df[cols["messages"]].astype(float)
    return df


def _plot_metric_vs_neighbors(df, value_col: str, agg: str, ylabel: str,
                               title: str, out_name: str, outdir: str, show: bool,
                               log_y: bool = False, label_fs: int = None,
                               title_fs: int = None, tick_fs: int = None):
    label_fs = label_fs or LABEL_FS
    title_fs = title_fs or TITLE_FS
    tick_fs = tick_fs or TICK_FS

    grouped = (
        df.groupby("neighbors_int")[value_col]
        .agg(agg)
        .reset_index()
        .sort_values("neighbors_int")
    )

    all_neighbors = sorted(grouped["neighbors_int"].unique().tolist())
    x_positions = _x_positions(all_neighbors)
    x_vals = [x_positions[n] for n in grouped["neighbors_int"]]

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.plot(x_vals, grouped[value_col],
            marker="o", linewidth=2, markersize=5, color="#1f77b4")

    ax.set_xlabel("Neighbors = Gossip Fanout", fontsize=label_fs)
    ax.set_ylabel(ylabel, fontsize=label_fs)
    ax.set_title(title, fontsize=title_fs)
    ax.set_xticks([x_positions[n] for n in all_neighbors])
    ax.set_xticklabels([str(n) for n in all_neighbors], rotation=45, ha="right")
    ax.tick_params(labelsize=tick_fs)
    if log_y:
        ax.set_yscale("log")
    ax.grid(True, linestyle="--", alpha=0.4)
    fig.tight_layout()

    savefig(outdir, out_name)
    print(f"[done] {os.path.join(outdir, out_name)}")
    if show:
        plt.show()
    else:
        plt.close(fig)


def run_tps_vs_neighbors(fanout_csv: str, outdir: str, show: bool):
    df = _load_fanout_csv(fanout_csv)
    if df is None:
        print(f"[skip] tps-vs-neighbors: no usable data in {fanout_csv}")
        return
    _plot_metric_vs_neighbors(
        df, "tps_val", "mean", "Mean TPS",
        "TPS vs Neighbors / Gossip Fanout (512 shards, block size 524288)",
        "network_tps_vs_neighbors.png", outdir, show,
        label_fs=LABEL_FS + 4, title_fs=TITLE_FS + 4, tick_fs=TICK_FS + 3,
    )


def run_messages_vs_neighbors(fanout_csv: str, outdir: str, show: bool):
    df = _load_fanout_csv(fanout_csv)
    if df is None:
        print(f"[skip] messages-vs-neighbors: no usable data in {fanout_csv}")
        return
    _plot_metric_vs_neighbors(
        df, "messages_val", "mean", "Messages",
        "Messages vs Neighbors / Gossip Fanout (512 shards, block size 524288)",
        "network_messages_vs_neighbors.png", outdir, show, log_y=True,
    )


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

    ax.set_xlabel("Number of Shards", fontsize=LABEL_FS)
    ax.set_ylabel(ylabel, fontsize=LABEL_FS)
    ax.set_title(title, fontsize=TITLE_FS)
    ax.set_xticks([x_positions[s] for s in all_shards])
    ax.set_xticklabels([str(s) for s in all_shards], rotation=45, ha="right")
    ax.tick_params(labelsize=TICK_FS)
    if log_y:
        ax.set_yscale("log")
    ax.legend(title="Broadcast Protocol", fontsize=LEGEND_FS, title_fontsize=LEGEND_TITLE_FS)
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

    ax.set_xlabel("Number of Shards", fontsize=LABEL_FS)
    ax.set_ylabel(ylabel, fontsize=LABEL_FS)
    ax.set_title(title, fontsize=TITLE_FS)
    ax.set_xticks(x)
    ax.set_xticklabels([str(s) for s in all_shards], rotation=45, ha="right")
    ax.tick_params(labelsize=TICK_FS)
    if log_y:
        ax.set_yscale("log")
    ax.legend(title="Broadcast Protocol", fontsize=LEGEND_FS, title_fontsize=LEGEND_TITLE_FS)
    ax.grid(True, axis="y", linestyle="--", alpha=0.4)
    fig.tight_layout()

    savefig(outdir, out_name)
    print(f"[done] {os.path.join(outdir, out_name)}")
    if show:
        plt.show()
    else:
        plt.close(fig)


def run_protocol_tps_vs_shards(network_csv: str, outdir: str, show: bool, conflict_check: str = None):
    df = _load_network_csv(network_csv, conflict_check=conflict_check)
    if df is None:
        print(f"[skip] tps-vs-shards: no usable data in {network_csv}")
        return
    _plot_metric_by_protocol_bar(
        df, "tps_val", "max", "Max TPS",
        "Broadcast Protocol Comparison: TPS vs Number of Shards",
        "network_tps_vs_shards.png", outdir, show,
    )


def _fmt_bt(t):
    if t < 1:
        return f"{t:.2f}s"
    elif t < 10:
        return f"{t:.1f}s"
    else:
        return f"{t:.0f}s"


def _draw_tps_vs_shards_grid(plot_df, abt_at_max_shards, block_sizes, all_blocktimes,
                              x_positions, xticks, xticklabels, rows, cols,
                              suptitle, out_name, outdir, show):
    # part1 (2x2, rows>1) stays at its original size; only the 1-row
    # part2/part3 grids get the larger text/height treatment, since their
    # subplots would otherwise end up squashed into a short wide strip.
    if rows > 1:
        title_fs, suptitle_fs = TITLE_FS + 3, SUPTITLE_FS + 3
        label_fs, tick_fs = LABEL_FS + 3, TICK_FS + 3
        legend_fs = LEGEND_FS + 3
    else:
        title_fs, suptitle_fs = TITLE_FS + 7, SUPTITLE_FS + 7
        label_fs, tick_fs = LABEL_FS + 6, TICK_FS + 6
        legend_fs = LEGEND_FS + 6

    row_height = 4.6 if rows > 1 else 8.0
    fig_height = row_height * rows
    fig, axes = plt.subplots(rows, cols, figsize=(6 * cols, fig_height), sharey=False)
    axes = np.array(axes).reshape(-1)

    legend_handles = None
    legend_labels = None

    for i, bs in enumerate(block_sizes):
        ax = axes[i]
        sub = plot_df[plot_df["block_size_int"] == bs]
        if sub.empty:
            ax.axis("off")
            continue

        _half = len(all_blocktimes) // 2
        for idx, bt in enumerate(all_blocktimes):
            line_df = sub[sub["blocktime_cfg"] == bt].sort_values("shards_int")
            if line_df.empty:
                continue
            x_vals = [x_positions[s] for s in line_df["shards_int"]]
            ax.plot(
                x_vals, line_df["tps_val"],
                marker="o", linewidth=2, markersize=5,
                linestyle=":" if idx < _half else "-",
                label=_fmt_bt(abt_at_max_shards.get((bs, bt), bt)),
            )

        ax.set_title(f"Block Size = {bs:,}", fontsize=title_fs)
        ax.set_xlabel("Number of Shards", fontsize=label_fs)
        ax.set_xticks(xticks)
        ax.set_xticklabels(xticklabels, rotation=45, ha="right")
        ax.tick_params(labelsize=tick_fs)
        ax.grid(True, linestyle="--", alpha=0.35)
        ax.set_yscale("log")
        ax.margins(y=0.15)
        ax.set_ylabel("TPS", fontsize=label_fs)

        if legend_handles is None:
            legend_handles, legend_labels = ax.get_legend_handles_labels()

    for j in range(len(block_sizes), len(axes)):
        axes[j].axis("off")

    fig.suptitle(suptitle, fontsize=suptitle_fs)
    # Legend and suptitle each need a roughly constant absolute amount of
    # vertical space (inches), regardless of the grid's row count or
    # per-row height — so reserve margins as inches-of-fig_height fractions
    # rather than a fixed fraction, or taller figures end up with a big
    # empty gap above the legend.
    legend_reserve_in = 1.6 if rows > 1 else 2.1
    top_reserve_in = 0.75 if rows > 1 else 0.9
    bottom_margin = legend_reserve_in / fig_height
    top_margin = 1 - (top_reserve_in / fig_height)
    fig.tight_layout(rect=[0, bottom_margin, 1, top_margin])

    if legend_handles:
        fig.legend(
            legend_handles, legend_labels,
            loc="lower center", ncol=min(7, len(legend_labels)),
            bbox_to_anchor=(0.5, 0.0), frameon=True, fontsize=legend_fs,
        )

    safe_mkdir(outdir)
    fig.savefig(os.path.join(outdir, out_name), dpi=300, bbox_inches="tight")
    print(f"[done] {os.path.join(outdir, out_name)}")
    if show:
        plt.show()
    else:
        plt.close(fig)


def run_tps_vs_shards_per_blocksize(network_csv: str, outdir: str, show: bool, conflict_check: str = None):
    """Mirrors graph.py's run_memo_per_blocksize: split into a part1 (2x2,
    4 largest block sizes), part2 (1x3, next 3), and part3 (1x3, remaining
    3) grid — one subplot per block size, one line per configured blocktime — for the
    network (protocol-sweep) results, which carry a seed axis memo's
    already-aggregated CSV doesn't, so each (block_size, blocktime, shards)
    point is meaned over seeds first. Shows the diminishing-returns/
    peak-then-decline shape (coordination overhead outgrowing parallelism
    gains) per block size."""
    df = _load_network_csv(network_csv, conflict_check=conflict_check)
    if df is None:
        print(f"[skip] tps-vs-shards-per-blocksize: no usable data in {network_csv}")
        return

    plot_df = (
        df.groupby(["block_size_int", "blocktime_cfg", "shards_int"])["tps_val"]
        .mean()
        .reset_index()
        .sort_values(["block_size_int", "blocktime_cfg", "shards_int"])
    )

    # Actual block time at max shard count per (block_size, configured_blocktime) — used as line label
    abt_at_max_shards = (
        df.sort_values("shards_int")
        .groupby(["block_size_int", "blocktime_cfg"])["abt_val"]
        .last()
        .to_dict()
    )

    block_sizes = sorted(plot_df["block_size_int"].unique().tolist(), reverse=True)
    if not block_sizes:
        print("[skip] no valid block sizes for tps-vs-shards-per-blocksize")
        return

    all_blocktimes = sorted(plot_df["blocktime_cfg"].unique().tolist())
    all_shards = sorted(plot_df["shards_int"].unique().tolist())
    x_positions = {s: i * 2 for i, s in enumerate(all_shards)}
    xticks = [x_positions[s] for s in all_shards]
    xticklabels = [str(s) for s in all_shards]

    groups = [block_sizes[:4], block_sizes[4:7], block_sizes[7:]]
    group_names = [
        "network_tps_vs_shards_part1.png",
        "network_tps_vs_shards_part2.png",
        "network_tps_vs_shards_part3.png",
    ]
    group_grids = [(2, 2), (1, 3), (1, 3)]  # (rows, cols) for part1, part2, part3

    for group_idx, bs_group in enumerate(groups):
        bs_group = [b for b in bs_group if b in block_sizes]
        if not bs_group:
            continue
        rows, cols = group_grids[group_idx]
        _draw_tps_vs_shards_grid(
            plot_df, abt_at_max_shards, bs_group, all_blocktimes,
            x_positions, xticks, xticklabels, rows, cols,
            "Network Protocol Sweep: TPS vs Number of Shards",
            group_names[group_idx], outdir, show,
        )


def run_protocol_blocktime_vs_shards(network_csv: str, outdir: str, show: bool, block_size: int = None, conflict_check: str = None):
    df = _load_network_csv(network_csv, conflict_check=conflict_check)
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


def run_blocktime_vs_shards_per_target(network_csv: str, outdir: str, show: bool,
                                        block_size: int, conflict_check: str = None):
    """Companion to run_tps_vs_shards_per_blocksize: instead of collapsing the
    14-value target-blocktime sweep down to a single legend number per line
    (the actual block time at max shards only), this plots every target's
    full actual-block-time-vs-shards curve for one fixed block size, with a
    dotted reference line at each target's *configured* value. The solid
    curve's value at shards=512 is exactly the number that shows up as a
    legend label in the TPS-vs-shards panel for that same (block_size,
    target) pair — this plot exists to explain where every one of those
    labels comes from, not just the fastest (min) one that the common
    blocktime-vs-shards graph shows."""
    df = _load_network_csv(network_csv, conflict_check=conflict_check)
    if df is None:
        print(f"[skip] blocktime-vs-shards-per-target: no usable data in {network_csv}")
        return
    df = df[df["block_size_int"] == block_size]
    if df.empty:
        print(f"[skip] block size {block_size} not found for blocktime-vs-shards-per-target plot")
        return

    grouped = (
        df.groupby(["blocktime_cfg", "shards_int"])["abt_val"]
        .mean()
        .reset_index()
        .sort_values(["blocktime_cfg", "shards_int"])
    )

    all_targets = sorted(grouped["blocktime_cfg"].unique().tolist())
    all_shards = sorted(grouped["shards_int"].unique().tolist())
    x_positions = _x_positions(all_shards)

    # Restricted to viridis's dark-purple-to-teal range (0.0-0.55) — the
    # colormap's green/yellow tail is too light/low-contrast to read.
    cmap = plt.cm.viridis
    fig, ax = plt.subplots(figsize=(12, 7))
    for i, bt in enumerate(all_targets):
        row = grouped[grouped["blocktime_cfg"] == bt].sort_values("shards_int")
        x_vals = [x_positions[s] for s in row["shards_int"]]
        color = cmap(0.55 * i / max(1, len(all_targets) - 1))
        ax.plot(x_vals, row["abt_val"], marker="o", linewidth=2, markersize=5,
                color=color, label=_fmt_bt(bt), zorder=3)
        ax.axhline(bt, color=color, linestyle=":", linewidth=1, alpha=0.6, zorder=1)

    ax.set_xlabel("Number of Shards", fontsize=LABEL_FS)
    ax.set_ylabel("Block Time (s)", fontsize=LABEL_FS)
    ax.set_title(
        f"Actual (solid) vs Configured (dotted) Block Time vs Number of Shards "
        f"(Block Size = {block_size:,})", fontsize=TITLE_FS,
    )
    ax.set_xticks([x_positions[s] for s in all_shards])
    ax.set_xticklabels([str(s) for s in all_shards], rotation=45, ha="right")
    ax.tick_params(labelsize=TICK_FS)
    ax.set_yscale("log")
    ax.legend(title="Configured Target", fontsize=LEGEND_FS, title_fontsize=LEGEND_TITLE_FS,
              ncol=2, loc="upper right")
    ax.grid(True, linestyle="--", alpha=0.3)
    fig.tight_layout()

    out_name = f"network_blocktime_vs_shards_per_target_bs{block_size}.png"
    savefig(outdir, out_name)
    print(f"[done] {os.path.join(outdir, out_name)}")
    if show:
        plt.show()
    else:
        plt.close(fig)


def run_protocol_messages_vs_shards(network_csv: str, outdir: str, show: bool, conflict_check: str = None):
    df = _load_network_csv(network_csv, conflict_check=conflict_check)
    if df is None:
        print(f"[skip] messages-vs-shards: no usable data in {network_csv}")
        return
    _plot_metric_by_protocol_bar(
        df, "messages_val", "mean", "Messages",
        "Broadcast Protocol Comparison: Messages vs Number of Shards",
        "network_messages_vs_shards.png", outdir, show, log_y=True,
    )


def run_protocol_rounds_vs_shards(network_csv: str, outdir: str, show: bool, percentile: str = "hop_p50", conflict_check: str = None):
    df = _load_network_csv(network_csv, conflict_check=conflict_check)
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


def run_protocol_broadcast_cpu_vs_shards(network_csv: str, outdir: str, show: bool, conflict_check: str = None):
    df = _load_network_csv(network_csv, conflict_check=conflict_check)
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
                               metric: str = "abt_val", ylabel: str = "Min Actual Block Time (s)",
                               conflict_check: str = None):
    df = _load_network_csv(network_csv, conflict_check=conflict_check)
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

    ax.set_xlabel("Number of Shards", fontsize=LABEL_FS)
    ax.set_ylabel(ylabel, fontsize=LABEL_FS)
    ax.set_title(f"Shard Communication Protocol Comparison: {ylabel} vs Number of Shards", fontsize=TITLE_FS)
    ax.set_xticks([x_positions[s] for s in all_shards])
    ax.set_xticklabels([str(s) for s in all_shards], rotation=45, ha="right")
    ax.tick_params(labelsize=TICK_FS)
    ax.set_yscale("log")
    ax.legend(title="Shard Comm Protocol", fontsize=LEGEND_FS, title_fontsize=LEGEND_TITLE_FS)
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
# Common: Messages vs Shards / Messages vs Neighbors across all 3 network
# environments (Datacenter, US WAN, Global WAN) — one line per environment
# on a single graph, matching the run_common_* pattern in graph.py.
# ----------------------------

# linestyle/marker/linewidth are set to still read as 3 distinct series even
# when the underlying values coincide exactly (as with `messages`, which is
# latency-independent) — drawn thickest-to-thinnest, bottom-to-top, so each
# ring peeks out from behind the next.
_ENV_CONFIGS = [
    # label,        env,      color,      linestyle, marker, linewidth, markersize, zorder
    ("Datacenter", "local",  "#1f77b4", "-",  "o", 6.0, 12, 1),
    ("US WAN",     "usa",    "#ff7f0e", "--", "s", 3.5, 8,  2),
    ("Global WAN", "global", "#2ca02c", ":",  "^", 1.8, 5,  3),
]


def run_common_network_blocktime_vs_shards(local_csv: str, usa_csv: str, global_csv: str,
                                            outdir: str, show: bool, block_size: int = None,
                                            conflict_check: str = None):
    csv_by_env = {"local": local_csv, "usa": usa_csv, "global": global_csv}

    fig, ax = plt.subplots(figsize=(12, 6))
    all_shards = None

    for label, env, color, linestyle, marker, linewidth, markersize, zorder in _ENV_CONFIGS:
        df = _load_network_csv(csv_by_env[env], conflict_check=conflict_check)
        if df is None:
            print(f"[skip] {label}: no usable data in {csv_by_env[env]}")
            continue

        if block_size is not None:
            available = sorted(df["block_size_int"].unique().tolist())
            if block_size not in available:
                print(f"[skip] {label}: block size {block_size} not found. Available: {available}")
                continue
            df = df[df["block_size_int"] == block_size]

        agg = (
            df.groupby("shards_int")["abt_val"]
            .min()
            .reset_index()
            .sort_values("shards_int")
        )

        if all_shards is None:
            all_shards = sorted(agg["shards_int"].unique().tolist())

        x_positions = _x_positions(sorted(agg["shards_int"].unique().tolist()))
        x_vals = [x_positions[s] for s in agg["shards_int"]]

        ax.plot(x_vals, agg["abt_val"], marker=marker, linestyle=linestyle,
                linewidth=linewidth, markersize=markersize, label=label,
                color=color, zorder=zorder, markeredgecolor="white", markeredgewidth=0.6)

    if all_shards is None:
        print("[skip] common network blocktime-vs-shards graph: no data plotted")
        plt.close(fig)
        return

    x_positions_global = _x_positions(all_shards)
    xticks = [x_positions_global[s] for s in all_shards]

    title_suffix = f" (Block Size = {block_size:,})" if block_size is not None else " (All Block Sizes — Min)"
    out_name = (
        f"common_network_blocktime_vs_shards_bs{block_size}.png"
        if block_size is not None
        else "common_network_blocktime_vs_shards.png"
    )

    ax.set_xlabel("Number of Shards", fontsize=LABEL_FS)
    ax.set_ylabel("Min Actual Block Time (s)", fontsize=LABEL_FS)
    ax.set_title(f"Network Protocol Sweep: Min Block Time vs Number of Shards{title_suffix}", fontsize=TITLE_FS)
    ax.set_xticks(xticks)
    ax.set_xticklabels([str(s) for s in all_shards], rotation=45, ha="right")
    ax.tick_params(labelsize=TICK_FS)
    ax.set_yscale("log")
    ax.legend(title="Network Environment", fontsize=LEGEND_FS, title_fontsize=LEGEND_TITLE_FS)
    ax.grid(True, linestyle="--", alpha=0.4)
    fig.tight_layout()

    savefig(outdir, out_name)
    print(f"[done] {os.path.join(outdir, out_name)}")
    if show:
        plt.show()
    else:
        plt.close(fig)


def run_common_messages_vs_shards(local_csv: str, usa_csv: str, global_csv: str,
                                   outdir: str, show: bool, conflict_check: str = None):
    csv_by_env = {"local": local_csv, "usa": usa_csv, "global": global_csv}

    fig, ax = plt.subplots(figsize=(12, 6))
    all_shards = None

    for label, env, color, linestyle, marker, linewidth, markersize, zorder in _ENV_CONFIGS:
        df = _load_network_csv(csv_by_env[env], conflict_check=conflict_check)
        if df is None:
            print(f"[skip] {label}: no usable data in {csv_by_env[env]}")
            continue

        agg = (
            df.groupby("shards_int")["messages_val"]
            .mean()
            .reset_index()
            .sort_values("shards_int")
        )

        if all_shards is None:
            all_shards = sorted(agg["shards_int"].unique().tolist())

        x_positions = _x_positions(sorted(agg["shards_int"].unique().tolist()))
        x_vals = [x_positions[s] for s in agg["shards_int"]]

        ax.plot(x_vals, agg["messages_val"], marker=marker, linestyle=linestyle,
                linewidth=linewidth, markersize=markersize, label=label,
                color=color, zorder=zorder, markeredgecolor="white", markeredgewidth=0.6)

    if all_shards is None:
        print("[skip] common messages-vs-shards graph: no data plotted")
        plt.close(fig)
        return

    x_positions_global = _x_positions(all_shards)
    xticks = [x_positions_global[s] for s in all_shards]

    ax.set_xlabel("Number of Shards", fontsize=LABEL_FS)
    ax.set_ylabel("Messages", fontsize=LABEL_FS)
    ax.set_title("Messages vs Number of Shards (Datacenter vs US WAN vs Global WAN)", fontsize=TITLE_FS)
    ax.set_xticks(xticks)
    ax.set_xticklabels([str(s) for s in all_shards], rotation=45, ha="right")
    ax.tick_params(labelsize=TICK_FS)
    ax.set_yscale("log")
    ax.legend(title="Network Environment", fontsize=LEGEND_FS, title_fontsize=LEGEND_TITLE_FS)
    ax.grid(True, linestyle="--", alpha=0.4)
    fig.tight_layout()

    out_name = "common_messages_vs_shards.png"
    savefig(outdir, out_name)
    print(f"[done] {os.path.join(outdir, out_name)}")
    if show:
        plt.show()
    else:
        plt.close(fig)


def run_common_tps_vs_neighbors(local_csv: str, usa_csv: str, global_csv: str,
                                 outdir: str, show: bool):
    csv_by_env = {"local": local_csv, "usa": usa_csv, "global": global_csv}

    fig, ax = plt.subplots(figsize=(10, 6))
    all_neighbors = None

    for label, env, color, linestyle, marker, linewidth, markersize, zorder in _ENV_CONFIGS:
        df = _load_fanout_csv(csv_by_env[env])
        if df is None:
            print(f"[skip] {label}: no usable data in {csv_by_env[env]}")
            continue

        agg = (
            df.groupby("neighbors_int")["tps_val"]
            .mean()
            .reset_index()
            .sort_values("neighbors_int")
        )

        if all_neighbors is None:
            all_neighbors = sorted(agg["neighbors_int"].unique().tolist())

        x_positions = _x_positions(sorted(agg["neighbors_int"].unique().tolist()))
        x_vals = [x_positions[n] for n in agg["neighbors_int"]]

        ax.plot(x_vals, agg["tps_val"], marker="o", linestyle="-",
                linewidth=2.5, markersize=8, label=label,
                color=color, zorder=zorder, markeredgecolor="white", markeredgewidth=0.6)

    if all_neighbors is None:
        print("[skip] common tps-vs-neighbors graph: no data plotted")
        plt.close(fig)
        return

    x_positions_global = _x_positions(all_neighbors)
    xticks = [x_positions_global[n] for n in all_neighbors]

    ax.set_xlabel("Neighbors = Gossip Fanout", fontsize=LABEL_FS + 4)
    ax.set_ylabel("Mean TPS", fontsize=LABEL_FS + 4)
    ax.set_title("TPS vs Neighbors / Gossip Fanout (Datacenter vs US WAN vs Global WAN)", fontsize=TITLE_FS + 4)
    ax.set_xticks(xticks)
    ax.set_xticklabels([str(n) for n in all_neighbors], rotation=45, ha="right")
    ax.tick_params(labelsize=TICK_FS + 3)
    ax.legend(title="Network Environment", fontsize=LEGEND_FS + 4, title_fontsize=LEGEND_TITLE_FS + 4)
    ax.grid(True, linestyle="--", alpha=0.4)
    fig.tight_layout()

    out_name = "common_tps_vs_neighbors.png"
    savefig(outdir, out_name)
    print(f"[done] {os.path.join(outdir, out_name)}")
    if show:
        plt.show()
    else:
        plt.close(fig)


def run_common_messages_vs_neighbors(local_csv: str, usa_csv: str, global_csv: str,
                                      outdir: str, show: bool):
    csv_by_env = {"local": local_csv, "usa": usa_csv, "global": global_csv}

    fig, ax = plt.subplots(figsize=(10, 6))
    all_neighbors = None

    for label, env, color, linestyle, marker, linewidth, markersize, zorder in _ENV_CONFIGS:
        df = _load_fanout_csv(csv_by_env[env])
        if df is None:
            print(f"[skip] {label}: no usable data in {csv_by_env[env]}")
            continue

        agg = (
            df.groupby("neighbors_int")["messages_val"]
            .mean()
            .reset_index()
            .sort_values("neighbors_int")
        )

        if all_neighbors is None:
            all_neighbors = sorted(agg["neighbors_int"].unique().tolist())

        x_positions = _x_positions(sorted(agg["neighbors_int"].unique().tolist()))
        x_vals = [x_positions[n] for n in agg["neighbors_int"]]

        ax.plot(x_vals, agg["messages_val"], marker=marker, linestyle=linestyle,
                linewidth=linewidth, markersize=markersize, label=label,
                color=color, zorder=zorder, markeredgecolor="white", markeredgewidth=0.6)

    if all_neighbors is None:
        print("[skip] common messages-vs-neighbors graph: no data plotted")
        plt.close(fig)
        return

    x_positions_global = _x_positions(all_neighbors)
    xticks = [x_positions_global[n] for n in all_neighbors]

    ax.set_xlabel("Neighbors = Gossip Fanout", fontsize=LABEL_FS)
    ax.set_ylabel("Messages", fontsize=LABEL_FS)
    ax.set_title("Messages vs Neighbors / Gossip Fanout (Datacenter vs US WAN vs Global WAN)", fontsize=TITLE_FS)
    ax.set_xticks(xticks)
    ax.set_xticklabels([str(n) for n in all_neighbors], rotation=45, ha="right")
    ax.tick_params(labelsize=TICK_FS)
    ax.set_yscale("log")
    ax.legend(title="Network Environment", fontsize=LEGEND_FS, title_fontsize=LEGEND_TITLE_FS)
    ax.grid(True, linestyle="--", alpha=0.4)
    fig.tight_layout()

    out_name = "common_messages_vs_neighbors.png"
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
    ax.set_xlabel("Hop count", fontsize=LABEL_FS)
    ax.set_ylabel("Fraction of network informed", fontsize=LABEL_FS)
    ax.set_title("Hop-Count CDF by Broadcast Protocol (reference config)", fontsize=TITLE_FS)
    ax.set_ylim(0, 1.02)
    ax.set_xlim(left=0)
    ax.tick_params(labelsize=TICK_FS)
    ax.legend(title="Protocol", fontsize=LEGEND_FS, title_fontsize=LEGEND_TITLE_FS)
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
        ax.set_xlabel("Block number", fontsize=LABEL_FS)
        ax.set_ylabel(ylabel, fontsize=LABEL_FS)
        ax.set_title(f"{ylabel} vs Block Number (per-block, reference config)", fontsize=TITLE_FS)
        ax.tick_params(labelsize=TICK_FS)
        if log_y:
            ax.set_yscale("log")
        ax.legend(title="Protocol", fontsize=LEGEND_FS, title_fontsize=LEGEND_TITLE_FS)
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
    ap.add_argument("--bt_per_target_blocksize", type=int, default=524288,
                    help="Block size for the per-target actual-vs-configured "
                         "blocktime-vs-shards plot (one line per configured "
                         "target, not collapsed to the min) — explains every "
                         "legend label in the TPS-vs-shards-per-blocksize panel "
                         "for that block size, not just the fastest one")
    ap.add_argument("--skip_blocktime_per_target", action="store_true")
    ap.add_argument("--conflict_check", default="nonce_hash_set",
                    help="Filter all protocol-comparison plots to this conflict_check method "
                         "(the CSV rows contain both nonce_hash_set and radix_sort runs per "
                         "shards/block_size; set to '' / none to disable and average across both)")

    ap.add_argument("--skip_tps", action="store_true")
    ap.add_argument("--skip_tps_per_blocksize", action="store_true")
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

    ap.add_argument("--fanout_csv", default="fanout_neighbors_results_local.csv",
                    help="CSV written by Parallel_processes/fanout_neighbors_parallel.py")
    ap.add_argument("--fanout_out", default=None,
                    help="Output folder for the neighbors/fanout plots (default: same as --network_out)")
    ap.add_argument("--skip_fanout_tps", action="store_true")
    ap.add_argument("--skip_fanout_messages", action="store_true")

    ap.add_argument("--common_out", default="common_graphs",
                    help="Output folder for cross-environment (Datacenter/US WAN/Global WAN) comparison graphs")
    ap.add_argument("--network_local_csv", default="network_results_local.csv")
    ap.add_argument("--network_usa_csv", default="network_results_usa.csv")
    ap.add_argument("--network_global_csv", default="network_results_global.csv")
    ap.add_argument("--fanout_local_csv", default="fanout_neighbors_results_local.csv")
    ap.add_argument("--fanout_usa_csv", default="fanout_neighbors_results_usa.csv")
    ap.add_argument("--fanout_global_csv", default="fanout_neighbors_results_global.csv")
    ap.add_argument("--skip_common_messages_shards", action="store_true")
    ap.add_argument("--skip_common_messages_neighbors", action="store_true")
    ap.add_argument("--skip_common_tps_neighbors", action="store_true")
    ap.add_argument("--skip_common_network_blocktime", action="store_true")
    ap.add_argument("--common_network_bt_blocksize", type=int, default=None,
                    help="If set, filter common network blocktime-vs-shards graph to this block size")

    args = ap.parse_args()
    show = not args.no_show
    conflict_check = args.conflict_check or None

    network_csv = os.path.join(args.results_dir, args.network_csv)
    hopcdf_dir  = args.hopcdf_dir or os.path.join(args.results_dir, "network_runs", "hopcdf")
    hopcdf_out  = args.hopcdf_out or args.network_out
    perblock_dir = args.perblock_dir or os.path.join(args.results_dir, "network_runs", "perblock")
    perblock_out = args.perblock_out or args.network_out

    if not args.skip_tps:
        run_protocol_tps_vs_shards(network_csv, args.network_out, show=show, conflict_check=conflict_check)
    if not args.skip_tps_per_blocksize:
        run_tps_vs_shards_per_blocksize(network_csv, args.network_out, show=show, conflict_check=conflict_check)
    if not args.skip_blocktime:
        run_protocol_blocktime_vs_shards(network_csv, args.network_out, show=show, block_size=args.bt_blocksize, conflict_check=conflict_check)
    if not args.skip_blocktime_per_target:
        run_blocktime_vs_shards_per_target(network_csv, args.network_out, show=show, block_size=args.bt_per_target_blocksize, conflict_check=conflict_check)
    if not args.skip_messages:
        run_protocol_messages_vs_shards(network_csv, args.network_out, show=show, conflict_check=conflict_check)
    if not args.skip_broadcast_cpu:
        run_protocol_broadcast_cpu_vs_shards(network_csv, args.network_out, show=show, conflict_check=conflict_check)
    if not args.skip_shard_comm:
        run_shard_comm_comparison(network_csv, args.network_out, show=show, conflict_check=conflict_check)
    if not args.skip_hop_cdf:
        run_hop_cdf_overlay(hopcdf_dir, hopcdf_out, show=show)
    if not args.skip_rounds:
        run_protocol_rounds_vs_shards(network_csv, args.network_out, show=show, percentile=args.rounds_percentile, conflict_check=conflict_check)
    if not args.skip_perblock:
        run_rounds_messages_per_block(perblock_dir, perblock_out, show=show)

    fanout_csv = os.path.join(args.results_dir, args.fanout_csv)
    fanout_out = args.fanout_out or args.network_out
    if not args.skip_fanout_tps:
        run_tps_vs_neighbors(fanout_csv, fanout_out, show=show)
    if not args.skip_fanout_messages:
        run_messages_vs_neighbors(fanout_csv, fanout_out, show=show)

    if not args.skip_common_network_blocktime:
        run_common_network_blocktime_vs_shards(
            os.path.join(args.results_dir, args.network_local_csv),
            os.path.join(args.results_dir, args.network_usa_csv),
            os.path.join(args.results_dir, args.network_global_csv),
            outdir=args.common_out, show=show,
            block_size=args.common_network_bt_blocksize, conflict_check=conflict_check,
        )
    if not args.skip_common_messages_shards:
        run_common_messages_vs_shards(
            os.path.join(args.results_dir, args.network_local_csv),
            os.path.join(args.results_dir, args.network_usa_csv),
            os.path.join(args.results_dir, args.network_global_csv),
            outdir=args.common_out, show=show, conflict_check=conflict_check,
        )
    if not args.skip_common_messages_neighbors:
        run_common_messages_vs_neighbors(
            os.path.join(args.results_dir, args.fanout_local_csv),
            os.path.join(args.results_dir, args.fanout_usa_csv),
            os.path.join(args.results_dir, args.fanout_global_csv),
            outdir=args.common_out, show=show,
        )
    if not args.skip_common_tps_neighbors:
        run_common_tps_vs_neighbors(
            os.path.join(args.results_dir, args.fanout_local_csv),
            os.path.join(args.results_dir, args.fanout_usa_csv),
            os.path.join(args.results_dir, args.fanout_global_csv),
            outdir=args.common_out, show=show,
        )


if __name__ == "__main__":
    main()
