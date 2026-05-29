#!/usr/bin/env python3
"""
make_graphs.py

Reads CSVs from Results/ (same level as graph folders) and writes plots to:
  - near_graphs/
  - memo_graphs/
  - non_sharded_graphs/
  - Validations/

No cross_compare_graphs. No extra "plots/" folder.

Update:
- Bubble chart labels ONLY TPS for the 4 corner bubbles.
- New sharded design graphs now use:
    x = number of shards (uniform spacing, 2 units apart)
    y = TPS
    part1: 2x2 grid (4 largest block sizes)
    part2: 2x3 grid (remaining block sizes)
    one line per configured blocktime
- New sharded design uses unique TPS values directly from CSV, no averaging.
"""

import argparse
import os
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as mtick


# ----------------------------
# Utilities
# ----------------------------
def safe_mkdir(path: str):
    os.makedirs(path, exist_ok=True)


def ensure_lower_currency(df: pd.DataFrame) -> pd.DataFrame:
    if "currency" in df.columns:
        df["currency"] = df["currency"].astype(str).str.lower()
    return df


def ensure_numeric(df: pd.DataFrame, cols) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def savefig(outdir: str, name: str, dpi: int = 300) -> str:
    safe_mkdir(outdir)
    path = os.path.join(outdir, name)
    plt.tight_layout()
    plt.savefig(path, dpi=dpi, bbox_inches="tight")
    return path


def _pick_col(df, candidates):
    cols = {str(c).strip().lower(): c for c in df.columns}
    for c in candidates:
        if c.lower() in cols:
            return cols[c.lower()]
    return None


# ----------------------------
# 1) Validation graphs -> Validations/
# ----------------------------
def run_validation(validation_csv: str, outdir: str, show: bool):
    if not validation_csv or not os.path.exists(validation_csv):
        print(f"[skip] Validation CSV not found: {validation_csv}")
        return

    df = pd.read_csv(validation_csv)
    df = ensure_lower_currency(df)
    ensure_numeric(df, ["average block time", "block size", "messages", "tps", "no. of blocks generated"])

    agg = (
        df.groupby("currency", as_index=False)
          .agg(
              sim_avg_block_time=("average block time", "mean"),
              sim_tps=("tps", "mean"),
              sim_messages=("messages", "mean"),
          )
    )

    real_world = {
        "btc":  {"name": "Bitcoin",      "tps": 7.0,   "avg_block_time": 600.0},
        "bch":  {"name": "Bitcoin Cash", "tps": 200.0, "avg_block_time": 600.0},
        "ltc":  {"name": "Litecoin",     "tps": 28.0,  "avg_block_time": 150.0},
        "doge": {"name": "Dogecoin",     "tps": 40.0,  "avg_block_time": 60.0},
    }

    def attach_real(row):
        c = row["currency"]
        info = real_world.get(c, {})
        row["name"] = info.get("name", c.upper())
        row["real_tps"] = info.get("tps", np.nan)
        row["real_avg_block_time"] = info.get("avg_block_time", np.nan)
        return row

    comparison = agg.apply(attach_real, axis=1)

    labels = comparison["name"].tolist()
    x = np.arange(len(labels))
    width = 0.35

    plt.figure(figsize=(7, 4))
    plt.bar(x - width / 2, comparison["sim_tps"].tolist(), width, label="Simulated TPS")
    plt.bar(x + width / 2, comparison["real_tps"].tolist(), width, label="Real-world TPS")
    plt.xticks(x, labels)
    plt.ylabel("Transactions per second")
    plt.title("TPS: Simulator vs Real-world")
    plt.grid(axis="y", linestyle="--", alpha=0.4)
    plt.legend()
    savefig(outdir, "validation_tps_comparison.png")
    if show:
        plt.show()
    else:
        plt.close()

    plt.figure(figsize=(7, 4))
    plt.bar(x - width / 2, comparison["sim_avg_block_time"].tolist(), width, label="Simulated Avg Block Time")
    plt.bar(x + width / 2, comparison["real_avg_block_time"].tolist(), width, label="Real-world Avg Block Time")
    plt.xticks(x, labels)
    plt.ylabel("Average block time (seconds)")
    plt.title("Block Time: Simulator vs Real-world")
    plt.grid(axis="y", linestyle="--", alpha=0.4)
    plt.legend()
    savefig(outdir, "validation_blocktime_comparison.png")
    if show:
        plt.show()
    else:
        plt.close()


