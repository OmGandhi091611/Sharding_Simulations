# Sharding Simulations

**A Python-based discrete-event simulation framework for evaluating non-sharded, NEAR-protocol-style, and MEMO-style sharded blockchain architectures under configurable network and workload conditions.**

> Om Gandhi — Illinois Institute of Technology
> Under review: IEEE ICDCS 2026

---

## Abstract

This framework provides a controlled simulation environment to study the throughput, latency, and communication overhead of three blockchain architectural designs: (1) a conventional non-sharded chain (Bitcoin-style), (2) a NEAR-protocol-inspired sharded design, and (3) a MEMO-style sharded design. Experiments are parameterized across shard count, block size, block time, and network topology (local, US-WAN, global-WAN), and results are aggregated for cross-condition comparison and visualization.

---

## Table of Contents

1. [Repository Structure](#repository-structure)
2. [Setup and Dependencies](#setup-and-dependencies)
3. [Single-Run Experiments](#single-run-experiments)
4. [Configuration Parameters](#configuration-parameters)
5. [Parallel Sweep Execution](#parallel-sweep-execution)
6. [Result Aggregation](#result-aggregation)
7. [Network Condition Variants](#network-condition-variants)
8. [Plot Generation](#plot-generation)
9. [Results CSV Format](#results-csv-format)
10. [Validation](#validation)
11. [Citation](#citation)

---

## Repository Structure

```
.
├── memo_config/              # JSON configs for MEMO experiments
├── memo_graphs/              # Output figures for MEMO experiments
│   ├── local/                #   Local network condition plots
│   ├── usa/                  #   US WAN condition plots
│   └── global/               #   Global WAN condition plots
├── near_config/              # JSON configs for NEAR-like experiments
├── near_graphs/              # Output figures for NEAR-like experiments
├── non_sharded_config/       # JSON configs for non-sharded experiments
├── non_sharded_graphs/       # Output figures for non-sharded (includes bubble plot)
├── Parallel_processes/       # Parallel sweep runners
├── Results/                  # Aggregated CSV outputs
│   ├── memo_results_local.csv
│   ├── memo_results_usa.csv
│   ├── memo_results_global.csv
│   ├── Near.csv
│   ├── non-sharded.csv
│   └── Validation.csv
├── Validations/              # Validation figures
├── simulation.py             # Main simulator entry point (single run)
├── make_graphs.py            # Plot generator
├── merge_results.c           # OpenMP-parallel CSV merger (see Result Aggregation)
└── requirements.txt
```

> `Results/` and all graph folders must reside at the **same directory level** as `make_graphs.py`.

---

## Setup and Dependencies

### Requirements

- Python 3.10 or later (3.11 / 3.12 supported)
- GCC with OpenMP support (for `merge_results.c`)

### Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Minimal `requirements.txt`:

```
simpy
pandas
numpy
matplotlib
```

### Compile the CSV merger

```bash
gcc -O2 -fopenmp merge_results.c -o merge_results
```

---

## Single-Run Experiments

Each experiment run consumes exactly one JSON config file:

```bash
python3 simulation.py --config non_sharded_config/<CONFIG>.json
python3 simulation.py --config near_config/<CONFIG>.json
python3 simulation.py --config memo_config/<CONFIG>.json
```

Results are appended to the CSV file specified by `results_dir` and `results_csv` inside the config.

---

## Configuration Parameters

All parameters are set inside the JSON config files in `memo_config/`, `near_config/`, or `non_sharded_config/`. Keys may vary slightly between experiment families.

### Identity and scale

| Key | Description |
|---|---|
| `currency` | Label written into results CSV (e.g., `btc`, `near`, `memo`) |
| `nodes` | Number of network nodes |
| `miners` | Number of block producers |
| `wallets` | Number of transaction-generating wallets |
| `neighbors` | Peer degree per node (overlay connectivity) |
| `shards` | Shard count; `1` = conventional chain, `>1` = sharded |

### Workload

| Key | Description |
|---|---|
| `transactions` | Total transactions produced per run |
| `interval` | Time between transaction-generation attempts |
| `tx_cost_ms` | Per-transaction processing cost (ms) |

### Block parameters

| Key | Description |
|---|---|
| `blocktime` | Configured target block interval (seconds) |
| `total_blocksize` | Maximum transactions per block |

### Network model

| Key | Description |
|---|---|
| `rtt_ms` | Baseline round-trip time (ms) |
| `msg_size` | Protocol message size (bytes) |
| `control_bw_mbps` | Bandwidth for coordination traffic (Mbps) |
| `broadcast_bw_mbps` | Bandwidth for broadcast traffic (Mbps) |
| `overlap_broadcast` | Whether broadcasts pipeline (`true`) or serialize (`false`) |

### Sharding overhead

| Key | Description |
|---|---|
| `coord_rounds` | Coordination rounds per block |
| `cost` | Cost-model toggle (if used by your sim) |

### Output routing

| Key | Description |
|---|---|
| `results_dir` | Directory for CSV output (recommended: `Results`) |
| `results_csv` | Filename within `results_dir` |

> Mode convention: `shards == 1` → `mode = conventional`; `shards > 1` → `mode = sharded`

---

## Parallel Sweep Execution

Parallel runners in `Parallel_processes/` launch many configs concurrently. Each simulation run writes to its own per-run CSV and log file inside `Results/runs/`, avoiding any file-write contention.

```bash
python Parallel_processes/non_sharded_parallel.py
python Parallel_processes/near_parallel.py
python Parallel_processes/memo_parallel.py
```

Editable knobs inside each runner:

| Key | Description |
|---|---|
| `SIM_SCRIPT` | Simulator file to execute (default: `simulation.py`) |
| `CONFIGS` | Mapping of `run_name → path/to/config.json` |
| `LOG_DIR` | Directory for per-run log files |

---

## Result Aggregation

After a parallel sweep, individual per-run CSVs are merged into a single consolidated output using the OpenMP-parallelized C merger. **The merger also automatically deletes all input CSVs and their matching `.log` files after a successful merge**, keeping the workspace clean.

```bash
./merge_results Results/memo_results_local.csv Results/runs/run_*.csv
```

General usage:

```bash
./merge_results <output.csv> <input1.csv> [input2.csv ...]
```

The merger:
- Reads all input files concurrently using OpenMP
- Writes a single header + one data row per input file to the output
- Removes all input CSVs and matching `.log` files only after the output is fully written and closed

---

## Network Condition Variants

Experiments are conducted under three network conditions to evaluate performance across deployment environments. Each condition writes to a separate results CSV to prevent overwriting.

| Condition | `results_csv` | Description |
|---|---|---|
| Local | `memo_results_local.csv` | Low latency, high bandwidth (server / LAN) |
| US WAN | `memo_results_usa.csv` | Moderate latency, continental-scale network |
| Global WAN | `memo_results_global.csv` | High latency, intercontinental network |

Relevant JSON knobs to differentiate conditions:

```json
// Local
{ "rtt_ms": 5, "control_bw_mbps": 1000, "broadcast_bw_mbps": 1000 }

// US WAN
{ "rtt_ms": 50, "control_bw_mbps": 100, "broadcast_bw_mbps": 100 }

// Global WAN
{ "rtt_ms": 200, "control_bw_mbps": 50, "broadcast_bw_mbps": 50 }
```

---

## Plot Generation

`make_graphs.py` reads CSVs from `Results/` and writes figures into the appropriate output folders. It does not create any additional `plots/` subdirectory.

### Generate all plots

```bash
python3 make_graphs.py --results_dir Results --no_show
```

### Generate MEMO plots only (all three network conditions)

```bash
# Local
python3 make_graphs.py \
  --memo_csv memo_results_local.csv \
  --memo_out memo_graphs/local \
  --skip_near --skip_non --skip_validation --no_show

# US WAN
python3 make_graphs.py \
  --memo_csv memo_results_usa.csv \
  --memo_out memo_graphs/usa \
  --skip_near --skip_non --skip_validation --no_show

# Global WAN
python3 make_graphs.py \
  --memo_csv memo_results_global.csv \
  --memo_out memo_graphs/global \
  --skip_near --skip_non --skip_validation --no_show
```

### All arguments

**Inputs:**

| Argument | Default | Description |
|---|---|---|
| `--results_dir` | `Results` | Folder containing CSVs |
| `--near_csv` | `Near.csv` | NEAR results filename |
| `--memo_csv` | `memo_results.csv` | MEMO results filename |
| `--non_csv` | `non-sharded.csv` | Non-sharded results filename |
| `--validation_csv` | `Validation.csv` | Validation results filename |

**Outputs:**

| Argument | Default | Description |
|---|---|---|
| `--near_out` | `near_graphs` | Output folder for NEAR figures |
| `--memo_out` | `memo_graphs` | Output folder for MEMO figures |
| `--non_out` | `non_sharded_graphs` | Output folder for non-sharded figures |
| `--val_out` | `Validations` | Output folder for validation figures |

**Display and selection:**

| Argument | Description |
|---|---|
| `--no_show` | Save only, do not display plots (recommended on servers) |
| `--skip_near` | Skip NEAR plot generation |
| `--skip_memo` | Skip MEMO plot generation |
| `--skip_non` | Skip non-sharded plot generation |
| `--skip_validation` | Skip validation plot generation |

---

## Results CSV Format

All result CSVs follow a consistent column schema:

```
currency, nodes, wallets, miners, transactions, interval, shards,
average block time, block size, messages, mode, tps, no. of blocks generated
```

Example (NEAR):

```csv
currency,nodes,wallets,miners,transactions,interval,shards,average block time,block size,messages,mode,tps,no. of blocks generated
near,1000,1000,1000,1000,0.01,4,0.61,1800,559892,sharded,2959.98,556
near,1000,1000,1000,1000,0.01,6,0.61,2100,482247,sharded,3418.71,477
near,1000,1000,1000,1000,0.01,9,0.59,2400,424089,sharded,4068.28,417
```

---

## Validation

Validation compares simulator output against known real-world values for Bitcoin, Bitcoin Cash, Litecoin, and Dogecoin. Figures are written to `Validations/` and kept separate from experiment plots.

```bash
python3 make_graphs.py --skip_near --skip_memo --skip_non --no_show
```

---

## Citation

If you use this simulator in a report or publication, please cite:

```
Om Gandhi, "Sharding Simulations," GitHub repository, 2025.
```

---

## License

MIT License. See `LICENSE` for details.