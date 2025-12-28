# Sharding_Simulations

Python-based simulation framework to evaluate **non-sharded** (Bitcoin-style), **NEAR-like sharded**, and **MEMO-style sharded** designs under configurable network + workload parameters.

This repo includes:
- `simulation.py` for **single-run** experiments (`--config <file>.json`)
- parallel runners (in `Parallel_processes/`) to execute many configs concurrently
- `make_graphs.py` to read CSVs from `Results/` and write figures directly into:
  - `non_sharded_graphs/`
  - `near_graphs/`
  - `memo_graphs/`
  - `Validations/`

Important: `Results/` and all graph folders are expected to be at the **same directory level**.

---

## Repository structure

```text
.
├── memo_config/              # JSON configs for MEMO experiments
├── memo_graphs/              # Figures for MEMO experiments
├── near_config/              # JSON configs for NEAR-like sharded experiments
├── near_graphs/              # Figures for NEAR-like experiments
├── non_sharded_config/       # JSON configs for non-sharded experiments
├── non_sharded_graphs/       # Figures for non-sharded experiments (includes bubble plot)
├── Parallel_processes/       # Parallel sweep runners (non-sharded / NEAR / MEMO)
├── Results/                  # Aggregated CSV outputs from runs
├── Validations/              # Validation figures (saved here)
├── simulation.py             # Main simulator (single run)
├── make_graphs.py            # Plot generator (reads Results/, writes to *_graphs/ + Validations/)
├── requirements.txt
└── README.md
```

---

## Setup

### Python
Recommended: Python 3.10+ (3.11/3.12 also fine).

### Create a virtual environment (recommended)

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### Install dependencies

```bash
pip install -r requirements.txt
```

Minimal `requirements.txt`:

```txt
simpy
pandas
numpy
matplotlib
```

If you pin versions and your environment doesn’t satisfy them, `pip` may refuse to install until you upgrade/downgrade Python or packages. If you keep versions unpinned, installs are easier but plots may look slightly different across machines.

---

## Running a single simulation

`simulation.py` is the single-run entry point. Each run uses exactly one config JSON:

```bash
python3 simulation.py --config non_sharded_config/<CONFIG>.json
python3 simulation.py --config near_config/<CONFIG>.json
python3 simulation.py --config memo_config/<CONFIG>.json
```

---

## Simulation config (JSON): arguments explained

You edit these inside each JSON file in `memo_config/`, `near_config/`, `non_sharded_config/`.

Note: some keys may differ slightly between experiment families. Always check your config JSON to see which keys exist in that run.

### Identity + size
- `currency` — label written into the results CSV (e.g., `btc`, `near`, `memo`); change the string to rename the series.
- `nodes` — number of network nodes; increase/decrease to scale network size.
- `miners` — number of miners/block producers; change to model different competition levels.
- `wallets` — number of wallets generating transactions; change to model user population.
- `neighbors` — peer degree per node (overlay connectivity); higher = denser network.
- `shards` — shard count; `1` means conventional, `>1` means sharded.

### Workload generation
- `transactions` — total number of transactions produced/processed in a run; increase for longer/heavier experiments.
- `interval` — time (or step) between transaction-generation attempts; smaller = higher offered load.
- `tx_cost_ms` — per-transaction processing cost in milliseconds; increase to simulate slower validation/CPU.

### Block / throughput knobs
- `blocktime` — configured/target block interval (seconds); change to simulate faster/slower chains.
- `total_blocksize` — maximum transactions per block (capacity); change to evaluate bigger/smaller blocks.

### Network model
- `rtt_ms` — baseline RTT (ms); increase to simulate higher latency.
- `msg_size` — assumed message size (bytes) for protocol messages; increase for heavier communication.
- `control_bw_mbps` — bandwidth for control/coordination traffic (Mbps); reduce to stress coordination.
- `broadcast_bw_mbps` — bandwidth for broadcast traffic (Mbps); reduce to slow propagation.
- `overlap_broadcast` — whether broadcasts overlap/pipeline (`true`) or serialize (`false`); toggling changes propagation timing.

### Coordination / sharding overhead (if present in your configs)
- `coord_rounds` — number of coordination rounds per block; increase to model more overhead.
- `cost` — cost-model toggle/constant (if your simulator uses it); change only if you know how your sim interprets it.

### Printing / progress
- `print_int` — how often the simulator prints progress stats; increase to reduce log spam.

