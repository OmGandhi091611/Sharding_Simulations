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
import csv
from pathlib import Path
from collections import deque
from typing import Optional, Deque, Tuple, List
from typing import Generator
from simpy.events import Event


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

# Network model knobs (set from CLI)
LINK_RTT_MS = 0.0       # logical RTT
LINK_JITTER_MS = 0.0    # extra jitter (one-way)
LINK_MSG_PROC_MS = 0.0  # CPU per received message
CTRL_BW_MBPS = 0.0      # control NIC throughput
DATA_BW_MBPS = 0.0      # block NIC throughput

# Propagation model
PROP_MODEL = "exact"    # exact | gossip | analytic
GOSSIP_FANOUT = 16
GOSSIP_ROUNDS = 10

# Cached for analytic propagation
GLOBAL_N = 0
GLOBAL_AVG_DEG = 0


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
    """
    Aggregate delay for num_msgs control messages of size msg_size_bytes,
    flowing through a single control-plane NIC of rate CTRL_BW_MBPS.
    """
    if num_msgs <= 0:
        return 0.0

    # Throughput part (NIC)
    if CTRL_BW_MBPS and CTRL_BW_MBPS > 0:
        Bps = CTRL_BW_MBPS * 1e6 / 8.0
        send_time = (num_msgs * msg_size_bytes) / max(Bps, 1e-9)
    else:
        send_time = 0.0

    # CPU + one-way latency
    cpu_time = num_msgs * recv_processing_s()
    latency = sample_one_way_latency_s()
    return send_time + cpu_time + latency


def data_plane_send_delay_s(total_bytes: float) -> float:
    """
    Deterministic delay to send total_bytes over the data-plane NIC.
    No token bucket (avoids tons of env events).
    """
    if total_bytes <= 0:
        return 0.0
    if DATA_BW_MBPS and DATA_BW_MBPS > 0:
        Bps = DATA_BW_MBPS * 1e6 / 8.0
        return total_bytes / max(Bps, 1e-9)
    return 0.0


# ============================================================
# Core objects
# ============================================================
class Block:
    def __init__(self, i: int, tx: int, dt: float):
        self.id = i
        self.tx = tx
        self.size = HEADER_SIZE + tx * 256
        self.dt = dt   # simulated round duration in seconds


