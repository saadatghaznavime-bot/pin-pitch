(() => {
  const agentPage = document.getElementById("agents");
  if (!agentPage) return;

  const panel = document.createElement("div");
  panel.className = "card";
  panel.style.marginTop = "18px";
  panel.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px">
      <div><div class="eyebrow">LOCAL AI · ZERO API COST</div><h2 style="font-size:18px;margin:5px 0">Talk to your agent</h2></div>
      <span class="status" id="voiceState">Checking local AI…</span>
    </div>
    <p class="muted">Uses your local Ollama model, faster-whisper and Piper when installed. Conversations are saved in your PIN&PITCH workspace.</p>
    <label class="field"><span>Select agent</span><select id="talkAgent" style="width:100%;padding:12px;border:1px solid #ead8cc;border-radius:11px;background:white"></select></label><label class="field"><span>Saved conversations</span><select id="savedAgentSessions" style="width:100%;padding:12px;border:1px solid #ead8cc;border-radius:11px;background:white"><option value="">No saved conversations loaded</option></select></label><button class="ghost" id="loadSavedSession" style="width:100%;margin-bottom:8px">Load selected conversation</button>
    <div id="talkTranscript" style="min-height:64px;max-height:220px;overflow:auto;background:#fff6f0;border-radius:12px;padding:10px;font-size:12px;color:#555;margin:12px 0">Your saved conversation will appear here.</div>
    <label class="field"><span>Or type a message</span><input id="freeVoiceText" type="text" placeholder="Ask about hours, pricing, bookings…"></label>
    <div style="display:flex;gap:8px">
      <button class="primary" id="talkStart" style="flex:1">🎙 Record message</button>
      <button class="ghost" id="talkSend">Send</button>
      <button class="ghost" id="talkStop" disabled>Stop</button>
    </div>
    <p class="muted" style="margin-top:10px">Microphone recordings are sent only to the loopback gateway on this PC for local transcription. If local AI is offline, approved FAQ matching or a safe fallback is used. No external calls or messages are sent.</p>`;
  const firstCard = agentPage.querySelector(".card");
  if (firstCard) firstCard.parentNode.insertBefore(panel, firstCard);
  else agentPage.appendChild(panel);

  const GATEWAY = "http://127.0.0.1:8765";
  const $ = id => document.getElementById(id);
  const state = value => { $("voiceState").textContent = value; };
  let agents = [], savedSessions = [], activeSessionId = null, activeAgentId = null, history = [];
  let mediaRecorder = null, mediaStream = null, audioChunks = [], listening = false;

  function log(who, text) {
    const box = $("talkTranscript");
    if (box.textContent === "Your saved conversation will appear here.") box.textContent = "";
    const line = document.createElement("p");
    line.style.margin = "5px 0";
    const strong = document.createElement("b"); strong.textContent = who + ": ";
    line.append(strong, document.createTextNode(text));
    box.appendChild(line); box.scrollTop = box.scrollHeight;
  }

  async function checkGateway() {
    try {
      const response = await fetch(GATEWAY + "/health", { cache: "no-store" });
      if (!response.ok) throw new Error("Gateway not ready");
      const health = await response.json();
      const model = health.ollama?.model_available;
      const whisper = health.whisper?.installed;
      const piper = health.piper?.configured;
      state(model ? "Local model ready" : "Local AI needs setup");
      return { online: true, model, whisper, piper };
    } catch {
      state("FAQ fallback · gateway offline");
      return { online: false, model: false, whisper: false, piper: false };
    }
  }

  async function loadAgents() {
    try {
      agents = await api("/agents");
      $("talkAgent").innerHTML = agents.map(a =>
        `<option value="${a.id}">${escapeHtml(a.name)}${a.enabled ? "" : " (draft)"}</option>`
      ).join("") || '<option value="">Create an agent first</option>';
      $("talkStart").disabled = agents.length === 0;
      if (agents.length) await checkGateway();\n      await loadSavedSessions();
    } catch (e) { state(e.message); }
  }

  async function loadSavedSessions() {
    if (!accessToken) return;
    try {
      savedSessions = await api("/agent-sessions");
      const select = $("savedAgentSessions");
      if (select) select.innerHTML = '<option value="">Choose a saved conversation…</option>' +
        savedSessions.map(s => '<option value="' + s.id + '">' + escapeHtml(s.title) + ' · ' + new Date(s.updated_at).toLocaleString() + '</option>').join("");
    } catch {}
  }

  async function loadSelectedSession() {
    const id = Number($("savedAgentSessions").value);
    const session = savedSessions.find(item => item.id === id);
    if (!session) { state("Choose a saved conversation"); return; }
    try {
      const turns = await api("/agent-sessions/" + id + "/turns");
      $("talkAgent").value = String(session.agent_id);
      activeSessionId = session.id;
      activeAgentId = session.agent_id;
      history = turns.map(turn => ({ role: turn.role, content: turn.content }));
      $("talkTranscript").textContent = "";
      turns.forEach(turn => log(turn.role === "user" ? "You" : "Agent · saved", turn.content));
      state("Saved conversation loaded");
    } catch (e) { state(e.message); }
  }

  async function ensureSession() {
    const agentId = Number($("talkAgent").value);
    if (!agentId) throw new Error("Create an agent first.");
    if (activeSessionId && activeAgentId === agentId) return activeSessionId;
    const session = await api("/agent-sessions", {
      method: "POST", body: JSON.stringify({ agent_id: agentId })
    });
    activeSessionId = session.id;
    activeAgentId = agentId;
    history = [];
    $("talkTranscript").textContent = "New saved conversation · " + session.title;
    return activeSessionId;
  }

  async function speak(text) {
    try {
      const response = await fetch(GATEWAY + "/speak", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text })
      });
      if (!response.ok) throw new Error("Piper not ready");
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      audio.onended = () => URL.revokeObjectURL(url);
      await audio.play();
      return;
    } catch {
      if ("speechSynthesis" in window) {
        window.speechSynthesis.cancel();
        const utterance = new SpeechSynthesisUtterance(text);
        utterance.lang = "en-US";
        window.speechSynthesis.speak(utterance);
      }
    }
  }

  async function answer(raw) {
    const question = (raw || "").trim();
    if (!question) return;
    $("talkSend").disabled = true;
    state("Thinking…");
    try {
      const sessionId = await ensureSession();
      log("You", question);
      const selected = agents.find(a => a.id === Number(activeAgentId)) || agents.find(a => a.id === Number($("talkAgent").value));
      let reply = "", mode = "safe_fallback";
      try {
        const response = await fetch(GATEWAY + "/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message: question,
            instructions: (selected?.instructions || "You are a helpful business receptionist.") +
              "\nBe truthful. Do not invent prices, opening hours, availability, policies, or completed actions. If information is missing, ask a question or offer staff follow-up.",
            history: history.slice(-12)
          })
        });
        if (!response.ok) throw new Error("Local model unavailable");
        const data = await response.json();
        reply = data.reply;
        mode = "local_ollama";
      } catch {
        const fallback = await api("/agent-sessions/" + sessionId + "/turn", {
          method: "POST", body: JSON.stringify({ message: question })
        });
        reply = fallback.assistant_turn.content;
        mode = fallback.mode;
      }
      if (mode === "local_ollama") {
        await api("/agent-sessions/" + sessionId + "/turn", {
          method: "POST", body: JSON.stringify({ message: question, assistant_reply: reply })
        });
      }
      history.push({ role: "user", content: question }, { role: "assistant", content: reply });
      log("Agent · " + mode.replaceAll("_", " "), reply);
      state(mode === "local_ollama" ? "Answered by local AI" : "Safe fallback response");
      await speak(reply);
    } catch (e) {
      state("Unable to respond");
      log("System", e.message || "Could not save this conversation.");
    } finally {
      $("talkSend").disabled = false;
    }
  }

  async function startRecording() {
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      state("Recording unavailable");
      log("System", "This browser does not support local audio recording. Use the text box instead.");
      return;
    }
    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" :
        (MediaRecorder.isTypeSupported("audio/mp4") ? "audio/mp4" : "");
      mediaRecorder = new MediaRecorder(mediaStream, mime ? { mimeType: mime } : undefined);
      audioChunks = [];
      mediaRecorder.ondataavailable = event => { if (event.data?.size) audioChunks.push(event.data); };
      mediaRecorder.onstop = async () => {
        try {
          state("Transcribing locally…");
          const blob = new Blob(audioChunks, { type: mediaRecorder?.mimeType || "audio/webm" });
          const form = new FormData();
          form.append("audio", blob, "pinpitch-recording" + (blob.type.includes("mp4") ? ".mp4" : ".webm"));
          const response = await fetch(GATEWAY + "/transcribe", { method: "POST", body: form });
          const data = await response.json().catch(() => ({}));
          if (!response.ok) throw new Error(data.detail || "Local transcription unavailable.");
          if (!data.text?.trim()) throw new Error("No speech was detected. Please try again.");
          log("Transcription · " + data.device, data.text);
          await answer(data.text);
        } catch (e) {
          state("Transcription unavailable");
          log("System", e.message + " You can type your message instead.");
        } finally {
          mediaStream?.getTracks().forEach(track => track.stop());
          mediaStream = null; mediaRecorder = null; audioChunks = [];
          $("talkStart").disabled = false; $("talkStop").disabled = true;
        }
      };
      mediaRecorder.start();
      listening = true;
      $("talkStart").disabled = true; $("talkStop").disabled = false;
      state("Recording · press Stop when finished");
    } catch (e) {
      state("Microphone permission needed");
      log("System", e.message || "Allow microphone access and try again.");
      mediaStream?.getTracks().forEach(track => track.stop());
      mediaStream = null;
    }
  }

  function stopRecording() {
    if (mediaRecorder && mediaRecorder.state !== "inactive") {
      listening = false;
      state("Processing recording…");
      mediaRecorder.stop();
    }
  }

  $("loadSavedSession").addEventListener("click", loadSelectedSession);\n  $("talkStart").addEventListener("click", startRecording);
  $("talkStop").addEventListener("click", stopRecording);
  $("talkSend").addEventListener("click", () => {
    const input = $("freeVoiceText"); const value = input.value;
    input.value = ""; answer(value);
  });
  $("freeVoiceText").addEventListener("keydown", event => {
    if (event.key === "Enter") {
      const input = $("freeVoiceText"); const value = input.value;
      input.value = ""; answer(value);
    }
  });
  $("talkAgent").addEventListener("change", () => {
    activeSessionId = null; activeAgentId = null; history = [];
    $("talkTranscript").textContent = "Agent changed · a new saved conversation will start with your next message.";
  });
  loadAgents();
  const originalRefresh = window.refreshAgents;
  if (typeof originalRefresh === "function") {
    window.refreshAgents = async function() { await originalRefresh(); await loadAgents(); };
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && mediaRecorder && mediaRecorder.state !== "inactive") stopRecording();
  });
})();