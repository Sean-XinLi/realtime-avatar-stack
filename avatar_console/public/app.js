import { AvatarStreamClient } from "./avatar_stream_client.js";

const FALLBACK_STREAM_SERVER = "http://127.0.0.1:8080";
const SERVER_URL_STORAGE_KEY = "avatar_console.server_url";
const LEGACY_SERVER_URL_STORAGE_KEY = "control_avatar.server_url";
const SPEECH_SHORTCUT_KEYS = new Set(["Control", "ControlLeft", "ControlRight"]);
const SPEECH_SHORTCUT_LABEL = "Control";
const SPEECH_SHORTCUT_FALLBACK_HINT = "如果浏览器不响应 Control，请直接点击按住说话。";
const SPEECH_LANGUAGE = "zh-CN";

const serverUrlInput = document.getElementById("serverUrl");
const captureMsInput = document.getElementById("captureMs");
const inputChunkMsInput = document.getElementById("inputChunkMs");
const avatarIdSelect = document.getElementById("avatarId");
const voiceSelect = document.getElementById("voice");
const speechModeSelect = document.getElementById("speechMode");
const speechBtn = document.getElementById("speechBtn");
const speechHintEl = document.getElementById("speechHint");
const speechStatusEl = document.getElementById("speechStatus");
const composer = document.getElementById("composer");
const prepareBtn = document.getElementById("prepareBtn");
const directBtn = document.getElementById("directBtn");
const replyBtn = document.getElementById("replyBtn");
const stopBtn = document.getElementById("stopBtn");
const disconnectBtn = document.getElementById("disconnectBtn");
const statusEl = document.getElementById("status");
const inputFileEl = document.getElementById("inputFile");
const outputFileEl = document.getElementById("outputFile");
const conversationEl = document.getElementById("conversation");
const remoteVideo = document.getElementById("remoteVideo");
const localAudio = document.getElementById("localAudio");

const state = {
  busy: false,
  defaultVoice: "",
  speechSupported: false,
  speechListening: false,
};

const speech = {
  recognition: null,
  activeTrigger: null,
  finalTranscript: "",
  interimTranscript: "",
  finalized: false,
  stopping: false,
};

function setStatus(message, extra = null) {
  statusEl.textContent = extra ? `${message}\n${JSON.stringify(extra, null, 2)}` : message;
}

function setSpeechStatus(message, tone = "idle") {
  speechStatusEl.textContent = message;
  speechStatusEl.dataset.tone = tone;
}

function getServerUrlValue() {
  return serverUrlInput.value.trim().replace(/\/$/, "");
}

function readStoredServerUrl() {
  try {
    return (
      window.localStorage.getItem(SERVER_URL_STORAGE_KEY) ||
      window.localStorage.getItem(LEGACY_SERVER_URL_STORAGE_KEY) ||
      ""
    ).trim();
  } catch (_) {
    return "";
  }
}

function persistServerUrl(value) {
  const normalized = typeof value === "string" ? value.trim().replace(/\/$/, "") : "";
  try {
    if (normalized) {
      window.localStorage.setItem(SERVER_URL_STORAGE_KEY, normalized);
      window.localStorage.removeItem(LEGACY_SERVER_URL_STORAGE_KEY);
    } else {
      window.localStorage.removeItem(SERVER_URL_STORAGE_KEY);
      window.localStorage.removeItem(LEGACY_SERVER_URL_STORAGE_KEY);
    }
  } catch (_) {
    // Ignore browsers where localStorage is unavailable.
  }
}

function getAvatarIdValue() {
  const value = avatarIdSelect.value.trim();
  return value || null;
}

function getVoiceValue() {
  const value = voiceSelect.value.trim();
  return value || "";
}

function getSpeechModeValue() {
  return speechModeSelect.value;
}

function speechModeNeedsServer() {
  return getSpeechModeValue() !== "stt_only";
}

function updateSpeechButtonLabel() {
  speechBtn.textContent = state.speechListening
    ? `松开 ${SPEECH_SHORTCUT_LABEL} 结束语音输入`
    : `按住 ${SPEECH_SHORTCUT_LABEL} / 点击按住说话`;
  speechBtn.dataset.listening = state.speechListening ? "true" : "false";
}

