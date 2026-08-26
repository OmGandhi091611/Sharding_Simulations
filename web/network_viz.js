// Reads Results/broadcast_animation/<protocol>.json (written by
// export_broadcast_animation.py) and renders a round-by-round animation of
// how each broadcast protocol propagates a message across a fixed overlay
// graph. Layout is computed once per protocol load and frozen before
// playback starts -- the force simulation never runs during animation, only
// the round/block cursor moves.

const PROTOCOL_COLORS = {
  gossip: "#1f77b4",
  flood: "#d62728",
  plumtree: "#2ca02c",
  gossipsub: "#ff7f0e",
};

// Edge visual style per (derived) event kind. "eager" events are split into
// eager_new / eager_pruned client-side based on their `outcome` field.
const EDGE_KIND_STYLE = {
  gossip_forward: { color: "#1f77b4", dash: null, label: "gossip forward" },
  eager_new: { color: "#2ca02c", dash: null, label: "eager push (new)" },
  eager_pruned: { color: "#d62728", dash: "5,3", label: "eager push (redundant -> pruned)" },
  lazy_ihave: { color: "#999999", dash: "2,3", label: "lazy IHAVE" },
  graft: { color: "#ff7f0e", dash: null, label: "graft" },
  mesh_push: { color: "#ff7f0e", dash: null, label: "mesh push" },
  ihave: { color: "#999999", dash: "2,3", label: "IHAVE" },
};

const PROTOCOL_KINDS = {
  gossip: ["gossip_forward"],
  plumtree: ["eager_new", "eager_pruned", "lazy_ihave", "graft"],
  gossipsub: ["mesh_push", "ihave"],
  flood: [],
};

const svg = d3.select("#viz");
const protocolSelect = document.getElementById("protocol-select");
const blockControl = document.getElementById("block-control");
const blockSelect = document.getElementById("block-select");
const stepBackBtn = document.getElementById("step-back");
const stepForwardBtn = document.getElementById("step-forward");
const playPauseBtn = document.getElementById("play-pause");
const speedInput = document.getElementById("speed");
const roundLabel = document.getElementById("round-label");
const messageLabel = document.getElementById("message-label");
const treeLabel = document.getElementById("tree-label");
const legendDiv = document.getElementById("legend");
const compareRowsDiv = document.getElementById("compare-rows");

const PERSISTENT_TREE_STYLE = {
  eager: { color: "#2ca02c", dash: null, width: 2.5, label: "current tree: eager (pushes full message)" },
  lazy: { color: "#999999", dash: "2,3", width: 1, label: "current tree: lazy (announce-only)" },
};

let data = null;
let precomputed = null;
let treeStateByBlockRound = null; // plumtree only: persistent eager/lazy classification, carried across blocks
let edgeSel = null;
let nodeSel = null;
let accentColor = "#333";
let currentBlock = 0;
let currentRound = 0;
let playing = false;
let playInterval = null;

function styleKeyFor(ev) {
  if (ev.kind === "eager") {
    return ev.outcome === "redundant_pruned" ? "eager_pruned" : "eager_new";
  }
  return ev.kind;
}

function edgeKey(from, to) {
  return `${from}->${to}`;
}

// Builds, for a single block: a cumulative edge-state map per round (latest
// event touching each directed edge, up to and including that round) and a
// cumulative message-sent count per round. Both arrays are indexed 0..maxRound
// so render() is a pure lookup, never a re-scan.
function precomputeBlock(block) {
  const maxRound = block.rounds_used;
  const eventsByRound = new Map();
  for (const ev of block.events) {
    if (!eventsByRound.has(ev.round)) eventsByRound.set(ev.round, []);
    eventsByRound.get(ev.round).push(ev);
  }

  const edgeStateByRound = [new Map()];
  const messageCountByRound = [0];
  let cumulativeEdges = new Map();
  let cumulativeMessages = 0;

  for (let r = 1; r <= maxRound; r++) {
    const roundEvents = eventsByRound.get(r) || [];
    for (const ev of roundEvents) cumulativeEdges.set(edgeKey(ev.from, ev.to), ev);
    cumulativeMessages += roundEvents.length;
    edgeStateByRound.push(new Map(cumulativeEdges));
    messageCountByRound.push(cumulativeMessages);
  }

  return { maxRound, edgeStateByRound, messageCountByRound };
}

// Plumtree only. Every directed edge defaults to "eager" (matching
// simulate_adaptive_plumtree's own default) until an event demotes/promotes
// it. This is derived entirely from the event stream already exported --
// no separate tree snapshot needs to come from Python. The map is carried
// FORWARD across block boundaries (not reset per block), because link_state
// itself persists across every broadcast in the real simulation: this is
// what lets you watch the same tree get refined block after block instead
// of looking like N independent one-shot runs.
function computeTreeStateByBlockRound(blocks) {
  const persistent = new Map();
  const result = [];
  for (const block of blocks) {
    const perRound = [new Map(persistent)];
    const eventsByRound = new Map();
    for (const ev of block.events) {
      if (!eventsByRound.has(ev.round)) eventsByRound.set(ev.round, []);
      eventsByRound.get(ev.round).push(ev);
    }
    for (let r = 1; r <= block.rounds_used; r++) {
      for (const ev of eventsByRound.get(r) || []) {
        if (ev.kind === "eager" && ev.outcome === "redundant_pruned") {
          persistent.set(edgeKey(ev.from, ev.to), "lazy");
        } else if (ev.kind === "graft") {
          persistent.set(edgeKey(ev.from, ev.to), "eager");
        }
      }
      perRound.push(new Map(persistent));
    }
    result.push(perRound);
  }
  return result;
}

