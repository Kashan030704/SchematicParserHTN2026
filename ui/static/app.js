const el = id => document.getElementById(id);
const token = document.querySelector('meta[name="hcp-token"]').content;
let proposal = null, polling = null, awaiting = false;
async function api(path, options = {}) {
  options.headers = {...options.headers, "X-HCP-UI-Token": token};
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || JSON.stringify(data));
  return data;
}
function error(err) { el("message").textContent = err.message; }
function enable() { el("approve").disabled = !awaiting || !el("confirm").checked; }
el("confirm").onchange = enable;
el("upload").onsubmit = async event => {
  event.preventDefault(); awaiting = false; enable(); el("message").textContent = "Reading schematic…";
  try {
    const result = await api("/proposals", {method:"POST", body: new FormData(event.target)});
    proposal = result.id; awaiting = true; el("confirm").checked = false;
    el("bom").value = JSON.stringify(result.bom, null, 2);
    el("detection").textContent = JSON.stringify(result.detection || result.detection_error, null, 2);
    el("message").textContent = "Review and edit. No motion has been dispatched.";
    enable();
  } catch (err) { error(err); }
};
el("refresh").onclick = async () => {
  try { el("detection").textContent = JSON.stringify(await api("/detect"), null, 2); }
  catch (err) { error(err); }
};
el("approve").onclick = async () => {
  if (!awaiting || !el("confirm").checked) return;
  try {
    const bom = JSON.parse(el("bom").value);
    awaiting = false; enable();
    await api("/proposals/" + proposal + "/approve", {method:"POST",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify({bom, approved:true})});
    clearInterval(polling);
    polling = setInterval(async () => {
      try {
        const run = await api("/proposals/" + proposal);
        el("progress").textContent = JSON.stringify(run, null, 2);
        if (run.state !== "running") clearInterval(polling);
      } catch (err) { clearInterval(polling); error(err); }
    }, 500);
  } catch (err) { error(err); }
};
el("stop").onclick = async () => {
  awaiting = false; enable();
  try { await api("/stop", {method:"POST"}); el("message").textContent = "Stop requested. Inspect hardware; restart to re-arm."; }
  catch (err) { error(err); }
};
