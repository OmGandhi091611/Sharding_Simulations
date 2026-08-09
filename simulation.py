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
import math
import random
import json
import os
import sys
import csv
from pathlib import Path
from collections import deque
from typing import Optional, Deque, Tuple, List, Dict
from io import StringIO
import tempfile


# ============================================================
# Globals
# ============================================================
network_data = 0      # bytes of block data sent
io_requests = 0       # number of block sends (edges)
total_tx = 0          # total transactions (including coinbase)
total_coins = 0.0     # minted coins
# Aggregate CPU-seconds burned processing broadcast fan-out messages
# (gossip/Plumtree/GossipSub), across all receivers network-wide. NOT
# charged to any single round's critical-path delay — see the note in
# gossip_broadcast_delay() for why.
total_broadcast_cpu_seconds = 0.0
# Every node's hop-count-to-first-delivery, pooled across every block's
# final-propagation broadcast for the whole run. Raw material for the
# hop-count CDF: sort this and take cumulative fractions.
hop_cdf_samples: List[int] = []
# Per-block record of the final-propagation broadcast (block_number, rounds,
# messages, coverage) — opt-in via --per_block_csv. Lets you see how a block's
# rounds/messages evolve over the run, e.g. Plumtree's tree converging.
per_block_log: List[Tuple[int, int, int, float]] = []

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
GOSSIP_FANOUT = 10      # peers contacted per node per gossip round
KADEMLIA_BUCKET_BITS = 1  # bits narrowed per Kademlia routing hop (1 = classical log2 N)
PLUMTREE_LAZY_FANOUT = 2      # non-tree neighbors sent an IHAVE per node per round
GOSSIPSUB_MESH_DEGREE = 8     # peers sent the FULL message per node per round
GOSSIPSUB_IHAVE_FANOUT = 4    # remaining neighbors sent an IHAVE per node per round
ANNOUNCE_MSG_SIZE_BYTES = 32  # lightweight IHAVE/announcement size (message-ID only)
HOP_TTL = 64            # fixed per-broadcast hop/round cap (real-world TTL-style bound,
                        # e.g. IP's default TTL=64 — independent of network size/fanout,
                        # unlike the dynamically-computed gossip_ttl() estimate)


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


def control_phase_delay(num_msgs: int, msg_size_bytes: int, charge_recv_cpu: bool = True) -> float:
    """
    `charge_recv_cpu` should be True for many-to-one phases (e.g. the
    leader receiving `num_msgs` reports) — the leader has one CPU, so
    processing them really does serialize on its critical path. It should
    be False for one-to-many phases (e.g. the leader announcing to
    `num_msgs` peers) — each peer processes its own copy on its own CPU
    concurrently, so charging N receivers' processing time onto the
    sender's timeline would double-count it, the same reasoning
    gossip_broadcast_delay() already applies to gossip's fan-out.
    """
    if num_msgs <= 0:
        return 0.0
    if CTRL_BW_MBPS and CTRL_BW_MBPS > 0:
        Bps = CTRL_BW_MBPS * 1e6 / 8.0
        send_time = (num_msgs * msg_size_bytes) / max(Bps, 1e-9)
    else:
        send_time = 0.0
    cpu_time = num_msgs * recv_processing_s() if charge_recv_cpu else 0.0
    latency = sample_one_way_latency_s()
    return send_time + cpu_time + latency


# ============================================================
# Gossip protocol (epidemic broadcast: bounded fanout + rounds)
# ============================================================
def gossip_ttl(num_nodes: int, fanout: int, slack: float = 2.0) -> int:
    """
    Round budget for near-certain full coverage of a push-based gossip
    broadcast: log_k(N) rounds to spread exponentially, plus a ln(N)
    "coupon collector" tail for the last stragglers, plus slack rounds.
    """
    N = max(1, int(num_nodes))
    k = max(1, int(fanout))
    if N <= 1:
        return 0
    spread_rounds = math.log(N) / math.log(k) if k > 1 else float(N)
    tail_rounds = math.log(max(N, 2))
    return max(1, math.ceil(spread_rounds + tail_rounds + slack))


def build_gossip_adjacency(nodes: List["Node"]) -> List[List[int]]:
    """
    Extract each node's fixed neighbor set (built by build_random_k_out_graph)
    as plain index lists, so gossip can be re-run round after round without
    touching Node objects directly. Assumes nodes[i].id == i.
    """
    return [[n.id for n in u.neighbors] for u in nodes]


def _gossip_peers(node_idx: int, adjacency: List[List[int]], fanout: int) -> List[int]:
    """Pick up to `fanout` distinct random peers from node_idx's fixed neighbor list."""
    candidates = adjacency[node_idx]
    k = min(max(0, fanout), len(candidates))
    if k <= 0:
        return []
    return random.sample(candidates, k)