function updateLegend(protocol) {
  legendDiv.innerHTML = "";
  if (protocol === "plumtree") {
    for (const style of Object.values(PERSISTENT_TREE_STYLE)) {
      const el = document.createElement("span");
      el.className = "legend-swatch";
      el.innerHTML = `<span class="legend-line" style="border-top-color:${style.color};border-top-style:${style.dash ? "dashed" : "solid"};border-top-width:${style.width}px"></span>${style.label}`;
      legendDiv.appendChild(el);
    }
  }
  for (const kind of PROTOCOL_KINDS[protocol] || []) {
    const style = EDGE_KIND_STYLE[kind];
    const el = document.createElement("span");
    el.className = "legend-swatch";
    el.innerHTML = `<span class="legend-line" style="border-top-color:${style.color};border-top-style:${style.dash ? "dashed" : "solid"}"></span>${style.label} (this round)`;
    legendDiv.appendChild(el);
  }
}

function render(blockIdx, round) {
  const pre = precomputed[blockIdx];
  round = Math.max(0, Math.min(round, pre.maxRound));
  currentBlock = blockIdx;
  currentRound = round;
  const block = data.blocks[blockIdx];
  const isPlumtree = data.protocol === "plumtree";
  const activeSource = isPlumtree ? block.source : data.source_node;

  nodeSel
    .attr("fill", (d) => {
      const fs = block.first_seen[d.id];
      return fs !== undefined && fs <= round ? accentColor : "#ccc";
    })
    .attr("r", (d) => (d.id === activeSource ? 10 : 7));

  const edgeState = pre.edgeStateByRound[round];

  if (isPlumtree) {
    const treeState = treeStateByBlockRound[blockIdx][round];
    let eagerCount = 0;
    let lazyCount = 0;
    edgeSel
      .attr("stroke", (d) => {
        const state = treeState.get(edgeKey(d.source.id, d.target.id)) || "eager";
        if (state === "eager") eagerCount++; else lazyCount++;
        return PERSISTENT_TREE_STYLE[state].color;
      })
      .attr("stroke-dasharray", (d) => {
        const state = treeState.get(edgeKey(d.source.id, d.target.id)) || "eager";
        return PERSISTENT_TREE_STYLE[state].dash;
      })
      .attr("stroke-width", (d) => {
        const state = treeState.get(edgeKey(d.source.id, d.target.id)) || "eager";
        return PERSISTENT_TREE_STYLE[state].width;
      })
      .classed("edge-active", (d) => {
        const ev = edgeState.get(edgeKey(d.source.id, d.target.id));
        return !!ev && ev.round === round;
      });
    treeLabel.style.display = "inline";
    treeLabel.textContent = `Tree: ${eagerCount} eager / ${lazyCount} lazy`;
  } else {
    edgeSel
      .attr("stroke", (d) => {
        const ev = edgeState.get(edgeKey(d.source.id, d.target.id));
        return ev ? EDGE_KIND_STYLE[styleKeyFor(ev)].color : "#ddd";
      })
      .attr("stroke-dasharray", (d) => {
        const ev = edgeState.get(edgeKey(d.source.id, d.target.id));
        return ev ? EDGE_KIND_STYLE[styleKeyFor(ev)].dash : null;
      })
      .attr("stroke-width", 1.5)
      .classed("edge-active", (d) => {
        const ev = edgeState.get(edgeKey(d.source.id, d.target.id));
        return !!ev && ev.round === round;
      });
    treeLabel.style.display = "none";
  }

  roundLabel.textContent = `Round ${round} / ${pre.maxRound}`;
  messageLabel.textContent = `Messages: ${pre.messageCountByRound[round]}`;
  blockSelect.value = String(blockIdx);
}

function stepForward() {
  const pre = precomputed[currentBlock];
  if (currentRound < pre.maxRound) {
    render(currentBlock, currentRound + 1);
  } else if (currentBlock < data.blocks.length - 1) {
    render(currentBlock + 1, 0);
  } else {
    pause();
  }
}

function stepBack() {
  if (currentRound > 0) {
    render(currentBlock, currentRound - 1);
  } else if (currentBlock > 0) {
    const prevMax = precomputed[currentBlock - 1].maxRound;
    render(currentBlock - 1, prevMax);
  }
}

function play() {
  if (playing) return;
  playing = true;
  playPauseBtn.textContent = "Pause";
  playInterval = setInterval(stepForward, Number(speedInput.value));
}