class Node:
    def __init__(self, env: simpy.Environment, i: int):
        self.env = env
        self.id = i
        self.blocks = set()
        self.neighbors: List["Node"] = []

    def _recv_block(self, b: Block):
        """Per-edge link latency + jitter + per-message CPU."""
        delay_s = sample_one_way_latency_s() + recv_processing_s()
        yield self.env.timeout(delay_s)

        if b.id in self.blocks:
            return
        self.blocks.add(b.id)

        # In exact mode, this node will also forward via receive()
        if PROP_MODEL == "exact":
            self.env.process(self.receive(b))

    def _gossip_round(self, frontier: List["Node"], b: Block) -> Generator[Event, None, List["Node"]]:
        """
        One gossip step:
          Each node in frontier contacts up to GOSSIP_FANOUT neighbors.
        Returns next frontier (nodes newly infected this round).
        """
        newly = []
        for u in frontier:
            # pick a bounded fanout
            if not u.neighbors:
                continue
            fan = min(GOSSIP_FANOUT, len(u.neighbors))
            picks = random.sample(u.neighbors, fan) if fan > 0 else []
            for v in picks:
                if b.id in v.blocks:
                    continue
                # receiver delay approximated once per contact
                delay_s = sample_one_way_latency_s() + recv_processing_s()
                yield self.env.timeout(delay_s)
                if b.id in v.blocks:
                    continue
                v.blocks.add(b.id)
                newly.append(v)
        return newly

    def receive(self, b: Block):
        """
        Called when this node learns about block b.

        Propagation models:
          - exact   : explicit flooding on the neighbor graph (slow at large N/deg)
          - gossip  : bounded fanout + limited rounds (faster)
          - analytic: O(1) approximation (fastest)

        This function updates global network_data/io_requests for accounting.
        """
        global network_data, io_requests, GLOBAL_N, GLOBAL_AVG_DEG

        if b.id in self.blocks:
            return
        self.blocks.add(b.id)

        # -------------------------
        # ANALYTIC (fastest)
        # -------------------------
        if PROP_MODEL == "analytic":
            N = max(0, int(GLOBAL_N))
            deg = max(0, int(GLOBAL_AVG_DEG))
            total_edges = N * deg

            io_requests += total_edges
            network_data += total_edges * b.size

            # One big broadcast delay: bandwidth + one-way latency + CPU
            total_bytes = total_edges * b.size
            delay = data_plane_send_delay_s(total_bytes) + sample_one_way_latency_s() + recv_processing_s()
            if delay > 0:
                yield self.env.timeout(delay)
            return

        # -------------------------
        # GOSSIP (bounded work)
        # -------------------------
        if PROP_MODEL == "gossip":
            # Approximate accounting: each contacted neighbor counts as an "edge send".
            # We'll account for fanout contacts per round.
            frontier = [self]
            rounds = max(0, int(GOSSIP_ROUNDS))
            for _ in range(rounds):
                if not frontier:
                    break

                # bytes/contact: assume one block message
                contacts = 0
                for u in frontier:
                    fan = min(GOSSIP_FANOUT, len(u.neighbors))
                    contacts += fan
                io_requests += contacts
                network_data += contacts * b.size

                # Send-time over data NIC for this round's contacts
                round_bytes = contacts * b.size
                send_delay = data_plane_send_delay_s(round_bytes)
                if send_delay > 0:
                    yield self.env.timeout(send_delay)

                # Propagate with per-contact latency+cpu
                newly = []
                for u in frontier:
                    if not u.neighbors:
                        continue
                    fan = min(GOSSIP_FANOUT, len(u.neighbors))
                    picks = random.sample(u.neighbors, fan) if fan > 0 else []
                    for v in picks:
                        if b.id in v.blocks:
                            continue
                        yield self.env.timeout(sample_one_way_latency_s() + recv_processing_s())
                        if b.id in v.blocks:
                            continue
                        v.blocks.add(b.id)
                        newly.append(v)

                frontier = newly
            return

        # -------------------------
        # EXACT (explicit neighbors)
        # -------------------------
        degree = len(self.neighbors)
        if degree > 0:
            total_bytes = degree * b.size
            send_time = data_plane_send_delay_s(total_bytes)
            if send_time > 0:
                yield self.env.timeout(send_time)

        for n in self.neighbors:
            io_requests += 1
            network_data += b.size
            self.env.process(n._recv_block(b))


class Miner:
    def __init__(self, i: int, h: float):
        self.id = i
        self.h = h


# ============================================================
# Workload
# ============================================================
def tx_arrivals(env: simpy.Environment, wallets: int, tx_per_wallet: int, interval: float):
    """
    Aggregate tx arrivals (fast):
      Each interval, each wallet emits ~1 tx, until tx_per_wallet per wallet is exhausted.
    """
    global pool_count, pool_deque, POOL_MODE

    total_remaining = int(max(0, wallets) * max(0, tx_per_wallet))
    if total_remaining <= 0:
        return

    # Edge case: interval <= 0 => dump all at t=0
    if interval is None or interval <= 0:
        if POOL_MODE == "count":
            pool_count += total_remaining
        else:
            # timestamps all 0.0
            for _ in range(total_remaining):
                pool_deque.append((-1, 0.0))
        return

    while total_remaining > 0:
        yield env.timeout(interval)
        batch = min(wallets, total_remaining)

        if POOL_MODE == "count":
            pool_count += batch
        else:
            now = env.now
            # We don't care which wallet specifically; keep wid=-1
            for _ in range(batch):
                pool_deque.append((-1, now))

        total_remaining -= batch


