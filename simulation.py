#!/usr/bin/env python3
"""
Winner-based sharding simulator (SimPy)

Key performance fixes included:
  1) Pool is O(1): default pool_mode="count" (no list pop(0)).
  2) Aggregate tx arrivals: one process instead of per-wallet processes.
  3) No background token-bucket refill loop (removes tons of events).
  4) Propagation model switch:
       - exact   : explicit neighbor flooding (slow for big N/degree)
       - gossip  : bounded fanout + rounds (much faster)
       - analytic: O(1) broadcast approximation (fastest)
  5) Neighbor generation avoids O(N^2) list rebuilds.

Default behavior remains similar, but you now control scalability knobs via CLI.

Example fast runs:
  ./sim.py --nodes 2000 --neighbors 50 --miners 2000 --hashrate 1e6 \
           --wallets 2000 --transactions 2000 --interval 1 \
           --shards 64 --total-blocksize 120000 --blocks 1000 \
           --prop_model analytic --pool_mode count --quiet_blocks
"""

import simpy
import argparse
import random
import json
import os
import sys
import csv
from pathlib import Path
from collections import deque
from typing import Optional, Deque, Tuple, List
from io import StringIO
import tempfile


# ============================================================
# Globals
# ============================================================
network_data = 0      # bytes of block data sent
io_requests = 0       # number of block sends (edges)
total_tx = 0          # total transactions (including coinbase)
total_coins = 0.0     # minted coins

# Pool modeling:
# - count mode: integer count of pending tx (fastest)
# - deque mode: stores (wallet_id, timestamp) FIFO (still fast with popleft)
POOL_MODE = "count"     # "count" or "deque"
pool_count = 0          # used when POOL_MODE == "count"
pool_deque: Deque[Tuple[int, float]] = deque()  # used when POOL_MODE == "deque"

sim_summary = {}      # final compact summary

HEADER_SIZE = 1024
YEAR = 365 * 24 * 3600
TX_BYTES = 128          # per-transaction wire size; overridden by --sig_scheme

# Network model knobs (set from CLI)
LINK_RTT_MS = 0.0       # logical RTT
LINK_JITTER_MS = 0.0    # extra jitter (one-way)
LINK_MSG_PROC_MS = 0.0  # CPU per received message
CTRL_BW_MBPS = 0.0      # control NIC throughput


# ============================================================
# Signature scheme benchmarks
#
# tx_cost_ms : verification time per transaction (milliseconds)
# sig_bytes  : signature size in bytes
# tx_bytes   : total per-transaction wire size (signature + pubkey + payload)
#
# Sources:
#   ed25519      — Bernstein et al., SUPERCOP benchmarks, ~134K cycles @ 2.6 GHz
#                  https://bench.cr.yp.to/results-sign.html
#   dilithium2   — Ducas et al., CRYSTALS-Dilithium NIST PQC Round 3 spec, Table 2:
#                  ~145K verify cycles @ 2.6 GHz; sig=2420 B, pk=1312 B
#                  https://pq-crystals.org/dilithium/
#   falcon512    — Fouque et al., FALCON NIST PQC Round 3 spec:
#                  ~36K verify cycles @ 2.6 GHz (fastest PQ verify); sig≈666 B avg, pk=897 B
#                  https://falcon-sign.info/
#   sphincs_sha2_128s — Bernstein et al., SPHINCS+ NIST PQC Round 3 spec:
#                  ~2.7M verify cycles @ 2.6 GHz; sig=7856 B, pk=32 B
#                  https://sphincs.org/
# ============================================================
SIG_SCHEMES = {
    "ed25519": {
        "tx_cost_ms": 0.05,
        "sig_bytes":  64,
        "tx_bytes":   128,
    },
    "dilithium2": {
        "tx_cost_ms": 0.06,
        "sig_bytes":  2420,
        "tx_bytes":   2500,
    },
    "falcon512": {
        "tx_cost_ms": 0.014,
        "sig_bytes":  666,
        "tx_bytes":   750,
    },
    "sphincs_sha2_128s": {
        "tx_cost_ms": 1.04,
        "sig_bytes":  7856,
        "tx_bytes":   7950,
    },
}


# ============================================================
# Utility helpers
# ============================================================
def human(n: float) -> str:
    a = abs(n)
    if a >= 1e9:
        v, s = n / 1e9, 'B'
    elif a >= 1e6:
        v, s = n / 1e6, 'M'
    elif a >= 1e3:
        v, s = n / 1e3, 'K'
    else:
        return str(int(n))
    if isinstance(v, float) and v.is_integer():
        return f"{int(v)}{s}"
    return f"{v:.1f}{s}"


def harmonic_number(S: int) -> float:
    return sum(1.0 / k for k in range(1, max(1, S) + 1))


