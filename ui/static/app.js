const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="hcp-token"]').content;
let proposal = null, current = null, stopped = false, submitting = false, polling = false;

function list(id, values) {
  $(id).replaceChildren(...values.map(value => {
    const item = document.createElement("li");
    item.textContent = value;
    return item;
  }));
}
async function api(path, options = {}) {
  options.headers = {...options.headers, "X-HCP-UI-Token": token};
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || JSON.stringify(data));
    error.status = response.status;
    throw error;
  }
  return data;
}
function controls() {
  const running = current?.state === "running";
  const awaiting = current?.state === "awaiting_confirmation";
  $("run-button").disabled = submitting || running;
  $("approve").disabled = submitting || stopped || !awaiting || !$("confirm").checked;
  $("confirm").disabled = running || stopped;
  $("bom-edit").disabled = running;
  $("refresh").disabled = submitting || running || stopped;
}
function render(run, edit = false) {
  current = run;
  proposal = run.id;
  $("confirmation").hidden = false;
  if (edit) {
    $("bom-edit").value = JSON.stringify(run.bom, null, 2);
    $("confirm").checked = false;
  }
  if (run.detection || run.detection_error) {
    $("detection").textContent = JSON.stringify(run.detection || run.detection_error, null, 2);
  }
  $("state").textContent = run.state;
  const events = run.events || [];
  const last = events.at(-1);
  $("step").textContent = last ? (last.op + (last.type ? " · " + last.type : ""))
    : "Review the proposed BOM and camera rollup before approving.";
  const completed = run.cups_commanded || events.filter(e => e.op === "complete_type").map(e => e.type);
  const missing = run.missing || events.filter(e => e.op === "missing").map(e => e.type);
  const total = Object.keys(run.bom || {}).length;
  $("count").max = Math.max(1, total);
  $("count").value = completed.length;
  $("quantities").textContent = completed.length + " / " + total + " cup commands completed (NOT physically verified)"
    + (missing.length ? " · " + missing.length + " missing/skipped" : "");
  $("error").textContent = run.error || "";
  list("warnings", [...(run.warnings || []), ...missing.map(type => "Missing cup: " + type), ...(run.stop_errors || [])]);
  list("llm-suggestions", run.llm_schematic_suggestions?.length ? run.llm_schematic_suggestions : ["No suggestions available."]);
  list("requested-parts", Object.entries(run.bom || {}).map(([type, qty]) => type + " × " + qty + " parts needed → one cup"));
  $("component-details").hidden = !(run.component_details || []).length;
  list("source-components", (run.component_details || []).map(p =>
    p.refdes.join(", ") + " — " + p.type + " " + p.value + " × " + p.quantity));
  list("delivered", completed.map(type => type + " · cup cycle commanded, delivery unverified"));
  $("bom-result").textContent = JSON.stringify(run.bom, null, 2);
  $("plan-result").textContent = JSON.stringify(Object.keys(run.bom || {}).map(type => ({
    type, operations: ["detect", "grasp (coarse + visual align + grip + retrace)", "drive", "drop", "advance", "return"]
  })), null, 2);
  $("motion-events").textContent = events.length ? events.slice(-80).map(e => JSON.stringify(e)).join("\n") : "No motion dispatched";
  controls();
}
async function refreshNodes() {
  try {
    const status = await api("/nodes");
    $("nodes").replaceChildren(...Object.entries(status.nodes).map(([name, node]) => {
      const badge = document.createElement("span");
      badge.className = "node" + (node.connected ? "" : " offline");
      badge.textContent = name + " · " + (node.connected ? (node.simulated === true ? "SIMULATED" :
        node.simulated === false ? "PHYSICAL" : "connected") + (node.state ? " · " + node.state : "") : "offline");
      if (node.error) badge.title = node.error;
      return badge;
    }));
  } catch (error) { $("nodes").textContent = "Node health unavailable: " + error.message; }
}
async function poll() {
  if (polling) return;
  polling = true;
  try {
    const status = await api("/status");
    stopped = status.stopped;
    $("mode").textContent = status.architecture + (stopped ? " · STOPPED: inspect and restart" : " · human approval required");
    if (!proposal && status.active_run) proposal = status.active_run;
    if (proposal && (!current || current.state === "running")) {
      render(await api("/proposals/" + proposal));
    }
    controls();
  } catch (error) { $("error").textContent = error.message; }
  finally { polling = false; }
}
$("confirm").onchange = controls;
$("run-form").onsubmit = async event => {
  event.preventDefault();
  submitting = true;
  controls();
  $("error").textContent = "";
  try {
    const text = $("bom-json").value.trim();
    const file = $("pdf").files[0];
    if (file && text) throw new Error("Choose a schematic file OR a BOM, not both.");
    if (!file && !text) throw new Error("Choose a schematic or paste a BOM. There is no implicit demo run.");
    let options;
    if (text) {
      JSON.parse(text); // Syntax check only: preserve raw JSON so the backend can reject duplicate keys.
      options = {method: "POST", headers: {"Content-Type": "application/json"}, body: '{"bom":' + text + '}'};
    } else {
      const data = new FormData();
      data.append("schematic", file);
      options = {method: "POST", body: data};
    }
    $("message").textContent = file?.name.toLowerCase().endsWith(".sch") ? "Reading SCH component data…" : "Preparing proposal…";
    render(await api("/proposals", options), true);
    $("message").textContent = "Review and edit the BOM. No motion has been dispatched.";
  } catch (error) { $("error").textContent = error.message; }
  finally { submitting = false; controls(); }
};
$("refresh").onclick = async () => {
  try {
    const value = await api("/detect");
    $("detection").textContent = JSON.stringify(value, null, 2);
    if (current) { current.detection = value; current.detection_error = null; }
  } catch (error) { $("error").textContent = error.message; }
};
$("approve").onclick = async () => {
  if (submitting || stopped || current?.state !== "awaiting_confirmation" || !$("confirm").checked) return;
  submitting = true; controls();
  try {
    const text = $("bom-edit").value;
    JSON.parse(text);
    await api("/proposals/" + proposal + "/approve", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: '{"approved":true,"bom":' + text + '}'});
    render(await api("/proposals/" + proposal));
    $("message").textContent = "Approved once. The controller is coordinating RoboMaster and the conveyor.";
  } catch (error) {
    $("error").textContent = error.message;
    // Never automatically retry approval. Re-read state after an uncertain response.
    try { render(await api("/proposals/" + proposal)); } catch (_) { stopped = true; }
  } finally { submitting = false; controls(); }
};
$("stop").onclick = async () => {
  stopped = true; controls();
  try {
    await api("/stop", {method: "POST"});
    $("message").textContent = "Stop requested for both nodes. Inspect hardware; restart locally to re-arm.";
  } catch (error) { $("error").textContent = error.message; }
};
poll();
refreshNodes();
setInterval(poll, 1000);
setInterval(refreshNodes, 5000);