def pool_available() -> int:
    global pool_count, pool_deque, POOL_MODE
    return pool_count if POOL_MODE == "count" else len(pool_deque)


def pool_take(k: int):
    """Remove k tx from pool, O(1) per tx in deque mode, O(1) total in count mode."""
    global pool_count, pool_deque, POOL_MODE
    if k <= 0:
        return
    if POOL_MODE == "count":
        pool_count = max(0, pool_count - k)
        return
    # deque mode
    for _ in range(k):
        if not pool_deque:
            break
        pool_deque.popleft()


# ============================================================
# Metronome mode – messaging model
# ============================================================
def metronome_messages(shards: int, num_nodes: int) -> int:
    """
    Approximate total control + broadcast messages in metronome mode.

      * winner <-> metronome: 2*S
      * winner <-> winner (pairwise): S*(S-1)/2
      * network broadcast (include sender): N
    """
    S = max(0, int(shards))
    N = max(0, int(num_nodes))
    if S <= 1:
        return N
    return 2 * S + (S * (S - 1)) // 2 + N


# ============================================================
# Coordinators
# ============================================================
def coord(env,
          nodes,
          miners,
          target_bt,
          diff0,
          blocks_limit,
          total_blocksize,
          print_int,
          dbg,
          wallets,
          tx_per_wallet,
          init_reward,
          halving_interval,
          shards,
          tx_cost_ms,
          rtt_ms,
          coord_rounds,
          msg_cost,
          msg_size,
          control_bw_mbps,
          broadcast_bw_mbps,
          overlap_broadcast,
          msg_proc_ms,
          quiet_blocks=False):
    """
    Metronome: S shard winners talk to metronome + each other, then block flood.
    Round time includes mining + verification + coordination + control NIC.
    Block flood uses chosen propagation model (exact/gossip/analytic).
    """
    global network_data, io_requests, total_tx, total_coins

    if not miners:
        raise ValueError("Need at least one miner")
    S = max(1, shards or 1)
    Hs = harmonic_number(S)

    total_hash = sum(m.h for m in miners)
    if total_hash <= 0:
        raise ValueError("Total hashrate must be > 0")

    if diff0 is not None:
        diff = float(diff0)
    else:
        lam_target = (S * Hs) / max(target_bt, 1e-9)
        diff = total_hash / lam_target

    lam_shard = total_hash / diff
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

        # start propagation
        env.process(random.choice(nodes).receive(b))

        reward = _apply_halving_and_mint(bc, reward, halving_interval)

        # Printing control
        if (not quiet_blocks) and (dbg or (print_int and bc % print_int == 0)):
            net_note = f"prop={PROP_MODEL}"
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
    """Each shard winner does a network-wide announcement: S * N msgs."""
    if S <= 0 or N <= 0:
        return 0.0, 0
    msgs = S * N
    dt = control_phase_delay(msgs, msg_size)
    return dt, msgs


def winner_pairwise_phase(S: int, msg_size: int):
    """Pairwise winner coordination: C(S,2) messages."""
    if S <= 1:
        return 0.0, 0
    pairs = (S * (S - 1)) // 2
    dt = control_phase_delay(pairs, msg_size)
    return dt, pairs


