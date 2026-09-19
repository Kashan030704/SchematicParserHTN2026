const $ = id => document.getElementById(id);
let activeRun = null;
let ready = false;
let polling = false;
const terminal = new Set(['complete', 'incomplete', 'failed']);

function list(id, values) {
  $(id).replaceChildren(...values.map(value => {
    const item = document.createElement('li');
    item.textContent = value;
    return item;
  }));
}

async function poll() {
  if (polling) return;
  polling = true;
  try {
    const status = await fetch('/api/status').then(r => r.json());
    ready = ['arm', 'conveyor', 'camera'].every(id => id in status.nodes);
    $('mode').textContent = status.simulation ? 'Simulation · fixture BOM and simulated motors' : 'Live bench';
    $('demo-note').hidden = !status.simulation;
    $('nodes').replaceChildren(...['arm', 'conveyor', 'camera'].map(id => {
      const item = document.createElement('span');
      const connected = id in status.nodes;
      item.className = `node ${connected ? '' : 'offline'}`;
      item.textContent = `${id} · ${connected ? (status.health[id]?.payload?.state || 'connected') : 'offline'}`;
      return item;
    }));
    activeRun = activeRun || status.active_run;
    if (activeRun) {
      const run = await fetch(`/api/runs/${activeRun}`).then(r => r.json());
      $('state').textContent = run.state;
      $('step').textContent = run.step;
      $('error').textContent = run.error || '';
      $('quantities').textContent = `${run.delivered.length} delivered / ${run.requested ?? '…'} requested`;
      $('count').max = Math.max(1, run.requested || 1);
      $('count').value = run.delivered.length;
      list('warnings', run.warnings || []);
      list('delivered', run.delivered.map(p => `${p.refdes} — ${p.type} ${p.value}`));
      if (terminal.has(run.state)) activeRun = null;
    }
    $('run-button').disabled = !ready || activeRun !== null;
  } catch (error) {
    $('error').textContent = 'Bench connection unavailable.';
    $('run-button').disabled = true;
  } finally {
    polling = false;
  }
}

$('run-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('run-button').disabled = true;
  $('error').textContent = '';
  const data = new FormData();
  if ($('pdf').files.length) data.append('pdf', $('pdf').files[0]);
  try {
    const response = await fetch('/api/runs', {method: 'POST', body: data});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error);
    activeRun = result.id;
    await poll();
  } catch (error) {
    $('error').textContent = error.message;
    $('run-button').disabled = !ready;
  }
});
poll();
setInterval(poll, 500);
