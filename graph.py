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
    plt.bar(x + width / 2, comparison["real_tps"].tolist(), width, label="Claimed TPS")
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
    plt.bar(x + width / 2, comparison["real_avg_block_time"].tolist(), width, label="Claimed Block Time")
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
    c_bt_cfg   = pick_col(["blocktime in configuration file", "configured blocktime", "blocktime"])
    c_tps      = pick_col(["tps"])

    missing = [n for n, c in [("currency", c_currency), ("block size", c_bs),
                               ("blocktime in configuration file", c_bt_cfg),
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

    for c in [c_bs, c_bt_cfg, c_tps]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=[c_bs, c_bt_cfg, c_tps])
    if df.empty:
        print("[skip] BTC heatmap has no usable rows after numeric coercion")
        return

    df["bs"]      = df[c_bs].astype(float).round().astype(int)
    df["bt_cfg"]  = df[c_bt_cfg].astype(float)
    df["tps_val"] = df[c_tps].astype(float)

    block_sizes   = sorted(df["bs"].unique().tolist())
    bt_cfg_keys   = sorted(df["bt_cfg"].unique().tolist())

    pivot_tps = df.pivot_table(index="bt_cfg", columns="bs", values="tps_val", aggfunc="mean")
    pivot_tps = pivot_tps.reindex(index=bt_cfg_keys, columns=block_sizes)

    tps_vals  = pivot_tps.values.astype(float)
    was_nan   = np.isnan(tps_vals)

    # Fill NaN cells using only vertical (top/bottom) neighbors — never sideways
    tps_filled = tps_vals.copy()
    n_rows_fill, n_cols_fill = tps_vals.shape
    for _j in range(n_cols_fill):
        for _i in range(n_rows_fill):
            if not np.isnan(tps_vals[_i, _j]):
                continue
            top = next((tps_vals[r, _j] for r in range(_i - 1, -1, -1)
                        if not np.isnan(tps_vals[r, _j])), None)
            bot = next((tps_vals[r, _j] for r in range(_i + 1, n_rows_fill)
                        if not np.isnan(tps_vals[r, _j])), None)
            if top is not None and bot is not None:
                tps_filled[_i, _j] = (top + bot) / 2
            elif top is not None:
                tps_filled[_i, _j] = top
            elif bot is not None:
                tps_filled[_i, _j] = bot

    masked = np.ma.masked_invalid(tps_filled)

    n_y, n_x = len(bt_cfg_keys), len(block_sizes)
    x_edges  = np.arange(n_x + 1) - 0.5
    y_edges  = np.arange(n_y + 1) - 0.5

    # Red (low TPS) → orange → yellow → deep sky blue (high TPS): highest is the goal
    # All colors kept dark/saturated enough for white text to remain readable
    cmap = plt.matplotlib.colors.LinearSegmentedColormap.from_list(
        "red_to_skyblue", ["#990000", "#cc4400", "#cc9900", "#0077bb"]
    )
    cmap.set_bad("#333333")

    vmin = float(np.nanmin(tps_filled))
    vmax = float(np.nanmax(tps_filled))
    norm = plt.Normalize(vmin=vmin, vmax=vmax)

    _, ax = plt.subplots(figsize=(14, max(10, n_y * 0.9)))
    ax.set_facecolor("#333333")

    im = ax.pcolormesh(x_edges, y_edges, masked, cmap=cmap, norm=norm, shading="flat")

    ax.set_xlim(x_edges[0], x_edges[-1])
    ax.set_ylim(y_edges[0], y_edges[-1])

    ax.set_xticks(range(n_x))
    ax.set_xticklabels([str(bs) for bs in block_sizes], rotation=45, ha="right", fontsize=16)
    ax.set_yticks(range(n_y))
    ax.set_yticklabels([f"{k:.0f}s" for k in bt_cfg_keys], fontsize=16)

    for i in range(n_y):
        for j in range(n_x):
            tps_val = tps_filled[i, j]
            if np.isnan(tps_val):
                continue
            tps_str = f"{tps_val:.0f}" if tps_val < 1000 else f"{tps_val/1000:.1f}k"
            r, g, b, _ = cmap(norm(tps_val))
            lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
            if was_nan[i, j]:
                color = "#444444" if lum > 0.45 else "#cccccc"
            else:
                color = "black" if lum > 0.45 else "white"
            ax.text(j, i, tps_str, ha="center", va="center",
                    fontsize=12, color=color, fontweight="bold",
                    fontstyle="italic" if was_nan[i, j] else "normal")

    ax.set_xlabel("Block Size (tx/block)", fontsize=17)
    ax.set_ylabel("Configured Mining Time (s)", fontsize=17)
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
# 2b) Non-sharded: TPS scatter -> non_sharded_graphs/
#     X = block size, Y = actual block time (raw), color = TPS
#     Each dot = one simulation run, no binning or averaging
# ----------------------------
def run_nonsharded_scatter(non_csv: str, outdir: str, show: bool):
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
    c_tps      = pick_col(["tps"])
    c_bt_cfg   = pick_col(["blocktime in configuration file", "configured blocktime", "blocktime"])

    missing = [n for n, c in [("currency", c_currency), ("block size", c_bs),
                               ("average block time", c_abt), ("tps", c_tps)] if c is None]
    if missing:
        print(f"[skip] non-sharded scatter missing columns: {missing}")
        return

    df[c_currency] = (
        df[c_currency].astype(str)
        .str.replace("﻿", "", regex=False)
        .str.lower().str.strip()
    )
    df = df[df[c_currency].eq("btc_hypothetical")].copy()
    if df.empty:
        print("[skip] no btc_hypothetical rows for scatter plot")
        return

    for c in [c_bs, c_abt, c_tps]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=[c_bs, c_abt, c_tps]).copy()

    df["bs"]      = df[c_bs].astype(float).round().astype(int)
    df["abt"]     = df[c_abt].astype(float)
    df["tps_val"] = df[c_tps].astype(float)

    block_sizes  = sorted(df["bs"].unique().tolist())
    x_positions  = {bs: i for i, bs in enumerate(block_sizes)}

    # Small x-jitter so overlapping points don't stack invisibly
    rng = np.random.default_rng(42)
    df["x"] = df["bs"].map(x_positions) + rng.uniform(-0.25, 0.25, size=len(df))

    fig, ax = plt.subplots(figsize=(14, 7))

    cmap = plt.matplotlib.colors.LinearSegmentedColormap.from_list(
        "red_to_skyblue", ["#FA0000", "#11ff00"]
    )
    tps_min = df["tps_val"][df["tps_val"] > 0].min()
    norm = plt.matplotlib.colors.LogNorm(vmin=tps_min, vmax=df["tps_val"].max())

    sc = ax.scatter(
        df["x"], df["abt"],
        c=df["tps_val"], cmap=cmap, norm=norm,
        s=200, alpha=0.85, edgecolors="none"
    )

    cbar = plt.colorbar(sc, ax=ax)
    cbar.set_label("TPS", fontsize=13)
    cbar.ax.tick_params(labelsize=11)

    ax.set_yscale("log")
    ax.set_xticks(range(len(block_sizes)))
    ax.set_xticklabels([str(bs) for bs in block_sizes], rotation=45, ha="right", fontsize=11)
    ax.set_xlabel("Block Size (tx/block)", fontsize=13)
    ax.set_ylabel("Actual Mining Time (s)", fontsize=13)
    ax.set_title("Non-Sharded TPS Scatter — BTC Hypothetical\n"
                 "Each point = one simulation run; color = TPS", fontsize=13)
    ax.grid(True, linestyle="--", alpha=0.35)
    fig.tight_layout()

    savefig(outdir, "nonsharded_tps_scatter.png")
    if show:
        plt.show()
    else:
        plt.close(fig)