# ============================================================
# Coordinator – no-metronome mode
# ============================================================
def coord_no_metronome(env,
                       nodes,
                       miners,
                       target_bt,
                       diff0,
                       blocks_limit,
                       total_blocksize,
                       print_int,
                       dbg,
                       wallets,
                       tx_per_wallet,
                       init_reward,
                       halving_interval,
                       shards,
                       tx_cost_ms,
                       msg_size,
                       control_bw_mbps,
                       broadcast_bw_mbps,
                       overlap_broadcast,
                       msg_proc_ms,
                       msg_cost,
                       quiet_blocks=False):
    """
    No metronome:
      * S shard winners, each announces to the whole network
      * Winners then coordinate pairwise
      * One winner submits merged block
    """
    global network_data, io_requests, total_tx, total_coins

    if not miners:
        raise ValueError("Need at least one miner")
    S = max(1, shards or 1)
    Hs = harmonic_number(S)

    total_hash = sum(m.h for m in miners)
    if total_hash <= 0:
        raise ValueError("Total hashrate must be > 0")

    if diff0 is not None:
        diff = float(diff0)
    else:
        lam_target = (S * Hs) / max(target_bt, 1e-9)
        diff = total_hash / lam_target

    lam_shard = total_hash / diff
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
            net_note = f"prop={PROP_MODEL}"
            print(
                f"[{env.now:.2f}] Block {bc} contains {txs_next-1} tx "
                f"(total_cap={tot_cap}, shards={S}, per_shard≈{(tot_cap+S-1)//S if S>0 else 0}) "
                f"dt={round_dt:.3f}s mine={dt_mine:.3f}s verify={dt_verify:.3f}s "
                f"announce+pair={dt_control:.3f}s [{net_note}] "
                f"CtrlMsgs:{total_control_msgs} MsgCost_tot:{total_msg_cost:.2f}"
            )

        if print_int and bc % print_int == 0:
            _print_summary(env.now, bc, blocks_limit, diff, total_hash,
                           total_msg_cost,
                           extra_fields=f"CtrlMsgs:{total_control_msgs}")

    _print_final(env.now, bc, blocks_limit, diff, total_hash,
                 total_control_msgs, total_msg_cost,
                 extra_fields="(no metronome)")


# ============================================================
# Leader-metronome helpers
# ============================================================
def leader_announce_phase(N: int, msg_size: int):
    """Leader announces to entire network: N msgs via control NIC."""
    if N <= 0:
        return 0.0, 0
    msgs = N
    dt = control_phase_delay(msgs, msg_size)
    return dt, msgs


def to_leader_phase(S: int, msg_size: int):
    """Other S-1 winners send shard metadata to leader."""
    if S <= 1:
        return 0.0, 0
    msgs = S - 1
    dt = control_phase_delay(msgs, msg_size)
    return dt, msgs