def simulate_gossip(source_idx: int, adjacency: List[List[int]], fanout: int,
                     ttl: Optional[int] = None,
                     events: Optional[List[dict]] = None
                     ) -> Tuple[int, int, float, Dict[int, int]]:
    """
    Round-based epidemic broadcast from `source_idx`, constrained to the
    fixed neighbor topology in `adjacency` (same graph flooding uses) rather
    than sampling from the whole node population.

    Each round, every newly-informed node (the "frontier") forwards to
    `fanout` random neighbors. A node that already holds the message discards
    a repeat delivery instead of re-forwarding it (seen-flag dedup) — this
    is what bounds the broadcast, not the TTL. TTL is a fixed, real-world-
    style hop cap (default HOP_TTL=64, independent of network size or
    fanout — analogous to IP's default TTL) rather than a size-derived
    estimate; if hit before full coverage, coverage may be < 1.0, which is
    itself a useful metric.

    Returns (rounds_used, total_messages_sent, coverage_fraction, first_seen).
    `first_seen` maps node id -> the round it first received the message
    (source is 0); this is the raw per-node hop-count data a CDF of
    delivery-by-hop gets built from. Message counts include deliveries to
    already-informed nodes, since bandwidth/CPU cost is paid on send
    regardless of whether the receiver needed the message.
    """
    N = max(1, len(adjacency))
    k = max(0, int(fanout))
    if N <= 1:
        return 0, 0, 1.0, {source_idx: 0}

    if ttl is None:
        ttl = HOP_TTL

    informed = {source_idx}
    frontier = {source_idx}
    first_seen: Dict[int, int] = {source_idx: 0}
    total_messages = 0
    rounds_used = 0

    for r in range(1, ttl + 1):
        if not frontier or len(informed) >= N:
            break
        rounds_used = r
        next_frontier: set = set()
        for node in frontier:
            for peer in _gossip_peers(node, adjacency, k):
                total_messages += 1
                is_new = peer not in informed
                if events is not None:
                    events.append({"round": r, "kind": "gossip_forward",
                                    "from": node, "to": peer, "new": is_new})
                if is_new:
                    informed.add(peer)
                    first_seen[peer] = r
                    next_frontier.add(peer)
        frontier = next_frontier
        if len(informed) >= N:
            break

    coverage = len(informed) / N
    return rounds_used, total_messages, coverage, first_seen


def gossip_broadcast_delay(rounds: int, total_messages: int, msg_size_bytes: int) -> float:
    """
    Wall-clock CRITICAL-PATH delay for a gossip broadcast: rounds are
    sequential (one one-way latency hop paid per round, since round r+1
    can't start before round r's informed set is known), and bandwidth
    cost accrues across every message sent in every round (a sender's own
    outbound link is a genuine, shared bottleneck for however many
    messages it fans out that round).

    CPU processing cost is deliberately NOT included in this return value.
    Broadcast fan-out is one-to-many: each message lands on a different,
    independent receiving node that processes its own single message on
    its own CPU, concurrently with every other receiver — there is no
    single bottlenecked resource serially working through the whole
    message count the way there is for a many-to-one phase (e.g. Kademlia
    routing to a single leader). Charging cpu_time onto this round's
    delay would model receivers as if they were all queued behind one
    shared processor, which isn't true for genuinely distributed nodes.
    That aggregate compute cost is still tracked network-wide (useful as
    a total-resource-cost metric) in `total_broadcast_cpu_seconds`, just
    not charged against this round's timing.
    """
    global total_broadcast_cpu_seconds
    if rounds <= 0 and total_messages <= 0:
        return 0.0
    latency_total = sum(sample_one_way_latency_s() for _ in range(max(0, rounds)))
    if CTRL_BW_MBPS and CTRL_BW_MBPS > 0:
        Bps = CTRL_BW_MBPS * 1e6 / 8.0
        send_time = (total_messages * msg_size_bytes) / max(Bps, 1e-9)
    else:
        send_time = 0.0
    total_broadcast_cpu_seconds += total_messages * recv_processing_s()
    return latency_total + send_time


