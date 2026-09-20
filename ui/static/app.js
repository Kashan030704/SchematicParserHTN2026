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
  const interpreting = typeof commandIsBusy === "function" && commandIsBusy();
  $("run-button").disabled = submitting || running;
  $("approve").disabled = submitting || interpreting || stopped || !awaiting || !$("confirm").checked;
  $("confirm").disabled = running || stopped;
  $("bom-edit").disabled = running;
  $("refresh").disabled = submitting || running || stopped;
}
function editedBomDiffers(original) {
  try {
    const edited = JSON.parse($("bom-edit").value);
    return Object.keys(edited).length !== Object.keys(original).length ||
      Object.entries(original).some(([type, qty]) => edited[type] !== qty);
  } catch (_) { return true; }
}
function renderClassification(run) {
  $("classification-card").hidden = !run.raw_bom;
  $("raw-bom-result").textContent = JSON.stringify(run.raw_bom || {}, null, 2);
  $("classification-method").textContent = run.classification_method === "llm"
    ? "LLM classification. Every original entry is assigned once; quantities and tag IDs come from your input and registered groups."
    : run.classification_method === "explicit_groups"
      ? "Your input already uses registered group names. No classification model call was needed." : "Classification incomplete.";
  $("classification-error").hidden = !run.classification_error;
  $("classification-error").textContent = run.classification_error
    ? "Classification unavailable: " + run.classification_error + ". Your original parts are preserved. Correct the grouped BOM using the registered names before approving." : "";
  const stale = run.state === "awaiting_confirmation" && run.classification_bom
    ? editedBomDiffers(run.classification_bom) : run.classification_stale;
  $("classification-stale").hidden = !stale;
  $("classification-rows").replaceChildren(...(run.component_classification || []).map(row => {
    const tr = document.createElement("tr");
    tr.className = row.confidence === "high" ? "" : "uncertain-classification";
    tr.replaceChildren(...[row.component, String(row.quantity), row.group + " · tag " + row.tag_id,
      row.confidence + " — " + row.reason].map(value => {
      const cell = document.createElement("td");
      cell.textContent = value;
      return cell;
    }));
    return tr;
  }));
}
function renderSchematicReview(run) {
  const review = run.schematic_review;
  $("review-error").hidden = !run.schematic_review_error;
  $("review-error").textContent = run.schematic_review_error
    ? "LLM review unavailable: " + run.schematic_review_error + ". Your extracted BOM and local checks are still available." : "";
  $("review-summary").textContent = review?.summary || (run.schematic_review_error
    ? "The schematic review did not complete."
    : "Upload a PDF or image for an LLM review of the actual schematic wiring. Local checks are available below.");
  $("review-cost-note").hidden = !review;
  let stale = !!run.schematic_review_stale;
  if (review && run.state === "awaiting_confirmation") {
    stale = editedBomDiffers(run.schematic_review_grouped_bom || run.schematic_review_bom || run.bom);
  }
  $("review-stale").hidden = !review || !stale;
  const paragraph = (label, value) => {
    const node = document.createElement("p");
    node.textContent = label + value;
    return node;
  };
  $("review-findings").replaceChildren(...(review?.findings || []).map(finding => {
    const card = document.createElement("article");
    card.className = "review-finding severity-" + finding.severity;
    const heading = document.createElement("h4");
    heading.textContent = finding.severity.toUpperCase() + " · " + finding.title;
    const cost = finding.estimated_damage_cost;
    const estimate = cost.low === null ? "Not estimated; see the missing information below."
      : "CAD $" + cost.low.toFixed(2) + "–$" + cost.high.toFixed(2) + " (illustrative)";
    card.replaceChildren(heading,
      paragraph("Components / nets: ", finding.components.length ? finding.components.join(", ") : "See schematic evidence below."),
      paragraph("Schematic evidence: ", finding.evidence),
      paragraph("Possible consequence: ", finding.consequence),
      paragraph("Suggested check: ", finding.recommendation),
      paragraph("Estimated parts replacement: ", estimate),
      paragraph("Estimate basis: ", cost.basis));
    return card;
  }));
  list("review-limitations", review?.limitations || []);
}
function render(run, edit = false) {
  current = run;
  proposal = run.id;
  $("confirmation").hidden = false;
  $("action-summary").textContent = "Deliver one cup per approved group to the collection point.";
  $("confirm-text").textContent = "I checked the BOM, tag IDs, taught image target, clear paths, and collection point.";
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
  renderSchematicReview(run);
  renderClassification(run);
  list("requested-parts", Object.entries(run.bom || {}).map(([type, qty]) => type +
    (run.group_tags?.[type] !== undefined ? " · tag " + run.group_tags[type] : "") + " × " + qty + " parts needed → one cup"));
  $("component-details").hidden = !(run.component_details || []).length;
  list("source-components", (run.component_details || []).map(p =>
    p.refdes.join(", ") + " — " + p.type + " " + p.value + " × " + p.quantity));
  list("delivered", completed.map(type => type + " · cup cycle commanded, delivery unverified"));
  $("bom-result").textContent = JSON.stringify(run.tagged_bom || run.bom, null, 2);
  $("plan-result").textContent = JSON.stringify(Object.keys(run.bom || {}).map(type => ({
    type, operations: ["detect", "grasp (coarse + visual align + grip + retrace)", "drive to collection point", "drop", "return"]
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
    $("registered-groups").hidden = !status.groups?.length;
    list("group-catalog", (status.groups || []).map(group => "Tag " + group.tag_id + " → " + group.name + ": " + group.description));
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
$("bom-edit").oninput = () => {
  $("confirm").checked = false;
  if (current) { renderSchematicReview(current); renderClassification(current); }
  controls();
};
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
    $("message").textContent = file?.name.toLowerCase().endsWith(".sch") ? "Reading SCH components and classifying them into groups…"
      : file ? "Extracting parts, reviewing the schematic, and classifying components…" : "Preparing and grouping the BOM…";
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
  if (typeof commandIsBusy === "function" && commandIsBusy()) return;
  if (submitting || stopped || current?.state !== "awaiting_confirmation" || !$("confirm").checked) return;
  submitting = true; controls();
  try {
    const text = $("bom-edit").value;
    JSON.parse(text);
    const body = '{"approved":true,"bom":' + text + '}';
    await api("/proposals/" + proposal + "/approve", {method: "POST",
      headers: {"Content-Type": "application/json"}, body});
    render(await api("/proposals/" + proposal));
    $("message").textContent = "Approved once. The controller is executing the displayed action.";
  } catch (error) {
    $("error").textContent = error.message;
    // Never automatically retry approval. Re-read state after an uncertain response.
    try { render(await api("/proposals/" + proposal)); } catch (_) { stopped = true; }
  } finally { submitting = false; controls(); }
};
$("stop").onclick = async () => {
  if (typeof cancelCommandInput === "function") cancelCommandInput();
  stopped = true; controls();
  try {
    await api("/stop", {method: "POST"});
    $("message").textContent = "RoboMaster stop requested. Inspect hardware; restart locally to re-arm.";
  } catch (error) { $("error").textContent = error.message; }
};
poll();
refreshNodes();
setInterval(poll, 1000);
setInterval(refreshNodes, 5000);
