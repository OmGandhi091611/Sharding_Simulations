# Sharding_Simulations

Python-based simulation framework to evaluate **non-sharded** (Bitcoin-style), **NEAR-like sharded**, and **MEMO-style sharded** designs under configurable network + workload parameters. The repo also includes **parallel runners** to execute entire experiment sweeps (non-sharded + NEAR + MEMO) and organize outputs into `Results/` and graph folders.

---

## Repository structure

```text
.
├── memo_config/              # JSON configs for MEMO experiments (vary shards, blocksize, etc.)
├── memo_graphs/              # Plots/figures generated for MEMO experiments
├── near_config/              # JSON configs for NEAR-like sharded experiments
├── near_graphs/              # Plots/figures generated for NEAR-like experiments
├── non_sharded_config/       # JSON configs for non-sharded (BTC/BCH/LTC/DOGE style) experiments
├── non_sharded_graphs/       # Plots/figures generated for non-sharded experiments
├── Paper/                    # Paper text/figures/tables (writeup, LaTeX, etc.)
├── Parallel_processes/       # Batch + parallel runners for ALL experiment families (non-sharded, NEAR, MEMO)
├── Results/                  # Collected outputs (CSVs/logs/summary tables) from runs
├── Validations/              # Validation scripts/results (sanity checks, reproducibility, baselines)
├── simulation.py             # Main simulator entry point (single run)
└── README.md
```

---

## Quick start

### 1) Create a Python environment (optional but recommended)
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2) Install dependencies
Install whatever `simulation.py` requires (example if you use SimPy / plotting / data tools):

```bash
pip install simpy pandas matplotlib numpy
```

---

## Running a single simulation

`simulation.py` is the **single-run entry point**. You typically run it with a JSON config:

```bash
python3 simulation.py --config non_sharded_config/<CONFIG>.json
python3 simulation.py --config near_config/<CONFIG>.json
python3 simulation.py --config memo_config/<CONFIG>.json
```

Examples:

```bash
# non-sharded baseline
python3 simulation.py --config non_sharded_config/btc_600s_262144.json

# NEAR-like sharded
python3 simulation.py --config near_config/near_s64.json

# MEMO sharded
python3 simulation.py --config memo_config/memo_s64.json
```

### Output
Depending on your simulator’s print mode, each run will:
- print progress + per-block summaries to the terminal
- emit a final summary line (often CSV-like) for easy aggregation

Collected/merged results should be stored under:
- `Results/` (recommended location for CSV/log tables)
- Currently it can be done manually only and the results will not be printed directly in this folder.
---

## Running full experiment sweeps in parallel (ALL families)

The folder **`Parallel_processes/`** contains batch runners that launch **many configs in parallel**.

This is used for:
- **non-sharded sweeps** (block size / block time sweeps across BTC/BCH/LTC/DOGE-style configs)
- **NEAR-like sweeps** (vary shard count, blocksize, coordination parameters)
- **MEMO sweeps** (vary shard count, blocksize, coordination style, verification mode, etc.)

### Typical usage
Go into the folder and run the appropriate runner:

```bash
cd Parallel_processes
python3 Parallel_processes/non_sharded_parallel.py
python3 Parallel_processes/near_parallel.py
python3 Parallel_processes/memo_parallel.py
```

> Names may differ in your repo—use `ls` to see what runner scripts exist:
```bash
ls
```

### What parallel runners usually do
Most runners follow this pattern:
- define a dictionary of `{experiment_name: config_path}`
- launch `simulation.py --config <path>` as a separate subprocess
- stream output to terminal (prefixed with `[name]`)
- write logs/results into `Results/` (or a logs folder)

If a runner has a variable like:
```py
SIM_SCRIPT = "simulation.py"
```
make sure it matches your actual simulator filename (it does in this repo).

---

## Graph generation

Graphs/plots are organized by experiment family:

- `non_sharded_graphs/`  → baseline plots (TPS vs blocksize, msg vs blocksize, blocktime vs blocksize)
- `near_graphs/`         → sharding plots for NEAR-like design (TPS vs shards, etc.)
- `memo_graphs/`         → sharding plots for MEMO (TPS vs shards, messages vs shards, etc.)

Your plotting scripts may live in:
- `Validations/` (if used for validation figures), **or**
- `Results/` (if you generate from aggregated CSVs), **or**
- inside each graphs folder (depends on your workflow)
- Currently working on the python file for the graphs as well.

A common pattern is:
1) Run sweeps → output CSV rows into `Results/*.csv`

---

## Validations

The `Validations/` folder is intended for:
- sanity checks (e.g., “non-sharded scales only up to a point”)
- consistency checks (sorted/monotonic metrics, stable output formats)
- regression comparisons when changing the simulator

If you add new features, consider adding a validation run + expected output here.

---

## Results organization (recommended convention)

If you want clean reproducibility, store:

- raw run logs: `Results/logs/<family>/<run_name>.log`
- aggregated tables: `Results/<family>_results.csv`
- final plots: `<family>_graphs/*.png` or `*.pdf`

This makes it easy to regenerate paper figures.

---

## Adding a new experiment

1) Create a new JSON config in one of:
- `non_sharded_config/`
- `near_config/`
- `memo_config/`

2) Test single-run:
```bash
python3 simulation.py --config <your_config_path.json>
```

3) Add it to the appropriate parallel runner inside `Parallel_processes/` so it runs in sweeps.

---

## Notes on performance

Parallel sweeps can be heavy (CPU + memory). If your laptop slows down:
- reduce the number of configs per run (run in batches)
- limit parallelism inside the runner (if supported)
- run large sweeps on a server/HPC node

---

## Paper

The `Paper/` folder contains the write-up and figure/table assets used in the paper.  
A typical workflow is:
- run sweeps → `Results/`
- generate figures → `*_graphs/`
- include figures/tables in `Paper/`

---

## License

Copyright (c) 2025 Om Gandhi

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

## Citation

If you use this simulator in a report/paper:

> Om Gandhi, *Sharding Simulations*, GitHub repository, 2025.