# ----------------------------
# 2) Heatmap -> non_sharded_graphs/
#    X = block size, Y = configured blocktime, color = log10(TPS), inferno colormap
# ----------------------------
def run_bubble_nonsharded_vs_memo_s1(non_csv: str, memo_csv: str, outdir: str, show: bool):
    if not os.path.exists(non_csv):
        print(f"[skip] non-sharded CSV not found: {non_csv}")
        return

    df = pd.read_csv(non_csv, engine="python", on_bad_lines="warn")
    df.columns = [str(c).strip().lower() for c in df.columns]

    def pick_col(candidates):
        for c in candidates:
            if c in df.columns:
                return c
        return None

    c_currency = pick_col(["currency"])
    c_bs       = pick_col(["block size", "block_size", "blocksize"])
    c_abt      = pick_col(["average block time", "avg block time", "avg_block_time"])
    c_bt_cfg   = pick_col(["blocktime in configuration file", "configured blocktime", "blocktime"])
    c_tps      = pick_col(["tps"])

    missing = [n for n, c in [("currency", c_currency), ("block size", c_bs),
                               ("average block time", c_abt), ("blocktime in configuration file", c_bt_cfg),
                               ("tps", c_tps)] if c is None]
    if missing:
        print(f"[skip] non-sharded CSV missing required columns: {missing}")
        print("Columns seen:", df.columns.tolist())
        return

    df[c_currency] = (
        df[c_currency].astype(str)
        .str.replace("\ufeff", "", regex=False)
        .str.lower().str.strip()
    )
    df = df[df[c_currency].eq("btc_hypothetical")].copy()
    if df.empty:
        print("[skip] non-sharded CSV has no rows for btc_hypothetical")
        return

    for c in [c_bs, c_abt, c_bt_cfg, c_tps]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=[c_bs, c_abt, c_bt_cfg, c_tps])
    if df.empty:
        print("[skip] BTC heatmap has no usable rows after numeric coercion")
        return

    df["bs"]      = df[c_bs].astype(float).round().astype(int)
    df["abt"]     = df[c_abt].astype(float)
    df["tps_val"] = df[c_tps].astype(float)

    # Y-axis bucketed to nearest 30-second interval
    df["abt_key"] = ((df["abt"] / 30).round() * 30).astype(int)

    block_sizes = sorted(df["bs"].unique().tolist())
    abt_keys    = sorted(df["abt_key"].unique().tolist())

    pivot_tps = df.pivot_table(index="abt_key", columns="bs", values="tps_val", aggfunc="mean")
    pivot_abt = df.pivot_table(index="abt_key", columns="bs", values="abt",     aggfunc="mean")
    pivot_tps = pivot_tps.reindex(index=abt_keys, columns=block_sizes)
    pivot_abt = pivot_abt.reindex(index=abt_keys, columns=block_sizes)

    tps_vals = pivot_tps.values.astype(float)
    masked   = np.ma.masked_invalid(tps_vals)

    n_y, n_x = len(abt_keys), len(block_sizes)
    x_edges  = np.arange(n_x + 1) - 0.5
    y_edges  = np.arange(n_y + 1) - 0.5

    # Light cyan (0.25) → dark red (0.9): no white anywhere, dark at the high-TPS top
    _turbo_colors = plt.cm.turbo(np.linspace(0.25, 0.9, 256))
    cmap = plt.matplotlib.colors.LinearSegmentedColormap.from_list("turbo_clipped", _turbo_colors)
    cmap.set_bad("black")

    fig, ax = plt.subplots(figsize=(14, max(10, n_y * 0.9)))
    ax.set_facecolor("black")

    im = ax.pcolormesh(x_edges, y_edges, masked, cmap=cmap, shading="flat")

    ax.set_xlim(x_edges[0], x_edges[-1])
    ax.set_ylim(y_edges[0], y_edges[-1])

    ax.set_xticks(range(n_x))
    ax.set_xticklabels([str(bs) for bs in block_sizes], rotation=45, ha="right", fontsize=16)
    ax.set_yticks(range(n_y))
    ax.set_yticklabels([f"{k}s" for k in abt_keys], fontsize=16)

    for i in range(n_y):
        for j in range(n_x):
            tps_val = tps_vals[i, j]
            if np.isnan(tps_val):
                continue
            tps_str = f"{tps_val:.0f}" if tps_val < 1000 else f"{tps_val/1000:.1f}k"
            ax.text(j, i, tps_str, ha="center", va="center",
                    fontsize=12, color="white", fontweight="bold")

    ax.set_xlabel("Block Size (tx/block)", fontsize=17)
    ax.set_ylabel("Actual Average Block Time (s)", fontsize=17)
    ax.set_title("TPS Heatmap — BTC Hypothetical (Non-Sharded)", fontsize=18)

    cbar = plt.colorbar(im, ax=ax)
    cbar.ax.tick_params(labelsize=13)
    cbar.set_label("TPS", fontsize=17)

    savefig(outdir, "heatmap_tps_blocktime_vs_blocksize_btc_hypothetical.png")
    if show:
        plt.show()
    else:
        plt.close()