# ============================================================
# Plumtree (epidemic broadcast trees): real per-node adaptive version.
#
# There is no global tree computed anywhere. Each node keeps its OWN
# persistent view of which of its links are "eager" (push the full
# message) vs "lazy" (gossip a lightweight IHAVE instead), seeded to
# all-eager the first time a link is used — matching the real protocol's
# bootstrap behavior, where the very first broadcast is close to a flood.
# The efficient tree structure emerges over many broadcasts from two
# purely local reactions:
#   - PRUNE: a node that receives the full message twice (once already
#     informed) tells the redundant eager sender to demote that link to
#     lazy — no more full pushes down that path.
#   - GRAFT: a node that only hears a lazy IHAVE this round, and never
#     gets the real thing via an eager link, pulls the message and
#     promotes that lazy link back to eager for next time.
# `link_state` must be threaded across every broadcast for the life of a
# run (not rebuilt per call) for the self-organization to actually happen;
# early blocks look flood-like and should show falling message counts and
# prune activity as the tree settles.
# ============================================================
def simulate_adaptive_plumtree(source_idx: int, adjacency: List[List[int]],
                                link_state: Dict[int, Dict[int, str]],
                                lazy_fanout: int = 2,
                                ttl: Optional[int] = None,
                                events: Optional[List[dict]] = None
                                ) -> Tuple[int, int, int, int, int, float, Dict[int, int]]:
    """
    Returns (rounds, eager_messages, lazy_messages, prunes, grafts, coverage,
    first_seen). `first_seen` maps node id -> the round it first received
    the message (source is 0) — the raw per-node hop-count data a CDF of
    delivery-by-hop gets built from.

    Eager sends are never capped — they're exactly the (small, converged)
    tree edges, and capping them would break coverage. Lazy IHAVE sends
    ARE capped to `lazy_fanout` per node per round: without a cap, a node
    would gossip IHAVE to literally every non-eager neighbor forever, and
    at high --neighbors degree that steady-state background cost (unlike
    the eager side) never shrinks as the tree converges — real
    GossipSub/Plumtree deployments bound this the same way.
    """
    N = len(adjacency)
    if N <= 1:
        return 0, 0, 0, 0, 0, 1.0, {source_idx: 0}

    if ttl is None:
        ttl = HOP_TTL

    informed = {source_idx}
    frontier = {source_idx}
    first_seen: Dict[int, int] = {source_idx: 0}
    eager_messages = 0
    lazy_messages = 0
    prunes = 0
    grafts = 0
    rounds = 0

    for _ in range(max(1, ttl)):
        if not frontier:
            break
        rounds += 1
        next_frontier: set = set()
        pending_lazy: Dict[int, int] = {}   # peer -> a candidate grafter

        for node in frontier:
            node_state = link_state.setdefault(node, {})
            eager_peers = []
            lazy_peers = []
            for peer in adjacency[node]:
                state = node_state.get(peer, "eager")
                (eager_peers if state == "eager" else lazy_peers).append(peer)

            for peer in eager_peers:
                eager_messages += 1
                if peer not in informed:
                    informed.add(peer)
                    first_seen[peer] = rounds
                    next_frontier.add(peer)
                    if events is not None:
                        events.append({"round": rounds, "kind": "eager",
                                        "from": node, "to": peer, "outcome": "new"})
                else:
                    # peer already had it — this eager push was wasted;
                    # peer would PRUNE this link, demoting it to lazy.
                    node_state[peer] = "lazy"
                    prunes += 1
                    if events is not None:
                        events.append({"round": rounds, "kind": "eager",
                                        "from": node, "to": peer, "outcome": "redundant_pruned"})

            lazy_targets = random.sample(
                lazy_peers, min(max(0, lazy_fanout), len(lazy_peers)))
            for peer in lazy_targets:
                lazy_messages += 1
                informed_already = peer in informed
                if events is not None:
                    events.append({"round": rounds, "kind": "lazy_ihave",
                                    "from": node, "to": peer,
                                    "informed_already": informed_already})
                if not informed_already:
                    pending_lazy.setdefault(peer, node)

        # GRAFT: anyone who only heard a lazy IHAVE this round (no eager
        # delivery reached them) pulls the message, promoting that link.
        for peer, grafter in pending_lazy.items():
            if peer not in informed:
                grafter_state = link_state.setdefault(grafter, {})
                grafter_state[peer] = "eager"
                eager_messages += 1
                grafts += 1
                informed.add(peer)
                first_seen[peer] = rounds
                next_frontier.add(peer)
                if events is not None:
                    events.append({"round": rounds, "kind": "graft",
                                    "from": grafter, "to": peer})

        frontier = next_frontier
        if len(informed) >= N:
            break

    coverage = len(informed) / N
    return rounds, eager_messages, lazy_messages, prunes, grafts, coverage, first_seen


def plumtree_broadcast_delay(rounds: int, eager_messages: int, lazy_messages: int,
                              msg_size_bytes: int) -> float:
    """
    Wall-clock CRITICAL-PATH delay. Eager tree-push messages carry the
    full payload; lazy IHAVE announcements are tiny (just a message ID),
    sized via ANNOUNCE_MSG_SIZE_BYTES. Rounds are sequential (one tree
    layer at a time); bandwidth cost aggregates over eager + lazy
    messages combined (a sender's own outbound link is a real bottleneck).

    CPU processing cost is tracked in `total_broadcast_cpu_seconds`
    instead of being charged here — same reasoning as gossip_broadcast_delay:
    every eager/lazy message lands on a different, independent receiver
    processing its own message concurrently with everyone else's, not one
    shared resource working through the whole count serially.
    """
    global total_broadcast_cpu_seconds
    if rounds <= 0 and eager_messages <= 0 and lazy_messages <= 0:
        return 0.0
    latency_total = sum(sample_one_way_latency_s() for _ in range(max(0, rounds)))
    total_bytes = eager_messages * msg_size_bytes + lazy_messages * ANNOUNCE_MSG_SIZE_BYTES
    if CTRL_BW_MBPS and CTRL_BW_MBPS > 0:
        Bps = CTRL_BW_MBPS * 1e6 / 8.0
        send_time = total_bytes / max(Bps, 1e-9)
    else:
        send_time = 0.0
    total_messages = eager_messages + lazy_messages
    total_broadcast_cpu_seconds += total_messages * recv_processing_s()
    return latency_total + send_time