# ----------------------------
# 2c) Non-sharded: Actual Block Time vs Block Size -> non_sharded_graphs/
#     One line per configured blocktime
# ----------------------------
def run_nonsharded_blocktime_vs_blocksize(non_csv: str, outdir: str, show: bool):
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

    missing = [n for n, c in [("currency", c_currency), ("block size", c_bs),
                               ("average block time", c_abt),
                               ("blocktime in configuration file", c_bt_cfg)] if c is None]
    if missing:
        print(f"[skip] non-sharded CSV missing columns for block time plot: {missing}")
        return

    df[c_currency] = (
        df[c_currency].astype(str)
        .str.replace("﻿", "", regex=False)
        .str.lower().str.strip()
    )
    df = df[df[c_currency].eq("btc_hypothetical")].copy()
    if df.empty:
        print("[skip] no btc_hypothetical rows for block time vs block size plot")
        return

    for c in [c_bs, c_abt, c_bt_cfg]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=[c_bs, c_abt, c_bt_cfg]).copy()

    df["bs"]         = df[c_bs].astype(float).round().astype(int)
    df["abt"]        = df[c_abt].astype(float)
    df["bt_cfg"]     = df[c_bt_cfg].astype(float).round(6)

    # Average actual block time per (configured blocktime, block size)
    agg = (
        df.groupby(["bt_cfg", "bs"])["abt"]
        .mean()
        .reset_index()
        .sort_values(["bt_cfg", "bs"])
    )

    block_sizes   = sorted(agg["bs"].unique().tolist())
    blocktimes    = sorted(agg["bt_cfg"].unique().tolist())
    x_positions   = {bs: i * 2 for i, bs in enumerate(block_sizes)}
    xticks        = [x_positions[bs] for bs in block_sizes]
    xticklabels   = [str(bs) for bs in block_sizes]

    fig, ax = plt.subplots(figsize=(14, 10))

    n_colors   = 10
    palette    = [plt.cm.tab10(i) for i in range(n_colors)]
    linestyles = ["-", ":"]   # solid for first cycle, dotted when colors repeat

    for idx, bt in enumerate(blocktimes):
        sub = agg[agg["bt_cfg"] == bt].sort_values("bs")
        x_vals    = [x_positions[bs] for bs in sub["bs"]]
        label     = f"{bt:.0f}s" if bt >= 1 else f"{bt:.2f}s"
        color     = palette[idx % n_colors]
        linestyle = linestyles[idx // n_colors % len(linestyles)]
        ax.plot(x_vals, sub["abt"], marker="o", linewidth=1.8, markersize=5,
                label=label, color=color, linestyle=linestyle)

    ax.set_xlabel("Block Size (tx/block)", fontsize=13)
    ax.set_ylabel("Actual Avg Block Time (s)", fontsize=13)
    ax.set_title("Non-Sharded: Actual Mining Time vs Block Size (BTC Hypothetical)", fontsize=14)
    ax.set_xticks(xticks)
    ax.set_xticklabels(xticklabels, rotation=45, ha="right")
    ax.legend(title="Configured\nMining Time", fontsize=9, title_fontsize=9,
              loc="upper right", ncol=2)
    ax.set_yscale("log")
    ax.grid(True, linestyle="--", alpha=0.4)
    fig.tight_layout()

    savefig(outdir, "nonsharded_blocktime_vs_blocksize.png")
    if show:
        plt.show()
    else:
        plt.close(fig)


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
# 4b) New sharded design Block Time vs Shards -> memo_graphs_<condition>/
#     One line per block size: minimum actual block time per (block size, shard count)
# ----------------------------
def run_memo_blocktime_vs_shards(memo_csv: str, outdir: str, show: bool, block_size: int = None):
    if not os.path.exists(memo_csv):
        print(f"[skip] memo CSV not found: {memo_csv}")
        return

    df = pd.read_csv(memo_csv, engine="python", on_bad_lines="warn")
    df.columns = [str(c).strip() for c in df.columns]

    c_shards = _pick_col(df, ["shards"])
    c_bs     = _pick_col(df, ["block size", "block_size", "blocksize"])
    c_abt    = _pick_col(df, ["average block time", "avg block time", "avg_block_time"])

    missing = [n for n, c in [("shards", c_shards), ("block size", c_bs),
                               ("average block time", c_abt)] if c is None]
    if missing:
        print(f"[skip] memo CSV missing required columns for blocktime plot: {missing}")
        return

    for c in [c_shards, c_bs, c_abt]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=[c_shards, c_bs, c_abt]).copy()
    if df.empty:
        print("[skip] memo CSV has no usable rows for blocktime vs shards plot")
        return

    df["shards_int"]     = df[c_shards].astype(int)
    df["block_size_int"] = df[c_bs].astype(int)
    df["abt_val"]        = df[c_abt].astype(float)

    # Minimum actual block time per (block size, shard count)
    agg = (
        df.groupby(["block_size_int", "shards_int"])["abt_val"]
        .min()
        .reset_index()
        .sort_values(["block_size_int", "shards_int"])
    )

    if block_size is not None:
        if block_size not in agg["block_size_int"].values:
            available = sorted(agg["block_size_int"].unique().tolist())
            print(f"[skip] block size {block_size} not found in data. Available: {available}")
            return
        agg = agg[agg["block_size_int"] == block_size].copy()
        print(f"[info] Filtering blocktime vs shards to block size = {block_size}")

    block_sizes = sorted(agg["block_size_int"].unique().tolist())
    all_shards  = sorted(agg["shards_int"].unique().tolist())

    x_positions = {s: i * 2 for i, s in enumerate(all_shards)}
    xticks      = [x_positions[s] for s in all_shards]

    fig, ax = plt.subplots(figsize=(12, 6))

    for bs in block_sizes:
        sub = agg[agg["block_size_int"] == bs].sort_values("shards_int")
        x_vals = [x_positions[s] for s in sub["shards_int"]]
        ax.plot(x_vals, sub["abt_val"], marker="o", linewidth=1.8,
                markersize=4, label=f"{bs:,}")

    title_suffix = f" (Block Size = {block_size:,})" if block_size is not None else ""
    out_name = f"memo_blocktime_vs_shards_bs{block_size}.png" if block_size is not None else "memo_blocktime_vs_shards.png"

    ax.set_xlabel("Number of Shards", fontsize=13)
    ax.set_ylabel("Min Actual Block Time (s)", fontsize=13)
    ax.set_title(f"New Sharded Design: Min Block Time vs Number of Shards{title_suffix}", fontsize=14)
    ax.set_xticks(xticks)
    ax.set_xticklabels([str(s) for s in all_shards], rotation=45, ha="right")
    ax.set_yscale("log")
    ax.legend(title="Block Size", fontsize=8, title_fontsize=9,
              loc="upper right", ncol=2)
    ax.grid(True, linestyle="--", alpha=0.4)
    fig.tight_layout()

    savefig(outdir, out_name)
    if show:
        plt.show()
    else:
        plt.close(fig)