# ----------------------------
# 3) New sharded design faceted TPS-vs-shards -> memo_graphs/
#    part1: 2x2 grid (4 largest block sizes)
#    part2: 2x3 grid (remaining block sizes)
#    one line per configured blocktime
# ----------------------------
def run_memo_per_blocksize(memo_csv: str, outdir: str, show: bool):
    if not os.path.exists(memo_csv):
        print(f"[skip] memo CSV not found: {memo_csv}")
        return

    df = pd.read_csv(memo_csv, engine="python", on_bad_lines="warn")
    df.columns = [str(c).strip() for c in df.columns]

    c_shards = _pick_col(df, ["shards"])
    c_bs = _pick_col(df, ["block size", "block_size", "blocksize"])
    c_tps = _pick_col(df, ["tps"])
    c_abt = _pick_col(df, ["average block time", "avg block time", "avg_block_time"])
    c_bt_cfg = _pick_col(df, [
        "blocktime in configuration file",
        "configured blocktime",
        "config blocktime",
        "blocktime",
        "block time"
    ])

    missing = []
    if c_shards is None:
        missing.append("shards")
    if c_bs is None:
        missing.append("block size")
    if c_tps is None:
        missing.append("tps")
    if c_bt_cfg is None:
        missing.append("blocktime in configuration file")

    if missing:
        print(f"[skip] memo CSV missing required columns: {missing}")
        print("Columns seen:", df.columns.tolist())
        return

    for c in [c_shards, c_bs, c_tps, c_bt_cfg]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if c_abt:
        df[c_abt] = pd.to_numeric(df[c_abt], errors="coerce")

    df = df.dropna(subset=[c_shards, c_bs, c_tps, c_bt_cfg]).copy()
    if df.empty:
        print("[skip] memo CSV has no usable rows after numeric coercion")
        return

    df["shards_int"] = df[c_shards].astype(int)
    df["block_size_int"] = df[c_bs].astype(int)
    df["blocktime_cfg"] = df[c_bt_cfg].astype(float).round(6)
    df["tps_val"] = df[c_tps].astype(float)
    df["abt_val"] = df[c_abt].astype(float) if c_abt else df["blocktime_cfg"]

    # Actual block time at max shard count per (block_size, configured_blocktime) — used as line label
    abt_at_max_shards = (
        df.sort_values("shards_int")
        .groupby(["block_size_int", "blocktime_cfg"])["abt_val"]
        .last()
        .to_dict()
    )

    def _fmt_bt(t):
        if t < 1:
            return f"{t:.2f}s"
        elif t < 10:
            return f"{t:.1f}s"
        else:
            return f"{t:.0f}s"

    dup_count = df.duplicated(subset=["block_size_int", "blocktime_cfg", "shards_int"]).sum()
    if dup_count > 0:
        print(f"[warn] Found {dup_count} duplicate new sharded design config rows; keeping first occurrence")

    plot_df = df[["block_size_int", "blocktime_cfg", "shards_int", "tps_val"]].copy()
    plot_df = plot_df.drop_duplicates(
        subset=["block_size_int", "blocktime_cfg", "shards_int"],
        keep="first"
    )
    plot_df = plot_df.sort_values(["block_size_int", "blocktime_cfg", "shards_int"])

    block_sizes = sorted(plot_df["block_size_int"].unique().tolist(), reverse=True)
    if not block_sizes:
        print("[skip] memo CSV has no valid block sizes")
        return

    all_blocktimes = sorted(plot_df["blocktime_cfg"].unique().tolist())
    all_shards = sorted(plot_df["shards_int"].unique().tolist())

    # Uniform spacing for shard counts
    x_positions = {s: i * 2 for i, s in enumerate(all_shards)}
    xticks = [x_positions[s] for s in all_shards]
    xticklabels = [str(s) for s in all_shards]

    # ---------- Part 1 / Part 2 figures ----------
    groups = [block_sizes[:4], block_sizes[4:]]
    group_names = ["memo_tps_vs_shards_part1.png", "memo_tps_vs_shards_part2.png"]
    group_grids = [(2, 2), (2, 3)]  # (rows, cols) for part1 and part2

    for group_idx, bs_group in enumerate(groups):
        bs_group = [b for b in bs_group if b in block_sizes]
        if not bs_group:
            continue

        rows, cols = group_grids[group_idx]
        fig, axes = plt.subplots(rows, cols, figsize=(5 * cols, 4.2 * rows), sharey=False)
        axes = np.array(axes).reshape(-1)

        legend_handles = None
        legend_labels = None

        for i, bs in enumerate(bs_group):
            ax = axes[i]
            sub = plot_df[plot_df["block_size_int"] == bs].copy()
            if sub.empty:
                ax.axis("off")
                continue

            _half = len(all_blocktimes) // 2
            for _idx, bt in enumerate(all_blocktimes):
                line_df = sub[sub["blocktime_cfg"] == bt].sort_values("shards_int")
                if line_df.empty:
                    continue

                x_vals = [x_positions[s] for s in line_df["shards_int"]]
                ax.plot(
                    x_vals,
                    line_df["tps_val"],
                    marker="o",
                    linewidth=1.8,
                    markersize=4,
                    linestyle=":" if _idx < _half else "-",
                    label=_fmt_bt(abt_at_max_shards.get((bs, bt), bt))
                )

            ax.set_title(f"Block Size = {bs:,}")
            ax.set_xlabel("Number of Shards")
            ax.set_xticks(xticks)
            ax.set_xticklabels(xticklabels)
            ax.grid(True, linestyle="--", alpha=0.35)

            if i % cols == 0:
                ax.set_ylabel("TPS")

            if legend_handles is None:
                legend_handles, legend_labels = ax.get_legend_handles_labels()

        # Hide unused axes
        for j in range(len(bs_group), len(axes)):
            axes[j].axis("off")

        fig.suptitle("New Sharded Design: TPS vs Number of Shards", fontsize=13)

        # Reserve space at bottom for legend, top for suptitle
        fig.tight_layout(rect=[0, 0.10, 1, 0.95])

        if legend_handles:
            fig.legend(
                legend_handles,
                legend_labels,
                loc="lower center",
                ncol=min(6, len(legend_labels)),
                bbox_to_anchor=(0.5, 0.01),
                frameon=True,
                fontsize=8
            )

        safe_mkdir(outdir)
        fig.savefig(os.path.join(outdir, group_names[group_idx]), dpi=300, bbox_inches="tight")

        if show:
            plt.show()
        else:
            plt.close(fig)

    # ---------- Full all-blocksizes figure ----------
    n_all = len(block_sizes)
    if n_all > 0:
        cols = 3
        rows = int(np.ceil(n_all / cols))
        fig, axes = plt.subplots(rows, cols, figsize=(5 * cols, 3.8 * rows), sharey=True)
        axes = np.array(axes).reshape(-1)

        legend_handles = None
        legend_labels = None

        for i, bs in enumerate(block_sizes):
            ax = axes[i]
            sub = plot_df[plot_df["block_size_int"] == bs].copy()
            if sub.empty:
                ax.axis("off")
                continue

            _half = len(all_blocktimes) // 2
            for _idx, bt in enumerate(all_blocktimes):
                line_df = sub[sub["blocktime_cfg"] == bt].sort_values("shards_int")
                if line_df.empty:
                    continue

                x_vals = [x_positions[s] for s in line_df["shards_int"]]
                ax.plot(
                    x_vals,
                    line_df["tps_val"],
                    marker="o",
                    linewidth=1.5,
                    markersize=3.5,
                    linestyle=":" if _idx < _half else "-",
                    label=_fmt_bt(abt_at_max_shards.get((bs, bt), bt))
                )

            ax.set_title(f"BS={bs}")
            ax.set_xlabel("Shards")
            ax.set_xticks(xticks)
            ax.set_xticklabels(xticklabels)
            ax.grid(True, linestyle="--", alpha=0.35)
            ax.set_yscale("log")
            ax.set_ylim(bottom=1e-1)

            if i % cols == 0:
                ax.set_ylabel("TPS")

            if legend_handles is None:
                legend_handles, legend_labels = ax.get_legend_handles_labels()

        for j in range(n_all, len(axes)):
            axes[j].axis("off")

        if legend_handles:
            fig.legend(
                legend_handles,
                legend_labels,
                loc="upper center",
                ncol=min(4, len(legend_labels)),
                bbox_to_anchor=(0.5, 1.02),
                frameon=True
            )

        fig.suptitle("New Sharded Design: TPS vs Number of Shards by Block Size and Blocktime", y=1.06, fontsize=13)
        savefig(outdir, "memo_tps_vs_shards_all_blocksizes.png")

        if show:
            plt.show()
        else:
            plt.close(fig)