# ============================================================
# GossipSub (libp2p pubsub): bounded-degree mesh + IHAVE/IWANT gossip
# ============================================================
def simulate_gossipsub(source_idx: int, adjacency: List[List[int]],
                       mesh_degree: int = 8, ihave_fanout: int = 4,
                       ttl: Optional[int] = None,
                       events: Optional[List[dict]] = None
                       ) -> Tuple[int, int, int, float, Dict[int, int]]:
    """
    GossipSub pushes the FULL message to only `mesh_degree` peers per
    node per round — a small, fixed "mesh" subset, unlike naive gossip
    where fanout scales with however large --neighbors is. Remaining
    neighbors instead get a lightweight IHAVE (message-ID-only)
    announcement, bounded by `ihave_fanout`; in the real protocol a peer
    that doesn't already have the message would follow up with an IWANT
    pull, modeled here as the peer simply becoming informed from the
    IHAVE directly (a stand-in for that one extra request/response hop)
    rather than simulating the full pull exchange.

    Returns (rounds, full_messages, ihave_messages, coverage, first_seen).
    `first_seen` maps node id -> the round it first received the message
    (source is 0) — the raw per-node hop-count data a CDF of
    delivery-by-hop gets built from.
    """
    N = len(adjacency)
    if N <= 1:
        return 0, 0, 0, 1.0, {source_idx: 0}
    if ttl is None:
        ttl = HOP_TTL

    informed = {source_idx}
    frontier = {source_idx}
    first_seen: Dict[int, int] = {source_idx: 0}
    full_messages = 0
    ihave_messages = 0
    rounds_used = 0

    for r in range(1, ttl + 1):
        if not frontier or len(informed) >= N:
            break
        rounds_used = r
        next_frontier: set = set()
        for node in frontier:
            candidates = adjacency[node]
            mesh_peers = set(random.sample(candidates, min(mesh_degree, len(candidates))))
            for peer in mesh_peers:
                full_messages += 1
                is_new = peer not in informed
                if events is not None:
                    events.append({"round": r, "kind": "mesh_push",
                                    "from": node, "to": peer, "new": is_new})
                if is_new:
                    informed.add(peer)
                    first_seen[peer] = r
                    next_frontier.add(peer)
            remaining = [p for p in candidates if p not in mesh_peers]
            ihave_peers = random.sample(remaining, min(ihave_fanout, len(remaining)))
            for peer in ihave_peers:
                ihave_messages += 1
                is_new = peer not in informed
                if events is not None:
                    events.append({"round": r, "kind": "ihave",
                                    "from": node, "to": peer, "new": is_new})
                if is_new:
                    informed.add(peer)
                    first_seen[peer] = r
                    next_frontier.add(peer)
        frontier = next_frontier
        if len(informed) >= N:
            break

    coverage = len(informed) / N
    return rounds_used, full_messages, ihave_messages, coverage, first_seen


def gossipsub_broadcast_delay(rounds: int, full_messages: int, ihave_messages: int,
                              msg_size_bytes: int) -> float:
    """
    Wall-clock CRITICAL-PATH delay. Mesh messages carry the full payload;
    IHAVE announcements are tiny (ANNOUNCE_MSG_SIZE_BYTES). Same
    sequential-rounds / sender-bandwidth-bottleneck accounting as gossip
    and Plumtree. CPU processing cost is tracked in
    `total_broadcast_cpu_seconds` rather than charged here, for the same
    reason: independent receivers process concurrently, not serially.
    """
    global total_broadcast_cpu_seconds
    if rounds <= 0 and full_messages <= 0 and ihave_messages <= 0:
        return 0.0
    latency_total = sum(sample_one_way_latency_s() for _ in range(max(0, rounds)))
    total_bytes = full_messages * msg_size_bytes + ihave_messages * ANNOUNCE_MSG_SIZE_BYTES
    if CTRL_BW_MBPS and CTRL_BW_MBPS > 0:
        Bps = CTRL_BW_MBPS * 1e6 / 8.0
        send_time = total_bytes / max(Bps, 1e-9)
    else:
        send_time = 0.0
    total_messages = full_messages + ihave_messages
    total_broadcast_cpu_seconds += total_messages * recv_processing_s()
    return latency_total + send_time


