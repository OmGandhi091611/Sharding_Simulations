#!/usr/bin/env python3
import subprocess
import sys
import os
import threading

# --- main simulator script ---
SIM_SCRIPT = "simulation.py"   # change if your file is named differently

# MEMO configs
CONFIGS = {
    "memo_s1": "memo_config/1.json",
    "memo_s2": "memo_config/2.json",
    "memo_s4": "memo_config/4.json",
    # "memo_s4_near_blocksize": "memo_config/4_near_blocksize.json",
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

            # Print to terminal with a tag so you know which run it is
            sys.stdout.write(f"[{name}] {line}")
            sys.stdout.flush()

            # Write to log file
            log_file.write(line)
            log_file.flush()

    proc.stdout.close()


def launch_sim(name: str, cfg_path: str) -> subprocess.Popen:
    """Launch one simulator process with a given config file."""
    if not os.path.exists(cfg_path):
        raise FileNotFoundError(f"Config not found: {cfg_path}")

    # -u => unbuffered stdout/stderr in the child
    proc = subprocess.Popen(
        [sys.executable, "-u", SIM_SCRIPT, "--config", cfg_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,  # line-buffered in the parent
    )
    return proc


def main():
    os.makedirs(LOG_DIR, exist_ok=True)

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
    for name, proc in procs:
        ret = proc.wait()
        status = "OK" if ret == 0 else f"FAILED (exit={ret})"
        print(f"Simulation {name} finished: {status}")

    # Ensure all streamer threads finish
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