# ----------------------------
# 4c) New sharded design Messages vs Shards -> memo_msg_graphs/
#     Single graph, one colored line per block size (averaged across block times).
#     Messages are nearly identical across block sizes/times, so lines nearly overlap.
#     If all lines visually collapse to one, a single representative line suffices.
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

    missing = [n for n, c in [("shards", c_shards), ("block size", c_bs), ("messages", c_msgs)]
               if c is None]
    if missing:
        print(f"[skip] memo CSV missing required columns for messages plot: {missing}")
        return

    for c in [c_shards, c_bs, c_msgs]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=[c_shards, c_bs, c_msgs]).copy()
    if df.empty:
        print("[skip] memo CSV has no usable rows for messages plot")
        return

    df["shards_int"]     = df[c_shards].astype(int)
    df["block_size_int"] = df[c_bs].astype(int)
    df["messages_val"]   = df[c_msgs].astype(float)

    # Average messages across all block times for each (block_size, shard) combo
    agg = (
        df.groupby(["block_size_int", "shards_int"])["messages_val"]
        .mean()
        .reset_index()
        .sort_values(["block_size_int", "shards_int"])
    )

    block_sizes = sorted(agg["block_size_int"].unique().tolist(), reverse=True)
    all_shards  = sorted(agg["shards_int"].unique().tolist())

    x_positions = {s: i * 2 for i, s in enumerate(all_shards)}
    xticks      = [x_positions[s] for s in all_shards]

    # Check if all block sizes produce the same messages (relative std < 0.1%)
    ref = agg.groupby("shards_int")["messages_val"]
    rel_std = (ref.std() / ref.mean() * 100).max()
    all_same = rel_std < 0.1

    fig, ax = plt.subplots(figsize=(12, 6))

    if all_same:
        # All block sizes give essentially the same message count — plot one averaged line
        single = agg.groupby("shards_int")["messages_val"].mean().reset_index()
        x_vals = [x_positions[s] for s in single["shards_int"]]
        ax.plot(x_vals, single["messages_val"], marker="o", linewidth=2,
                markersize=5, color="steelblue", label="All block sizes (identical)")
        print(f"[info] Messages are essentially identical across all block sizes "
              f"(max relative std = {rel_std:.4f}%) — plotting single representative line.")
    else:
        cmap = plt.cm.get_cmap("tab10", len(block_sizes))
        for idx, bs in enumerate(block_sizes):
            sub = agg[agg["block_size_int"] == bs].sort_values("shards_int")
            x_vals = [x_positions[s] for s in sub["shards_int"]]
            ax.plot(x_vals, sub["messages_val"], marker="o", linewidth=1.8,
                    markersize=4, color=cmap(idx), label=f"{bs:,}")

        ax.legend(title="Block Size", fontsize=8, title_fontsize=9,
                  loc="upper left", ncol=2)

    if all_same:
        ax.legend(fontsize=9)

    ax.set_xlabel("Number of Shards", fontsize=13)
    ax.set_ylabel("Messages", fontsize=13)
    ax.set_title("New Sharded Design: Messages vs Number of Shards", fontsize=14)
    ax.set_xticks(xticks)
    ax.set_xticklabels([str(s) for s in all_shards], rotation=45, ha="right")
    ax.grid(True, linestyle="--", alpha=0.4)
    fig.tight_layout()

    safe_mkdir(outdir)
    out_path = os.path.join(outdir, "memo_messages_vs_shards.png")
    fig.savefig(out_path, dpi=300, bbox_inches="tight")

    if show:
        plt.show()
    else:
        plt.close(fig)

    print(f"[done] {out_path}")


