(() => {
  const agentPage = document.getElementById("agents");
  if (!agentPage) return;
  const panel = document.createElement("div");
  panel.className = "card";
  panel.style.marginTop = "18px";
  panel.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px">
      <div><div class="eyebrow">LIVE VOICE</div><h2 style="font-size:18px;margin:5px 0">Talk to your agent</h2></div>
      <span class="status" id="voiceState">Ready</span>
    </div>
    <p class="muted">Have a natural, real-time voice conversation with your AI agent.</p>
    <label class="field"><span>Select agent</span><select id="talkAgent" style="width:100%;padding:12px;border:1px solid #e6e5eb;border-radius:11px;background:white"></select></label>
    <div id="talkTranscript" style="min-height:50px;max-height:180px;overflow:auto;background:#f8f7fb;border-radius:12px;padding:10px;font-size:12px;color:#555;margin:12px 0">Your conversation will appear here.</div>
    <div style="display:flex;gap:8px">
      <button class="primary" id="talkStart" style="flex:1">🎙 Talk to agent</button>
      <button class="ghost" id="talkStop" disabled>End</button>
    </div>
    <p class="muted" style="margin-top:10px">Microphone access is required. Your OpenAI API key stays on the PIN&PITCH server.</p>`;
  const firstCard = agentPage.querySelector(".card");
  if (firstCard) firstCard.parentNode.insertBefore(panel, firstCard);
  else agentPage.appendChild(panel);

  let pc = null, channel = null, mic = null, remoteAudio = null;
  const $ = id => document.getElementById(id);
  const state = value => $("voiceState").textContent = value;
  const log = (who, text) => {
    const box = $("talkTranscript");
    if (box.textContent === "Your conversation will appear here.") box.textContent = "";
    const line = document.createElement("p");
    line.style.margin = "5px 0";
    const strong = document.createElement("b"); strong.textContent = who + ": ";
    line.append(strong, document.createTextNode(text));
    box.appendChild(line); box.scrollTop = box.scrollHeight;
  };
  async function loadAgents() {
    try {
      const agents = await api("/agents");
      $("talkAgent").innerHTML = agents.map(a =>
        `<option value="${a.id}" >${escapeHtml(a.name)}${a.enabled ? "" : " (draft)"}</option>`
      ).join("") || '<option value="">Create an agent first</option>';
      $("talkStart").disabled = agents.length === 0;
    } catch (e) { state(e.message); }
  }
  async function start() {
    if (!accessToken) { state("Please sign in"); return; }
    $("talkStart").disabled = true; state("Connecting…");
    try {
      const agentId = Number($("talkAgent").value);
      if (!agentId) throw new Error("Choose an agent first.");
      mic = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
      pc = new RTCPeerConnection();
      remoteAudio = document.createElement("audio"); remoteAudio.autoplay = true; remoteAudio.playsInline = true;
      remoteAudio.style.display = "none"; document.body.appendChild(remoteAudio);
      pc.ontrack = event => { remoteAudio.srcObject = event.streams[0]; remoteAudio.play().catch(()=>{}); };
      mic.getTracks().forEach(track => pc.addTrack(track, mic));
      channel = pc.createDataChannel("oai-events");
      channel.onopen = () => {
        state("Listening");
        channel.send(JSON.stringify({type:"response.create",response:{instructions:"Greet the user warmly and ask how you can help."}}));
      };
      channel.onmessage = event => {
        let data; try { data = JSON.parse(event.data); } catch { return; }
        if (data.type === "input_audio_buffer.speech_started") state("Listening");
        if (data.type === "response.created") state("Agent speaking");
        if (data.type === "response.output_audio_transcript.delta") {
          const box = $("talkTranscript");
          if (box.textContent === "Your conversation will appear here.") box.textContent = "";
          let last = box.lastElementChild;
          if (!last || last.dataset.role !== "agent") {
            last = document.createElement("p"); last.dataset.role = "agent"; last.style.margin = "5px 0";
            const b = document.createElement("b"); b.textContent = "Agent: "; last.appendChild(b); box.appendChild(last);
          }
          last.appendChild(document.createTextNode(data.delta || "")); box.scrollTop = box.scrollHeight;
        }
        if (data.type === "conversation.item.input_audio_transcription.completed" && data.transcript) log("You", data.transcript);
        if (data.type === "error") { state("Voice error"); log("System", data.error?.message || "Realtime voice error"); }
        if (data.type === "response.done") state("Listening");
      };
      pc.onconnectionstatechange = () => {
        if (pc && ["failed","disconnected","closed"].includes(pc.connectionState)) state("Disconnected");
      };
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      const response = await fetch("/voice/session", {
        method:"POST",
        headers:{"Content-Type":"application/json","Authorization":"Bearer "+accessToken},
        body:JSON.stringify({agent_id:agentId,sdp:offer.sdp})
      });
      if (!response.ok) {
        let detail = "Could not start the voice session.";
        try { detail = (await response.json()).detail || detail; } catch {}
        throw new Error(detail);
      }
      const answerSdp = await response.text();
      await pc.setRemoteDescription({type:"answer",sdp:answerSdp});
      $("talkStop").disabled = false;
      state("Connected · Speak now");
    } catch (e) {
      log("System", e.message);
      state("Not connected");
      await stop(false);
    } finally { $("talkStart").disabled = false; }
  }
  async function stop(show=true) {
    if (channel) { try { channel.close(); } catch {} channel = null; }
    if (pc) { try { pc.close(); } catch {} pc = null; }
    if (mic) { mic.getTracks().forEach(t => t.stop()); mic = null; }
    if (remoteAudio) { remoteAudio.srcObject = null; remoteAudio.remove(); remoteAudio = null; }
    $("talkStop").disabled = true;
    if (show) state("Call ended");
  }
  $("talkStart").addEventListener("click", start);
  $("talkStop").addEventListener("click", () => stop(true));
  loadAgents();
  const originalRefresh = window.refreshAgents;
  if (typeof originalRefresh === "function") {
    window.refreshAgents = async function() { await originalRefresh(); await loadAgents(); };
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && pc) stop(true);
  });
})();