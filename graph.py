#!/usr/bin/env python3
"""
make_graphs.py

Reads CSVs from Results/ (same level as graph folders) and writes plots to:
  - near_graphs/
  - memo_graphs/
  - non_sharded_graphs/
  - Validations/

No cross_compare_graphs. No extra "plots/" folder.
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
    plt.savefig(path, dpi=dpi)
    return path


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

    # If multiple rows per currency, average them
    agg = (
        df.groupby("currency", as_index=False)
          .agg(
              sim_avg_block_time=("average block time", "mean"),
              sim_tps=("tps", "mean"),
              sim_messages=("messages", "mean"),
          )
    )

    # Reference values (edit as needed)
    real_world = {
        "btc":  {"name": "Bitcoin",      "tps": 7.0,   "avg_block_time": 600.0},
        "bch":  {"name": "Bitcoin Cash", "tps": 200.0, "avg_block_time": 600.0},
        "ltc":  {"name": "Litecoin",     "tps": 28.0,  "avg_block_time": 150.0},
        "doge": {"name": "Dogecoin",     "tps": 33.0,  "avg_block_time": 60.0},
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

    # TPS compare
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

    # Block time compare
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
# 2) NEW Bubble chart -> non_sharded_graphs/
#    (BTC/BCH/LTC/DOGE vs MEMO, S=1)
# ----------------------------
def run_bubble_nonsharded_vs_memo_s1(non_csv: str, memo_csv: str, outdir: str, show: bool):
    if not os.path.exists(non_csv):
        print(f"[skip] non-sharded CSV not found: {non_csv}")
        return
    if not os.path.exists(memo_csv):
        print(f"[skip] memo CSV not found: {memo_csv}")
        return

    df_base = pd.read_csv(non_csv)
    df_memo = pd.read_csv(memo_csv)

    df_base = ensure_lower_currency(df_base)
    df_memo = ensure_lower_currency(df_memo)

    ensure_numeric(df_base, ["block size", "average block time", "tps"])
    ensure_numeric(df_memo, ["block size", "average block time", "tps", "shards"])

    keep = {"btc", "bch", "ltc", "doge"}
    df_base = df_base[df_base["currency"].isin(keep)].copy()

    # Normalize block size to int (stable mapping)
    df_base["bs"] = df_base["block size"].apply(lambda x: int(float(x)) if pd.notna(x) else np.nan)
    df_memo["bs"] = df_memo["block size"].apply(lambda x: int(float(x)) if pd.notna(x) else np.nan)

    # X axis categories ONLY from non-sharded
    base_block_sizes = sorted(df_base["bs"].dropna().unique().tolist())
    if not base_block_sizes:
        print("[skip] non-sharded CSV has no valid block sizes")
        return

    # MEMO S=1 (keep currency label as memo)
    df_memo_s1 = df_memo[(df_memo["currency"] == "memo") & (df_memo["shards"] == 1)].copy()
    df_memo_s1 = df_memo_s1[df_memo_s1["bs"].isin(base_block_sizes)].copy()

    df_base = df_base[["currency", "bs", "average block time", "tps"]].copy()
    df_memo_s1 = df_memo_s1[["currency", "bs", "average block time", "tps"]].copy()

    df = pd.concat([df_base, df_memo_s1], ignore_index=True)
    df = df.dropna(subset=["currency", "bs", "average block time", "tps"])
    if df.empty:
        print("[skip] bubble plot has no data after filtering")
        return

    # Categorical x positions spaced by 2
    x_positions = {bs: i * 2 for i, bs in enumerate(base_block_sizes)}
    xticks = [x_positions[bs] for bs in base_block_sizes]
    xticklabels = [str(bs) for bs in base_block_sizes]

    # IMPORTANT: bubble area proportional to TPS (classic bubble chart)
    max_tps = float(df["tps"].max()) if float(df["tps"].max()) > 0 else 1.0
    target_max_area = 1600.0  # tweak if you want bigger/smaller bubbles
    scale = target_max_area / max_tps

    min_area = 60.0  # ensures tiny TPS still visible

    plt.figure(figsize=(12, 7))

    # Draw order affects overlap — keep it fixed
    order = ["btc", "bch", "ltc", "doge", "memo"]
    for chain in order:
        if chain not in set(df["currency"]):
            continue

        sub = df[df["currency"] == chain].copy().sort_values("bs")
        x = np.array([x_positions[int(v)] for v in sub["bs"].to_numpy(dtype=float)], dtype=float)
        y = sub["average block time"].to_numpy(dtype=float)

        # area ∝ TPS
        s = (sub["tps"].to_numpy(dtype=float) * scale) + min_area

        plt.scatter(
            x, y,
            s=s,
            alpha=0.6,
            label=chain,
            edgecolors="black",
            linewidth=0.7,
        )

    plt.xticks(xticks, xticklabels, rotation=45)
    plt.xlabel("Block size (transactions per block)")
    plt.ylabel("Average block time (s)")
    plt.title("TPS as Bubble Size over Block Size vs Average Block Time\n(BTC/BCH/LTC/DOGE vs MEMO, S=1)")
    plt.grid(True, linestyle="--", alpha=0.35)
    plt.legend(title="Chain", loc="upper left")

    savefig(outdir, "bubble_tps_blocktime_vs_blocksize_nonsharded_vs_memo_s1.png")
    if show:
        plt.show()
    else:
        plt.close()

# ----------------------------
# 3) MEMO per-blocksize bar panels -> memo_graphs/
# ----------------------------
def global_log_range(series, pad_factor=1.5, min_exp=0):
    vals = series.to_numpy(dtype=float)
    vals = vals[vals > 0]
    if vals.size == 0:
        max_exp = 1
        exps = np.arange(min_exp, max_exp + 1)
        return 10.0**min_exp, 10.0**max_exp, exps
    vmax = vals.max()
    hi = vmax * pad_factor
    max_exp = int(np.ceil(np.log10(hi)))
    exps = np.arange(min_exp, max_exp + 1)
    return 10.0**min_exp, 10.0**max_exp, exps


def run_memo_per_blocksize(memo_csv: str, outdir: str, show: bool):
    if not os.path.exists(memo_csv):
        print(f"[skip] memo CSV not found: {memo_csv}")
        return

    df = pd.read_csv(memo_csv)
    ensure_numeric(df, ["shards", "block size", "tps", "messages", "average block time"])
    df = df.dropna(subset=["shards", "block size"])

    if df.empty:
        print("[skip] memo CSV has no usable rows")
        return

    tps_ymin, tps_ymax, tps_exps = global_log_range(df["tps"], pad_factor=1.5, min_exp=0)
    msg_ymin, msg_ymax, msg_exps = global_log_range(df["messages"], pad_factor=1.5, min_exp=0)

    bt_ymin, bt_ymax, bt_exps_full = global_log_range(df["average block time"], pad_factor=1.5, min_exp=-2)
    bt_exps = np.arange(-1, bt_exps_full.max() + 1)

    block_sizes = sorted(df["block size"].unique())

    def log_fmt_local(val, pos):
        if val <= 0:
            return ""
        exp = int(np.round(np.log10(val)))
        return rf"$10^{{{exp}}}$"

    for bs in block_sizes:
        sub = df[df["block size"] == bs].sort_values("shards")
        if sub.empty:
            continue

        shards = sub["shards"].tolist()
        tps = sub["tps"].tolist()
        msgs = sub["messages"].tolist()
        bt = sub["average block time"].tolist()

        x = np.arange(len(shards))
        width = 0.6

        fig, axes = plt.subplots(1, 3, figsize=(15, 4))

        ax0 = axes[0]
        ax0.bar(x, tps, width=width)
        ax0.set_xticks(x)
        ax0.set_xticklabels([str(int(s)) for s in shards])
        ax0.set_xlabel("Shards")
        ax0.set_ylabel("TPS (log scale)")
        ax0.set_title(f"MEMO: TPS vs Shards\n(block size = {bs})")
        ax0.set_yscale("log")
        ax0.set_ylim(tps_ymin, tps_ymax)
        ax0.set_yticks([10.0**e for e in tps_exps])
        ax0.yaxis.set_major_formatter(mtick.FuncFormatter(log_fmt_local))
        ax0.grid(axis="y", linestyle="--", alpha=0.4, which="both")

        ax1 = axes[1]
        ax1.bar(x, msgs, width=width)
        ax1.set_xticks(x)
        ax1.set_xticklabels([str(int(s)) for s in shards])
        ax1.set_xlabel("Shards")
        ax1.set_ylabel("Messages (log scale)")
        ax1.set_title(f"MEMO: Messages vs Shards\n(block size = {bs})")
        ax1.set_yscale("log")
        ax1.set_ylim(msg_ymin, msg_ymax)
        ax1.set_yticks([10.0**e for e in msg_exps])
        ax1.yaxis.set_major_formatter(mtick.FuncFormatter(log_fmt_local))
        ax1.grid(axis="y", linestyle="--", alpha=0.4, which="both")

        ax2 = axes[2]
        ax2.bar(x, bt, width=width)
        ax2.set_xticks(x)
        ax2.set_xticklabels([str(int(s)) for s in shards])
        ax2.set_xlabel("Shards")
        ax2.set_ylabel("Average Block Time (s, log scale)")
        ax2.set_title(f"MEMO: Block Time vs Shards\n(block size = {bs})")
        ax2.set_yscale("log")
        ax2.set_ylim(bt_ymin, bt_ymax)
        ax2.set_yticks([10.0**e for e in bt_exps])
        ax2.yaxis.set_major_formatter(mtick.FuncFormatter(log_fmt_local))
        ax2.grid(axis="y", linestyle="--", alpha=0.4, which="both")

        savefig(outdir, f"memo_bs_{int(float(bs))}_summary_logBT.png")
        if show:
            plt.show()
        else:
            plt.close()


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

    # Your targets (edit if needed)
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
    ap.add_argument("--memo_out", default="memo_graphs", help="Output folder for MEMO graphs")
    ap.add_argument("--non_out", default="non_sharded_graphs", help="Output folder for non-sharded graphs")
    ap.add_argument("--val_out", default="Validations", help="Output folder for validation graphs")

    ap.add_argument("--near_csv", default="Near.csv")
    ap.add_argument("--memo_csv", default="memo_results.csv")
    ap.add_argument("--non_csv", default="non-sharded.csv")
    ap.add_argument("--validation_csv", default="Validation.csv")

    ap.add_argument("--skip_near", action="store_true")
    ap.add_argument("--skip_memo", action="store_true")
    ap.add_argument("--skip_non", action="store_true")
    ap.add_argument("--skip_validation", action="store_true")

    args = ap.parse_args()
    show = not args.no_show

    near_csv = os.path.join(args.results_dir, args.near_csv)
    memo_csv = os.path.join(args.results_dir, args.memo_csv)
    non_csv = os.path.join(args.results_dir, args.non_csv)
    val_csv = os.path.join(args.results_dir, args.validation_csv)

    safe_mkdir(args.near_out)
    safe_mkdir(args.memo_out)
    safe_mkdir(args.non_out)
    safe_mkdir(args.val_out)

    if not args.skip_near:
        run_near_vs_targets(near_csv, args.near_out, show=show)

    if not args.skip_memo:
        run_memo_per_blocksize(memo_csv, args.memo_out, show=show)

    if not args.skip_non:
        run_bubble_nonsharded_vs_memo_s1(non_csv, memo_csv, args.non_out, show=show)

    if not args.skip_validation:
        run_validation(val_csv, args.val_out, show=show)

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


if __name__ == "__main__":
    main()
