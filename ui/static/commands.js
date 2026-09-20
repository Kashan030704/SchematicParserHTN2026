// Optional input adapter. No direct node access; motion still uses app.js's click approval.
let commandBusy = false, commandVersion = 0, recognition = null;
let listening = false, held = false, finalTranscript = "", voiceFailed = false;
let voiceTimer = null;
const SpeechAPI = globalThis.SpeechRecognition || globalThis.webkitSpeechRecognition;
const exactStop = text => /^(stop|stop now|stop robot|stop the robot|stop everything|emergency stop|halt|halt now)[.!?,]*$/i.test(text.trim());

function commandIsBusy() { return commandBusy; }

function voiceControls() {
  $("voice-talk").disabled = !SpeechAPI || !$("voice-consent").checked;
  $("voice-talk").setAttribute("aria-pressed", String(listening));
  $("command-send").disabled = commandBusy;
  controls();
}
function cancelCommandInput() {
  commandVersion++;
  commandBusy = false;
  held = false;
  finalTranscript = "";
  voiceFailed = true;
  clearTimeout(voiceTimer);
  if (recognition && listening) {
    try { recognition.abort(); } catch (_) { /* Recognition may still be starting. */ }
  }
  $("voice-state").textContent = "Microphone input cancelled. Stop requested separately; check hardware.";
  voiceControls();
}
async function sendCommand(text) {
  text = text.trim();
  if (!text) return;
  // The explicit stop button remains available even with another model request in flight.
  if (exactStop(text)) {
    await $("stop").onclick();
    $("command-message").textContent = "Stop requested without an LLM call. Check node status and physical stops.";
    return;
  }
  if (commandBusy) return;
  const version = ++commandVersion;
  commandBusy = true; voiceControls();
  $("command-error").textContent = "";
  $("command-message").textContent = "Interpreting… no motion is authorized by this request.";
  try {
    const result = await api("/commands", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text, proposal_id: proposal})});
    if (version !== commandVersion) return; // Ignore stale replies after a stop.
    $("command-result").textContent = JSON.stringify(result, null, 2);
    $("command-message").textContent = result.message;
    if (result.stopped) { stopped = true; controls(); }
    if (result.proposal) {
      if (stopped || current?.state === "running") {
        throw new Error("State changed during interpretation. No motion started; refresh before another request.");
      }
      // 'Run this BOM' preserves unsaved editor changes, but never preserves checked approval.
      const same = current?.id === result.proposal.id;
      $("confirm").checked = false;
      render(result.proposal, !same);
    }
    if (result.detection) $("detection").textContent = JSON.stringify(result.detection, null, 2);
  } catch (error) {
    if (version === commandVersion) $("command-error").textContent = error.message;
  } finally {
    if (version === commandVersion) { commandBusy = false; voiceControls(); }
  }
}
$("command-form").onsubmit = async event => {
  event.preventDefault();
  await sendCommand($("command-text").value);
};

function endVoice(cancel = false) {
  held = false;
  if (!recognition || !listening) return;
  try {
    if (cancel) { voiceFailed = true; finalTranscript = ""; recognition.abort(); }
    else recognition.stop();
  } catch (_) { /* Early release before onstart: that handler also checks held. */ }
}
function startVoice() {
  if (!SpeechAPI || !$("voice-consent").checked || listening) return;
  held = true;
  listening = true;
  voiceFailed = false;
  finalTranscript = "";
  try { recognition = new SpeechAPI(); }
  catch (error) {
    listening = false; held = false; voiceFailed = true;
    $("voice-state").textContent = "Speech unavailable: " + error.message + ". Type your request instead.";
    voiceControls();
    return;
  }
  recognition.lang = "en-US";
  recognition.continuous = false;
  recognition.interimResults = true;
  recognition.maxAlternatives = 1;
  recognition.onstart = () => { if (!held) recognition.stop(); };
  recognition.onresult = event => {
    if (voiceFailed) return;
    const finalParts = [], previewParts = [];
    for (let i = 0; i < event.results.length; i++) {
      const result = event.results[i];
      previewParts.push(result[0].transcript);
      if (result.isFinal) finalParts.push(result[0].transcript);
    }
    finalTranscript = finalParts.join(" ").trim();
    $("command-text").value = previewParts.join(" ").trim();
    if (finalTranscript && exactStop(finalTranscript)) sendCommand(finalTranscript);
  };
  recognition.onerror = event => {
    if (voiceFailed && event.error === "aborted") return;
    voiceFailed = true;
    finalTranscript = "";
    $("voice-state").textContent = "Speech unavailable (" + event.error + "). Type your request instead; nothing was dispatched.";
  };
  recognition.onend = () => {
    listening = false; held = false;
    clearTimeout(voiceTimer);
    const transcript = finalTranscript;
    finalTranscript = "";
    voiceControls();
    if (!voiceFailed && transcript) {
      $("voice-state").textContent = "Recognized: " + transcript + ". Check the interpretation before approving motion.";
      sendCommand(transcript);
    } else if (!voiceFailed) $("voice-state").textContent = "No final speech recognized. Hold to try again, or type.";
  };
  try {
    recognition.start();
    $("voice-state").textContent = "Listening… release to interpret. Maximum 15 seconds.";
    voiceTimer = setTimeout(() => endVoice(), 15000);
  } catch (error) {
    listening = false; held = false; voiceFailed = true;
    $("voice-state").textContent = "Could not start microphone: " + error.message + ". Typing still works.";
  }
  voiceControls();
}
$("voice-talk").onpointerdown = event => {
  event.preventDefault();
  if (event.button !== 0) return;
  event.currentTarget.setPointerCapture?.(event.pointerId);
  startVoice();
};
$("voice-talk").onpointerup = () => endVoice();
$("voice-talk").onpointercancel = () => endVoice(true);
$("voice-talk").onkeydown = event => {
  if ((event.key === " " || event.key === "Enter") && !event.repeat) { event.preventDefault(); startVoice(); }
};
$("voice-talk").onkeyup = event => {
  if (event.key === " " || event.key === "Enter") { event.preventDefault(); endVoice(); }
};
$("voice-talk").onblur = () => endVoice(true);
$("voice-consent").onchange = () => { if (!$("voice-consent").checked) endVoice(true); voiceControls(); };
document.addEventListener("visibilitychange", () => { if (document.hidden) endVoice(true); });
globalThis.addEventListener?.("pagehide", () => endVoice(true));
$("voice-state").textContent = SpeechAPI
  ? "Optional: enable the microphone notice, then hold the button (or Space/Enter while focused)."
  : "Speech recognition is unavailable in this browser. Type a request; the rest of Schematic to Fetch still works.";
voiceControls();
api("/commands/capabilities").then(result => {
  $("command-mode").textContent = result.mode === "demo"
    ? "DEMO command mode — limited local grammar, NOT an LLM. RoboMaster may still be physical; approval is required."
    : "Baseten command LLM — separate from schematic ingestion. No automatic model fallback.";
}).catch(error => { $("command-mode").textContent = "Command service unavailable: " + error.message; });