# ============================================================
# Kademlia routing cost (winning-shard -> leader-shard communication)
# ============================================================
def kademlia_hops(num_participants: int, bucket_bits: int = 1) -> int:
    """
    Expected number of routing hops for a Kademlia lookup among
    `num_participants` DHT participants (shards, in our case). Each hop
    narrows the XOR-distance search space by a factor of `2**bucket_bits`;
    bucket_bits=1 matches the classical Kademlia paper's O(log2 N) hop
    bound. This is a closed-form approximation, not a simulated routing
    table walk — real Kademlia's per-lookup hop count concentrates tightly
    around this value, so the extra fidelity of modeling k-buckets/XOR
    distance directly isn't worth the added complexity here.
    """
    N = max(1, int(num_participants))
    if N <= 1:
        return 0
    branch = max(2, 2 ** max(1, int(bucket_bits)))
    return max(1, math.ceil(math.log(N, branch)))


def kademlia_route_delay(hops: int, msg_size_bytes: int) -> float:
    """
    Wall-clock delay for routing one message across `hops` sequential
    Kademlia hops: one one-way latency hop paid per hop (hops are
    sequential, can't overlap), plus bandwidth/CPU cost accumulated across
    all hops — same accounting model as gossip_broadcast_delay.
    """
    if hops <= 0:
        return 0.0
    latency_total = sum(sample_one_way_latency_s() for _ in range(hops))
    if CTRL_BW_MBPS and CTRL_BW_MBPS > 0:
        Bps = CTRL_BW_MBPS * 1e6 / 8.0
        send_time = (hops * msg_size_bytes) / max(Bps, 1e-9)
    else:
        send_time = 0.0
    cpu_time = hops * recv_processing_s()
    return latency_total + send_time + cpu_time


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
    dt = control_phase_delay(msgs, msg_size, charge_recv_cpu=False)
    return dt, msgs


def to_leader_phase(S: int, msg_size: int):
    if S <= 1:
        return 0.0, 0
    msgs = S - 1
    dt = control_phase_delay(msgs, msg_size)
    return dt, msgs


def broadcast_phase(protocol: str, source_node: int, N: int,
                     adjacency: List[List[int]], msg_size: int,
                     plumtree_state: Optional[Dict[int, Dict[int, str]]] = None
                     ) -> Tuple[float, int, float, int, Dict[int, int]]:
    """
    Dispatches the leader's "I won" announcement (or the final block
    announcement) to the selected broadcast protocol, returning a common
    (dt, messages, coverage, rounds, first_seen) shape regardless of which
    one runs — callers don't need protocol-specific branching. Callers
    that don't need the delay (e.g. the final propagation stats pass,
    which doesn't cost round time) can simply discard `dt`.

    `first_seen` maps node id -> the round/hop it first received the
    message — the raw per-node data a CDF of delivery-by-hop is built
    from. For "flood" there's no round-by-round structure to draw this
    from (it's a flat one-shot broadcast), so it's synthesized as
    "everyone but the source arrives at hop 1", matching flood's own
    idealized single-hop assumption.

    `plumtree_state` must be the SAME dict passed across every call for
    the life of a run when protocol=="plumtree" — it's Plumtree's
    per-node eager/lazy link memory, and it only self-organizes if it
    persists across broadcasts rather than being rebuilt each time.
    """
    if protocol == "gossip":
        rounds, messages, coverage, first_seen = simulate_gossip(
            source_node, adjacency, GOSSIP_FANOUT)
        dt = gossip_broadcast_delay(rounds, messages, msg_size)
        return dt, messages, coverage, rounds, first_seen
    elif protocol == "plumtree":
        if plumtree_state is None:
            plumtree_state = {}
        rounds, eager, lazy, _prunes, _grafts, coverage, first_seen = simulate_adaptive_plumtree(
            source_node, adjacency, plumtree_state, PLUMTREE_LAZY_FANOUT)
        dt = plumtree_broadcast_delay(rounds, eager, lazy, msg_size)
        return dt, eager + lazy, coverage, rounds, first_seen
    elif protocol == "gossipsub":
        rounds, full, ihave, coverage, first_seen = simulate_gossipsub(
            source_node, adjacency, GOSSIPSUB_MESH_DEGREE, GOSSIPSUB_IHAVE_FANOUT)
        dt = gossipsub_broadcast_delay(rounds, full, ihave, msg_size)
        return dt, full + ihave, coverage, rounds, first_seen
    else:  # "flood": the original flat O(N) one-shot broadcast baseline
        dt, messages = leader_announce_phase(N, msg_size)
        first_seen = {i: (0 if i == source_node else 1) for i in range(N)}
        return dt, messages, 1.0, 0, first_seen