# ----------------------------
# 4b) New sharded design Messages vs Shards -> memo_msg_graphs/
# ----------------------------
def run_memo_messages_vs_shards(memo_csv: str, outdir: str, show: bool):
    if not os.path.exists(memo_csv):
        print(f"[skip] memo CSV not found: {memo_csv}")
        return

    df = pd.read_csv(memo_csv, engine="python", on_bad_lines="warn")
    df.columns = [str(c).strip() for c in df.columns]

    c_shards = _pick_col(df, ["shards"])
    c_bs     = _pick_col(df, ["block size", "block_size", "blocksize"])
    c_msgs   = _pick_col(df, ["messages"])
    c_bt_cfg = _pick_col(df, [
        "blocktime in configuration file",
        "configured blocktime",
        "config blocktime",
        "blocktime",
        "block time",
    ])

    missing = [n for n, c in [("shards", c_shards), ("block size", c_bs),
                               ("messages", c_msgs), ("blocktime in configuration file", c_bt_cfg)]
               if c is None]
    if missing:
        print(f"[skip] memo CSV missing required columns for messages plot: {missing}")
        return

    for c in [c_shards, c_bs, c_msgs, c_bt_cfg]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=[c_shards, c_bs, c_msgs, c_bt_cfg]).copy()
    if df.empty:
        print("[skip] memo CSV has no usable rows for messages plot")
        return

    c_abt = _pick_col(df, ["average block time", "avg block time", "avg_block_time"])
    if c_abt:
        df[c_abt] = pd.to_numeric(df[c_abt], errors="coerce")

    df["shards_int"]     = df[c_shards].astype(int)
    df["block_size_int"] = df[c_bs].astype(int)
    df["blocktime_cfg"]  = df[c_bt_cfg].astype(float).round(6)
    df["messages_val"]   = df[c_msgs].astype(float)
    df["abt_val"]        = df[c_abt].astype(float) if c_abt else df["blocktime_cfg"]

    abt_at_max_shards = (
        df.sort_values("shards_int")
        .groupby(["block_size_int", "blocktime_cfg"])["abt_val"]
        .last()
        .to_dict()
    )

    def _fmt_bt(t):
        if t < 1:
            return f"{t:.2f}s"
        elif t < 10:
            return f"{t:.1f}s"
        else:
            return f"{t:.0f}s"

    plot_df = (
        df[["block_size_int", "blocktime_cfg", "shards_int", "messages_val"]]
        .drop_duplicates(subset=["block_size_int", "blocktime_cfg", "shards_int"], keep="first")
        .sort_values(["block_size_int", "blocktime_cfg", "shards_int"])
    )

    block_sizes    = sorted(plot_df["block_size_int"].unique().tolist(), reverse=True)
    all_blocktimes = sorted(plot_df["blocktime_cfg"].unique().tolist())
    all_shards     = sorted(plot_df["shards_int"].unique().tolist())

    x_positions  = {s: i * 2 for i, s in enumerate(all_shards)}
    xticks       = [x_positions[s] for s in all_shards]
    xticklabels  = [str(s) for s in all_shards]

    groups      = [block_sizes[:4], block_sizes[4:]]
    group_names = ["memo_messages_vs_shards_part1.png", "memo_messages_vs_shards_part2.png"]
    group_grids = [(2, 2), (2, 3)]

    for group_idx, bs_group in enumerate(groups):
        bs_group = [b for b in bs_group if b in block_sizes]
        if not bs_group:
            continue

        rows, cols = group_grids[group_idx]
        fig, axes = plt.subplots(rows, cols, figsize=(5 * cols, 4.2 * rows), sharey=False)
        axes = np.array(axes).reshape(-1)

        legend_handles = legend_labels = None

        for i, bs in enumerate(bs_group):
            ax  = axes[i]
            sub = plot_df[plot_df["block_size_int"] == bs].copy()
            if sub.empty:
                ax.axis("off")
                continue

            _half = len(all_blocktimes) // 2
            for _idx, bt in enumerate(all_blocktimes):
                line_df = sub[sub["blocktime_cfg"] == bt].sort_values("shards_int")
                if line_df.empty:
                    continue
                x_vals = [x_positions[s] for s in line_df["shards_int"]]
                ax.plot(x_vals, line_df["messages_val"],
                        marker="o", linewidth=1.8, markersize=4,
                        linestyle=":" if _idx < _half else "-",
                        label=_fmt_bt(abt_at_max_shards.get((bs, bt), bt)))

            ax.set_title(f"Block Size = {bs:,}")
            ax.set_xlabel("Number of Shards")
            ax.set_xticks(xticks)
            ax.set_xticklabels(xticklabels)
            ax.grid(True, linestyle="--", alpha=0.35)

            if i % cols == 0:
                ax.set_ylabel("Messages")

            if legend_handles is None:
                legend_handles, legend_labels = ax.get_legend_handles_labels()

        for j in range(len(bs_group), len(axes)):
            axes[j].axis("off")

        fig.suptitle("New Sharded Design: Messages vs Number of Shards", fontsize=13)
        fig.tight_layout(rect=[0, 0.10, 1, 0.95])

        if legend_handles:
            fig.legend(legend_handles, legend_labels,
                       loc="lower center",
                       ncol=min(6, len(legend_labels)),
                       bbox_to_anchor=(0.5, 0.01),
                       frameon=True, fontsize=8)

        safe_mkdir(outdir)
        fig.savefig(os.path.join(outdir, group_names[group_idx]), dpi=300, bbox_inches="tight")

        if show:
            plt.show()
        else:
            plt.close(fig)

        print(f"[done] {outdir}/{group_names[group_idx]}")


