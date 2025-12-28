#!/usr/bin/env python3
import subprocess
import sys
import os
import threading
import json
import re
from datetime import datetime
from typing import Dict, Any, Optional, Tuple, List

# --- main simulator script ---
SIM_SCRIPT = "simulation.py"   # change if your file is named differently

# MEMO configs
CONFIGS = {
    "memo_s1": "memo_config/1.json",
    "memo_s2": "memo_config/2.json",
    "memo_s4": "memo_config/4.json",
    "memo_s6": "memo_config/6.json",
    "memo_s8": "memo_config/8.json",
    "memo_s9": "memo_config/9.json",
    "memo_s16": "memo_config/16.json",
    "memo_s32": "memo_config/32.json",
    "memo_s64": "memo_config/64.json",
    "memo_s128": "memo_config/128.json",
    "memo_s256": "memo_config/256.json",
}

LOG_DIR = "memo_logs"

# Where you want the results:
RESULTS_DIR = "Results"
RESULTS_CSV = os.path.join(RESULTS_DIR, "memo_results.csv")

# How to identify a row uniquely for update-vs-append:
# - Recommended: config path is unique per experiment
ROW_KEY_FIELD = "config"


# ----------------------------
# Helpers: config reading
# ----------------------------

def _first_present(d: Dict[str, Any], keys: List[str]) -> Optional[Any]:
    for k in keys:
        if k in d:
            return d[k]
    return None

def read_memo_config(cfg_path: str) -> Dict[str, Any]:
    """
    Read JSON config and extract fields we care about: shards, blocktime, blocksize.
    Keeps everything else too, in case you want to store more later.
    """
    with open(cfg_path, "r") as f:
        cfg = json.load(f)

    # Try multiple common key names so this works even if you rename fields
    shards = _first_present(cfg, ["shards", "shard_count", "num_shards", "S"])
    blocktime = _first_present(cfg, ["blocktime", "target_blocktime", "block_interval", "interval_seconds"])
    blocksize = _first_present(cfg, ["total_blocksize", "blocksize", "block_size", "max_block_size", "block_size_txs"])

    return {
        "raw": cfg,
        "shards_cfg": shards,
        "blocktime_cfg": blocktime,
        "blocksize_cfg": blocksize,
    }


# ----------------------------
# Helpers: log parsing
# ----------------------------

def _extract_first_float(text: str, patterns: List[str]) -> Optional[float]:
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE | re.MULTILINE)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                pass
    return None

def _extract_first_int(text: str, patterns: List[str]) -> Optional[int]:
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE | re.MULTILINE)
        if m:
            try:
                return int(float(m.group(1)))
            except Exception:
                pass
    return None

def parse_metrics_from_log(log_path: str) -> Dict[str, Any]:
    """
    Parse key metrics from the simulation log.

    IMPORTANT:
    - Your simulation needs to print these values somewhere.
    - If your log uses different labels, just tweak the regex patterns below.
    """
    if not os.path.exists(log_path):
        return {}

    with open(log_path, "r") as f:
        text = f.read()

    # Adjust these patterns to match your simulator's output exactly
    tps = _extract_first_float(text, [
        r"\btps\b[^0-9\-]*([0-9]+(?:\.[0-9]+)?)",
        r"\bthroughput\b[^0-9\-]*([0-9]+(?:\.[0-9]+)?)",
    ])

    avg_block_time = _extract_first_float(text, [
        r"average\s+block\s+time[^0-9\-]*([0-9]+(?:\.[0-9]+)?)",
        r"avg\s+block\s+time[^0-9\-]*([0-9]+(?:\.[0-9]+)?)",
        r"\babt\b[^0-9\-]*([0-9]+(?:\.[0-9]+)?)",
    ])

    messages = _extract_first_int(text, [
        r"\bmessages\b[^0-9\-]*([0-9]+)",
        r"\bmsg\b[^0-9\-]*([0-9]+)",
    ])

    blocks_generated = _extract_first_int(text, [
        r"no\.\s*of\s*blocks\s*generated[^0-9\-]*([0-9]+)",
        r"\bblocks\s*generated\b[^0-9\-]*([0-9]+)",
    ])

    # Some sims print "block size" as observed in run; store if found
    block_size_observed = _extract_first_int(text, [
        r"\bblock\s*size\b[^0-9\-]*([0-9]+)",
    ])

    # If you print a mode label (e.g., conventional / near / memo)
    mode = None
    m = re.search(r"\bmode\b[^A-Za-z0-9]*([A-Za-z0-9_\-]+)", text, flags=re.IGNORECASE)
    if m:
        mode = m.group(1)

    return {
        "tps": tps,
        "average_block_time": avg_block_time,
        "messages": messages,
        "blocks_generated": blocks_generated,
        "block_size_observed": block_size_observed,
        "mode": mode,
    }