def to_leader_kademlia_phase(num_senders: int, dht_size: int, msg_size: int,
                              bucket_bits: int = 1) -> Tuple[float, int]:
    """
    Route `num_senders` messages to the leader shard via Kademlia lookup
    instead of one direct hop each. Hop count is derived from `dht_size`
    (the fixed total shard count — the DHT's actual membership), not the
    transient number of senders: routing-table depth doesn't shrink just
    because fewer shards happened to win this particular round. Different
    senders' lookups run concurrently, so the round's critical-path latency
    is a single lookup's hop chain, while bandwidth/CPU cost scales with the
    total hops summed across all senders.
    """
    W = max(0, int(num_senders))
    if W <= 0:
        return 0.0, 0
    hops = kademlia_hops(dht_size, bucket_bits)
    total_messages = W * hops
    latency_total = sum(sample_one_way_latency_s() for _ in range(hops))
    if CTRL_BW_MBPS and CTRL_BW_MBPS > 0:
        Bps = CTRL_BW_MBPS * 1e6 / 8.0
        send_time = (total_messages * msg_size) / max(Bps, 1e-9)
    else:
        send_time = 0.0
    cpu_time = total_messages * recv_processing_s()
    dt = latency_total + send_time + cpu_time
    return dt, total_messages


def coord_leader_metronome(env, nodes, miners, target_bt, diff0, blocks_limit,
                           total_blocksize, print_int, dbg, wallets, tx_per_wallet,
                           init_reward, halving_interval, shards, tx_cost_ms,
                           msg_size, control_bw_mbps,
                           overlap_broadcast, msg_proc_ms, msg_cost,
                           verify_mode="leader", quiet_blocks=False,
                           broadcast_protocol="gossip", shard_comm_protocol="kademlia"):
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

    N = len(nodes)
    adjacency = build_gossip_adjacency(nodes)
    # Plumtree's per-node eager/lazy link memory — must persist across
    # every block in this run for the tree to self-organize; it is NOT
    # rebuilt per block.
    plumtree_state: Dict[int, Dict[int, str]] = {}

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

        # Leader announces "I won"; other winning shards report to the
        # leader. Protocol for each is selectable so gossip/Plumtree/
        # GossipSub/Kademlia can be compared against the original flat
        # flood/direct baseline.
        leader_node = random.randrange(N) if N > 0 else 0
        dt_ann, msgs_ann, ann_coverage, ann_rounds, _ann_first_seen = broadcast_phase(
            broadcast_protocol, leader_node, N, adjacency, msg_size, plumtree_state)

        if shard_comm_protocol == "kademlia":
            dt_to_leader, msgs_to_leader = to_leader_kademlia_phase(
                max(0, actual_winners - 1), S, msg_size, KADEMLIA_BUCKET_BITS)
        else:
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
            if shard_comm_protocol == "kademlia":
                dt_attest, msgs_attest = to_leader_kademlia_phase(
                    actual_winners, S, msg_size, KADEMLIA_BUCKET_BITS)
            else:
                dt_attest = control_phase_delay(actual_winners, msg_size)
                msgs_attest = actual_winners
            dt_control += dt_attest
            total_control_msgs += msgs_attest
            total_msg_cost += msgs_attest * float(msg_cost or 0.0)
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

        # Leader announces the finished block to the network. Propagation
        # is tracked as network-load stats but doesn't block the mining
        # loop, whichever protocol is selected (matches the original
        # flood's zero-added-delay behavior) — the returned `dt` is
        # deliberately discarded here.
        if broadcast_protocol == "flood":
            env.process(random.choice(nodes).receive(b))
            prop_rounds, prop_coverage = 0, 1.0
            prop_msgs = max(0, N - 1)
            prop_first_seen = {i: (0 if i == 0 else 1) for i in range(N)}
        else:
            prop_source = random.randrange(N) if N > 0 else 0
            _, prop_msgs, prop_coverage, prop_rounds, prop_first_seen = broadcast_phase(
                broadcast_protocol, prop_source, N, adjacency, msg_size, plumtree_state)
            io_requests += prop_msgs
            network_data += prop_msgs * b.size
        hop_cdf_samples.extend(prop_first_seen.values())
        per_block_log.append((bc, prop_rounds, prop_msgs, prop_coverage))

        reward = _apply_halving_and_mint(bc, reward, halving_interval)

        if (not quiet_blocks) and (dbg or (print_int and bc % print_int == 0)):
            net_note = (f"broadcast={broadcast_protocol},shard_comm={shard_comm_protocol} "
                        f"(ann_cov={ann_coverage:.2f},"
                        f"final_cov={prop_coverage:.2f},final_rounds={prop_rounds})")
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
    global total_broadcast_cpu_seconds
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
        "broadcast_cpu_seconds": float(total_broadcast_cpu_seconds),
        "extra_fields": str(extra_fields),
    }

    print(
        f"[******] End B:{bc}/{blocks_limit or bc} "
        f"abt_global:{abt_global:.2f}s tps:{tps_total} "
        f"Diff:{human(diff)} ΣH:{human(total_hash)} "
        f"Tx:{total_tx} C:{total_coins} Pool:{pool_len} "
        f"NMB:{network_data/1e6:.2f} IO:{io_requests} "
        f"Msgs:{total_msgs} MsgCost_tot:{total_msg_cost:.2f} "
        f"(per_tx:{msg_cost_per_tx:.6f}) "
        f"BroadcastCPU_tot:{total_broadcast_cpu_seconds:.2f}s {extra_fields}"
    )


