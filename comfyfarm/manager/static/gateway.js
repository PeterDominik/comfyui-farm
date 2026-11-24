// The farm's gateway on the Machines tab (decisions 0055, 0056): an agent with lanes for
// outside providers instead of a GPU and a ComfyUI. What to show for it, from a worker as
// GET /api/v1/workers gives it (its `lanes`); index.html draws it. No page is touched here.

// The gateway, or a GPU machine.
function isGateway(w) {
  return !!Object.keys((w && w.lanes) || {}).length;
}

// Its lanes, in its GPU cell: "gateway · fal ×2".
function laneText(w) {
  const lanes = Object.entries((w && w.lanes) || {}).map(([p, n]) => `${p} ×${n}`);
  return lanes.length ? `gateway · ${lanes.join(", ")}` : "";
}

// What its GPU cell says when pointed at.
function laneTip(w) {
  const lanes = Object.entries((w && w.lanes) || {});
  if (!lanes.length) return "";
  const each = lanes.map(([p, n]) => `${p}: ${n} job${n > 1 ? "s" : ""} at once`).join(", ");
  return `The farm's gateway: it sends API jobs to outside providers (${each}, gateway.yaml). It has no GPU ` +
         `and no ComfyUI, and it is left out of rollouts and ComfyUI syncs.`;
}