def sample_one_way_latency_s() -> float:
    """One-way latency: RTT/2 + random jitter, in seconds."""
    base_s = (LINK_RTT_MS / 2.0) / 1000.0 if LINK_RTT_MS > 0 else 0.0
    jitter_s = random.uniform(0.0, LINK_JITTER_MS) / 1000.0 if LINK_JITTER_MS > 0 else 0.0
    return base_s + jitter_s


def recv_processing_s() -> float:
    """Receiver CPU time per message, in seconds."""
    return LINK_MSG_PROC_MS / 1000.0 if LINK_MSG_PROC_MS > 0 else 0.0


def control_phase_delay(num_msgs: int, msg_size_bytes: int) -> float:
    if num_msgs <= 0:
        return 0.0
    if CTRL_BW_MBPS and CTRL_BW_MBPS > 0:
        Bps = CTRL_BW_MBPS * 1e6 / 8.0
        send_time = (num_msgs * msg_size_bytes) / max(Bps, 1e-9)
    else:
        send_time = 0.0
    cpu_time = num_msgs * recv_processing_s()
    latency = sample_one_way_latency_s()
    return send_time + cpu_time + latency


# ============================================================
# Core objects
# ============================================================
class Block:
    def __init__(self, i: int, tx: int, dt: float):
        self.id = i
        self.tx = tx
        self.size = HEADER_SIZE + tx * TX_BYTES
        self.dt = dt


class Node:
    def __init__(self, env: simpy.Environment, i: int):
        self.env = env
        self.id = i
        self.blocks = set()
        self.neighbors: List["Node"] = []

    def receive(self, b: Block):
        global network_data, io_requests
        yield self.env.timeout(0)
        if b.id in self.blocks:
            return
        self.blocks.add(b.id)
        for n in self.neighbors:
            io_requests += 1
            network_data += b.size
            self.env.process(n.receive(b))


class Miner:
    def __init__(self, i: int, h: float):
        self.id = i
        self.h = h


# ============================================================
# Workload
# ============================================================
def wallet(env: simpy.Environment, wid: int, count: int, interval: float):
    global pool_count, pool_deque, POOL_MODE
    for _ in range(count):
        yield env.timeout(interval)
        if POOL_MODE == "count":
            pool_count += 1
        else:
            pool_deque.append((wid, env.now))


def pool_available() -> int:
    global pool_count, pool_deque, POOL_MODE
    return pool_count if POOL_MODE == "count" else len(pool_deque)


def pool_take(k: int):
    global pool_count, pool_deque, POOL_MODE
    if k <= 0:
        return
    if POOL_MODE == "count":
        pool_count = max(0, pool_count - k)
        return
    for _ in range(k):
        if not pool_deque:
            break
        pool_deque.popleft()


# ============================================================
# Metronome mode
# ============================================================
def metronome_messages(shards: int, num_nodes: int) -> int:
    S = max(0, int(shards))
    N = max(0, int(num_nodes))
    if S <= 1:
        return N
    return 2 * S + (S * (S - 1)) // 2 + N


# ============================================================
# Mining params helper
# ============================================================
def _mining_params(total_hash: float, S: int, target_bt: float, diff0: Optional[float]) -> Tuple[float, float]:
    S = max(1, int(S))
    Hs = harmonic_number(S)
    if total_hash <= 0:
        raise ValueError("Total hashrate must be > 0")
    if diff0 is not None:
        diff = float(diff0)
    else:
        diff = (float(total_hash) * float(target_bt)) / (S * Hs)
    lam_shard = (float(total_hash) / S) / max(diff, 1e-12)
    return diff, lam_shard


