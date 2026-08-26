#!/usr/bin/env python3
"""
export_broadcast_animation.py

Runs each broadcast protocol (gossip, plumtree, gossipsub, flood) once
against a single small, fixed overlay topology, capturing every
individual send as an event (not just aggregate counts), and writes one
JSON file per protocol to Results/broadcast_animation/<protocol>.json.

This is purely an illustrative/explanatory export — unlike
Parallel_processes/network_parallel.py's large sweep, it always uses a
small node count so the resulting animation (rendered by web/index.html)
stays legible. All protocols share the identical topology (same seed)
so the web UI can flip between them without the node layout changing.

    python export_broadcast_animation.py
    python export_broadcast_animation.py --protocol plumtree --blocks 5
"""

import argparse
import json
import os
import random
from typing import Dict, List

from graph import safe_mkdir
from simulation import (
    Node, build_random_k_out_graph, build_gossip_adjacency,
    simulate_gossip, simulate_adaptive_plumtree, simulate_gossipsub,
)

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Results", "broadcast_animation")


def build_topology(nodes: int, neighbors: int, seed: int):
    random.seed(seed)
    node_objs = [Node(None, i) for i in range(nodes)]
    build_random_k_out_graph(node_objs, neighbors, seed=seed)
    adjacency = build_gossip_adjacency(node_objs)
    node_list = [{"id": i} for i in range(nodes)]
    edges = [{"source": u, "target": v} for u, row in enumerate(adjacency) for v in row]
    return adjacency, node_list, edges


def export_gossip(adjacency, node_list, edges, args) -> Dict:
    random.seed(args.seed)
    events: List[dict] = []
    rounds, messages, coverage, first_seen = simulate_gossip(
        args.source, adjacency, args.gossip_fanout, ttl=args.hop_ttl, events=events)
    block = {
        "block": 0,
        "rounds_used": rounds,
        "message_totals": {"messages": messages},
        "first_seen": first_seen,
        "events": events,
    }
    return {
        "protocol": "gossip",
        "params": {"nodes": args.nodes, "neighbors": args.neighbors, "seed": args.seed,
                    "gossip_fanout": args.gossip_fanout, "hop_ttl": args.hop_ttl},
        "nodes": node_list, "edges": edges, "source_node": args.source,
        "blocks": [block],
    }


def export_gossipsub(adjacency, node_list, edges, args) -> Dict:
    random.seed(args.seed)
    events: List[dict] = []
    rounds, full, ihave, coverage, first_seen = simulate_gossipsub(
        args.source, adjacency, args.gossipsub_mesh_degree, args.gossipsub_ihave_fanout,
        ttl=args.hop_ttl, events=events)
    block = {
        "block": 0,
        "rounds_used": rounds,
        "message_totals": {"mesh_push": full, "ihave": ihave},
        "first_seen": first_seen,
        "events": events,
    }
    return {
        "protocol": "gossipsub",
        "params": {"nodes": args.nodes, "neighbors": args.neighbors, "seed": args.seed,
                    "mesh_degree": args.gossipsub_mesh_degree,
                    "ihave_fanout": args.gossipsub_ihave_fanout, "hop_ttl": args.hop_ttl},
        "nodes": node_list, "edges": edges, "source_node": args.source,
        "blocks": [block],
    }


def export_plumtree(adjacency, node_list, edges, args) -> Dict:
    # Mirrors coord_leader_metronome: link_state is one dict threaded across
    # every block, and the source is a NEW RANDOM node each block (a
    # different shard/miner wins each time), not the same fixed source
    # replayed over and over. This distinction matters a lot: with a FIXED
    # source, the eager tree converges to one static shortest-path tree
    # from that source and stays just as shallow as gossip/gossipsub. Only
    # with a rotating source do competing prune/graft decisions from
    # different vantage points keep the tree from ever settling into a
    # trivially short structure -- which is what actually produces
    # Plumtree's real fewer-messages-but-more-hops tradeoff.
    random.seed(args.seed)
    link_state: Dict[int, Dict[int, str]] = {}
    blocks = []
    for b in range(args.blocks):
        source = random.randrange(args.nodes)
        events: List[dict] = []
        rounds, eager, lazy, prunes, grafts, coverage, first_seen, _per_round_max_bytes = simulate_adaptive_plumtree(
            source, adjacency, link_state, args.plumtree_lazy_fanout,
            ttl=args.hop_ttl, events=events)
        blocks.append({
            "block": b,
            "source": source,
            "rounds_used": rounds,
            "message_totals": {"eager": eager, "lazy": lazy, "prunes": prunes, "grafts": grafts},
            "first_seen": first_seen,
            "events": events,
        })

    final_link_state = [
        {"node": node, "peer": peer, "state": link_state.get(node, {}).get(peer, "eager")}
        for node, row in enumerate(adjacency) for peer in row
    ]

    return {
        "protocol": "plumtree",
        "params": {"nodes": args.nodes, "neighbors": args.neighbors, "seed": args.seed,
                    "lazy_fanout": args.plumtree_lazy_fanout, "hop_ttl": args.hop_ttl,
                    "blocks": args.blocks},
        "nodes": node_list, "edges": edges,
        "blocks": blocks,
        "final_link_state": final_link_state,
    }


def export_flood(adjacency, node_list, edges, args) -> Dict:
    first_seen = {i: (0 if i == args.source else 1) for i in range(args.nodes)}
    block = {
        "block": 0,
        "rounds_used": 1,
        "message_totals": {"messages": args.nodes - 1},
        "first_seen": first_seen,
        "events": [],
    }
    return {
        "protocol": "flood",
        "params": {"nodes": args.nodes, "neighbors": args.neighbors, "seed": args.seed},
        "nodes": node_list, "edges": edges, "source_node": args.source,
        "blocks": [block],
    }


EXPORTERS = {
    "gossip": export_gossip,
    "plumtree": export_plumtree,
    "gossipsub": export_gossipsub,
    "flood": export_flood,
}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--protocol", choices=list(EXPORTERS) + ["all"], default="all")
    p.add_argument("--nodes", type=int, default=20)
    p.add_argument("--neighbors", type=int, default=8)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--source", type=int, default=0, help="gossip/gossipsub/flood only -- plumtree picks a new random source every block")
    p.add_argument("--gossip_fanout", type=int, default=5)
    p.add_argument("--plumtree_lazy_fanout", type=int, default=2)
    p.add_argument("--gossipsub_mesh_degree", type=int, default=4)
    p.add_argument("--gossipsub_ihave_fanout", type=int, default=3)
    p.add_argument("--hop_ttl", type=int, default=64)
    p.add_argument("--blocks", type=int, default=12, help="plumtree only: broadcasts over which link_state persists")
    p.add_argument("--out_dir", type=str, default=OUT_DIR)
    args = p.parse_args()

    if args.source >= args.nodes:
        p.error(f"--source ({args.source}) must be < --nodes ({args.nodes})")

    safe_mkdir(args.out_dir)
    adjacency, node_list, edges = build_topology(args.nodes, args.neighbors, args.seed)

    protocols = list(EXPORTERS) if args.protocol == "all" else [args.protocol]
    for protocol in protocols:
        payload = EXPORTERS[protocol](adjacency, node_list, edges, args)
        out_path = os.path.join(args.out_dir, f"{protocol}.json")
        with open(out_path, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