# ============================================================
# Coordinator – leader-metronome mode
# ============================================================
def coord_leader_metronome(env,
                           nodes,
                           miners,
                           target_bt,
                           diff0,
                           blocks_limit,
                           total_blocksize,
                           print_int,
                           dbg,
                           wallets,
                           tx_per_wallet,
                           init_reward,
                           halving_interval,
                           shards,
                           tx_cost_ms,
                           msg_size,
                           control_bw_mbps,
                           broadcast_bw_mbps,
                           overlap_broadcast,
                           msg_proc_ms,
                           msg_cost,
                           verify_mode="leader",
                           quiet_blocks=False):
    """
    Leader metronome:

      1) S shards mine in parallel; earliest finisher becomes leader.
      2) Leader announces to entire network (N msgs).
      3) Other winners send to leader only (S-1 msgs).
      4) Verification depends on verify_mode:
         * leader      – all tx at leader
         * leader_par  – leader verifies in parallel (threads env var)
         * shard       – per-shard verify + S attestations to leader
      5) Leader broadcasts final block via chosen propagation model.
    """
    global network_data, io_requests, total_tx, total_coins

    if not miners:
        raise ValueError("Need at least one miner")
    S = max(1, shards or 1)
    Hs = harmonic_number(S)

    total_hash = sum(m.h for m in miners)
    if total_hash <= 0:
        raise ValueError("Total hashrate must be > 0")

    if diff0 is not None:
        diff = float(diff0)
    else:
        lam_target = (S * Hs) / max(target_bt, 1e-9)
        diff = total_hash / lam_target

    lam_shard = total_hash / diff
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
        leader_idx = min(range(S), key=lambda i: shard_times[i])
        yield env.timeout(dt_mine)

        if has_tx:
            avail = pool_available()
            take = min(avail, tot_cap)
            pool_processed += take
            pool_take(take)
        else:
            take = 0

        N = len(nodes)
        dt_ann, msgs_ann = leader_announce_phase(N, msg_size)
        dt_to_leader, msgs_to_leader = to_leader_phase(S, msg_size)
        dt_control = dt_ann + dt_to_leader
        total_control_msgs += (msgs_ann + msgs_to_leader)
        total_msg_cost += (msgs_ann + msgs_to_leader) * float(msg_cost or 0.0)

        txs_next = (take + 1) if has_tx else 1
        verify_mode_used = verify_mode or "leader"

        if verify_mode_used == "leader":
            dt_verify_term = (txs_next - 1) * tx_cost_s if has_tx else 0.0
            verify_note = "verify_all@leader"

        elif verify_mode_used == "leader_par":
            threads = max(1, int(os.getenv("LEADER_VERIFY_THREADS", "8")))
            dt_verify_term = ((txs_next - 1) * tx_cost_s / threads) if has_tx else 0.0
            verify_note = f"verify_all@leader_par({threads}t)"

        elif verify_mode_used == "shard":
            max_shard_tx = (txs_next - 1 + S - 1) // S if has_tx else 0
            dt_verify_term = max_shard_tx * tx_cost_s

            dt_attest = control_phase_delay(S, msg_size)
            dt_control += dt_attest
            total_control_msgs += S
            total_msg_cost += S * float(msg_cost or 0.0)
            verify_note = f"verify@shards + attest(S={S})"

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
            net_note = f"prop={PROP_MODEL}"
            print(
                f"[{env.now:.2f}] Block {bc} (leader shard={leader_idx}) "
                f"tx={txs_next-1} dt={round_dt:.3f}s "
                f"mine={dt_mine:.3f}s control={dt_control:.3f}s "
                f"{verify_note}={dt_verify_term:.3f}s "
                f"[{net_note}] CtrlMsgs_tot:{total_control_msgs} MsgCost_tot:{total_msg_cost:.2f}"
            )

        if print_int and bc % print_int == 0:
            _print_summary(env.now, bc, blocks_limit, diff, total_hash,
                           total_msg_cost,
                           extra_fields=f"LeaderMode verify={verify_mode_used} "
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
# Graph generation helpers
# ============================================================
def build_random_k_out_graph(nodes: List[Node], k: int, seed: Optional[int] = None):
    """
    Build a directed k-out graph:
      each node chooses up to k distinct neighbors uniformly at random.

    Runs in ~O(N*k) and avoids O(N^2) per-node list rebuilds.
    """
    if seed is not None:
        random.seed(seed)

    n = len(nodes)
    if n <= 1 or k <= 0:
        for u in nodes:
            u.neighbors = []
        return

    k = min(k, n - 1)
    node_ids = list(range(n))

    for i, u in enumerate(nodes):
        # sample k distinct ids != i via rejection sampling
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

    # Label for MEMO-style CSV row
    p.add_argument("--currency", type=str, default="memo")

    # Workload / network
    p.add_argument("--nodes", type=int)
    p.add_argument("--neighbors", type=int)
    p.add_argument("--miners", type=int)
    p.add_argument("--hashrate", type=float)
    p.add_argument("--wallets", type=int)
    p.add_argument("--transactions", type=int)
    p.add_argument("--interval", type=float)

    # Timing & supply
    p.add_argument("--blocktime", type=float, default=600.0,
                   help="Baseline target block time (s) for S=1")
    p.add_argument("--difficulty", dest="diff0", type=float,
                   help="Override shard difficulty")
    p.add_argument("--blocks", dest="blocks_limit", type=int,
                   help="Number of merged blocks (optional)")
    p.add_argument("--years", dest="years", type=float,
                   help="Run ~years if --blocks omitted")
    p.add_argument("--reward", dest="init_reward", type=float, default=50.0)
    p.add_argument("--halving", dest="halving_interval", type=int, default=210000)

    # Sharding & capacity
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("--total-blocksize", dest="total_blocksize", type=int, default=4096,
                   help="Total tx per merged block across ALL shards")

    # Verification & coordination
    p.add_argument("--tx_cost_ms", type=float, default=1.0)
    p.add_argument("--rtt_ms", type=float, default=0.0)
    p.add_argument("--jitter_ms", type=float, default=1.0,
                   help="One-way jitter upper-bound (ms)")
    p.add_argument("--coord_rounds", type=int, default=0)
    p.add_argument("--cost", type=float, default=0.0,
                   help="Accounting cost per coordination message")

    # Network throughput knobs
    p.add_argument("--msg_size", type=int, default=200,
                   help="Avg size of control message (bytes)")
    p.add_argument("--control_bw_mbps", type=float, default=0.0,
                   help="Control-plane bandwidth in Mbps")
    p.add_argument("--broadcast_bw_mbps", type=float, default=0.0,
                   help="Broadcast bandwidth in Mbps for blocks")
    p.add_argument("--overlap_broadcast", action="store_true",
                   help="(unused now; kept for compatibility)")
    p.add_argument("--msg_proc_ms", type=float, default=1.0,
                   help="Processing cost per message (ms)")

    # I/O & config
    p.add_argument("--print", dest="print_int", type=int, default=144)
    p.add_argument("--debug", action="store_true")
    p.add_argument("--config", type=str,
                   help="JSON config file (overlays CLI)")
    p.add_argument("--prefill", action="store_true",
                   help="Prefill all workload tx at t=0 (fast)")

    # Modes
    p.add_argument("--no_metronome", action="store_true")
    p.add_argument("--leader_metronome", action="store_true")

    # Verification strategy for leader_metronome
    p.add_argument("--verify_mode", type=str, default="leader",
                   choices=["leader", "shard", "leader_par"])

    # Performance knobs
    p.add_argument("--pool_mode", type=str, default="count",
                   choices=["count", "deque"],
                   help="Transaction pool storage: count (fastest) or deque (FIFO timestamps)")
    p.add_argument("--prop_model", type=str, default="exact",
                   choices=["exact", "gossip", "analytic"],
                   help="Block propagation model")
    p.add_argument("--gossip_fanout", type=int, default=16)
    p.add_argument("--gossip_rounds", type=int, default=10)
    p.add_argument("--quiet_blocks", action="store_true",
                   help="Suppress per-block prints; keep summary prints")

    # Results output
    p.add_argument("--results_csv", type=str, default="",
                   help="Relative path under Results/ (e.g., Near.csv, non-sharded.csv)")
    p.add_argument("--results_dir", type=str, default="Results",
                   help="Results output directory (default: Results)")

    args = p.parse_args()

    # JSON overlay
    config_data = {}
    if args.config:
        with open(args.config, "r") as f:
            config_data = json.load(f)
    for k, v in config_data.items():
        if hasattr(args, k):
            setattr(args, k, v)

    # Run length
    blocks_limit = args.blocks_limit
    if blocks_limit is None and args.years:
        blocks_limit = int(args.years * YEAR / (args.blocktime or 1))
    args.blocks_limit = blocks_limit

    # Globals setup
    global LINK_RTT_MS, LINK_JITTER_MS, LINK_MSG_PROC_MS, CTRL_BW_MBPS, DATA_BW_MBPS
    LINK_RTT_MS = float(args.rtt_ms or 0.0)
    LINK_JITTER_MS = float(args.jitter_ms or 0.0)
    LINK_MSG_PROC_MS = float(args.msg_proc_ms or 0.0)
    CTRL_BW_MBPS = float(args.control_bw_mbps or 0.0)
    DATA_BW_MBPS = float(args.broadcast_bw_mbps or 0.0)

    global PROP_MODEL, GOSSIP_FANOUT, GOSSIP_ROUNDS
    PROP_MODEL = str(args.prop_model or "exact")
    GOSSIP_FANOUT = int(args.gossip_fanout or 16)
    GOSSIP_ROUNDS = int(args.gossip_rounds or 10)

    global POOL_MODE, pool_count, pool_deque
    POOL_MODE = str(args.pool_mode or "count")
    pool_count = 0
    pool_deque = deque()

    env = simpy.Environment()

    # Build workload
    total_tx_need = (args.wallets or 0) * (args.transactions or 0)
    if args.prefill and total_tx_need > 0:
        if POOL_MODE == "count":
            pool_count = total_tx_need
        else:
            for _ in range(total_tx_need):
                pool_deque.append((-1, 0.0))
    else:
        env.process(tx_arrivals(env, args.wallets or 0, args.transactions or 0, args.interval or 0.0))

    # Build nodes + graph
    nodes = [Node(env, i) for i in range(args.nodes or 0)]
    build_random_k_out_graph(nodes, int(args.neighbors or 0), seed=None)

    global GLOBAL_N, GLOBAL_AVG_DEG
    GLOBAL_N = len(nodes)
    GLOBAL_AVG_DEG = int(args.neighbors or 0)

    # Miners
    miners = [Miner(i, args.hashrate or 0.0) for i in range(args.miners or 0)]

    # Choose coordinator
    if args.leader_metronome:
        coord_proc = env.process(
            coord_leader_metronome(
                env, nodes, miners,
                target_bt=args.blocktime,
                diff0=args.diff0,
                blocks_limit=args.blocks_limit,
                total_blocksize=args.total_blocksize,
                print_int=args.print_int,
                dbg=args.debug,
                wallets=args.wallets,
                tx_per_wallet=args.transactions,
                init_reward=args.init_reward,
                halving_interval=args.halving_interval,
                shards=args.shards,
                tx_cost_ms=args.tx_cost_ms,
                msg_size=args.msg_size,
                control_bw_mbps=args.control_bw_mbps,
                broadcast_bw_mbps=args.broadcast_bw_mbps,
                overlap_broadcast=args.overlap_broadcast,
                msg_proc_ms=args.msg_proc_ms,
                msg_cost=args.cost,
                verify_mode=args.verify_mode,
                quiet_blocks=args.quiet_blocks,
            )
        )
    elif args.no_metronome:
        coord_proc = env.process(
            coord_no_metronome(
                env, nodes, miners,
                target_bt=args.blocktime,
                diff0=args.diff0,
                blocks_limit=args.blocks_limit,
                total_blocksize=args.total_blocksize,
                print_int=args.print_int,
                dbg=args.debug,
                wallets=args.wallets,
                tx_per_wallet=args.transactions,
                init_reward=args.init_reward,
                halving_interval=args.halving_interval,
                shards=args.shards,
                tx_cost_ms=args.tx_cost_ms,
                msg_size=args.msg_size,
                control_bw_mbps=args.control_bw_mbps,
                broadcast_bw_mbps=args.broadcast_bw_mbps,
                overlap_broadcast=args.overlap_broadcast,
                msg_proc_ms=args.msg_proc_ms,
                msg_cost=args.cost,
                quiet_blocks=args.quiet_blocks,
            )
        )
    else:
        coord_proc = env.process(
            coord(
                env, nodes, miners,
                target_bt=args.blocktime,
                diff0=args.diff0,
                blocks_limit=args.blocks_limit,
                total_blocksize=args.total_blocksize,
                print_int=args.print_int,
                dbg=args.debug,
                wallets=args.wallets,
                tx_per_wallet=args.transactions,
                init_reward=args.init_reward,
                halving_interval=args.halving_interval,
                shards=args.shards,
                tx_cost_ms=args.tx_cost_ms,
                rtt_ms=args.rtt_ms,
                coord_rounds=args.coord_rounds,
                msg_cost=args.cost,
                msg_size=args.msg_size,
                control_bw_mbps=args.control_bw_mbps,
                broadcast_bw_mbps=args.broadcast_bw_mbps,
                overlap_broadcast=args.overlap_broadcast,
                msg_proc_ms=args.msg_proc_ms,
                quiet_blocks=args.quiet_blocks,
            )
        )

    env.run(until=coord_proc)

    # MEMO-style row
    global sim_summary
    if sim_summary:
        blocks = sim_summary["blocks"]
        avg_bt = sim_summary["avg_block_time"]
        tps = sim_summary["tps"]
        msgs = sim_summary["total_msgs"]
    else:
        blocks = 0
        avg_bt = 0.0
        tps = 0.0
        msgs = 0

    mode = "conventional" if int(args.shards or 1) == 1 else "sharded"
    block_size_tx = float(args.total_blocksize or 0)
    shards = int(args.shards or 1)
    throughput_shard = tps / shards if shards > 0 else 0.0
    currency = getattr(args, "currency", "memo")

    print("\n===== MEMO-style Table Row (CSV) =====")
    print("currency,nodes,wallets,miners,transactions,interval,shards,"
          "average_block_time,block_size,messages,mode,tps,throughput_shard,"
          "num_blocks,blocktime_cfg,expected_blocktime")

    print(
        f"{currency},"
        f"{args.nodes or 0},"
        f"{args.wallets or 0},"
        f"{args.miners or 0},"
        f"{args.transactions or 0},"
        f"{args.interval or 0},"
        f"{shards},"
        f"{avg_bt:.3f},"
        f"{block_size_tx:.3f},"
        f"{float(msgs):.3f},"
        f"{mode},"
        f"{tps:.3f},"
        f"{throughput_shard:.3f},"
        f"{blocks},"
        f"{float(args.blocktime):.1f},"
        f"{float(args.blocktime):.1f}"
    )

    PAPER_CSV_HEADER = [
        "currency",
        "nodes",
        "wallets",
        "miners",
        "transactions",
        "interval",
        "shards",
        "average block time",
        "block size",
        "messages",
        "mode",
        "tps",
        "no. of blocks generated",
        "blocktime in configuration file"
    ]

    def upsert_paper_csv_row(results_path: str, row: dict):
        """
        Upsert a row into Results CSV using a composite key:
        (currency, shards, block size, mode, blocktime in configuration file)
        """
        path = Path(results_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        key_fields = ["currency", "shards", "block size", "mode", "blocktime in configuration file"]

        def row_key(r: dict):
            return tuple(str(r.get(k, "")) for k in key_fields)

        rows = []
        if path.exists() and path.stat().st_size > 0:
            with path.open("r", newline="") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    rows.append(r)

        new_key = row_key(row)
        replaced = False
        for i, r in enumerate(rows):
            if row_key(r) == new_key:
                rows[i] = {**r, **row}
                replaced = True
                break
        if not replaced:
            rows.append(row)

        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=PAPER_CSV_HEADER, extrasaction="ignore")
            writer.writeheader()
            for r in rows:
                writer.writerow({k: r.get(k, "") for k in PAPER_CSV_HEADER})

    paper_row = {
        "currency": currency,
        "nodes": int(args.nodes or 0),
        "wallets": int(args.wallets or 0),
        "miners": int(args.miners or 0),
        "transactions": int(args.transactions or 0),
        "interval": float(args.interval or 0.0),
        "shards": int(shards),
        "average block time": float(avg_bt),
        "block size": int(block_size_tx),
        "messages": int(msgs),
        "mode": mode,
        "tps": float(tps),
        "no. of blocks generated": int(blocks),
        "blocktime in configuration file": int(args.blocktime),
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