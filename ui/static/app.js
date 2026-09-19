const $ = id => document.getElementById(id);
let activeRun = null;
let ready = false;
let polling = false;
let hasRun = false;
let acceptsBom = false;
const terminal = new Set(['complete', 'incomplete', 'failed']);

function list(id, values) {
  $(id).replaceChildren(...values.map(value => {
    const item = document.createElement('li');
    item.textContent = value;
    return item;
  }));
}

async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed');
  return data;
}

async function poll() {
  if (polling) return;
  polling = true;
  try {
    const status = await api('/api/status');
    const cameraFree = ['palette', 'palette-hcp'].includes(status.backend);
    const remote = status.backend === 'palette-hcp';
    ready = status.ready;
    acceptsBom = status.accepts_bom;
    const ingestion = status.ingestion === 'live' ? 'Live Baseten schematic ingestion'
      : (remote && !status.simulation ? 'Reviewed JSON BOM only' : 'Fixture BOM for PDF/images; SCH uses component data');
    $('mode').textContent = cameraFree
      ? `Camera-Augmented · ${remote ? 'HCP TCP · ' : ''}${status.simulation ? 'SimDriver' : 'PHYSICAL PWM — no grip feedback'} · ${ingestion}`
      : `${status.simulation ? 'Simulated HCP hardware' : 'Live bench'} · ${ingestion}`;
    $('demo-note').hidden = !status.demo_available;
    $('bom-input').hidden = !acceptsBom;
    const badges = cameraFree && !remote
      ? ['Fixed palette ready', 'SG90 arm · simulated', 'Camera · augmented']
      : status.required_nodes.map(id => `${id} · ${id in status.nodes ? (status.health[id]?.payload?.state || 'connected') : 'offline'}`);
    $('nodes').replaceChildren(...badges.map(label => {
      const item = document.createElement('span');
      item.className = `node ${label.endsWith('offline') ? 'offline' : ''}`;
      item.textContent = label;
      return item;
    }));
    activeRun = activeRun || status.active_run;
    if (activeRun) {
      const run = await api(`/api/runs/${activeRun}`);
      hasRun = true;
      $('state').textContent = run.state;
      $('step').textContent = run.step;
      $('error').textContent = run.error || '';
      const noun = run.simulation ? 'simulated placements' : (cameraFree ? 'placement commands completed (unverified)' : 'delivered');
      $('quantities').textContent = `${run.delivered.length} ${noun} / ${run.requested ?? '…'} requested`;
      $('count').max = Math.max(1, run.requested || 1);
      $('count').value = run.delivered.length;
      list('warnings', run.warnings || []);
      list('delivered', run.delivered.map(p => p.slot_id
        ? `${p.refdes ? p.refdes + ' — ' : ''}${p.component_id} from ${p.slot_id} (${p.simulated ? 'simulated' : 'commanded, NOT sensed'})`
        : `${p.refdes} — ${p.type} ${p.value}`));
      $('bom-result').textContent = run.bom ? JSON.stringify(run.bom, null, 2) : 'Awaiting BOM';
      $('plan-details').hidden = !cameraFree;
      $('plan-result').textContent = run.plan ? JSON.stringify(run.plan, null, 2) : 'No plan executed';
      $('motion-count').textContent = `${run.motion_steps || 0} ${run.simulation ? 'simulated' : 'commanded'} slew steps. Angles below are commanded estimates, not feedback.`;
      $('angles').textContent = JSON.stringify(run.commanded_angles || {}, null, 2);
      $('motion-events').textContent = (run.events || []).map(e => JSON.stringify(e)).join('\n');
      if (terminal.has(run.state)) activeRun = null;
    } else if (!hasRun) {
      $('state').textContent = ready ? 'Ready' : 'Waiting for nodes';
      $('step').textContent = cameraFree
        ? (remote ? 'HCP palette node required; physical mode also needs local ARM/START on the Pi. Paste a BOM to begin.'
          : 'Upload a PDF, image, or SCH file, or paste a BOM. Every arm move stays simulated.')
        : 'Arm, conveyor, and camera are required.';
    }
    $('run-button').disabled = !ready || activeRun !== null;
  } catch (error) {
    $('error').textContent = error.message || 'Backend connection unavailable.';
    $('run-button').disabled = true;
  } finally {
    polling = false;
  }
}

$('run-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('run-button').disabled = true;
  $('error').textContent = '';
  try {
    const bomText = acceptsBom ? $('bom-json').value.trim() : '';
    const hasFile = $('pdf').files.length > 0;
    if (bomText && hasFile) throw new Error('Choose a schematic file OR a BOM, not both.');
    let options;
    if (bomText) {
      options = {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({bom: JSON.parse(bomText)})};
    } else {
      const data = new FormData();
      if (hasFile) data.append('pdf', $('pdf').files[0]);
      options = {method: 'POST', body: data};
    }
    const result = await api('/api/runs', options);
    activeRun = result.id;
    await poll();
  } catch (error) {
    $('error').textContent = error.message;
    $('run-button').disabled = !ready;
  }
});
poll();
setInterval(poll, 500);