def write_hop_cdf(results_dir: str, out_name: str = "hop_cdf.csv") -> Optional[Dict[str, float]]:
    """
    Computes and writes the empirical CDF of `hop_cdf_samples` — every
    node's hop-count-to-first-delivery, pooled across every block's final
    propagation broadcast this run — and prints the key percentiles.

    Returns the percentile summary (hop_p50/hop_p90/hop_p99/hop_max) so
    callers can fold it into a per-run results row, or None if there were
    no samples (e.g. not using the leader-metronome coordinator).
    """
    if not hop_cdf_samples:
        return None
    samples = sorted(hop_cdf_samples)
    n = len(samples)

    rows = []
    i = 0
    while i < n:
        h = samples[i]
        j = i
        while j < n and samples[j] == h:
            j += 1
        rows.append((h, j / n))
        i = j

    path = Path(results_dir) / out_name
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["hop_count", "cumulative_fraction"])
        for h, frac in rows:
            writer.writerow([h, f"{frac:.6f}"])

    def percentile(p: float) -> int:
        idx = min(n - 1, max(0, int(p * n)))
        return samples[idx]

    stats = {
        "hop_p50": percentile(0.50),
        "hop_p90": percentile(0.90),
        "hop_p99": percentile(0.99),
        "hop_max": samples[-1],
    }

    print(f"\n===== Hop-count CDF ({n} samples) =====")
    print(f"  p50={stats['hop_p50']}  p90={stats['hop_p90']}  "
          f"p99={stats['hop_p99']}  max={stats['hop_max']}")
    print(f"  Wrote -> {path}")

    return stats


def write_per_block_log(results_dir: str, out_name: str) -> None:
    """
    Writes `per_block_log` (one row per block: rounds/messages/coverage of
    that block's final-propagation broadcast) so the block-by-block
    evolution can be plotted — e.g. Plumtree's tree converging over the run.
    No-op if per_block_log is empty (not populated outside the
    leader-metronome coordinator).
    """
    if not per_block_log:
        return
    path = Path(results_dir) / out_name
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["block", "rounds", "messages", "coverage"])
        for bc, rounds, msgs, coverage in per_block_log:
            writer.writerow([bc, rounds, msgs, f"{coverage:.6f}"])
    print(f"  Per-block log -> {path}")