# ============================================================
# Coordinators
# ============================================================
def coord(env, nodes, miners, target_bt, diff0, blocks_limit, total_blocksize,
          print_int, dbg, wallets, tx_per_wallet, init_reward, halving_interval,
          shards, tx_cost_ms, rtt_ms, coord_rounds, msg_cost, msg_size,
          control_bw_mbps, overlap_broadcast, msg_proc_ms,
          quiet_blocks=False):
    global network_data, io_requests, total_tx, total_coins

    if not miners:
        raise ValueError("Need at least one miner")
    S = max(1, shards or 1)
    total_hash = sum(m.h for m in miners)
    if total_hash <= 0:
        raise ValueError("Total hashrate must be > 0")

    diff, lam_shard = _mining_params(total_hash, S, target_bt, diff0)
    tx_cost_s = max(0.0, float(tx_cost_ms)) / 1000.0
    rtt_s = max(0.0, float(rtt_ms)) / 1000.0
    coord_delay = max(0, int(coord_rounds)) * rtt_s
    tot_cap = max(0, int(total_blocksize or 0))

    bc = 0
    total_coordination_messages = 0
    total_msg_cost = 0.0
    reward = init_reward if init_reward is not None else 50.0
    has_tx = (tx_per_wallet or 0) > 0 and (wallets or 0) > 0
    total_needed = (wallets or 0) * (tx_per_wallet or 0) if has_tx else None
    pool_processed = 0

    while True:
        if blocks_limit is not None and bc >= blocks_limit:
            break
        if has_tx and pool_processed is not None and pool_processed >= total_needed:
            break

        shard_times = [random.expovariate(lam_shard) for _ in range(S)]
        dt_mine = max(shard_times)
        yield env.timeout(dt_mine)

        if has_tx:
            avail = pool_available()
            take = min(avail, tot_cap)
            pool_processed += take
            pool_take(take)
        else:
            take = 0

        max_shard_tx = (take + S - 1) // S if S > 0 else 0
        dt_verify = max_shard_tx * tx_cost_s
        dt_coord = coord_delay
        msgs = metronome_messages(S, len(nodes))
        total_coordination_messages += msgs
        dt_ctrl_net = control_phase_delay(msgs, int(msg_size or 0))
        block_msg_cost = msgs * float(msg_cost or 0.0)
        total_msg_cost += block_msg_cost

        dt_rest = dt_verify + dt_coord + dt_ctrl_net
        round_dt = dt_mine + dt_rest
        if dt_rest > 0:
            yield env.timeout(dt_rest)

        bc += 1
        txs_next = (take + 1) if has_tx else 1
        b = Block(bc, txs_next, round_dt)
        total_tx += txs_next
        env.process(random.choice(nodes).receive(b))
        reward = _apply_halving_and_mint(bc, reward, halving_interval)

        if (not quiet_blocks) and (dbg or (print_int and bc % print_int == 0)):
            net_note = "prop=flood"
            print(
                f"[{env.now:.2f}] Block {bc} contains {txs_next-1} tx "
                f"(total_cap={tot_cap}, shards={S}, per_shard≈{(tot_cap+S-1)//S if S>0 else 0}) "
                f"dt={round_dt:.3f}s mine={dt_mine:.3f}s verify={dt_verify:.3f}s "
                f"coord={dt_coord:.3f}s ctrl_net={dt_ctrl_net:.3f}s "
                f"[{net_note}] Msgs_tot:{total_coordination_messages} MsgCost_blk:{block_msg_cost:.2f}"
            )
        if print_int and bc % print_int == 0:
            _print_summary(env.now, bc, blocks_limit, diff, total_hash, total_msg_cost)

    _print_final(env.now, bc, blocks_limit, diff, total_hash,
                 total_coordination_messages, total_msg_cost)


# ============================================================
# No-metronome helpers
# ============================================================
def winner_announce_phase(S: int, N: int, msg_size: int):
    if S <= 0 or N <= 0:
        return 0.0, 0
    msgs = S * N
    dt = control_phase_delay(msgs, msg_size)
    return dt, msgs


def winner_pairwise_phase(S: int, msg_size: int):
    if S <= 1:
        return 0.0, 0
    pairs = (S * (S - 1)) // 2
    dt = control_phase_delay(pairs, msg_size)
    return dt, pairs


def coord_no_metronome(env, nodes, miners, target_bt, diff0, blocks_limit,
                       total_blocksize, print_int, dbg, wallets, tx_per_wallet,
                       init_reward, halving_interval, shards, tx_cost_ms,
                       msg_size, control_bw_mbps,
                       overlap_broadcast, msg_proc_ms, msg_cost, quiet_blocks=False):
    global network_data, io_requests, total_tx, total_coins

    if not miners:
        raise ValueError("Need at least one miner")
    S = max(1, shards or 1)
    total_hash = sum(m.h for m in miners)
    if total_hash <= 0:
        raise ValueError("Total hashrate must be > 0")

    diff, lam_shard = _mining_params(total_hash, S, target_bt, diff0)
    tx_cost_s = max(0.0, float(tx_cost_ms)) / 1000.0

    bc = 0
    total_msg_cost = 0.0
    total_control_msgs = 0
    reward = init_reward if init_reward is not None else 50.0
    has_tx = (tx_per_wallet or 0) > 0 and (wallets or 0) > 0
    total_needed = (wallets or 0) * (tx_per_wallet or 0) if has_tx else None
    pool_processed = 0
    tot_cap = max(0, int(total_blocksize or 0))

    while True:
        if blocks_limit is not None and bc >= blocks_limit:
            break
        if has_tx and pool_processed is not None and pool_processed >= total_needed:
            break

        shard_times = [random.expovariate(lam_shard) for _ in range(S)]
        dt_mine = max(shard_times)
        yield env.timeout(dt_mine)

        if has_tx:
            avail = pool_available()
            take = min(avail, tot_cap)
            pool_processed += take
            pool_take(take)
        else:
            take = 0

        max_shard_tx = (take + S - 1) // S if S > 0 else 0
        dt_verify = max_shard_tx * tx_cost_s
        N = len(nodes)
        dt_ann, msgs_ann = winner_announce_phase(S, N, msg_size)
        dt_pair, msgs_pair = winner_pairwise_phase(S, msg_size)
        dt_control = dt_ann + dt_pair
        total_control_msgs += (msgs_ann + msgs_pair)
        total_msg_cost += (msgs_ann + msgs_pair) * float(msg_cost or 0.0)

        dt_rest = dt_verify + dt_control
        round_dt = dt_mine + dt_rest
        if dt_rest > 0:
            yield env.timeout(dt_rest)

        bc += 1
        txs_next = (take + 1) if has_tx else 1
        b = Block(bc, txs_next, round_dt)
        total_tx += txs_next
        env.process(random.choice(nodes).receive(b))
        reward = _apply_halving_and_mint(bc, reward, halving_interval)

        if (not quiet_blocks) and (dbg or (print_int and bc % print_int == 0)):
            net_note = "prop=flood"
            print(
                f"[{env.now:.2f}] Block {bc} contains {txs_next-1} tx "
                f"(total_cap={tot_cap}, shards={S}, per_shard≈{(tot_cap+S-1)//S if S>0 else 0}) "
                f"dt={round_dt:.3f}s mine={dt_mine:.3f}s verify={dt_verify:.3f}s "
                f"announce+pair={dt_control:.3f}s [{net_note}] "
                f"CtrlMsgs:{total_control_msgs} MsgCost_tot:{total_msg_cost:.2f}"
            )
        if print_int and bc % print_int == 0:
            _print_summary(env.now, bc, blocks_limit, diff, total_hash,
                           total_msg_cost, extra_fields=f"CtrlMsgs:{total_control_msgs}")

    _print_final(env.now, bc, blocks_limit, diff, total_hash,
                 total_control_msgs, total_msg_cost, extra_fields="(no metronome)")