function updateSpeechHint() {
  if (!state.speechSupported) {
    speechHintEl.textContent = "当前浏览器不支持 Web Speech API，建议在 localhost 上使用 Chrome 或 Safari。";
    return;
  }
  const mode = getSpeechModeValue();
  if (mode === "stt_only") {
    speechHintEl.textContent = `按住 ${SPEECH_SHORTCUT_LABEL} 说话，松开后只写入 input.txt。${SPEECH_SHORTCUT_FALLBACK_HINT}`;
    return;
  }
  if (mode === "direct") {
    speechHintEl.textContent = `按住 ${SPEECH_SHORTCUT_LABEL} 说话，松开后转写到 input.txt，并直接走朗读播放流程。${SPEECH_SHORTCUT_FALLBACK_HINT}`;
    return;
  }
  speechHintEl.textContent = `按住 ${SPEECH_SHORTCUT_LABEL} 说话，松开后转写到 input.txt，再走回答写入 output.txt 并播放。${SPEECH_SHORTCUT_FALLBACK_HINT}`;
}

function updateButtons() {
  const hasText = composer.value.trim().length > 0;
  const hasServerUrl = Boolean(getServerUrlValue());
  prepareBtn.disabled = state.busy || !hasServerUrl;
  directBtn.disabled = state.busy || !hasText || !hasServerUrl;
  replyBtn.disabled = state.busy || !hasText || !hasServerUrl;
  stopBtn.disabled = state.busy || !client.hasActiveSession;
  disconnectBtn.disabled = state.busy || !client.hasActiveConnection;
  speechBtn.disabled =
    state.busy ||
    !state.speechSupported ||
    (!state.speechListening && speechModeNeedsServer() && !hasServerUrl);
  updateSpeechButtonLabel();
}

function setBusy(busy, message = "") {
  state.busy = busy;
  if (message) {
    setStatus(message);
  }
  updateButtons();
}

function renderTextFile(target, text) {
  target.textContent = text || "";
}

function renderState(payload) {
  renderTextFile(inputFileEl, payload.inputText || "");
  renderTextFile(outputFileEl, payload.outputText || "");
}

function appendMessage(role, text, meta = "") {
  const article = document.createElement("article");
  article.className = `bubble ${role}`;

  const label = document.createElement("div");
  label.className = "bubbleLabel";
  label.textContent = role === "assistant" ? "output.txt" : meta || "input.txt";

  const body = document.createElement("div");
  body.className = "bubbleBody";
  body.textContent = text;

  article.append(label, body);
  conversationEl.append(article);
  conversationEl.scrollTop = conversationEl.scrollHeight;

  return {
    article,
    label,
    body,
  };
}

function updateMessageText(handle, text) {
  if (!handle?.body) {
    return;
  }
  handle.body.textContent = text;
  conversationEl.scrollTop = conversationEl.scrollHeight;
}

function resetConversationView() {
  conversationEl.replaceChildren();
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `request failed: ${response.status}`);
  }
  return payload;
}

function syncAvatarOptions(iceConfig) {
  const avatars = Array.isArray(iceConfig?.avatars) ? iceConfig.avatars : [];
  const selectedAvatarId = getAvatarIdValue();
  avatarIdSelect.innerHTML = "";

  if (!avatars.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "Default avatar";
    avatarIdSelect.append(option);
    avatarIdSelect.disabled = true;
    return;
  }

  for (const avatar of avatars) {
    const option = document.createElement("option");
    option.value = avatar.id;
    option.textContent = avatar.label || avatar.id;
    avatarIdSelect.append(option);
  }

  avatarIdSelect.value =
    avatars.some((avatar) => avatar.id === selectedAvatarId)
      ? selectedAvatarId
      : iceConfig.defaultAvatarId || avatars[0].id;
  avatarIdSelect.disabled = avatars.length <= 1;
}

function syncNumericInputFromRemote(input, value) {
  if (!Number.isFinite(value) || input.dataset.userEdited === "true") {
    return;
  }
  input.value = String(value);
}