# ----------------------------
# 5) Common: Block Time vs Shards across all 3 network environments
#    Three lines (datacenter, US WAN, global WAN) on one graph.
#    Optional --common_bt_blocksize to filter to a single block size.
# ----------------------------
def run_common_blocktime_vs_shards(
    local_csv: str,
    usa_csv: str,
    global_csv: str,
    outdir: str,
    show: bool,
    block_size: int = None,
):
    configs = [
        ("Datacenter",  local_csv,  "#1f77b4"),
        ("US WAN",      usa_csv,    "#ff7f0e"),
        ("Global WAN",  global_csv, "#2ca02c"),
    ]

    missing_files = [label for label, path, _ in configs if not os.path.exists(path)]
    if missing_files:
        print(f"[skip] common blocktime graph: missing CSVs for {missing_files}")
        return

    fig, ax = plt.subplots(figsize=(12, 6))

    all_shards = None

    for label, csv_path, color in configs:
        df = pd.read_csv(csv_path, engine="python", on_bad_lines="warn")
        df.columns = [str(c).strip() for c in df.columns]

        c_shards = _pick_col(df, ["shards"])
        c_bs     = _pick_col(df, ["block size", "block_size", "blocksize"])
        c_abt    = _pick_col(df, ["average block time_mean", "average block time",
                                   "avg block time", "avg_block_time"])

        if any(c is None for c in [c_shards, c_bs, c_abt]):
            print(f"[skip] {label}: missing required columns in {csv_path}")
            continue

        for c in [c_shards, c_bs, c_abt]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=[c_shards, c_bs, c_abt]).copy()

        df["shards_int"]     = df[c_shards].astype(int)
        df["block_size_int"] = df[c_bs].astype(int)
        df["abt_val"]        = df[c_abt].astype(float)

        if block_size is not None:
            available = sorted(df["block_size_int"].unique().tolist())
            if block_size not in available:
                print(f"[skip] {label}: block size {block_size} not found. Available: {available}")
                continue
            df = df[df["block_size_int"] == block_size].copy()

        agg = (
            df.groupby("shards_int")["abt_val"]
            .min()
            .reset_index()
            .sort_values("shards_int")
        )

        if all_shards is None:
            all_shards = sorted(agg["shards_int"].unique().tolist())

        x_positions = {s: i * 2 for i, s in enumerate(
            sorted(agg["shards_int"].unique().tolist())
        )}
        x_vals = [x_positions[s] for s in agg["shards_int"]]

        ax.plot(x_vals, agg["abt_val"], marker="o", linewidth=2,
                markersize=5, label=label, color=color)

    if all_shards is None:
        print("[skip] common blocktime graph: no data plotted")
        plt.close(fig)
        return

    x_positions_global = {s: i * 2 for i, s in enumerate(all_shards)}
    xticks = [x_positions_global[s] for s in all_shards]

    title_suffix = f" (Block Size = {block_size:,})" if block_size is not None else " (All Block Sizes — Min)"
    out_name = (
        f"common_blocktime_vs_shards_bs{block_size}.png"
        if block_size is not None
        else "common_blocktime_vs_shards.png"
    )

    ax.set_xlabel("Number of Shards", fontsize=13)
    ax.set_ylabel("Min Actual Block Time (s)", fontsize=13)
    ax.set_title(f"Min Block Time vs Number of Shards{title_suffix}", fontsize=14)
    ax.set_xticks(xticks)
    ax.set_xticklabels([str(s) for s in all_shards], rotation=45, ha="right")
    ax.set_yscale("log")
    ax.legend(title="Network Environment", fontsize=10, title_fontsize=10)
    ax.grid(True, linestyle="--", alpha=0.4)
    fig.tight_layout()

    savefig(outdir, out_name)
    print(f"[done] {os.path.join(outdir, out_name)}")

    if show:
        plt.show()
    else:
        plt.close(fig)