# ============================================================
# Leader-metronome helpers
# ============================================================
def leader_announce_phase(N: int, msg_size: int):
    if N <= 0:
        return 0.0, 0
    msgs = N
    dt = control_phase_delay(msgs, msg_size)
    return dt, msgs


def to_leader_phase(S: int, msg_size: int):
    if S <= 1:
        return 0.0, 0
    msgs = S - 1
    dt = control_phase_delay(msgs, msg_size)
    return dt, msgs


def coord_leader_metronome(env, nodes, miners, target_bt, diff0, blocks_limit,
                           total_blocksize, print_int, dbg, wallets, tx_per_wallet,
                           init_reward, halving_interval, shards, tx_cost_ms,
                           msg_size, control_bw_mbps,
                           overlap_broadcast, msg_proc_ms, msg_cost,
                           verify_mode="leader", quiet_blocks=False):
    global network_data, io_requests, total_tx, total_coins

    if not miners:
        raise ValueError("Need at least one miner")
    S = max(1, shards or 1)
    total_hash = sum(m.h for m in miners)
    if total_hash <= 0:
        raise ValueError("Total hashrate must be > 0")

    diff, lam_shard = _mining_params(total_hash, S, target_bt, diff0)
    tx_cost_s = max(0.0, float(tx_cost_ms)) / 1000.0
    per_shard_cap = (max(0, int(total_blocksize or 0)) + S - 1) // S

    bc = 0
    total_msg_cost = 0.0
    total_control_msgs = 0
    reward = init_reward if init_reward is not None else 50.0
    has_tx = (tx_per_wallet or 0) > 0 and (wallets or 0) > 0
    total_needed = (wallets or 0) * (tx_per_wallet or 0) if has_tx else None
    pool_processed = 0
    verify_mode_used = verify_mode or "leader"

    while True:
        if blocks_limit is not None and bc >= blocks_limit:
            break
        if has_tx and pool_processed is not None and pool_processed >= total_needed:
            break

        shard_times = [random.expovariate(lam_shard) for _ in range(S)]
        leader_idx = min(range(S), key=lambda i: shard_times[i])

        # Shards that find a winner within the mining timeout
        winning_times = [t for t in shard_times if t <= target_bt]
        actual_winners = len(winning_times)

        # Stop early if all shards won before timeout; otherwise wait the full timeout
        if actual_winners == S:
            dt_mine = max(shard_times)
        else:
            dt_mine = float(target_bt)

        yield env.timeout(dt_mine)

        # Extremely unlikely: no shard won within timeout — skip this round
        if actual_winners == 0:
            continue

        last_winner_time = max(winning_times)

        # Transactions capped to winning shards only
        if has_tx:
            avail = pool_available()
            take = min(avail, per_shard_cap * actual_winners)
            pool_processed += take
            pool_take(take)
        else:
            take = 0

        N = len(nodes)
        # Only actual_winners shards report to leader
        dt_ann, msgs_ann = leader_announce_phase(N, msg_size)
        dt_to_leader, msgs_to_leader = to_leader_phase(actual_winners, msg_size)
        dt_control = dt_ann + dt_to_leader
        total_control_msgs += (msgs_ann + msgs_to_leader)
        total_msg_cost += (msgs_ann + msgs_to_leader) * float(msg_cost or 0.0)

        txs_next = (take + 1) if has_tx else 1

        if verify_mode_used == "leader":
            dt_verify_term = (take * tx_cost_s) if has_tx else 0.0
            verify_note = "verify_all@leader"
        elif verify_mode_used == "leader_par":
            threads = max(1, int(os.getenv("LEADER_VERIFY_THREADS", "8")))
            dt_verify_term = (take * tx_cost_s / threads) if has_tx else 0.0
            verify_note = f"verify_all@leader_par({threads}t)"
        elif verify_mode_used == "shard":
            # Each winning shard verifies its own transactions starting when it won.
            # Verification overlaps with remaining mining time for other shards.
            # Only the remaining tail after dt_mine adds to round time.
            actual_per_shard = (take + actual_winners - 1) // actual_winners if (has_tx and actual_winners > 0) else 0
            dt_v = actual_per_shard * tx_cost_s
            dt_verify_term = max(0.0, last_winner_time + dt_v - dt_mine)
            dt_attest = control_phase_delay(actual_winners, msg_size)
            dt_control += dt_attest
            total_control_msgs += actual_winners
            total_msg_cost += actual_winners * float(msg_cost or 0.0)
            verify_note = f"verify@shards+attest(W={actual_winners}/{S})"
        else:
            raise ValueError("--verify_mode must be one of: leader, shard, leader_par")

        dt_rest = dt_control + dt_verify_term
        round_dt = dt_mine + dt_rest
        if dt_rest > 0:
            yield env.timeout(dt_rest)

        bc += 1
        b = Block(bc, txs_next, round_dt)
        total_tx += txs_next
        env.process(random.choice(nodes).receive(b))
        reward = _apply_halving_and_mint(bc, reward, halving_interval)

        if (not quiet_blocks) and (dbg or (print_int and bc % print_int == 0)):
            net_note = "prop=flood"
            print(
                f"[{env.now:.2f}] Block {bc} (leader={leader_idx}, winners={actual_winners}/{S}) "
                f"tx={txs_next-1} dt={round_dt:.3f}s "
                f"mine={dt_mine:.3f}s control={dt_control:.3f}s "
                f"{verify_note}={dt_verify_term:.3f}s "
                f"[{net_note}] CtrlMsgs_tot:{total_control_msgs} MsgCost_tot:{total_msg_cost:.2f}"
            )
        if print_int and bc % print_int == 0:
            _print_summary(env.now, bc, blocks_limit, diff, total_hash,
                           total_msg_cost,
                           extra_fields=f"LeaderMode verify={verify_mode_used} "
                                        f"Winners:{actual_winners}/{S} "
                                        f"CtrlMsgs:{total_control_msgs}")

    _print_final(env.now, bc, blocks_limit, diff, total_hash,
                 total_control_msgs, total_msg_cost,
                 extra_fields=f"(leader metronome, verify={verify_mode_used})")


