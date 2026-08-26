# Broadcast Protocol Animation

An interactive, round-by-round visualization of how Gossip, Plumtree, and
GossipSub actually propagate a message across the network, built to make the
protocol-comparison results (`Results/network_results_*.csv`,
`Results/network_runs/hopcdf/`) easier to reason about than aggregate numbers
alone.

## Purpose

To build intuition for why our network-protocol comparison shows Plumtree
using fewer messages but more hops than Gossip and GossipSub, this tool
animates each protocol's actual message propagation round-by-round over a
shared network topology. Rather than only looking at the aggregate
throughput/message-count numbers in our CSV outputs, it lets us watch *how*
each protocol behaves mechanically — which nodes get informed when, and
which links carry the traffic.

## Implementation

The visualization is not a separate reimplementation of the protocols — it
directly instruments the same `simulate_gossip`, `simulate_adaptive_plumtree`,
and `simulate_gossipsub` functions already used in the simulation and
parameter sweep (`simulation.py`), adding an opt-in event log (sender,
receiver, round, message kind) that costs nothing on the production runs
since it's only populated when explicitly requested. `export_broadcast_animation.py`
runs each protocol against a fixed, legible topology (20 nodes, 8 neighbors)
and writes the resulting per-round events to `Results/broadcast_animation/*.json`,
which the browser-based D3.js viewer in `web/` renders as a force-directed
graph: nodes light up as they're informed, edges are color-coded by what kind
of message moved along them (Plumtree's eager push vs. lazy announcement vs.
prune vs. graft), and playback controls let you step through rounds or whole
simulated blocks.

## Key finding this reveals

The reason Plumtree trades messages for hops only shows up correctly once
the animation reflects how the real simulation runs blocks: the winning
shard/leader is a **new random node every block**, not a fixed one, and
Plumtree's eager/lazy tree state persists across every block rather than
being rebuilt from scratch. With a fixed source, the tree just converges
once to a shallow shortest-path tree and looks no worse than Gossip. With a
rotating source — which is what actually happens in the simulation —
competing prune and graft decisions from different broadcast origins keep
reshaping the same shared tree, so it never settles into a trivially short
structure for any single source. That's mechanically why Plumtree ends up
with a sparser, more bandwidth-efficient tree (fewer total messages) that
nonetheless requires more sequential hops to reach full coverage than
Gossip's or GossipSub's fixed, stateless random fan-out. This matches the
large-scale hop-count CDFs already collected (Plumtree's max hop count
around 14 vs. roughly 5-6 for Gossip/GossipSub), and the animation makes the
underlying cause — the persistent, evolving tree — directly visible rather
than something that has to be inferred from summary statistics alone.

## Running it

```bash
python export_broadcast_animation.py     # regenerate Results/broadcast_animation/*.json
python -m http.server 8000               # from the repo root
# open http://localhost:8000/web/index.html
```

Pick a protocol from the dropdown, then Step or Play through rounds. For
Plumtree, use the Block selector to jump between successive broadcasts and
watch the persistent tree overlay (thick green = currently eager/tree edge,
thin dashed grey = currently lazy) sparsen and stabilize over the first few
blocks.

## How Plumtree actually works (in plain English)

Picture a rumor spreading through a school.

**Flood** is the loud, brute-force way: the second you hear something, you
run and tell literally every friend you have, and so does everyone else.
Everybody hears it almost instantly, but you're all repeating the same news
to people who've already heard it three times over. Fast, but wasteful.

**Gossip** is a bit smarter: each person who hears the rumor tells a handful
of random friends (not everyone), who each tell a handful more. It still
spreads fast because there are lots of overlapping paths racing each other,
but plenty of people still end up hearing it twice from two different
friends — that's redundant, wasted retelling.

**GossipSub** is the same idea but with a rule: you always fully tell a
small, fixed set of close friends (your "mesh"), and for everyone else you
just casually mention "hey, I heard something" without the details, unless
they specifically ask. It keeps the redundant retelling under control by
capping how many people get the *full* story from you each time.

**Plumtree** starts out looking exactly like Flood — the very first time a
rumor goes around, everyone tells everyone, because nobody yet knows who
already heard it from someone else. But here's the trick: every time you
proudly tell a friend something and they say "yeah, I already know" — you
learn your lesson. You stop telling *that* friend directly next time (this
is a **prune**); instead you just mention that you heard *something* without
repeating the whole story (a **lazy** announcement). If that friend ever
says "wait, I only heard a rumor, nobody actually told me the real story" —
you tell them properly again, and that connection gets reinstated as a real
telling-relationship (a **graft**).

Do this over and over, rumor after rumor (block after block), and the
friend group quietly reorganizes itself into something like an efficient
phone tree: most people only ever tell one or two specific friends directly,
instead of blasting it to everyone. That's why the total number of
"tellings" (messages) drops a lot compared to Flood or Gossip — barely
anyone is wasting effort re-telling someone who already knows.

But here's the catch, and it's the whole point: a thin phone tree means a
rumor that starts on one side of the school has to pass through a longer
chain of people — hop after hop after hop — to reach someone all the way on
the other side. Gossip and GossipSub never build any memory of who already
knows what, so every single time, they just re-blast to a generous handful
of random friends, which happens to reach everyone in only a few hops,
every time, no matter who started it. Plumtree's tree is much leaner
(fewer total messages), but leaner means skinnier branches, and skinnier
branches mean the news has to travel farther, person to person, to get all
the way across (more hops).

And because a *different* kid starts the rumor every single day (a new
shard/leader wins each block, not always the same one), the phone tree that
quietly optimized itself for yesterday's rumor-starter isn't perfectly
suited to today's — so the tree that emerges is a compromise that works
"good enough" for everyone, never perfectly short for any one person. That
compromise is exactly why Plumtree's hop count in the animation (and in the
real hop-count data) settles higher than Gossip's or GossipSub's, even
though it's clearly sending far fewer messages overall.
