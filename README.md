# Sharding Simulations

**A Python-based discrete-event simulation framework for evaluating non-sharded, NEAR-protocol-style, and MEMO-style sharded blockchain architectures under configurable network and workload conditions.**

> Om Amit Gandhi — Illinois Institute of Technology
> Under review: IEEE/ACM MASCOTS 2026

If you find this useful, please consider giving it a star ⭐ — it helps others find the work.

---

## Abstract

This framework provides a controlled simulation environment to study the throughput, latency, and communication overhead of three blockchain architectural designs: (1) a conventional non-sharded chain (Bitcoin-style), (2) a NEAR-protocol-inspired sharded design, and (3) a MEMO-style sharded design. Experiments are parameterized across shard count, block size, block time, and network topology (local, US-WAN, global-WAN), and results are aggregated for cross-condition comparison and visualization.

---

## Table of Contents

1. [Repository Structure](#repository-structure)
2. [Setup and Dependencies](#setup-and-dependencies)
3. [How the Simulation Works](#how-the-simulation-works)
4. [Chain 1 — Non-Sharded (BTC Hypothetical)](#chain-1--non-sharded-btc-hypothetical)
5. [Chain 2 — NEAR-Protocol-Inspired](#chain-2--near-protocol-inspired)
6. [Chain 3 — MEMO-Style Sharded](#chain-3--memo-style-sharded)
7. [Configuration Parameters](#configuration-parameters)
8. [Parallel Sweep Execution](#parallel-sweep-execution)
9. [Result Aggregation](#result-aggregation)
10. [Network Condition Variants](#network-condition-variants)
11. [Plot Generation](#plot-generation)
12. [Graphs Produced Per Chain](#graphs-produced-per-chain)
13. [Results CSV Format](#results-csv-format)
14. [Validation](#validation)
15. [Citation](#citation)

---

## Repository Structure

```
.
├── simulation.py                  # Main discrete-event simulator (SimPy)
├── graph.py                       # Plot generator — reads CSVs, writes PNG figures
├── merge_results.c                # OpenMP-parallel CSV merger
├── requirements.txt               # Python dependencies
├── LICENSE
│
├── memo_config/
│   └── base.json                  # Base parameters for all MEMO runs
├── near_config/
│   ├── 4.json                     # 4-shard NEAR config
│   ├── 6.json                     # 6-shard NEAR config
│   ├── 9.json                     # 9-shard NEAR config
│   └── million.json               # Large-scale optional config
├── non_sharded_config/
│   └── base.json                  # Non-sharded (BTC-style) base config
│
├── Parallel_processes/
│   ├── memo_parallel.py           # Grid sweep: shards × blocksize × blocktime × sig_scheme
│   ├── near_parallel.py           # Fixed 3-run sweep (4/6/9 shards)
│   └── non_sharded_parallel.py    # Grid sweep: blocksize × blocktime
│
├── Results/
│   ├── memo_results_local.csv     # MEMO results — local network (RTT ~2 ms)
│   ├── memo_results_usa.csv       # MEMO results — US WAN (RTT ~50 ms)
│   ├── memo_results_global.csv    # MEMO results — global WAN (RTT ~150 ms)
│   ├── Near.csv                   # NEAR 4/6/9-shard results
│   ├── non-sharded.csv            # Non-sharded results
│   └── Validation.csv             # Validation against real cryptocurrencies
│
├── memo_graphs_local/             # MEMO figures — local condition
├── memo_graphs_usa/               # MEMO figures — US WAN condition
├── memo_graphs_global/            # MEMO figures — global WAN condition
├── near_graphs/                   # NEAR bar charts vs targets
├── non_sharded_graphs/            # Non-sharded heatmaps
├── Validations/                   # Real-world comparison figures
│
├── memo_logs/                     # Per-run stdout logs for MEMO
├── near_logs/                     # Per-run stdout logs for NEAR
└── environment/                   # Python virtual environment
```

> `Results/` and all graph folders must reside at the **same directory level** as `graph.py`.

---

## Setup and Dependencies

### Requirements

- Python 3.10 or later (3.11 / 3.12 supported)
- GCC with OpenMP support (for `merge_results.c`)

### Python environment

```bash
python3 -m venv environment
source environment/bin/activate
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

## How the Simulation Works

The core engine (`simulation.py`) is a **SimPy discrete-event simulation**. Every event — transaction generation, block mining, message propagation, and coordination rounds — is scheduled on a virtual clock. No real time passes between events; only the modeled latency and compute costs advance the clock.

### Execution flow

1. **Transaction generation** — Wallets generate transactions at a configurable interval and push them into a shared transaction pool. The pool operates in O(1) count mode by default (no per-transaction objects until a block is assembled).

2. **Mining** — Each miner samples an exponential inter-arrival time derived from the target `blocktime` and the current difficulty. The first miner to fire wins the block. Difficulty auto-adjusts based on shard count and target blocktime; a halving schedule is supported.

3. **Block assembly** — The winning miner pulls up to `total_blocksize` transactions from the pool and pays a per-transaction verification cost (`tx_cost_ms`). The cryptographic overhead depends on `sig_scheme` (see below).

4. **Propagation** — Blocks flood through the peer overlay. Each hop incurs RTT/2 + jitter + transmission delay (derived from block size and `control_bw_mbps`). Three propagation modes are supported:
   - `exact` — explicit per-hop SimPy events
   - `gossip` — bounded fanout
   - `analytic` — O(1) closed-form approximation

5. **Coordination** — After each block, shards exchange control messages. Three coordination modes exist:

   | Mode | Description |
   |---|---|
   | `coord` (standard metronome) | All shards coordinate every round; message count = `2·S + S·(S−1)/2 + N` |
   | `coord_no_metronome` | Winners announce dynamically; pairwise reconciliation; lower overhead |
   | `coord_leader_metronome` | Designated leader per round; only winning shards report; used by MEMO and NEAR |

6. **Metrics collection** — At the end of the run the simulator writes one row to a CSV with TPS, average block time, message count, network bytes, and other counters.

### Signature schemes

The `sig_scheme` key selects a crypto cost template that sets `tx_cost_ms` and message sizes:

| Scheme | Verify cost (ms) | TX size (bytes) |
|---|---|---|
| `ed25519` | 0.05 | 128 |
| `dilithium2` | 0.06 | 2 500 |
| `falcon512` | 0.014 | 750 |
| `sphincs_sha2_128s` | 1.04 | 7 950 |

### Key metrics collected per run

| Metric | Column in CSV | Description |
|---|---|---|
| TPS | `tps` | Transactions confirmed per second across all shards |
| Throughput per shard | `throughput_shard` | `tps / shards` — load distribution |
| Average block time | `average block time` | Actual seconds between block generations |
| Messages | `messages` | Total control-plane coordination messages |
| Network data | _(logged, not in CSV)_ | Total bytes transmitted (blocks + control) |

---

## Chain 1 — Non-Sharded (BTC Hypothetical)

A single-chain Bitcoin-style design with `shards = 1` and `mode = conventional`. Used as the performance baseline.

### Config

`non_sharded_config/base.json` — sets `currency = "btc_hypothetical"` and `shards = 1`.

### What the sweep covers

`Parallel_processes/non_sharded_parallel.py` runs a **7 × 20 = 140-simulation grid**:

| Dimension | Values |
|---|---|
| Block sizes | 4 096, 8 192, 16 384, 32 768, 65 536, 131 072, 262 144 tx/block |
| Block times | 600, 540, 510, …, 30, 1 s (20 values) |

Per combination: `transactions = blocksize × 200`, `wallets = transactions`.

### Run

```bash
python Parallel_processes/non_sharded_parallel.py
./merge_results Results/non-sharded.csv Results/runs/run_*.csv
```

### Output graphs

Stored in `non_sharded_graphs/`:

| File | Description |
|---|---|
| `heatmap_tps_blocktime_vs_blocksize_btc_hypothetical.png` | TPS heatmap — X: block size, Y: actual block time binned in 30 s ranges, color: TPS (red→orange→yellow→sky blue) |

Generate the heatmap alone:

```bash
python graph.py --no_show --skip_near --skip_memo --skip_memo_msg --skip_validation --skip_sig_schemes
```

---

## Chain 2 — NEAR-Protocol-Inspired

A sharded design based on the NEAR protocol with fixed shard counts (4, 6, and 9). Uses `coord_leader_metronome` coordination. Targets are compared against published NEAR performance figures.

### Configs

| File | Shards | Target TPS | Target block time |
|---|---|---|---|
| `near_config/4.json` | 4 | 3 000 | 0.6 s |
| `near_config/6.json` | 6 | 3 500 | 0.6 s |
| `near_config/9.json` | 9 | 4 000 | 0.6 s |

### What the sweep covers

`Parallel_processes/near_parallel.py` runs **3 fixed configurations** — one per shard count — using pre-set nodes (1 000), miners, wallets, and block parameters from each JSON.

### Run

```bash
python Parallel_processes/near_parallel.py
./merge_results Results/Near.csv Results/near_runs/*.csv
```

Or run a single config directly:

```bash
python3 simulation.py --config near_config/4.json
python3 simulation.py --config near_config/6.json
python3 simulation.py --config near_config/9.json
```

### Output graphs

Stored in `near_graphs/`:

| File | Description |
|---|---|
| `near_tps_comparison.png` | Simulated vs target TPS for 4, 6, and 9 shards (side-by-side bars) |
| `near_blocktime_comparison.png` | Simulated vs target average block time per shard count |

Generate NEAR graphs alone:

```bash
python graph.py --no_show --skip_memo --skip_memo_msg --skip_non --skip_validation --skip_sig_schemes
```

---

## Chain 3 — MEMO-Style Sharded

A novel sharded architecture with leader-based coordination. This is the primary design under study. Experiments sweep shard count, block size, block time, and cryptographic signature scheme across three network conditions.

### Config

`memo_config/base.json` — shared base for all MEMO runs. Network condition is selected by editing `rtt_ms` and `control_bw_mbps` before each sweep (see [Network Condition Variants](#network-condition-variants)).

### What the sweep covers

`Parallel_processes/memo_parallel.py` runs a **Cartesian product** of:

| Dimension | Values | Count |
|---|---|---|
| Shard counts | 1, 2, 4, 8, 16, 32, 64, 128, 256, 512 | 10 |
| Block sizes | 1 024, 2 048, 4 096, 8 192, 16 384, 32 768, 65 536, 131 072, 262 144, 524 288 | 10 |
| Block times | 1 200, 600, 300, 150, 75, 37.5, 18.75, 9.375, 4.688, 2.344, 1.172, 0.586, 0.293, 0.146 | 14 |
| Signature schemes | ed25519, dilithium2, falcon512, sphincs_sha2_128s | 4 |

**Total: 10 × 10 × 14 × 4 = 5 600 simulations per network condition.**

Node count scales with shard count: 128→256 nodes, 256→512 nodes, 512→1 000 nodes, else→100 nodes.

### Run (one network condition at a time)

```bash
# 1. Edit memo_config/base.json to set rtt_ms and control_bw_mbps
# 2. Run the sweep
python Parallel_processes/memo_parallel.py
# 3. Merge into the matching CSV
./merge_results Results/memo_results_local.csv Results/runs/run_*.csv
```

Repeat steps 1–3 for each network condition, directing output to the appropriate CSV (`memo_results_local.csv`, `memo_results_usa.csv`, `memo_results_global.csv`).

### Run (per-signature-scheme)

```bash
# Edit memo_config/base.json to set sig_scheme, then:
python Parallel_processes/memo_parallel.py
./merge_results Results/memo_results_ed25519.csv Results/runs/run_*.csv
```

Repeat for `dilithium2`, `falcon512`, and `sphincs_sha2_128s`.

### Output graphs

**TPS vs shards (faceted by block size)** — stored in `memo_graphs_<condition>/`:

| File | Description |
|---|---|
| `memo_tps_vs_shards_part1.png` | 2×2 grid — 4 largest block sizes; one line per block time |
| `memo_tps_vs_shards_part2.png` | 2×3 grid — remaining block sizes |
| `memo_tps_vs_shards_all_blocksizes.png` | Full grid, log scale |

**Messages vs shards** — stored in `memo_graphs_<condition>/`:

| File | Description |
|---|---|
| `memo_messages_vs_shards.png` | Single graph — coordination messages vs shard count; one colored line per block size. If all block sizes produce essentially the same message counts (relative std < 0.1%), collapses automatically to a single representative line. |

**Per-signature-scheme graphs** — same structure, stored in `memo_graphs_<scheme>/`:

```
memo_graphs_ed25519/
memo_graphs_dilithium2/
memo_graphs_falcon512/
memo_graphs_sphincs_sha2_128s/
```

Generate MEMO graphs for all three network conditions:

```bash
python graph.py --results_dir Results --memo_csv memo_results_local.csv  --memo_out memo_graphs_local  --memo_msg_out memo_graphs_local  --skip_near --skip_non --skip_validation --no_show
python graph.py --results_dir Results --memo_csv memo_results_usa.csv    --memo_out memo_graphs_usa    --memo_msg_out memo_graphs_usa    --skip_near --skip_non --skip_validation --no_show
python graph.py --results_dir Results --memo_csv memo_results_global.csv --memo_out memo_graphs_global --memo_msg_out memo_graphs_global --skip_near --skip_non --skip_validation --no_show
```

Generate only the messages-vs-shards graph for all three conditions:

```bash
python graph.py --no_show --skip_near --skip_memo --skip_memo_bt --skip_non --skip_validation --skip_sig_schemes --memo_csv memo_results_local.csv  --memo_msg_out memo_graphs_local
python graph.py --no_show --skip_near --skip_memo --skip_memo_bt --skip_non --skip_validation --skip_sig_schemes --memo_csv memo_results_usa.csv    --memo_msg_out memo_graphs_usa
python graph.py --no_show --skip_near --skip_memo --skip_memo_bt --skip_non --skip_validation --skip_sig_schemes --memo_csv memo_results_global.csv --memo_msg_out memo_graphs_global
```

Generate the blocktime-vs-shards graph filtered to a specific block size:

```bash
python graph.py --no_show --skip_near --skip_memo --skip_memo_msg --skip_non --skip_validation --skip_sig_schemes \
  --memo_csv memo_results_global.csv --memo_bt_out memo_graphs_global --memo_bt_blocksize 4096
# Output: memo_graphs_global/memo_blocktime_vs_shards_bs4096.png
```

Generate per-signature-scheme graphs (requires `memo_results_<scheme>.csv` in `Results/`):

```bash
python graph.py --no_show --skip_near --skip_non --skip_validation
# graph.py auto-detects memo_results_ed25519.csv etc. and writes to memo_graphs_<scheme>/
```

---

## Configuration Parameters

All parameters live in the JSON config files under `memo_config/`, `near_config/`, or `non_sharded_config/`.

### Identity and scale

| Key | Description |
|---|---|
| `currency` | Label written into results CSV (`btc_hypothetical`, `near`, `memo`) |
| `nodes` | Number of network nodes |
| `miners` | Number of block producers |
| `wallets` | Number of transaction-generating wallets |
| `neighbors` | Peer degree per node (overlay connectivity, typically 50) |
| `shards` | Shard count — `1` = conventional chain, `>1` = sharded |

### Workload

| Key | Description |
|---|---|
| `transactions` | Total transactions produced per run |
| `interval` | Time between transaction-generation attempts (seconds) |
| `tx_cost_ms` | Per-transaction verification cost (ms); overridden by `sig_scheme` |

### Block parameters

| Key | Description |
|---|---|
| `blocktime` | Target block interval (seconds) |
| `total_blocksize` | Maximum transactions per block |
| `blocks` | Number of blocks to simulate (alternative to `years`) |
| `years` | Simulate N years of chain activity (alternative to `blocks`) |
| `prefill` | Pre-populate transaction pool to skip warmup (`true`/`false`) |

### Network model

| Key | Description |
|---|---|
| `rtt_ms` | Baseline round-trip time (ms) |
| `msg_size` | Protocol control message size (bytes) |
| `control_bw_mbps` | Bandwidth for coordination traffic (Mbps) |
| `broadcast_bw_mbps` | Bandwidth for block broadcast traffic (Mbps) |
| `overlap_broadcast` | Whether broadcasts pipeline (`true`) or serialize (`false`) |
| `msg_proc_ms` | CPU cost per received message (ms) |

### Sharding and coordination

| Key | Description |
|---|---|
| `coord_rounds` | Coordination rounds per block |
| `leader_metronome` | Use leader-based coordination (MEMO and NEAR) |
| `no_metronome` | Use pairwise winner announcement |
| `verify_mode` | `"leader"`, `"shard"`, or `"leader_par"` — where verification happens |

### Cryptographic scheme

| Key | Description |
|---|---|
| `sig_scheme` | `ed25519`, `dilithium2`, `falcon512`, or `sphincs_sha2_128s` — sets TX size and verify cost |

### Output routing

| Key | Description |
|---|---|
| `results_dir` | Directory for CSV output (recommended: `Results`) |
| `results_csv` | Filename within `results_dir` |

> Mode convention: `shards == 1` → `mode = conventional`; `shards > 1` → `mode = sharded`.

---

## Parallel Sweep Execution

Parallel runners in `Parallel_processes/` launch many configs concurrently. Each run writes to its own per-run CSV inside `Results/runs/` and its own log file in the protocol log directory, avoiding all write contention.

```bash
python Parallel_processes/non_sharded_parallel.py
python Parallel_processes/near_parallel.py
python Parallel_processes/memo_parallel.py
```

Control the worker pool size:

```bash
GRID_WORKERS=32 python Parallel_processes/memo_parallel.py
```

### Changing the MEMO sweep grid

Open `Parallel_processes/memo_parallel.py` and edit the lists near the top:

```python
SHARD_COUNTS = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]

BLOCK_SIZES = [1024, 2048, 4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288]

BLOCK_TIMES = [1200, 600, 300, 150, 75, 37.5, 18.75, 9.375, 4.6875,
               2.34375, 1.171875, 0.5859375, 0.29296875, 0.146484375]

SIG_SCHEMES = ["ed25519", "dilithium2", "falcon512", "sphincs_sha2_128s"]
```

The runner computes every combination automatically. Total runs = `len(SHARD_COUNTS) × len(BLOCK_SIZES) × len(BLOCK_TIMES) × len(SIG_SCHEMES)`.

### Editable knobs

| Key | Description |
|---|---|
| `SIM_SCRIPT` | Simulator file to execute (default: `simulation.py`) |
| `BASE_CONFIG` | Base JSON for common params |
| `LOG_DIR` | Directory for per-run log files |
| `RUNS_DIR` | Directory for per-run CSV outputs (default: `Results/runs/`) |
| `FINAL_CSV` | Merged output CSV path |

---

## Result Aggregation

After a parallel sweep, merge the per-run CSVs into one consolidated file using the OpenMP-parallelized C merger. **The merger deletes all input CSVs and their matching `.log` files after a successful merge.**

```bash
./merge_results Results/memo_results_local.csv Results/runs/run_*.csv
./merge_results Results/Near.csv               Results/near_runs/*.csv
./merge_results Results/non-sharded.csv        Results/non_sharded_runs/*.csv
```

General usage:

```bash
./merge_results <output.csv> <input1.csv> [input2.csv ...]
```

The merger reads all input files concurrently (OpenMP), writes a single header + one data row per input, then removes inputs only after the output is fully written and closed.

---

## Network Condition Variants

Three network conditions model different deployment environments. Each writes to a separate results CSV.

| Condition | `rtt_ms` | `control_bw_mbps` | `results_csv` | Graph folder |
|---|---|---|---|---|
| Local | 2 | 2 000 | `memo_results_local.csv` | `memo_graphs_local/` |
| US WAN | 50 | 200 | `memo_results_usa.csv` | `memo_graphs_usa/` |
| Global WAN | 150 | 100 | `memo_results_global.csv` | `memo_graphs_global/` |

Edit `memo_config/base.json` before each sweep:

```json
{ "rtt_ms": 2,   "control_bw_mbps": 2000 }   // Local
{ "rtt_ms": 50,  "control_bw_mbps": 200  }   // US WAN
{ "rtt_ms": 150, "control_bw_mbps": 100  }   // Global WAN
```

---

## Plot Generation

`graph.py` reads CSVs from `Results/` and writes PNG figures to the appropriate output folders.

### Generate all plots

```bash
python graph.py --results_dir Results --no_show
```

### Generate only the non-sharded heatmap

```bash
python graph.py --no_show --skip_near --skip_memo --skip_memo_msg --skip_validation --skip_sig_schemes
```

### Generate MEMO plots for all three network conditions

```bash
python graph.py --memo_csv memo_results_local.csv  --memo_out memo_graphs_local  --memo_msg_out memo_graphs_local  --skip_near --skip_non --skip_validation --no_show
python graph.py --memo_csv memo_results_usa.csv    --memo_out memo_graphs_usa    --memo_msg_out memo_graphs_usa    --skip_near --skip_non --skip_validation --no_show
python graph.py --memo_csv memo_results_global.csv --memo_out memo_graphs_global --memo_msg_out memo_graphs_global --skip_near --skip_non --skip_validation --no_show
```

### Generate NEAR plots only

```bash
python graph.py --no_show --skip_memo --skip_memo_msg --skip_non --skip_validation --skip_sig_schemes
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
| `--memo_out` | `memo_graphs` | Output folder for MEMO TPS figures |
| `--memo_msg_out` | `memo_msg_graphs` | Output folder for MEMO messages-vs-shards figures |
| `--memo_bt_out` | `memo_graphs` | Output folder for MEMO blocktime-vs-shards figures |
| `--non_out` | `non_sharded_graphs` | Output folder for non-sharded figures |
| `--val_out` | `Validations` | Output folder for validation figures |

**Display and selection:**

| Argument | Description |
|---|---|
| `--no_show` | Save only, do not display plots (recommended on servers) |
| `--skip_near` | Skip NEAR plot generation |
| `--skip_memo` | Skip MEMO TPS plot generation |
| `--skip_memo_msg` | Skip MEMO messages-vs-shards plot generation |
| `--skip_memo_bt` | Skip MEMO blocktime-vs-shards plot generation |
| `--skip_non` | Skip non-sharded plot generation |
| `--skip_validation` | Skip validation plot generation |
| `--skip_sig_schemes` | Skip per-signature-scheme plot generation |
| `--memo_bt_blocksize` | If set, filter the blocktime-vs-shards graph to this single block size (e.g. `--memo_bt_blocksize 4096`). Output filename becomes `memo_blocktime_vs_shards_bs<N>.png`. |

---

## Graphs Produced Per Chain

### Non-Sharded (BTC Hypothetical) — `non_sharded_graphs/`

| File | X-axis | Y-axis | Color |
|---|---|---|---|
| `heatmap_tps_blocktime_vs_blocksize_btc_hypothetical.png` | Block size (tx/block) | Actual block time (30 s bins, upper bound labeled) | TPS — red (low) → orange → yellow → sky blue (high) |

Missing cells are filled by linear interpolation across adjacent columns (then rows). Interpolated values are shown in italic gray text; measured values in bold white/black chosen for contrast.

### NEAR-Protocol-Inspired — `near_graphs/`

| File | Description |
|---|---|
| `near_tps_comparison.png` | Simulated TPS vs target TPS for 4, 6, and 9 shards (side-by-side bars) |
| `near_blocktime_comparison.png` | Simulated vs target average block time per shard count |

### MEMO-Style Sharded — `memo_graphs_<condition>/`

| File | Description |
|---|---|
| `memo_tps_vs_shards_part1.png` | 2×2 facet grid — TPS vs shards for 4 largest block sizes; one line per configured block time |
| `memo_tps_vs_shards_part2.png` | 2×3 facet grid — remaining block sizes |
| `memo_tps_vs_shards_all_blocksizes.png` | Full grid at log scale |
| `memo_messages_vs_shards.png` | Single graph — messages vs shards; one colored line per block size. Auto-collapses to one line if all block sizes are identical (< 0.1% relative variation). |
| `memo_blocktime_vs_shards.png` | Single graph — minimum actual block time vs shards; one colored line per block size (log scale) |
| `memo_blocktime_vs_shards_bs<N>.png` | Same as above, filtered to block size N (produced when `--memo_bt_blocksize` is set) |

The same set is generated for each signature scheme under `memo_graphs_<scheme>/`.

### Validation — `Validations/`

| File | Description |
|---|---|
| `validation_tps_comparison.png` | Simulated vs real-world TPS for Bitcoin, Bitcoin Cash, Litecoin, Dogecoin |
| `validation_blocktime_comparison.png` | Simulated vs real-world average block time |

---

## Results CSV Format

All result CSVs share the same column schema:

```
currency, nodes, wallets, miners, transactions, interval, shards,
average block time, block size, messages, mode, tps, throughput_shard,
no. of blocks generated, blocktime in configuration file, sig_scheme
```

| Column | Description |
|---|---|
| `currency` | Chain label (`btc_hypothetical`, `near`, `memo`) |
| `nodes` | Node count |
| `wallets` | Wallet count |
| `miners` | Miner count |
| `transactions` | Total transactions attempted |
| `interval` | Transaction generation interval (s) |
| `shards` | Shard count |
| `average block time` | Measured seconds between blocks |
| `block size` | Transactions per block (from `total_blocksize`) |
| `messages` | Total control-plane messages sent |
| `mode` | `conventional` (shards=1) or `sharded` (shards>1) |
| `tps` | Overall transactions per second |
| `throughput_shard` | `tps / shards` |
| `no. of blocks generated` | Total blocks mined |
| `blocktime in configuration file` | Target blocktime from config (s) |
| `sig_scheme` | Signature scheme used (empty string if not set) |

Example rows (NEAR):

```csv
near,1000,1000,1000,1000,0.01,4,0.61,1800,559892,sharded,2959.98,739.99,556,1.3,ed25519
near,1000,1000,1000,1000,0.01,6,0.61,2100,482247,sharded,3418.71,569.79,477,1.3,ed25519
near,1000,1000,1000,1000,0.01,9,0.59,2400,424089,sharded,4068.28,452.03,417,1.3,ed25519
```

---

## Validation

Validation compares simulator output against known real-world values for Bitcoin, Bitcoin Cash, Litecoin, and Dogecoin. Figures are written to `Validations/` and kept separate from experiment plots.

| Chain | Claimed TPS | Claimed block time |
|---|---|---|
| Bitcoin | 7 | 600 s |
| Bitcoin Cash | 200 | 600 s |
| Litecoin | 28 | 150 s |
| Dogecoin | 40 | 60 s |

```bash
python graph.py --skip_near --skip_memo --skip_non --no_show
```

---

## Citation

If you use this simulator in a report or publication, please cite:

```
Om Amit Gandhi, "Sharding Simulations," GitHub repository, 2025.
```

---

## License

MIT License. See `LICENSE` for details.