# ============================================================
# Graph generation
# ============================================================
def build_random_k_out_graph(nodes: List[Node], k: int):
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
    p.add_argument("--gossip_fanout", type=int, default=None)
    p.add_argument("--kademlia_bucket_bits", type=int, default=1)
    p.add_argument("--plumtree_lazy_fanout", type=int, default=2)
    p.add_argument("--gossipsub_mesh_degree", type=int, default=8)
    p.add_argument("--gossipsub_ihave_fanout", type=int, default=4)
    p.add_argument("--hop_ttl", type=int, default=64)
    p.add_argument("--overlap_broadcast", action="store_true")
    p.add_argument("--msg_proc_ms", type=float, default=1.0)
    p.add_argument("--print", dest="print_int", type=int, default=144)
    p.add_argument("--debug", action="store_true")
    p.add_argument("--config", type=str)
    p.add_argument("--prefill", action="store_true")
    p.add_argument("--no_metronome", action="store_true")
    p.add_argument("--leader_metronome", action="store_true")
    p.add_argument("--broadcast_protocol", type=str, default="gossip",
                   choices=["gossip", "flood", "plumtree", "gossipsub"])
    p.add_argument("--shard_comm_protocol", type=str, default="kademlia",
                   choices=["kademlia", "direct"])
    p.add_argument("--verify_mode", type=str, default="leader",
                   choices=["leader", "shard", "leader_par"])
    p.add_argument("--pool_mode", type=str, default="count",
                   choices=["count", "deque"])
    p.add_argument("--seed", type=int, default=None,
                   help="Single source of truth for this run's randomness. "
                        "Seeds the global random module before any random draw "
                        "(graph generation, mining, gossip, etc). Unset = "
                        "OS-entropy seeded (non-reproducible).")
    p.add_argument("--quiet_blocks", action="store_true")
    p.add_argument("--results_csv", type=str, default="")
    p.add_argument("--results_dir", type=str, default="Results")
    p.add_argument("--hop_cdf_csv", type=str, default="hop_cdf.csv",
                   help="Filename (within --results_dir) for the per-run hop-count CDF. "
                        "Give parallel runs distinct names to avoid clobbering each other.")
    p.add_argument("--per_block_csv", type=str, default="",
                   help="Filename (within --results_dir) for a per-block rounds/messages/"
                        "coverage log of the final-propagation broadcast. Empty = disabled.")

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

    # Single source of truth for this run's randomness — must run before
    # build_random_k_out_graph or any other random draw. random.seed(None)
    # falls back to OS entropy, matching prior unseeded behavior.
    random.seed(args.seed)

    # Default gossip fanout to half the neighbor count, unless explicitly
    # set via CLI or config — a small fanout relative to neighbors is what
    # makes gossip's message complexity lower than flood's; this default
    # keeps that property out of the box rather than reusing a fixed
    # constant that could exceed a small --neighbors value.
    if args.gossip_fanout is None:
        args.gossip_fanout = max(1, (args.neighbors or 0) // 2)

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
    global LINK_RTT_MS, LINK_JITTER_MS, LINK_MSG_PROC_MS, CTRL_BW_MBPS, GOSSIP_FANOUT, KADEMLIA_BUCKET_BITS
    global PLUMTREE_LAZY_FANOUT, GOSSIPSUB_MESH_DEGREE, GOSSIPSUB_IHAVE_FANOUT, HOP_TTL
    LINK_RTT_MS      = float(args.rtt_ms or 0.0)
    LINK_JITTER_MS   = float(args.jitter_ms or 0.0)
    LINK_MSG_PROC_MS = float(args.msg_proc_ms or 0.0)
    CTRL_BW_MBPS     = float(args.control_bw_mbps or 0.0)
    GOSSIP_FANOUT    = int(args.gossip_fanout)
    KADEMLIA_BUCKET_BITS = int(args.kademlia_bucket_bits or 1)
    PLUMTREE_LAZY_FANOUT = int(args.plumtree_lazy_fanout or 2)
    GOSSIPSUB_MESH_DEGREE = int(args.gossipsub_mesh_degree or 8)
    GOSSIPSUB_IHAVE_FANOUT = int(args.gossipsub_ihave_fanout or 4)
    HOP_TTL          = int(args.hop_ttl or 64)

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
    build_random_k_out_graph(nodes, int(args.neighbors or 0))

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
                broadcast_protocol=args.broadcast_protocol,
                shard_comm_protocol=args.shard_comm_protocol,
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

    hop_stats = write_hop_cdf(str(args.results_dir or "Results"), str(args.hop_cdf_csv or "hop_cdf.csv"))
    if args.per_block_csv:
        write_per_block_log(str(args.results_dir or "Results"), str(args.per_block_csv))

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

    # Machine-readable line for Parallel_processes/near_parallel.py, which
    # scrapes stdout logs for this instead of reading a CSV directly.
    result_json = {
        "currency":            currency,
        "nodes":               int(args.nodes or 0),
        "wallets":             int(args.wallets or 0),
        "miners":              int(args.miners or 0),
        "transactions":        int(args.transactions or 0),
        "interval":            float(args.interval or 0.0),
        "shards":              int(shards),
        "average_block_time":  float(avg_bt),
        "block_size":          float(block_size_tx),
        "messages":            float(msgs),
        "mode":                mode,
        "tps":                 float(tps),
        "throughput_shard":    float(throughput_shard),
        "num_blocks":          int(blocks),
        "blocktime_cfg":       float(args.blocktime),
        "expected_blocktime":  float(args.blocktime),
        "broadcast_protocol":  str(args.broadcast_protocol or "") if args.leader_metronome else "n/a",
        "shard_comm_protocol": str(args.shard_comm_protocol or "") if args.leader_metronome else "n/a",
    }
    print(f"RESULT_JSON {json.dumps(result_json)}")

    PAPER_CSV_HEADER = [
        "currency", "nodes", "wallets", "miners", "transactions", "interval",
        "shards", "average block time", "block size", "messages", "mode", "tps",
        "no. of blocks generated", "blocktime in configuration file", "sig_scheme",
        "broadcast_protocol", "shard_comm_protocol", "seed",
        "broadcast_cpu_seconds", "hop_p50", "hop_p90", "hop_p99", "hop_max",
    ]

    def upsert_paper_csv_row(results_path: str, row: dict):
        path = Path(results_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        key_fields = ["currency", "shards", "block size", "mode",
                      "blocktime in configuration file", "sig_scheme",
                      "broadcast_protocol", "shard_comm_protocol", "seed"]

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

    # broadcast_protocol/shard_comm_protocol only apply to coord_leader_metronome;
    # coord()/coord_no_metronome() never read these flags, so recording them
    # there would misrepresent what the run actually did.
    protocol_relevant = bool(args.leader_metronome)

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
        "broadcast_protocol":                str(args.broadcast_protocol or "") if protocol_relevant else "n/a",
        "shard_comm_protocol":               str(args.shard_comm_protocol or "") if protocol_relevant else "n/a",
        "seed":                              int(args.seed) if args.seed is not None else "",
        "broadcast_cpu_seconds":             float(sim_summary.get("broadcast_cpu_seconds", 0.0)) if sim_summary else 0.0,
        "hop_p50":                           hop_stats["hop_p50"] if hop_stats else "",
        "hop_p90":                           hop_stats["hop_p90"] if hop_stats else "",
        "hop_p99":                           hop_stats["hop_p99"] if hop_stats else "",
        "hop_max":                           hop_stats["hop_max"] if hop_stats else "",
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