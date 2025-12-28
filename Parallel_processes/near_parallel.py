#!/usr/bin/env python3
import subprocess
import sys
import os
import threading
import json
import csv
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple

# --- adjust this if your main sim file has a different name ---
SIM_SCRIPT = "simulation.py"

CONFIGS = {
    "near_obs_s4": "near_config/4.json",
    "near_obs_s6": "near_config/6.json",
    "near_obs_s9": "near_config/9.json",
}

LOG_DIR = "near_logs"

# Your NEAR results file name (as you requested)
RESULTS_CSV = "Near.csv"

# Used to decide update vs append
ROW_KEY_FIELD = "config"


# ----------------------------
# Config reading helpers
# ----------------------------

def _first_present(d: Dict[str, Any], keys: List[str]) -> Optional[Any]:
    for k in keys:
        if k in d:
            return d[k]
    return None


def read_config_fields(cfg_path: str) -> Dict[str, Any]:
    """
    Read config JSON and extract shards/blocktime/blocksize using common key fallbacks.
    This keeps your runner resilient even if near configs use different key names.
    """
    with open(cfg_path, "r") as f:
        cfg = json.load(f)

    shards = _first_present(cfg, ["shards", "num_shards", "shard_count", "S"])
    blocktime = _first_present(cfg, ["blocktime", "target_blocktime", "block_interval", "interval_seconds"])
    blocksize = _first_present(cfg, ["total_blocksize", "blocksize", "block_size", "max_block_size", "block_size_txs"])

    return {
        "shards_cfg": shards,
        "blocktime_cfg": blocktime,
        "blocksize_cfg": blocksize,
        "config_raw": cfg,  # optional: keep if you want later
    }


# ----------------------------
# Log parsing: RESULT_JSON
# ----------------------------

def parse_result_json_from_log(log_path: str) -> Dict[str, Any]:
    """
    Reads the log and extracts the JSON payload from a line:
      RESULT_JSON {...}

    This requires your simulation.py to print that line at the end (recommended).
    """
    if not os.path.exists(log_path):
        return {}

    last_payload = None
    with open(log_path, "r") as f:
        for line in f:
            if line.startswith("RESULT_JSON "):
                payload_str = line[len("RESULT_JSON "):].strip()
                try:
                    last_payload = json.loads(payload_str)
                except Exception:
                    # If JSON is malformed for some reason, ignore and continue scanning
                    pass

    return last_payload or {}


# ----------------------------
# CSV upsert helpers
# ----------------------------

def read_csv_rows(path: str) -> Tuple[List[str], List[Dict[str, str]]]:
    if not os.path.exists(path):
        return [], []
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        rows = list(reader)
    return header, rows


def write_csv_rows(path: str, header: List[str], rows: List[Dict[str, Any]]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            out = {}
            for k in header:
                v = r.get(k)
                out[k] = "" if v is None else v
            writer.writerow(out)


def upsert_results_csv(csv_path: str, key_field: str, new_rows: List[Dict[str, Any]]) -> None:
    header, rows = read_csv_rows(csv_path)

    index_by_key = {}
    for i, r in enumerate(rows):
        index_by_key[r.get(key_field, "")] = i

    all_fields = set(header)
    for nr in new_rows:
        all_fields.update(nr.keys())

    preferred_order = [
        "timestamp",
        "run",
        "config",
        "exit_status",
        # config-derived
        "shards_cfg",
        "blocktime_cfg",
        "blocksize_cfg",
        # result_json-derived (common)
        "currency",
        "nodes",
        "wallets",
        "miners",
        "transactions",
        "interval",
        "shards",
        "average_block_time",
        "block_size",
        "messages",
        "mode",
        "tps",
        "throughput_shard",
        "num_blocks",
        "blocktime_cfg_sim",
        "expected_blocktime",
    ]
    ordered = [f for f in preferred_order if f in all_fields]
    remaining = sorted([f for f in all_fields if f not in ordered])
    header_out = ordered + remaining

    for nr in new_rows:
        k = str(nr.get(key_field, ""))
        if k in index_by_key:
            rows[index_by_key[k]].update(nr)
        else:
            rows.append(nr)

    write_csv_rows(csv_path, header_out, rows)


# ----------------------------
# Process launch + streaming
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

    # read config metadata up-front
    meta: Dict[str, Dict[str, Any]] = {}
    for name, cfg_path in CONFIGS.items():
        cfg_info = read_config_fields(cfg_path)
        meta[name] = {
            "run": name,
            "config": cfg_path,
            "shards_cfg": cfg_info.get("shards_cfg"),
            "blocktime_cfg": cfg_info.get("blocktime_cfg"),
            "blocksize_cfg": cfg_info.get("blocksize_cfg"),
        }

    procs = []
    threads = []

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

    # Wait for all processes to finish
    exit_status: Dict[str, str] = {}
    for name, proc in procs:
        ret = proc.wait()
        status = "OK" if ret == 0 else f"FAILED (exit={ret})"
        exit_status[name] = status
        print(f"Simulation {name} finished: {status}")

    # Ensure all streamer threads finish (flush logs)
    for t in threads:
        t.join()

    # Parse RESULT_JSON from logs + build rows
    new_rows: List[Dict[str, Any]] = []
    for name, _proc in procs:
        log_path = os.path.join(LOG_DIR, f"{name}.log")
        result_json = parse_result_json_from_log(log_path)

        row = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            **meta.get(name, {}),
            "exit_status": exit_status.get(name, ""),
        }

        if result_json:
            # Merge all fields from RESULT_JSON into row
            row.update(result_json)

            # Optional: avoid clobbering our config-derived fields by renaming sim’s blocktime
            if "blocktime_cfg" in result_json:
                row["blocktime_cfg_sim"] = result_json.get("blocktime_cfg")
        else:
            # If RESULT_JSON isn't present, still write the config + status row
            row["parse_error"] = "Missing RESULT_JSON in log"

        new_rows.append(row)

    # Upsert into Near.csv
    upsert_results_csv(RESULTS_CSV, ROW_KEY_FIELD, new_rows)

    print(f"\nUpdated results written to: {RESULTS_CSV}")


if __name__ == "__main__":
    main()