### Results output (where CSV rows go)
- `results_dir` — directory where CSV results are written (recommended: `Results`); change to redirect output.
- `results_csv` — filename inside `results_dir` (e.g., `non-sharded.csv`, `Near.csv`, `memo_results.csv`); change to route output into a specific CSV.

Mode convention in CSV output:
- if `shards == 1` → `mode = conventional`
- if `shards > 1` → `mode = sharded`

---

## Results CSV format

All result CSVs follow this column order:

```text
currency,nodes,wallets,miners,transactions,interval,shards,average block time,block size,messages,mode,tps,no. of blocks generated
```

Example:

```csv
currency,nodes,wallets,miners,transactions,interval,shards,average block time,block size,messages,mode,tps,no. of blocks generated
near,1000,1000,1000,1000,0.01,4,0.61,1800,559892,sharded,2959.9847,556
near,1000,1000,1000,1000,0.01,6,0.61,2100,482247,sharded,3418.7128,477
near,1000,1000,1000,1000,0.01,9,0.59,2400,424089,sharded,4068.2828,417
```

---

## Running experiment sweeps in parallel

Parallel runners live in `Parallel_processes/` and launch many configs concurrently.

```bash
cd Parallel_processes
python3 non_sharded_parallel.py
python3 near_parallel.py
python3 memo_parallel.py
```

Parallel runner script knobs (edit inside the runner file):
- `SIM_SCRIPT` — which sim file to execute (typically `simulation.py`); change only if you rename the sim file.
- `CONFIGS` — mapping of `run_name → path/to/config.json`; add/remove entries to change your sweep.
- `LOG_DIR` — folder where per-run logs are stored; rename to keep logs separated.

---

## Plot generation (`make_graphs.py`)

What it does:
- reads CSVs from `Results/`
- writes graphs directly into:
  - `non_sharded_graphs/`
  - `near_graphs/`
  - `memo_graphs/`
  - `Validations/`
- does not create any extra `plots/` directory

Run it:

```bash
python3 make_graphs.py --results_dir Results --no_show
```

### Graph script arguments explained

Inputs:
- `--results_dir` — folder containing CSVs (default: `Results`); change if your results folder differs.
- `--near_csv` — NEAR results filename inside `Results/` (default: `Near.csv`).
- `--memo_csv` — MEMO results filename inside `Results/` (default: `memo_results.csv`).
- `--non_csv` — non-sharded results filename inside `Results/` (default: `non-sharded.csv`).
- `--validation_csv` — validation results filename inside `Results/` (default: `Validation.csv`).

Outputs:
- `--near_out` — output folder for NEAR figures (default: `near_graphs`).
- `--memo_out` — output folder for MEMO figures (default: `memo_graphs`).
- `--non_out` — output folder for non-sharded figures (default: `non_sharded_graphs`).
- `--val_out` — output folder for validation figures (default: `Validations`).

Display + selection:
- `--no_show` — don’t display plots (save only); recommended on servers/Colab.
- `--skip_near` / `--skip_memo` / `--skip_non` / `--skip_validation` — skip generating that plot category.

Examples:

```bash
python3 make_graphs.py --no_show
python3 make_graphs.py --skip_validation --no_show
python3 make_graphs.py --near_csv Near.csv --near_out near_graphs --no_show
```

---

## Bubble plot (non-sharded vs MEMO S=1)

Saved into `non_sharded_graphs/` as:

```text
bubble_tps_blocktime_vs_blocksize_nonsharded_vs_memo_s1.png
```

Rules:
- X axis: block sizes taken only from `Results/non-sharded.csv`
- X spacing: categorical positions with a 2-unit gap between each block size (0,2,4,...)
- Y axis: `average block time`
- bubble size: `tps`
- MEMO series: uses `currency == "memo"` filtered to `shards == 1` (currency stays `memo`)

Even with identical CSV values, plots can look slightly different across Colab vs local due to matplotlib backends/fonts/DPI. For consistent visuals, pin versions and set explicit DPI/font in the plot script.

---

## Validations

Validation figures should be written into:

- `Validations/`

This keeps validation plots separated from the main experiment plots.

---

## License

MIT is a common choice for research code. Add a `LICENSE` file if you want a standalone license file.

---

## Citation

If you use this simulator in a report/paper, cite as:

Om Gandhi, *Sharding Simulations*, GitHub repository, 2025.