function pause() {
  playing = false;
  playPauseBtn.textContent = "Play";
  clearInterval(playInterval);
}

async function loadProtocol(protocol) {
  pause();
  const resp = await fetch(`../Results/broadcast_animation/${protocol}.json`);
  data = await resp.json();
  accentColor = PROTOCOL_COLORS[data.protocol] || "#333";

  svg.selectAll("*").remove();
  const width = svg.node().clientWidth || 800;
  const height = svg.node().clientHeight || 600;

  const sim = d3
    .forceSimulation(data.nodes)
    .force("link", d3.forceLink(data.edges).id((d) => d.id).distance(140))
    .force("charge", d3.forceManyBody().strength(-500))
    .force("center", d3.forceCenter(width / 2, height / 2))
    .force("collide", d3.forceCollide(30))
    .stop();
  for (let i = 0; i < 500; i++) sim.tick();

  // Stretch the settled layout's bounding box to fill the actual viewport
  // (minus a margin), regardless of how tightly the physics happened to
  // converge -- this is what guarantees the whole window gets used, not
  // just whatever spread the charge/link forces produced on their own.
  const margin = 50;
  const xs = data.nodes.map((n) => n.x);
  const ys = data.nodes.map((n) => n.y);
  const [minX, maxX] = [Math.min(...xs), Math.max(...xs)];
  const [minY, maxY] = [Math.min(...ys), Math.max(...ys)];
  const scaleX = (width - 2 * margin) / Math.max(1, maxX - minX);
  const scaleY = (height - 2 * margin) / Math.max(1, maxY - minY);
  data.nodes.forEach((n) => {
    n.x = margin + (n.x - minX) * scaleX;
    n.y = margin + (n.y - minY) * scaleY;
    n.fx = n.x;
    n.fy = n.y;
  });

  edgeSel = svg
    .append("g")
    .attr("class", "edges")
    .selectAll("line")
    .data(data.edges)
    .join("line")
    .attr("x1", (d) => d.source.x)
    .attr("y1", (d) => d.source.y)
    .attr("x2", (d) => d.target.x)
    .attr("y2", (d) => d.target.y)
    .attr("stroke", "#ddd")
    .attr("stroke-width", 1.5);

  nodeSel = svg
    .append("g")
    .attr("class", "nodes")
    .selectAll("circle")
    .data(data.nodes)
    .join("circle")
    .attr("r", 7)
    .attr("cx", (d) => d.x)
    .attr("cy", (d) => d.y)
    .attr("stroke", "#333")
    .attr("stroke-width", 1)
    .attr("fill", "#ccc");
  nodeSel.append("title").text((d) => `node ${d.id}`);

  precomputed = data.blocks.map(precomputeBlock);
  treeStateByBlockRound = data.protocol === "plumtree" ? computeTreeStateByBlockRound(data.blocks) : null;

  blockSelect.innerHTML = "";
  data.blocks.forEach((b, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = `${i + 1} / ${data.blocks.length}`;
    blockSelect.appendChild(opt);
  });
  blockControl.style.display = data.blocks.length > 1 ? "flex" : "none";

  updateLegend(data.protocol);
  render(0, 0);
}

stepBackBtn.addEventListener("click", () => {
  pause();
  stepBack();
});
stepForwardBtn.addEventListener("click", () => {
  pause();
  stepForward();
});
playPauseBtn.addEventListener("click", () => {
  if (playing) pause();
  else play();
});
speedInput.addEventListener("input", () => {
  if (playing) {
    clearInterval(playInterval);
    playInterval = setInterval(stepForward, Number(speedInput.value));
  }
});
protocolSelect.addEventListener("change", () => loadProtocol(protocolSelect.value));
blockSelect.addEventListener("change", () => {
  pause();
  render(Number(blockSelect.value), 0);
});

// Independent of whichever protocol is currently animating: fetches all
// four JSONs once and shows each protocol's stabilized (last-block) round
// count next to its message count, so the "plumtree = fewer messages, more
// hops" tradeoff is visible as a number, not just something you have to
// infer from stepping through the animation.
async function loadCompareStats() {
  const protocols = ["gossip", "plumtree", "gossipsub", "flood"];
  const rows = await Promise.all(
    protocols.map(async (protocol) => {
      const resp = await fetch(`../Results/broadcast_animation/${protocol}.json`);
      const d = await resp.json();
      const block = d.blocks[d.blocks.length - 1];
      const messages = Object.values(block.message_totals).reduce((a, b) => a + b, 0);
      return { protocol, rounds: block.rounds_used, messages };
    })
  );
  compareRowsDiv.innerHTML = rows
    .map(
      (r) =>
        `<span class="compare-row"><span class="compare-dot" style="background:${PROTOCOL_COLORS[r.protocol]}"></span>${r.protocol}: ${r.rounds} rounds, ${r.messages} msgs</span>`
    )
    .join("");
}

loadProtocol(protocolSelect.value);
loadCompareStats();