# ============================================================
# Rewards & summaries
# ============================================================
def _apply_halving_and_mint(bc: int, reward: float, halving_interval: int):
    global total_coins
    total_coins += reward
    if halving_interval > 0 and bc % halving_interval == 0 and reward > 0:
        return reward / 2.0
    return reward


def _print_summary(now, bc, blocks_limit, diff, total_hash, total_msg_cost, extra_fields=""):
    global total_tx, total_coins, network_data, io_requests, POOL_MODE, pool_count, pool_deque
    abt = now / bc if bc else 0.0
    tps = total_tx / now if now > 0 else 0.0
    infl = 0.0
    eta = ((blocks_limit - bc) * abt) if blocks_limit else 0.0
    pct = (bc / blocks_limit) * 100 if blocks_limit else 0.0
    pool_len = pool_count if POOL_MODE == "count" else len(pool_deque)
    print(
        f"[{now:.2f}] Sum B:{bc}/{blocks_limit} {pct:.1f}% abt:{abt:.2f}s "
        f"tps:{tps} infl:{infl:.2f}% ETA:{eta:.2f}s "
        f"Diff:{human(diff)} ΣH:{human(total_hash)} Tx:{total_tx} "
        f"C:{total_coins} Pool:{pool_len} "
        f"NMB:{network_data/1e6:.2f} IO:{io_requests} "
        f"MsgCost_tot:{total_msg_cost:.2f} {extra_fields}"
    )