# ----------------------------
# 4) NEAR vs targets -> near_graphs/
# ----------------------------
def run_near_vs_targets(near_csv: str, outdir: str, show: bool):
    if not os.path.exists(near_csv):
        print(f"[skip] Near CSV not found: {near_csv}")
        return

    df = pd.read_csv(near_csv)
    df = ensure_lower_currency(df)
    ensure_numeric(df, ["average block time", "block size", "messages", "tps", "no. of blocks generated", "shards"])

    df_near = df[df["currency"] == "near"].copy()
    if df_near.empty:
        print("[skip] No rows with currency == near found in Near.csv")
        return

    df_near = df_near.sort_values("shards")

    near_targets = {
        4: {"tps": 3000.0, "avg_block_time": 0.6},
        6: {"tps": 3500.0, "avg_block_time": 0.6},
        9: {"tps": 4000.0, "avg_block_time": 0.6},
    }

    def attach_near_real(row):
        s = int(row["shards"])
        info = near_targets.get(s, {})
        row["real_tps"] = info.get("tps", np.nan)
        row["real_avg_block_time"] = info.get("avg_block_time", np.nan)
        return row

    df_near = df_near.apply(attach_near_real, axis=1)

    labels = [f"{int(s)} shards" for s in df_near["shards"]]
    x = np.arange(len(labels))
    width = 0.35

    plt.figure(figsize=(7, 4))
    plt.bar(x - width / 2, df_near["tps"].tolist(), width, label="Simulated TPS")
    plt.bar(x + width / 2, df_near["real_tps"].tolist(), width, label="Target NEAR TPS")
    plt.xticks(x, labels)
    plt.ylabel("Transactions per second")
    plt.title("NEAR TPS: Simulator vs Target (by shard count)")
    plt.grid(axis="y", linestyle="--", alpha=0.4)
    plt.legend()
    savefig(outdir, "near_tps_comparison.png")
    if show:
        plt.show()
    else:
        plt.close()

    plt.figure(figsize=(7, 4))
    plt.bar(x - width / 2, df_near["average block time"].tolist(), width, label="Simulated Avg Block Time")
    plt.bar(x + width / 2, df_near["real_avg_block_time"].tolist(), width, label="Target NEAR Block Time")
    plt.xticks(x, labels)
    plt.ylabel("Average block time (seconds)")
    plt.title("NEAR Block Time: Simulator vs Target (by shard count)")
    plt.grid(axis="y", linestyle="--", alpha=0.4)
    plt.legend()
    savefig(outdir, "near_blocktime_comparison.png")
    if show:
        plt.show()
    else:
        plt.close()