function syncRemoteConfig(iceConfig) {
  syncAvatarOptions(iceConfig);
  syncNumericInputFromRemote(captureMsInput, iceConfig?.captureMs);
  syncNumericInputFromRemote(inputChunkMsInput, iceConfig?.inputChunkMs);
  setStatus("Remote stream config loaded.", {
    iceMode: iceConfig?.iceMode || "auto",
    captureMs: iceConfig?.captureMs ?? null,
    inputChunkMs: iceConfig?.inputChunkMs ?? null,
    startupMinAudioMs: iceConfig?.startupMinAudioMs ?? null,
    supportsSignalWebSocket: iceConfig?.supportsSignalWebSocket === true,
    avatarCount: Array.isArray(iceConfig?.avatars) ? iceConfig.avatars.length : 0,
  });
}

function populateVoices(voices, defaultVoice) {
  voiceSelect.innerHTML = "";
  const blankOption = document.createElement("option");
  blankOption.value = "";
  blankOption.textContent = "System default";
  voiceSelect.append(blankOption);

  for (const voice of voices) {
    const option = document.createElement("option");
    option.value = voice;
    option.textContent = voice;
    voiceSelect.append(option);
  }

  if (defaultVoice && voices.includes(defaultVoice)) {
    voiceSelect.value = defaultVoice;
  }
}

function createStreamPayload(audioUrl, label) {
  return {
    serverUrl: getServerUrlValue(),
    captureMs: Number.parseInt(captureMsInput.value, 10),
    inputChunkMs: Number.parseInt(inputChunkMsInput.value, 10),
    avatarId: getAvatarIdValue(),
    audioUrl,
    label,
  };
}

async function persistInputText(text) {
  const payload = await fetchJson("/api/input", {
    method: "POST",
    body: JSON.stringify({ text }),
  });
  renderState(payload);
  return payload;
}

async function createStreamJob(mode, text) {
  return fetchJson("/api/stream-jobs", {
    method: "POST",
    body: JSON.stringify({
      mode,
      text,
      voice: getVoiceValue(),
    }),
  });
}

async function consumeStreamJob(jobId, { assistantMessage = null } = {}) {
  if (typeof window.EventSource !== "function") {
    throw new Error("browser does not support EventSource");
  }

  return new Promise((resolve, reject) => {
    const source = new window.EventSource(`/api/stream-jobs/${encodeURIComponent(jobId)}/events`);
    let settled = false;
    let audioChain = Promise.resolve();

    function finish(error = null) {
      if (settled) {
        return;
      }
      settled = true;
      source.close();
      if (error) {
        reject(error);
        return;
      }
      audioChain.then(resolve).catch(reject);
    }

    source.onmessage = (event) => {
      let payload = null;
      try {
        payload = JSON.parse(event.data);
      } catch (_) {
        return;
      }

      renderState(payload);
      if (assistantMessage) {
        updateMessageText(assistantMessage, payload.outputText || "");
      }

      if (payload.message) {
        setStatus(payload.message, {
          jobId: payload.jobId,
          mode: payload.mode,
          jobState: payload.jobState,
          segmentIndex: payload.segmentIndex || null,
        });
      }

      if (payload.type === "segment" && payload.audioUrl) {
        const label = `${payload.targetFile || payload.mode}-${payload.segmentIndex || "segment"}`;
        audioChain = audioChain.then(() => client.streamAudio(createStreamPayload(payload.audioUrl, label)));
        audioChain.catch((error) => {
          finish(error);
        });
        return;
      }

      if (payload.type === "done") {
        finish();
        return;
      }

      if (payload.type === "error") {
        finish(new Error(payload.error || "stream job failed"));
      }
    };

    source.onerror = () => {
      finish(new Error("stream job event connection closed unexpectedly"));
    };
  });
}

async function runStreamConversation(mode, text, { assistantMessage = null } = {}) {
  const payload = await createStreamJob(mode, text);
  renderState(payload);
  await consumeStreamJob(payload.jobId, {
    assistantMessage,
  });
  return payload;
}