def _print_final(now, bc, blocks_limit, diff, total_hash,
                 total_msgs, total_msg_cost, extra_fields=""):
    global total_tx, total_coins, network_data, io_requests, sim_summary, POOL_MODE, pool_count, pool_deque
    total_time = now
    abt_global = total_time / bc if bc else 0.0
    tps_total = total_tx / total_time if total_time > 0 else 0.0
    msg_cost_per_tx = (total_msg_cost / total_tx) if total_tx > 0 else 0.0
    pool_len = pool_count if POOL_MODE == "count" else len(pool_deque)

    sim_summary = {
        "blocks": bc,
        "total_time": total_time,
        "avg_block_time": abt_global,
        "tps": tps_total,
        "total_msgs": int(total_msgs),
        "total_tx": int(total_tx),
        "total_coins": float(total_coins),
        "network_bytes": float(network_data),
        "io_requests": int(io_requests),
        "msg_cost_total": float(total_msg_cost),
        "msg_cost_per_tx": float(msg_cost_per_tx),
        "extra_fields": str(extra_fields),
    }

    print(
        f"[******] End B:{bc}/{blocks_limit or bc} "
        f"abt_global:{abt_global:.2f}s tps:{tps_total} "
        f"Diff:{human(diff)} ΣH:{human(total_hash)} "
        f"Tx:{total_tx} C:{total_coins} Pool:{pool_len} "
        f"NMB:{network_data/1e6:.2f} IO:{io_requests} "
        f"Msgs:{total_msgs} MsgCost_tot:{total_msg_cost:.2f} "
        f"(per_tx:{msg_cost_per_tx:.6f}) {extra_fields}"
    )


# ============================================================
# Graph generation
# ============================================================
def build_random_k_out_graph(nodes: List[Node], k: int, seed: Optional[int] = None):
    if seed is not None:
        random.seed(seed)
    n = len(nodes)
    if n <= 1 or k <= 0:
        for u in nodes:
            u.neighbors = []
        return
    k = min(k, n - 1)
    for i, u in enumerate(nodes):
        picks = set()
        while len(picks) < k:
            j = random.randrange(n)
            if j != i:
                picks.add(j)
        u.neighbors = [nodes[j] for j in picks]