# ----------------------------
# Helpers: CSV update/append
# ----------------------------

def _read_csv_rows(path: str) -> Tuple[List[str], List[Dict[str, str]]]:
    """
    Minimal CSV reader (no pandas). Returns (header, rows).
    """
    import csv
    if not os.path.exists(path):
        return ([], [])

    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        rows = list(reader)
    return header, rows

def _write_csv_rows(path: str, header: List[str], rows: List[Dict[str, Any]]) -> None:
    import csv
    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in header})

def upsert_results_csv(csv_path: str, key_field: str, new_rows: List[Dict[str, Any]]) -> None:
    """
    For each new row:
    - if a row with same key_field exists -> update it
    - else -> append it
    """
    header, rows = _read_csv_rows(csv_path)
    existing_by_key = {}
    for i, r in enumerate(rows):
        existing_by_key[r.get(key_field, "")] = i

    # Build union header
    all_fields = set(header)
    for nr in new_rows:
        all_fields.update(nr.keys())

    # Keep header stable-ish: put common fields first if present
    preferred_order = [
        "timestamp", "run", "config",
        "shards_cfg", "blocktime_cfg", "blocksize_cfg",
        "tps", "average_block_time", "messages", "blocks_generated", "block_size_observed", "mode",
        "exit_status",
    ]
    ordered = [f for f in preferred_order if f in all_fields]
    remaining = sorted([f for f in all_fields if f not in ordered])
    header_out = ordered + remaining

    # Upsert
    for nr in new_rows:
        k = str(nr.get(key_field, ""))
        if k in existing_by_key:
            idx = existing_by_key[k]
            # update existing row dict with new values (store as strings or numbers; writer will handle)
            rows[idx].update({kk: ("" if vv is None else vv) for kk, vv in nr.items()})
        else:
            rows.append({kk: ("" if vv is None else vv) for kk, vv in nr.items()})

    _write_csv_rows(csv_path, header_out, rows)


# ----------------------------
# Process streaming + running
# ----------------------------

def stream_output(name: str, proc: subprocess.Popen, log_path: str):
    """
    Read lines from proc.stdout and write them both to:
      - terminal (prefixed with [name])
      - log file at log_path
    """
    with open(log_path, "w") as log_file:
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break

            sys.stdout.write(f"[{name}] {line}")
            sys.stdout.flush()

            log_file.write(line)
            log_file.flush()

    try:
        proc.stdout.close()
    except Exception:
        pass

def launch_sim(name: str, cfg_path: str) -> subprocess.Popen:
    """Launch one simulator process with a given config file."""
    if not os.path.exists(cfg_path):
        raise FileNotFoundError(f"Config not found: {cfg_path}")

    proc = subprocess.Popen(
        [sys.executable, "-u", SIM_SCRIPT, "--config", cfg_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    return proc


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)

    procs = []
    threads = []

    # We'll collect experiment metadata up front
    exp_meta: Dict[str, Dict[str, Any]] = {}

    for name, cfg_path in CONFIGS.items():
        cfg_info = read_memo_config(cfg_path)
        exp_meta[name] = {
            "run": name,
            "config": cfg_path,
            "shards_cfg": cfg_info.get("shards_cfg"),
            "blocktime_cfg": cfg_info.get("blocktime_cfg"),
            "blocksize_cfg": cfg_info.get("blocksize_cfg"),
        }

    # Start all runs in parallel
    for name, cfg_path in CONFIGS.items():
        print(f"Starting simulation {name} with config {cfg_path}")
        proc = launch_sim(name, cfg_path)
        procs.append((name, proc))

        log_path = os.path.join(LOG_DIR, f"{name}.log")
        t = threading.Thread(
            target=stream_output,
            args=(name, proc, log_path),
            daemon=True,
        )
        t.start()
        threads.append(t)

    # Wait for all processes to finish and build result rows
    result_rows: List[Dict[str, Any]] = []

    for name, proc in procs:
        ret = proc.wait()
        status = "OK" if ret == 0 else f"FAILED (exit={ret})"
        print(f"Simulation {name} finished: {status}")

        # Ensure the streamer thread has flushed the log for this run
        # (We'll also join all threads later, but this makes per-run parsing more reliable.)
        # Note: thread is daemon, but join below guarantees completion.
        log_path = os.path.join(LOG_DIR, f"{name}.log")

        metrics = parse_metrics_from_log(log_path)

        row = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            **exp_meta.get(name, {}),
            **metrics,
            "exit_status": status,
        }
        result_rows.append(row)

    # Ensure all streamer threads finish
    for t in threads:
        t.join()

    # Update/append into Results/memo_results.csv
    upsert_results_csv(RESULTS_CSV, ROW_KEY_FIELD, result_rows)

    print(f"\nUpdated results written to: {RESULTS_CSV}")


if __name__ == "__main__":
    main()
