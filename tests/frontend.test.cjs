// Exercise the shipped browser script with a small DOM/fetch harness: no npm dependencies.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {test} = require("node:test");
const ui = path.join(__dirname, "../ui/static");

async function browser() {
  const elements = {};
  const element = () => ({value: "", files: [], checked: false, disabled: false,
    hidden: false, textContent: "", children: [],
    replaceChildren(...children) { this.children = children; }});
  for (const match of fs.readFileSync(path.join(ui, "index.html"), "utf8").matchAll(/id="([^"]+)"/g)) {
    elements[match[1]] = element();
  }
  const calls = [];
  let stopped = false, run;
  const context = vm.createContext({
    document: {
      getElementById: id => {
        assert.ok(elements[id], "Browser script references a real HTML element: " + id);
        return elements[id];
      },
      querySelector: () => ({content: "ui-csrf-token"}), createElement: element,
    },
    FormData: class { append(name, value) { this[name] = value; } },
    setInterval() {},
    async fetch(url, options = {}) {
      calls.push({url, ...options});
      assert.equal(options.headers["X-HCP-UI-Token"], "ui-csrf-token");
      let result;
      if (url === "/status") result = {stopped, architecture: "RoboMaster + conveyor HTTP"};
      else if (url === "/nodes") result = {nodes: {robomaster: {connected: true, simulated: true}}};
      else if (url === "/stop") { stopped = true; result = {ok: true}; }
      else if (url === "/proposals") {
        run = {id: "p1", state: "awaiting_confirmation", events: [],
          bom: typeof options.body === "string" ? JSON.parse(options.body).bom : {R3: 2},
          llm_schematic_suggestions: ["Advisory only"], detection: {rollup: {}}};
        result = run;
      } else if (url === "/proposals/p1/approve") {
        const data = JSON.parse(options.body);
        assert.equal(data.approved, true);
        run = {...run, bom: data.bom, state: "running"};
        result = {id: "p1", state: "running"};
      } else if (url === "/proposals/p1") result = run;
      else throw Error("Unexpected browser endpoint: " + url);
      return {ok: true, json: async () => structuredClone(result)};
    },
  });
  vm.runInContext(fs.readFileSync(path.join(ui, "app.js"), "utf8"), context);
  await new Promise(resolve => setImmediate(resolve));
  return {elements, calls};
}

test("proposal renders incoming cards but only explicit approval starts a run", async () => {
  const {elements: el, calls} = await browser();
  assert.match(el.nodes.children[0].textContent, /SIMULATED/);
  el["bom-json"].value = '{"R3":2,"C1":1}';
  await el["run-form"].onsubmit({preventDefault() {}});
  assert.equal(el["requested-parts"].children.length, 2);
  assert.equal(el["llm-suggestions"].children[0].textContent, "Advisory only");
  assert.equal(el["motion-events"].textContent, "No motion dispatched");
  assert.equal(el.approve.disabled, true);
  await el.approve.onclick();
  assert.equal(calls.filter(c => c.url.endsWith("/approve")).length, 0);
  el.confirm.checked = true;
  el.confirm.onchange();
  assert.equal(el.approve.disabled, false);
  el["bom-edit"].value = '{"C1":4}';
  await el.approve.onclick();
  await el.approve.onclick(); // Programmatic double click cannot replay the run.
  const approvals = calls.filter(c => c.url.endsWith("/approve"));
  assert.equal(approvals.length, 1);
  assert.deepEqual(JSON.parse(approvals[0].body), {approved: true, bom: {C1: 4}});
  assert.equal(el["run-button"].disabled, true);
});

test("SCH uploads use the proposal gate and expected multipart field", async () => {
  const {elements: el, calls} = await browser();
  const file = {name: "circuit.sch"};
  el.pdf.files = [file];
  await el["run-form"].onsubmit({preventDefault() {}});
  assert.equal(calls.find(c => c.url === "/proposals").body.schematic, file);
  assert.equal(calls.filter(c => c.url.endsWith("/approve")).length, 0);
  assert.match(el.message.textContent, /No motion/);
});

test("manual JSON preserves duplicate keys for strict server rejection", async () => {
  const {elements: el, calls} = await browser();
  el["bom-json"].value = '{"R3":1,"R3":2}';
  await el["run-form"].onsubmit({preventDefault() {}});
  assert.equal(calls.find(c => c.url === "/proposals").body, '{"bom":{"R3":1,"R3":2}}');
});

test("stop stays available and prevents approval of a pending proposal", async () => {
  const {elements: el, calls} = await browser();
  el["bom-json"].value = '{"R3":1}';
  await el["run-form"].onsubmit({preventDefault() {}});
  el.confirm.checked = true;
  await el.stop.onclick();
  await el.approve.onclick();
  assert.equal(calls.filter(c => c.url === "/stop").length, 1);
  assert.equal(calls.filter(c => c.url.endsWith("/approve")).length, 0);
  assert.equal(el.approve.disabled, true);
});

function background({reduceMotion = false, failWebGL = false} = {}) {
  const THREE = {...require("../ui/static/vendor/three-r128.min.js")};
  const canvas = {hidden: false}, listeners = {};
  let frames = 0, scheduled = 0;
  THREE.WebGLRenderer = class {
    constructor() { if (failWebGL) throw Error("No WebGL"); }
    setPixelRatio() {} setSize() {} setClearColor() {}
    render() { frames++; }
  };
  const context = vm.createContext({THREE,
    window: {innerWidth: 1024, innerHeight: 768, scrollY: 100, devicePixelRatio: 1,
      matchMedia: () => ({matches: reduceMotion}),
      addEventListener: (name, callback) => { listeners[name] = callback; }},
    document: {getElementById: () => canvas, documentElement: {scrollHeight: 2000},
      createElement: () => ({getContext: () => ({
        createRadialGradient: () => ({addColorStop() {}}), fillRect() {},
        createLinearGradient: () => ({addColorStop() {}}), beginPath() {},
        arc() {}, fill() {}, strokeRect() {}, fillText() {}, stroke() {}, moveTo() {}, lineTo() {},
      })})},
    requestAnimationFrame() { scheduled++; },
  });
  vm.runInContext(fs.readFileSync(path.join(ui, "bg3d.js"), "utf8"), context);
  return {canvas, listeners, get frames() { return frames; }, get scheduled() { return scheduled; }};
}

test("3D background builds with the locally pinned Three.js and starts rendering", () => {
  const result = background();
  assert.equal(result.frames, 1);
  assert.equal(result.scheduled, 1);
  assert.equal(result.canvas.hidden, false);
});

test("reduced-motion background renders statically without an animation loop", () => {
  const result = background({reduceMotion: true});
  assert.equal(result.frames, 1);
  assert.equal(result.scheduled, 0);
  result.listeners.scroll();
  result.listeners.resize();
  assert.equal(result.frames, 2);
  assert.equal(result.scheduled, 0);
});

test("missing WebGL or missing dependency cannot break the robot-control UI", () => {
  const result = background({failWebGL: true});
  assert.equal(result.canvas.hidden, true);
  assert.equal(result.frames, 0);
  assert.equal(result.scheduled, 0);
  vm.runInNewContext(fs.readFileSync(path.join(ui, "bg3d.js"), "utf8"), {});
});