# ----------------------------
# Main
# ----------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Read CSVs from Results/ and write plots to near_graphs/, memo_graphs/, non_sharded_graphs/, Validations/."
    )
    ap.add_argument("--no_show", action="store_true", help="Don't show plots, only save them")
    ap.add_argument("--results_dir", default="Results", help="Folder containing the CSVs")

    ap.add_argument("--near_out", default="near_graphs", help="Output folder for NEAR graphs")
    ap.add_argument("--memo_out", default="memo_graphs", help="Output folder for new sharded design graphs")
    ap.add_argument("--memo_msg_out", default="memo_msg_graphs", help="Output folder for new sharded design messages vs shards graphs")
    ap.add_argument("--non_out", default="non_sharded_graphs", help="Output folder for non-sharded graphs")
    ap.add_argument("--val_out", default="Validations", help="Output folder for validation graphs")

    ap.add_argument("--near_csv", default="Near.csv")
    ap.add_argument("--memo_csv", default="memo_results.csv")
    ap.add_argument("--non_csv", default="non-sharded.csv")
    ap.add_argument("--validation_csv", default="Validation.csv")

    ap.add_argument("--skip_near", action="store_true")
    ap.add_argument("--skip_memo", action="store_true")
    ap.add_argument("--skip_memo_msg", action="store_true")
    ap.add_argument("--skip_non", action="store_true")
    ap.add_argument("--skip_validation", action="store_true")
    ap.add_argument("--skip_sig_schemes", action="store_true",
                    help="Skip per-signature-scheme plot generation")

    args = ap.parse_args()
    show = not args.no_show

    near_csv = os.path.join(args.results_dir, args.near_csv)
    memo_csv = os.path.join(args.results_dir, args.memo_csv)
    non_csv  = os.path.join(args.results_dir, args.non_csv)
    val_csv  = os.path.join(args.results_dir, args.validation_csv)

    safe_mkdir(args.near_out)
    safe_mkdir(args.memo_out)
    safe_mkdir(args.memo_msg_out)
    safe_mkdir(args.non_out)
    safe_mkdir(args.val_out)

    if not args.skip_near:
        run_near_vs_targets(near_csv, args.near_out, show=show)

    if not args.skip_memo:
        run_memo_per_blocksize(memo_csv, args.memo_out, show=show)

    if not args.skip_memo_msg:
        run_memo_messages_vs_shards(memo_csv, args.memo_msg_out, show=show)

    if not args.skip_non:
        run_bubble_nonsharded_vs_memo_s1(non_csv, memo_csv, args.non_out, show=show)

    if not args.skip_validation:
        run_validation(val_csv, args.val_out, show=show)

    # Per-signature-scheme plots
    _SIG_SCHEMES = ["ed25519", "dilithium2", "falcon512", "sphincs_sha2_128s"]
    if not args.skip_sig_schemes:
        for scheme in _SIG_SCHEMES:
            scheme_csv = os.path.join(args.results_dir, f"memo_results_{scheme}.csv")
            if not os.path.exists(scheme_csv):
                print(f"[skip] {scheme_csv} not found — run memo_parallel.py first")
                continue
            out_dir = f"memo_graphs_{scheme}"
            safe_mkdir(out_dir)
            run_memo_per_blocksize(scheme_csv, out_dir, show=show)
            run_memo_messages_vs_shards(scheme_csv, out_dir, show=show)
            print(f"[done] {scheme} -> {out_dir}/")

    print("\nDone.")
    print("Inputs read from:")
    print(f" - {near_csv}")
    print(f" - {memo_csv}")
    print(f" - {non_csv}")
    if not args.skip_validation:
        print(f" - {val_csv}")
    print("Outputs written to:")
    print(f" - {args.near_out}/")
    print(f" - {args.memo_out}/")
    print(f" - {args.non_out}/ (bubble)")
    print(f" - {args.val_out}/ (validation)")
    if not args.skip_sig_schemes:
        for scheme in _SIG_SCHEMES:
            print(f" - memo_graphs_{scheme}/")


if __name__ == "__main__":
    main()