# ============================================================
# CLI / main
# ============================================================
def main():
    p = argparse.ArgumentParser(description="Winner-based sharding simulator")

    p.add_argument("--currency", type=str, default="memo")
    p.add_argument("--nodes", type=int)
    p.add_argument("--neighbors", type=int)
    p.add_argument("--miners", type=int)
    p.add_argument("--hashrate", type=float)
    p.add_argument("--wallets", type=int)
    p.add_argument("--transactions", type=int)
    p.add_argument("--interval", type=float)
    p.add_argument("--blocktime", type=float, default=600.0)
    p.add_argument("--difficulty", dest="diff0", type=float)
    p.add_argument("--blocks", dest="blocks_limit", type=int)
    p.add_argument("--years", dest="years", type=float)
    p.add_argument("--reward", dest="init_reward", type=float, default=50.0)
    p.add_argument("--halving", dest="halving_interval", type=int, default=210000)
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("--total-blocksize", dest="total_blocksize", type=int, default=4096)
    p.add_argument("--sig_scheme", type=str, default=None,
                   choices=["ed25519", "dilithium2", "falcon512", "sphincs_sha2_128s"],
                   help="Signature scheme; sets tx_cost_ms and tx_bytes from published benchmarks. "
                        "Overridden by --tx_cost_ms if that flag is also passed explicitly.")
    p.add_argument("--tx_cost_ms", type=float, default=1.0)
    p.add_argument("--rtt_ms", type=float, default=0.0)
    p.add_argument("--jitter_ms", type=float, default=1.0)
    p.add_argument("--coord_rounds", type=int, default=0)
    p.add_argument("--cost", type=float, default=0.0)
    p.add_argument("--msg_size", type=int, default=200)
    p.add_argument("--control_bw_mbps", type=float, default=0.0)
    p.add_argument("--overlap_broadcast", action="store_true")
    p.add_argument("--msg_proc_ms", type=float, default=1.0)
    p.add_argument("--print", dest="print_int", type=int, default=144)
    p.add_argument("--debug", action="store_true")
    p.add_argument("--config", type=str)
    p.add_argument("--prefill", action="store_true")
    p.add_argument("--no_metronome", action="store_true")
    p.add_argument("--leader_metronome", action="store_true")
    p.add_argument("--verify_mode", type=str, default="leader",
                   choices=["leader", "shard", "leader_par"])
    p.add_argument("--pool_mode", type=str, default="count",
                   choices=["count", "deque"])
    p.add_argument("--quiet_blocks", action="store_true")
    p.add_argument("--results_csv", type=str, default="")
    p.add_argument("--results_dir", type=str, default="Results")

    args = p.parse_args()

    # ------------------------------------------------------------------
    # JSON overlay — CLI args always win over JSON values.
    #
    # We check sys.argv for which flags were explicitly passed on the
    # command line. JSON only fills in the gaps (args left at default).
    # This prevents JSON fields like "results_csv" from overriding the
    # per-run output path that the parallel runner sets via CLI.
    # ------------------------------------------------------------------
    cli_provided = set()
    for action in p._actions:
        for opt_str in action.option_strings:
            if opt_str in sys.argv:
                cli_provided.add(action.dest)

    config_data = {}
    if args.config:
        with open(args.config, "r") as f:
            config_data = json.load(f)

    for k, v in config_data.items():
        if hasattr(args, k) and k not in cli_provided:
            setattr(args, k, v)
    # ------------------------------------------------------------------

    # Run length
    blocks_limit = args.blocks_limit
    if blocks_limit is None and args.years:
        blocks_limit = int(args.years * YEAR / (args.blocktime or 1))
    args.blocks_limit = blocks_limit

    # Apply signature scheme if given and --tx_cost_ms / --msg_size were not explicit
    global TX_BYTES
    if args.sig_scheme:
        scheme = SIG_SCHEMES[args.sig_scheme]
        TX_BYTES = scheme["tx_bytes"]
        if "tx_cost_ms" not in cli_provided:
            args.tx_cost_ms = scheme["tx_cost_ms"]
        if "msg_size" not in cli_provided:
            args.msg_size = scheme["sig_bytes"]
        print(f"[sig_scheme] {args.sig_scheme}: "
              f"tx_cost_ms={args.tx_cost_ms}ms  "
              f"sig_bytes={scheme['sig_bytes']}B  "
              f"tx_bytes={TX_BYTES}B  "
              f"msg_size={args.msg_size}B")

    # Globals setup
    global LINK_RTT_MS, LINK_JITTER_MS, LINK_MSG_PROC_MS, CTRL_BW_MBPS
    LINK_RTT_MS      = float(args.rtt_ms or 0.0)
    LINK_JITTER_MS   = float(args.jitter_ms or 0.0)
    LINK_MSG_PROC_MS = float(args.msg_proc_ms or 0.0)
    CTRL_BW_MBPS     = float(args.control_bw_mbps or 0.0)

    global POOL_MODE, pool_count, pool_deque
    POOL_MODE  = str(args.pool_mode or "count")
    pool_count = 0
    pool_deque = deque()

    env = simpy.Environment()

    # Workload
    total_tx_need = (args.wallets or 0) * (args.transactions or 0)
    if args.prefill and total_tx_need > 0:
        if POOL_MODE == "count":
            pool_count = total_tx_need
        else:
            for wid in range(args.wallets or 0):
                for _ in range(args.transactions or 0):
                    pool_deque.append((wid, 0.0))
    else:
        for i in range(args.wallets or 0):
            env.process(wallet(env, i, args.transactions or 0, args.interval or 0.0))

    # Nodes + graph
    nodes = [Node(env, i) for i in range(args.nodes or 0)]
    build_random_k_out_graph(nodes, int(args.neighbors or 0), seed=None)

    # Miners
    miners = [Miner(i, args.hashrate or 0.0) for i in range(args.miners or 0)]

    # Choose coordinator
    if args.leader_metronome:
        coord_proc = env.process(
            coord_leader_metronome(
                env, nodes, miners,
                target_bt=args.blocktime, diff0=args.diff0,
                blocks_limit=args.blocks_limit, total_blocksize=args.total_blocksize,
                print_int=args.print_int, dbg=args.debug,
                wallets=args.wallets, tx_per_wallet=args.transactions,
                init_reward=args.init_reward, halving_interval=args.halving_interval,
                shards=args.shards, tx_cost_ms=args.tx_cost_ms,
                msg_size=args.msg_size, control_bw_mbps=args.control_bw_mbps,
                overlap_broadcast=args.overlap_broadcast,
                msg_proc_ms=args.msg_proc_ms, msg_cost=args.cost,
                verify_mode=args.verify_mode, quiet_blocks=args.quiet_blocks,
            )
        )
    elif args.no_metronome:
        coord_proc = env.process(
            coord_no_metronome(
                env, nodes, miners,
                target_bt=args.blocktime, diff0=args.diff0,
                blocks_limit=args.blocks_limit, total_blocksize=args.total_blocksize,
                print_int=args.print_int, dbg=args.debug,
                wallets=args.wallets, tx_per_wallet=args.transactions,
                init_reward=args.init_reward, halving_interval=args.halving_interval,
                shards=args.shards, tx_cost_ms=args.tx_cost_ms,
                msg_size=args.msg_size, control_bw_mbps=args.control_bw_mbps,
                overlap_broadcast=args.overlap_broadcast,
                msg_proc_ms=args.msg_proc_ms, msg_cost=args.cost,
                quiet_blocks=args.quiet_blocks,
            )
        )
    else:
        coord_proc = env.process(
            coord(
                env, nodes, miners,
                target_bt=args.blocktime, diff0=args.diff0,
                blocks_limit=args.blocks_limit, total_blocksize=args.total_blocksize,
                print_int=args.print_int, dbg=args.debug,
                wallets=args.wallets, tx_per_wallet=args.transactions,
                init_reward=args.init_reward, halving_interval=args.halving_interval,
                shards=args.shards, tx_cost_ms=args.tx_cost_ms,
                rtt_ms=args.rtt_ms, coord_rounds=args.coord_rounds,
                msg_cost=args.cost, msg_size=args.msg_size,
                control_bw_mbps=args.control_bw_mbps,
                overlap_broadcast=args.overlap_broadcast,
                msg_proc_ms=args.msg_proc_ms, quiet_blocks=args.quiet_blocks,
            )
        )

    env.run(until=coord_proc)

    # Results
    global sim_summary
    if sim_summary:
        blocks  = sim_summary["blocks"]
        avg_bt  = sim_summary["avg_block_time"]
        tps     = sim_summary["tps"]
        msgs    = sim_summary["total_msgs"]
    else:
        blocks = 0; avg_bt = 0.0; tps = 0.0; msgs = 0

    mode             = "conventional" if int(args.shards or 1) == 1 else "sharded"
    block_size_tx    = float(args.total_blocksize or 0)
    shards           = int(args.shards or 1)
    throughput_shard = tps / shards if shards > 0 else 0.0
    currency         = getattr(args, "currency", "memo")

    print("\n===== MEMO-style Table Row (CSV) =====")
    print("currency,nodes,wallets,miners,transactions,interval,shards,"
          "average_block_time,block_size,messages,mode,tps,throughput_shard,"
          "num_blocks,blocktime_cfg,expected_blocktime")
    print(
        f"{currency},{args.nodes or 0},{args.wallets or 0},{args.miners or 0},"
        f"{args.transactions or 0},{args.interval or 0},{shards},"
        f"{avg_bt:.3f},{block_size_tx:.3f},{float(msgs):.3f},{mode},"
        f"{tps:.3f},{throughput_shard:.3f},{blocks},"
        f"{float(args.blocktime):.1f},{float(args.blocktime):.1f}"
    )

    PAPER_CSV_HEADER = [
        "currency", "nodes", "wallets", "miners", "transactions", "interval",
        "shards", "average block time", "block size", "messages", "mode", "tps",
        "no. of blocks generated", "blocktime in configuration file", "sig_scheme",
    ]

    def upsert_paper_csv_row(results_path: str, row: dict):
        path = Path(results_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        key_fields = ["currency", "shards", "block size", "mode",
                      "blocktime in configuration file", "sig_scheme"]

        def row_key(r: dict):
            return tuple(str(r.get(k, "")) for k in key_fields)

        rows = []
        if path.exists() and path.stat().st_size > 0:
            try:
                raw  = path.read_bytes()
                nul_count = raw.count(b"\x00")
                if nul_count > 0:
                    print(f"[warn] Found {nul_count} NUL byte(s) in {path}; sanitizing")
                    raw = raw.replace(b"\x00", b"")
                text = raw.decode("utf-8", errors="replace")
                reader = csv.DictReader(StringIO(text))
                for r in reader:
                    if r is None:
                        continue
                    if not any(str(v).strip() for v in r.values() if v is not None):
                        continue
                    rows.append(r)
            except csv.Error as e:
                bad_path = path.with_suffix(path.suffix + ".corrupt")
                try:
                    if not bad_path.exists():
                        path.replace(bad_path)
                    else:
                        bad_path.write_bytes(path.read_bytes())
                        path.unlink(missing_ok=True)
                except Exception:
                    pass
                print(f"[warn] CSV parse failed for {path}: {e}. Started fresh.")
                rows = []

        new_key  = row_key(row)
        replaced = False
        for i, r in enumerate(rows):
            if row_key(r) == new_key:
                merged = dict(r)
                merged.update(row)
                rows[i] = merged
                replaced = True
                break
        if not replaced:
            rows.append(row)

        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=".tmp_results_", suffix=".csv", dir=str(path.parent))
        try:
            with os.fdopen(tmp_fd, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=PAPER_CSV_HEADER,
                                        extrasaction="ignore")
                writer.writeheader()
                for r in rows:
                    writer.writerow({k: r.get(k, "") for k in PAPER_CSV_HEADER})
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass

    paper_row = {
        "currency":                          currency,
        "nodes":                             int(args.nodes or 0),
        "wallets":                           int(args.wallets or 0),
        "miners":                            int(args.miners or 0),
        "transactions":                      int(args.transactions or 0),
        "interval":                          float(args.interval or 0.0),
        "shards":                            int(shards),
        "average block time":                float(avg_bt),
        "block size":                        int(block_size_tx),
        "messages":                          int(msgs),
        "mode":                              mode,
        "tps":                               float(tps),
        "no. of blocks generated":           int(blocks),
        "blocktime in configuration file":   float(args.blocktime),
        "sig_scheme":                        str(args.sig_scheme or ""),
    }

    print("\n===== PAPER CSV Row =====")
    print(",".join(PAPER_CSV_HEADER))
    print(",".join(str(paper_row[h]) for h in PAPER_CSV_HEADER))

    if getattr(args, "results_csv", ""):
        out_path = str(Path(args.results_dir) / args.results_csv)
        upsert_paper_csv_row(out_path, paper_row)
        print(f"Wrote Results row -> {out_path}")


if __name__ == "__main__":
    main()