// Exercise the shipped browser script with a small DOM/fetch harness: no npm dependencies.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {test} = require("node:test");
const ui = path.join(__dirname, "../ui/static");

async function browser({speech = false, commandResponder = null, proposalFields = {}} = {}) {
  const elements = {};
  const element = () => ({value: "", files: [], checked: false, disabled: false,
    hidden: false, textContent: "", children: [],
    setAttribute(name, value) { this[name] = value; },
    replaceChildren(...children) { this.children = children; }});
  for (const match of fs.readFileSync(path.join(ui, "index.html"), "utf8").matchAll(/id="([^"]+)"/g)) {
    elements[match[1]] = element();
  }
  const calls = [];
  const microphones = [];
  class SpeechStub {
    constructor() { microphones.push(this); }
    start() { this.onstart?.(); }
    stop() { this.onend?.(); }
    abort() { this.onend?.(); }
    result(text, final = true) { this.onresult({results: [{0: {transcript: text}, isFinal: final}]}); }
  }
  let stopped = false, run;
  const context = vm.createContext({
    document: {
      getElementById: id => {
        assert.ok(elements[id], "Browser script references a real HTML element: " + id);
        return elements[id];
      },
      querySelector: () => ({content: "ui-csrf-token"}), createElement: element, addEventListener() {},
    },
    FormData: class { append(name, value) { this[name] = value; } },
    setInterval() {}, setTimeout() {}, clearTimeout() {},
    ...(speech ? {SpeechRecognition: SpeechStub} : {}),
    async fetch(url, options = {}) {
      calls.push({url, ...options});
      assert.equal(options.headers["X-HCP-UI-Token"], "ui-csrf-token");
      let result;
      if (url === "/status") result = {stopped, architecture: "RoboMaster HTTP"};
      else if (url === "/nodes") result = {nodes: {robomaster: {connected: true, simulated: true}}};
      else if (url === "/commands/capabilities") result = {mode: "demo"};
      else if (url === "/commands") {
        const data = JSON.parse(options.body);
        if (commandResponder) result = await commandResponder(data);
        else {
          run = {id: "p1", kind: "fulfillment",
            state: "awaiting_confirmation", bom: {LED_RX: 1}, events: []};
          result = {action: "fetch", proposal: run, message: "Proposal only; click approval required."};
        }
      }
      else if (url === "/stop") { stopped = true; result = {ok: true}; }
      else if (url === "/proposals") {
        run = {id: "p1", state: "awaiting_confirmation", events: [],
          bom: typeof options.body === "string" ? JSON.parse(options.body).bom : {R3: 2},
          llm_schematic_suggestions: ["Advisory only"], detection: {rollup: {}}, ...proposalFields};
        result = run;
      } else if (url === "/proposals/p1/approve") {
        const data = JSON.parse(options.body);
        assert.equal(data.approved, true);
        run = {...run, bom: data.bom || run.bom, state: "running"};
        result = {id: "p1", state: "running"};
      } else if (url === "/proposals/p1") result = run;
      else throw Error("Unexpected browser endpoint: " + url);
      return {ok: true, json: async () => structuredClone(result)};
    },
  });
  vm.runInContext(fs.readFileSync(path.join(ui, "app.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(ui, "commands.js"), "utf8"), context);
  await new Promise(resolve => setImmediate(resolve));
  return {elements, calls, microphones};
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

const visionReview = {
  summary: "Check a possible short before powering.", limitations: ["Part ratings are unreadable."],
  findings: [{title: "<img src=x onerror=alert(1)>", severity: "high", components: ["R3"],
    evidence: "Possible VCC/GND link on page 1.", consequence: "Possible component damage.",
    recommendation: "Verify the connection.", estimated_damage_cost: {
      currency: "CAD", low: 2, high: 10, basis: "Illustrative test estimate for small passives."}}],
};

test("vision findings render evidence and cost as text and flag edited BOMs", async () => {
  const {elements: el, calls} = await browser({proposalFields: {
    schematic_review: visionReview, schematic_review_bom: {R3: 2}}});
  el.pdf.files = [{name: "schematic.png"}];
  await el["run-form"].onsubmit({preventDefault() {}});
  assert.equal(el["review-summary"].textContent, visionReview.summary);
  const card = el["review-findings"].children[0];
  assert.equal(card.children[0].textContent, "HIGH · " + visionReview.findings[0].title);
  assert.match(card.children[2].textContent, /VCC\/GND/);
  assert.match(card.children[5].textContent, /CAD \$2.00–\$10.00 \(illustrative\)/);
  assert.equal(el["review-cost-note"].hidden, false);
  assert.equal(el["review-stale"].hidden, true);
  el.confirm.checked = true;
  el["bom-edit"].value = '{"C1":1}';
  el["bom-edit"].oninput();
  assert.equal(el["review-stale"].hidden, false);
  assert.equal(el.confirm.checked, false);
  assert.equal(el.approve.disabled, true);
  el["bom-edit"].value = '{"R3":2}';
  el["bom-edit"].oninput();
  assert.equal(el["review-stale"].hidden, true);
  assert.equal(calls.filter(c => c.url.endsWith("/approve")).length, 0);
});

test("unknown review costs are not displayed as zero", async () => {
  const review = structuredClone(visionReview);
  Object.assign(review.findings[0].estimated_damage_cost, {low: null, high: null, basis: "Part identity unknown."});
  const {elements: el} = await browser({proposalFields: {schematic_review: review}});
  el.pdf.files = [{name: "schematic.pdf"}];
  await el["run-form"].onsubmit({preventDefault() {}});
  const card = el["review-findings"].children[0];
  assert.match(card.children[5].textContent, /Not estimated/);
  assert.doesNotMatch(card.children[5].textContent, /\$0/);
  assert.match(card.children[6].textContent, /Part identity unknown/);
});

test("review failure leaves extracted parts and local checks available for approval", async () => {
  const {elements: el} = await browser({proposalFields: {schematic_review_error: "429 rate limited"}});
  el.pdf.files = [{name: "schematic.pdf"}];
  await el["run-form"].onsubmit({preventDefault() {}});
  assert.equal(el["review-error"].hidden, false);
  assert.match(el["review-error"].textContent, /429 rate limited/);
  assert.equal(el["requested-parts"].children.length, 1);
  assert.equal(el["llm-suggestions"].children[0].textContent, "Advisory only");
  assert.equal(el["review-cost-note"].hidden, true);
  el.confirm.checked = true;
  el.confirm.onchange();
  assert.equal(el.approve.disabled, false);
});

test("classification shows original components, sums, tag zero, and uncertain matches", async () => {
  const raw = {"R1 10k": 2, "<img src=x onerror=alert(1)>": 1};
  const grouped = {resistors: 2, "custom printed circuit boards": 1};
  const tagged = {resistors: {quantity: 2, tag_id: "tag36h11[0]"}, "custom printed circuit boards": {quantity: 1, tag_id: "tag36h11[5]"}};
  const {elements: el, calls} = await browser({proposalFields: {
    bom: grouped, raw_bom: raw, classification_bom: grouped, classification_method: "llm",
    tagged_bom: tagged,
    group_tags: {resistors: 0, "custom printed circuit boards": 5},
    component_classification: [
      {component: "R1 10k", quantity: 2, group: "resistors", tag_id: 0, confidence: "high", reason: "Resistor."},
      {component: Object.keys(raw)[1], quantity: 1, group: "custom printed circuit boards", tag_id: 5, confidence: "low", reason: "Forced storage match."},
    ],
    schematic_review: visionReview, schematic_review_bom: raw, schematic_review_grouped_bom: grouped,
  }});
  el.pdf.files = [{name: "schematic.png"}];
  await el["run-form"].onsubmit({preventDefault() {}});
  assert.equal(el["classification-card"].hidden, false);
  assert.deepEqual(JSON.parse(el["raw-bom-result"].textContent), raw);
  assert.deepEqual(JSON.parse(el["bom-result"].textContent), tagged);
  assert.deepEqual(JSON.parse(el["bom-edit"].value), grouped);
  const rows = el["classification-rows"].children;
  assert.equal(rows[0].children[2].textContent, "resistors · tag 0");
  assert.equal(rows[1].children[0].textContent, Object.keys(raw)[1]);
  assert.equal(rows[1].className, "uncertain-classification");
  assert.match(el["requested-parts"].children[0].textContent, /tag 0/);
  assert.equal(el["review-stale"].hidden, true);
  assert.equal(el["classification-stale"].hidden, true);
  el["bom-edit"].value = '{"resistors":3}';
  el["bom-edit"].oninput();
  assert.equal(el["classification-stale"].hidden, false);
  assert.equal(el["review-stale"].hidden, false);
  assert.equal(calls.filter(c => c.url.endsWith("/approve")).length, 0);
});

test("classification failure keeps the source JSON visible for human correction", async () => {
  const {elements: el} = await browser({proposalFields: {
    raw_bom: {"R1 10k": 2}, classification_error: "Incomplete model assignment",
  }});
  el["bom-json"].value = '{"R1 10k":2}';
  await el["run-form"].onsubmit({preventDefault() {}});
  assert.equal(el["classification-error"].hidden, false);
  assert.match(el["classification-error"].textContent, /Incomplete model assignment/);
  assert.deepEqual(JSON.parse(el["raw-bom-result"].textContent), {"R1 10k": 2});
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

test("typed commands create a proposal, with no microphone or automatic approval", async () => {
  const {elements: el, calls} = await browser();
  assert.equal(el["voice-talk"].disabled, true);
  assert.match(el["voice-state"].textContent, /unavailable/);
  assert.match(el["command-mode"].textContent, /NOT an LLM/);
  el["command-text"].value = "fetch LED_RX";
  await el["command-form"].onsubmit({preventDefault() {}});
  assert.equal(el["requested-parts"].children.length, 1);
  assert.equal(el.confirm.checked, false);
  assert.equal(el.approve.disabled, true);
  assert.equal(calls.filter(c => c.url.endsWith("/approve")).length, 0);
});

test("push-to-talk requires consent, sends only a final transcript, and never approves", async () => {
  const {elements: el, calls, microphones} = await browser({speech: true});
  const press = () => el["voice-talk"].onpointerdown({preventDefault() {}, button: 0, currentTarget: el["voice-talk"]});
  press();
  assert.equal(microphones.length, 0);
  el["voice-consent"].checked = true;
  el["voice-consent"].onchange();
  press();
  microphones[0].result("fetch LED_RX", false);
  assert.equal(calls.filter(c => c.url === "/commands").length, 0);
  microphones[0].result("fetch LED_RX");
  el["voice-talk"].onpointerup();
  await new Promise(resolve => setImmediate(resolve));
  const commands = calls.filter(c => c.url === "/commands");
  assert.equal(commands.length, 1);
  assert.equal(JSON.parse(commands[0].body).text, "fetch LED_RX");
  assert.equal(el.approve.disabled, true);
  assert.equal(calls.filter(c => c.url.endsWith("/approve")).length, 0);
});

test("speech failure cannot dispatch stale text or disable typed commands", async () => {
  const {elements: el, calls, microphones} = await browser({speech: true});
  el["voice-consent"].checked = true;
  el["voice-talk"].onkeydown({key: " ", preventDefault() {}});
  microphones[0].result("fetch LED_RX", false);
  microphones[0].onerror({error: "not-allowed"});
  microphones[0].onend();
  assert.match(el["voice-state"].textContent, /not-allowed/);
  assert.equal(calls.filter(c => c.url === "/commands").length, 0);
  el["command-text"].value = "fetch LED_RX";
  await el["command-form"].onsubmit({preventDefault() {}});
  assert.equal(calls.filter(c => c.url === "/commands").length, 1);
});

test("recognized stop bypasses a pending command and suppresses its late proposal", async () => {
  let resolveModel;
  const pending = new Promise(resolve => { resolveModel = resolve; });
  const {elements: el, calls, microphones} = await browser({speech: true, commandResponder: () => pending});
  el["command-text"].value = "fetch LED_RX";
  const submission = el["command-form"].onsubmit({preventDefault() {}});
  el["voice-consent"].checked = true;
  el["voice-talk"].onkeydown({key: "Enter", preventDefault() {}});
  microphones[0].result("stop!");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.filter(c => c.url === "/stop").length, 1);
  assert.equal(calls.filter(c => c.url === "/commands").length, 1);
  resolveModel({proposal: {id: "late", bom: {LED_RX: 1}, state: "awaiting_confirmation"}});
  await submission;
  assert.equal(el["requested-parts"].children.length, 0);
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