async function runConversationFlow(
  mode,
  rawText,
  { sourceLabel = "input.txt", clearComposerAfterSuccess = false, originatedFromSpeech = false } = {}
) {
  const text = rawText.trim();
  if (!text) {
    throw new Error("message text is required");
  }

  if (mode !== "stt_only" && !getServerUrlValue()) {
    throw new Error("streaming server URL is required");
  }

  appendMessage("user", text, sourceLabel);

  if (mode === "stt_only") {
    setBusy(true, originatedFromSpeech ? "Writing STT transcript into input.txt..." : "Writing input.txt...");
    try {
      await persistInputText(text);
      composer.value = text;
      if (originatedFromSpeech) {
        setSpeechStatus("语音已转写并写入 input.txt。", "ready");
      }
    } finally {
      setBusy(false);
      updateButtons();
    }
    return;
  }

  if (mode === "direct") {
    setBusy(
      true,
      originatedFromSpeech
        ? "Converting speech to text, writing input.txt, and streaming segmented TTS into the persistent session..."
        : "Writing input.txt and streaming segmented TTS into the persistent session..."
    );
    try {
      await runStreamConversation("direct", text);
      composer.value = text;
      if (clearComposerAfterSuccess) {
        composer.value = "";
      }
      if (originatedFromSpeech) {
        setSpeechStatus("语音已转写，并开始按 input.txt 分段直推播放。", "ready");
      }
      return;
    } finally {
      setBusy(false);
      updateButtons();
    }
  }

  if (mode === "reply") {
    setBusy(
      true,
      originatedFromSpeech
        ? "Converting speech to text, streaming codex output into output.txt, and speaking it segment by segment..."
        : "Writing input.txt, streaming codex output into output.txt, and speaking it segment by segment..."
    );
    try {
      const assistantMessage = appendMessage("assistant", "", "output.txt");
      await runStreamConversation("reply", text, {
        assistantMessage,
      });
      composer.value = text;
      if (clearComposerAfterSuccess) {
        composer.value = "";
      }
      if (originatedFromSpeech) {
        setSpeechStatus("语音已转写，并开始边写 output.txt 边分段播放。", "ready");
      }
      return;
    } finally {
      setBusy(false);
      updateButtons();
    }
  }

  throw new Error(`unsupported mode: ${mode}`);
}