# ----------------------------
# 5b) Common: Average TPS vs Shards across all 3 network environments
#     Three lines (datacenter, US WAN, global WAN) on one graph.
#     Optional --common_tps_blocksize to filter to a single block size.
# ----------------------------
def run_common_tps_vs_shards(
    local_csv: str,
    usa_csv: str,
    global_csv: str,
    outdir: str,
    show: bool,
    block_size: int = None,
):
    configs = [
        ("Datacenter",  local_csv,  "#1f77b4"),
        ("US WAN",      usa_csv,    "#ff7f0e"),
        ("Global WAN",  global_csv, "#2ca02c"),
    ]

    missing_files = [label for label, path, _ in configs if not os.path.exists(path)]
    if missing_files:
        print(f"[skip] common tps graph: missing CSVs for {missing_files}")
        return

    fig, ax = plt.subplots(figsize=(12, 6))

    all_shards = None

    for label, csv_path, color in configs:
        df = pd.read_csv(csv_path, engine="python", on_bad_lines="warn")
        df.columns = [str(c).strip() for c in df.columns]

        c_shards = _pick_col(df, ["shards"])
        c_bs     = _pick_col(df, ["block size", "block_size", "blocksize"])
        c_tps    = _pick_col(df, ["tps_mean", "tps"])

        if any(c is None for c in [c_shards, c_bs, c_tps]):
            print(f"[skip] {label}: missing required columns in {csv_path}")
            continue

        for c in [c_shards, c_bs, c_tps]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=[c_shards, c_bs, c_tps]).copy()

        df["shards_int"]     = df[c_shards].astype(int)
        df["block_size_int"] = df[c_bs].astype(int)
        df["tps_val"]        = df[c_tps].astype(float)

        if block_size is not None:
            available = sorted(df["block_size_int"].unique().tolist())
            if block_size not in available:
                print(f"[skip] {label}: block size {block_size} not found. Available: {available}")
                continue
            df = df[df["block_size_int"] == block_size].copy()

        agg = (
            df.groupby("shards_int")["tps_val"]
            .mean()
            .reset_index()
            .sort_values("shards_int")
        )

        if all_shards is None:
            all_shards = sorted(agg["shards_int"].unique().tolist())

        x_positions = {s: i * 2 for i, s in enumerate(
            sorted(agg["shards_int"].unique().tolist())
        )}
        x_vals = [x_positions[s] for s in agg["shards_int"]]

        ax.plot(x_vals, agg["tps_val"], marker="o", linewidth=2,
                markersize=5, label=label, color=color)

    if all_shards is None:
        print("[skip] common tps graph: no data plotted")
        plt.close(fig)
        return

    x_positions_global = {s: i * 2 for i, s in enumerate(all_shards)}
    xticks = [x_positions_global[s] for s in all_shards]

    title_suffix = f" (Block Size = {block_size:,})" if block_size is not None else " (All Block Sizes — Mean)"
    out_name = (
        f"common_tps_vs_shards_bs{block_size}.png"
        if block_size is not None
        else "common_tps_vs_shards.png"
    )

    ax.set_xlabel("Number of Shards", fontsize=13)
    ax.set_ylabel("Average TPS", fontsize=13)
    ax.set_title(f"Average TPS vs Number of Shards{title_suffix}", fontsize=14)
    ax.set_xticks(xticks)
    ax.set_xticklabels([str(s) for s in all_shards], rotation=45, ha="right")
    ax.legend(title="Network Environment", fontsize=10, title_fontsize=10)
    ax.grid(True, linestyle="--", alpha=0.4)
    fig.tight_layout()

    savefig(outdir, out_name)
    print(f"[done] {os.path.join(outdir, out_name)}")

    if show:
        plt.show()
    else:
        plt.close(fig)


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
# 7) Hop-count CDF -> given outdir (from simulation.py's write_hop_cdf())
# ----------------------------
def run_hop_cdf(hop_cdf_csv: str, outdir: str, show: bool):
    if not os.path.exists(hop_cdf_csv):
        print(f"[skip] hop-count CDF CSV not found: {hop_cdf_csv}")
        return

    df = pd.read_csv(hop_cdf_csv)
    if "hop_count" not in df.columns or "cumulative_fraction" not in df.columns:
        print(f"[skip] {hop_cdf_csv} missing hop_count/cumulative_fraction columns")
        return

    df = ensure_numeric(df, ["hop_count", "cumulative_fraction"]).dropna()
    df = df.sort_values("hop_count")
    if df.empty:
        print(f"[skip] {hop_cdf_csv} has no usable rows")
        return

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.step(df["hop_count"], df["cumulative_fraction"], where="post",
            linewidth=2, color="#1f77b4")
    ax.axhline(0.5, linestyle="--", alpha=0.3, color="gray")
    ax.axhline(0.9, linestyle="--", alpha=0.3, color="gray")

    ax.set_xlabel("Hop count", fontsize=13)
    ax.set_ylabel("Fraction of network informed", fontsize=13)
    ax.set_title("Hop-Count CDF — Broadcast Delivery by Hop", fontsize=13)
    ax.yaxis.set_major_formatter(mtick.PercentFormatter(xmax=1.0))
    ax.set_ylim(0, 1.02)
    ax.set_xlim(left=0)
    ax.grid(True, linestyle="--", alpha=0.35)
    fig.tight_layout()

    savefig(outdir, "hop_cdf.png")
    if show:
        plt.show()
    else:
        plt.close(fig)


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
    ap.add_argument("--memo_bt_out", default="memo_graphs", help="Output folder for block time vs shards graphs")
    ap.add_argument("--non_out", default="non_sharded_graphs", help="Output folder for non-sharded graphs")
    ap.add_argument("--val_out", default="Validations", help="Output folder for validation graphs")

    ap.add_argument("--near_csv", default="Near.csv")
    ap.add_argument("--memo_csv", default="memo_results.csv")
    ap.add_argument("--non_csv", default="non-sharded.csv")
    ap.add_argument("--validation_csv", default="Validation.csv")

    ap.add_argument("--skip_near", action="store_true")
    ap.add_argument("--skip_memo", action="store_true")
    ap.add_argument("--skip_memo_msg", action="store_true")
    ap.add_argument("--skip_memo_bt", action="store_true")
    ap.add_argument("--skip_non", action="store_true")
    ap.add_argument("--skip_non_scatter", action="store_true")
    ap.add_argument("--skip_non_bt", action="store_true")
    ap.add_argument("--skip_validation", action="store_true")
    ap.add_argument("--skip_sig_schemes", action="store_true",
                    help="Skip per-signature-scheme plot generation")
    ap.add_argument("--memo_bt_blocksize", type=int, default=None,
                    help="If set, only plot this block size in the blocktime vs shards graph")

    ap.add_argument("--common_out", default="common_graphs",
                    help="Output folder for cross-environment comparison graphs")
    ap.add_argument("--memo_local_csv", default="memo_results_local.csv")
    ap.add_argument("--memo_usa_csv",   default="memo_results_usa.csv")
    ap.add_argument("--memo_global_csv", default="memo_results_global.csv")
    ap.add_argument("--common_tps_blocksize", type=int, default=None,
                    help="If set, filter common tps-vs-shards plot to this block size")
    ap.add_argument("--common_bt_blocksize", type=int, default=None,
                    help="If set, filter common blocktime-vs-shards graph to this block size")
    ap.add_argument("--skip_common", action="store_true",
                    help="Skip common cross-environment graphs")

    ap.add_argument("--hop_cdf_csv", default="hop_cdf.csv",
                    help="Hop-count CDF CSV written by simulation.py's write_hop_cdf()")
    ap.add_argument("--hop_cdf_out", default="hop_cdf_graphs",
                    help="Output folder for the hop-count CDF plot")
    ap.add_argument("--skip_hop_cdf", action="store_true",
                    help="Skip the hop-count CDF plot")

    args = ap.parse_args()
    show = not args.no_show

    near_csv = os.path.join(args.results_dir, args.near_csv)
    memo_csv = os.path.join(args.results_dir, args.memo_csv)
    non_csv  = os.path.join(args.results_dir, args.non_csv)
    val_csv  = os.path.join(args.results_dir, args.validation_csv)

    if not args.skip_near:
        run_near_vs_targets(near_csv, args.near_out, show=show)

    if not args.skip_memo:
        run_memo_per_blocksize(memo_csv, args.memo_out, show=show)

    if not args.skip_memo_msg:
        run_memo_messages_vs_shards(memo_csv, args.memo_msg_out, show=show)

    if not args.skip_memo_bt:
        run_memo_blocktime_vs_shards(memo_csv, args.memo_bt_out, show=show, block_size=args.memo_bt_blocksize)

    if not args.skip_non:
        run_bubble_nonsharded_vs_memo_s1(non_csv, memo_csv, args.non_out, show=show)

    if not args.skip_non_scatter:
        run_nonsharded_scatter(non_csv, args.non_out, show=show)

    if not args.skip_non_bt:
        run_nonsharded_blocktime_vs_blocksize(non_csv, args.non_out, show=show)

    if not args.skip_validation:
        run_validation(val_csv, args.val_out, show=show)

    if not args.skip_hop_cdf:
        hop_cdf_csv = os.path.join(args.results_dir, args.hop_cdf_csv)
        run_hop_cdf(hop_cdf_csv, args.hop_cdf_out, show=show)

    if not args.skip_common:
        local_csv  = os.path.join(args.results_dir, args.memo_local_csv)
        usa_csv    = os.path.join(args.results_dir, args.memo_usa_csv)
        global_csv = os.path.join(args.results_dir, args.memo_global_csv)
        run_common_blocktime_vs_shards(
            local_csv, usa_csv, global_csv,
            outdir=args.common_out,
            show=show,
            block_size=args.common_bt_blocksize,
        )
        run_common_tps_vs_shards(
            local_csv, usa_csv, global_csv,
            outdir=args.common_out,
            show=show,
            block_size=args.common_tps_blocksize,
        )

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