function getSpeechRecognitionCtor() {
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

function resetSpeechSession() {
  speech.finalTranscript = "";
  speech.interimTranscript = "";
  speech.finalized = false;
  speech.stopping = false;
}

function getSpeechTranscript() {
  return `${speech.finalTranscript} ${speech.interimTranscript}`.replace(/\s+/g, " ").trim();
}

function setSpeechListening(listening) {
  state.speechListening = listening;
  document.body.classList.toggle("is-recording", listening);
  updateButtons();
}

async function finalizeSpeechRecognition() {
  if (speech.finalized) {
    return;
  }
  speech.finalized = true;
  speech.activeTrigger = null;

  const transcript = getSpeechTranscript();
  if (!transcript) {
    setSpeechStatus(`未识别到语音。按住 ${SPEECH_SHORTCUT_LABEL} 重试。`, "warning");
    updateButtons();
    return;
  }

  composer.value = transcript;
  updateButtons();

  try {
    await runConversationFlow(getSpeechModeValue(), transcript, {
      sourceLabel: "input.txt · STT",
      originatedFromSpeech: true,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setSpeechStatus(`语音流程失败: ${message}`, "error");
    setStatus(`Speech flow failed: ${message}`);
  }
}

async function startSpeechCapture(trigger) {
  if (!state.speechSupported || !speech.recognition) {
    throw new Error("speech recognition is not available in this browser");
  }
  if (state.busy || state.speechListening) {
    return;
  }
  if (speechModeNeedsServer() && !getServerUrlValue()) {
    throw new Error("streaming server URL is required before voice input can continue");
  }

  resetSpeechSession();
  speech.activeTrigger = trigger;
  speech.recognition.lang = SPEECH_LANGUAGE;
  speech.recognition.interimResults = true;
  speech.recognition.continuous = true;
  speech.recognition.maxAlternatives = 1;

  setSpeechStatus(`正在听写，松开 ${SPEECH_SHORTCUT_LABEL} 结束。`, "recording");
  setStatus("Listening for speech input...", {
    shortcut: SPEECH_SHORTCUT_LABEL,
    lang: SPEECH_LANGUAGE,
    mode: getSpeechModeValue(),
  });

  try {
    speech.recognition.start();
  } catch (error) {
    speech.activeTrigger = null;
    const message = error instanceof Error ? error.message : String(error);
    if (!message.toLowerCase().includes("start")) {
      throw error;
    }
  }
}

function stopSpeechCapture(trigger) {
  if (!speech.recognition) {
    return;
  }
  if (speech.activeTrigger !== trigger) {
    return;
  }
  speech.stopping = true;
  setSpeechStatus("正在结束语音输入并整理转写...", "processing");
  try {
    speech.recognition.stop();
  } catch (_) {
    finalizeSpeechRecognition().catch((error) => {
      const message = error instanceof Error ? error.message : String(error);
      setSpeechStatus(`语音流程失败: ${message}`, "error");
    });
  }
}

function handleSpeechResult(event) {
  const finalParts = [];
  const interimParts = [];

  for (let index = 0; index < event.results.length; index += 1) {
    const result = event.results[index];
    const transcript = result[0]?.transcript?.trim() || "";
    if (!transcript) {
      continue;
    }
    if (result.isFinal) {
      finalParts.push(transcript);
    } else {
      interimParts.push(transcript);
    }
  }

  speech.finalTranscript = finalParts.join(" ").trim();
  speech.interimTranscript = interimParts.join(" ").trim();

  const liveText = getSpeechTranscript();
  if (liveText) {
    composer.value = liveText;
    setSpeechStatus(`正在听写: ${liveText}`, "recording");
  }
}

function initializeSpeechRecognition() {
  const RecognitionCtor = getSpeechRecognitionCtor();
  if (!RecognitionCtor) {
    state.speechSupported = false;
    setSpeechStatus("当前浏览器不支持语音输入。", "warning");
    updateSpeechHint();
    updateButtons();
    return;
  }

  const recognition = new RecognitionCtor();
  recognition.lang = SPEECH_LANGUAGE;
  recognition.interimResults = true;
  recognition.continuous = true;
  recognition.maxAlternatives = 1;

  recognition.onstart = () => {
    setSpeechListening(true);
    setSpeechStatus(`正在听写，松开 ${SPEECH_SHORTCUT_LABEL} 结束。`, "recording");
  };

  recognition.onresult = (event) => {
    handleSpeechResult(event);
  };

  recognition.onerror = (event) => {
    const errorCode = event.error || "unknown";
    if (errorCode === "aborted") {
      return;
    }
    if (errorCode === "no-speech") {
      setSpeechStatus(`没有检测到语音。按住 ${SPEECH_SHORTCUT_LABEL} 重试。`, "warning");
      return;
    }
    if (errorCode === "not-allowed" || errorCode === "service-not-allowed") {
      setSpeechStatus("浏览器没有麦克风权限，无法进行语音输入。", "error");
      return;
    }
    setSpeechStatus(`语音识别失败: ${errorCode}`, "error");
  };

  recognition.onend = () => {
    const wasListening = state.speechListening;
    setSpeechListening(false);
    if (wasListening || speech.stopping) {
      finalizeSpeechRecognition().catch((error) => {
        const message = error instanceof Error ? error.message : String(error);
        setSpeechStatus(`语音流程失败: ${message}`, "error");
      });
      return;
    }
    speech.activeTrigger = null;
  };

  speech.recognition = recognition;
  state.speechSupported = true;
  setSpeechStatus(`语音输入待命。按住 ${SPEECH_SHORTCUT_LABEL} 开始说话。`, "idle");
  updateSpeechHint();
  updateButtons();
}

function isSpeechShortcutEvent(event) {
  return (
    (SPEECH_SHORTCUT_KEYS.has(event.key) || SPEECH_SHORTCUT_KEYS.has(event.code)) &&
    !event.repeat &&
    !event.metaKey &&
    !event.altKey &&
    !event.shiftKey
  );
}

async function handlePrepare() {
  setBusy(true, "Starting persistent session...");
  try {
    await client.startSession({
      serverUrl: getServerUrlValue(),
      captureMs: Number.parseInt(captureMsInput.value, 10),
      inputChunkMs: Number.parseInt(inputChunkMsInput.value, 10),
      avatarId: getAvatarIdValue(),
    });
    const payload = await fetchJson("/api/session/reset", {
      method: "POST",
    });
    renderState(payload);
    resetConversationView();
    composer.value = "";
    setStatus("Persistent session ready. Chat memory reset for a new connection.");
  } finally {
    setBusy(false);
  }
}

async function handleDirect() {
  await runConversationFlow("direct", composer.value, {
    clearComposerAfterSuccess: true,
  });
}

async function handleReply() {
  await runConversationFlow("reply", composer.value, {
    clearComposerAfterSuccess: true,
  });
}

async function initialize() {
  setStatus("Loading local control panel...");
  const [config, currentState] = await Promise.all([
    fetchJson("/api/config"),
    fetchJson("/api/state"),
  ]);

  const initialServerUrl =
    readStoredServerUrl() ||
    (typeof config.defaultStreamServer === "string" ? config.defaultStreamServer.trim() : "") ||
    FALLBACK_STREAM_SERVER;
  serverUrlInput.value = initialServerUrl;
  persistServerUrl(initialServerUrl);
  state.defaultVoice = config.defaultVoice || "";
  populateVoices(config.voices || [], state.defaultVoice);
  renderState(currentState);
  initializeSpeechRecognition();
  updateButtons();
  setStatus("Ready. Start the persistent session, then keep inserting text-generated audio into it.");
}

const client = new AvatarStreamClient({
  remoteVideo,
  localAudio,
  onStatus: setStatus,
  onRemoteConfig: syncRemoteConfig,
});

prepareBtn.addEventListener("click", () => {
  handlePrepare().catch((error) => {
    setBusy(false);
    setStatus(`Prepare failed: ${error.message}`);
  });
});

directBtn.addEventListener("click", () => {
  handleDirect().catch((error) => {
    setBusy(false);
    setStatus(`Direct stream failed: ${error.message}`);
  });
});

replyBtn.addEventListener("click", () => {
  handleReply().catch((error) => {
    setBusy(false);
    setStatus(`Reply stream failed: ${error.message}`);
  });
});

stopBtn.addEventListener("click", () => {
  setBusy(true, "Stopping session...");
  client
    .stopSession()
    .catch((error) => {
      setStatus(`Stop failed: ${error.message}`);
    })
    .finally(() => {
      setBusy(false);
    });
});

disconnectBtn.addEventListener("click", () => {
  setBusy(true, "Disconnecting peer connection...");
  client
    .disconnect()
    .catch((error) => {
      setStatus(`Disconnect failed: ${error.message}`);
    })
    .finally(() => {
      setBusy(false);
    });
});

speechBtn.addEventListener("pointerdown", (event) => {
  event.preventDefault();
  startSpeechCapture("button").catch((error) => {
    const message = error instanceof Error ? error.message : String(error);
    setSpeechStatus(`语音输入无法启动: ${message}`, "error");
    setStatus(`Speech input failed: ${message}`);
  });
});

for (const eventName of ["pointerup", "pointerleave", "pointercancel"]) {
  speechBtn.addEventListener(eventName, (event) => {
    event.preventDefault();
    stopSpeechCapture("button");
  });
}

window.addEventListener(
  "keydown",
  (event) => {
    if (!isSpeechShortcutEvent(event)) {
      return;
    }
    event.preventDefault();
    startSpeechCapture("keyboard").catch((error) => {
      const message = error instanceof Error ? error.message : String(error);
      setSpeechStatus(`语音输入无法启动: ${message}`, "error");
      setStatus(`Speech input failed: ${message}`);
    });
  },
  true
);

window.addEventListener(
  "keyup",
  (event) => {
    if (!SPEECH_SHORTCUT_KEYS.has(event.key) && !SPEECH_SHORTCUT_KEYS.has(event.code)) {
      return;
    }
    event.preventDefault();
    stopSpeechCapture("keyboard");
  },
  true
);

window.addEventListener("blur", () => {
  if (speech.activeTrigger) {
    stopSpeechCapture(speech.activeTrigger);
  }
});

composer.addEventListener("input", updateButtons);
serverUrlInput.addEventListener("input", () => {
  persistServerUrl(serverUrlInput.value);
  updateButtons();
});
captureMsInput.addEventListener("input", () => {
  captureMsInput.dataset.userEdited = "true";
  updateButtons();
});
inputChunkMsInput.addEventListener("input", () => {
  inputChunkMsInput.dataset.userEdited = "true";
  updateButtons();
});
avatarIdSelect.addEventListener("change", updateButtons);
voiceSelect.addEventListener("change", updateButtons);
speechModeSelect.addEventListener("change", () => {
  updateSpeechHint();
  updateButtons();
});

initialize().catch((error) => {
  setStatus(`Initialization failed: ${error.message}`);
});
