import { BootstrapAudioBuffer } from "./bootstrap_audio.js";
import { loadIceConfig, shouldUseIceCandidate } from "./ice_policy.js";
import { PeerSignalClient } from "./signal_client.js";

const serverUrlInput = document.getElementById("serverUrl");
const captureMsInput = document.getElementById("captureMs");
const inputChunkMsInput = document.getElementById("inputChunkMs");
const avatarIdSelect = document.getElementById("avatarId");
const startBtn = document.getElementById("startBtn");
const stopBtn = document.getElementById("stopBtn");
const disconnectBtn = document.getElementById("disconnectBtn");
const statusEl = document.getElementById("status");
const localAudio = document.getElementById("localAudio");
const remoteVideo = document.getElementById("remoteVideo");

const PRECONNECT_READY_TIMEOUT_MS = 5000;
const CONNECTION_READY_TIMEOUT_MS = 400;
const DEFAULT_BOOTSTRAP_FLOOR_MS = 640;
const BOOTSTRAP_SLACK_MS = 80;

let pc = null;
let localStream = null;
let remoteStream = null;
let bootstrapAudioBuffer = null;
let senderTrack = null;
let activeConnection = null;
let activeSession = null;
let warmupPromise = null;
let warmupInputTimer = null;
let warmupState = {
  preparing: false,
  ready: false,
  error: null,
  serverUrl: null,
  inputChunkMs: null,
  bootstrapTargetMs: null,
  bufferedAudioMs: 0,
};

function delay(ms) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function setStatus(message, extra = null) {
  statusEl.textContent = extra ? `${message}\n${JSON.stringify(extra, null, 2)}` : message;
}

function getServerUrlValue() {
  return serverUrlInput.value.trim().replace(/\/$/, "");
}

function getAvatarIdValue() {
  const value = avatarIdSelect.value.trim();
  return value || null;
}

function isConfiguredServerUrl(serverUrl) {
  if (!serverUrl || serverUrl.includes("<server-ip>")) {
    return false;
  }
  try {
    new URL(serverUrl);
    return true;
  } catch (_) {
    return false;
  }
}

function updateButtons() {
  startBtn.disabled = activeSession !== null || warmupState.preparing || (!warmupState.ready && !warmupState.error);
  stopBtn.disabled = activeSession === null;
  disconnectBtn.disabled = activeConnection === null;
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

  const nextAvatarId =
    avatars.some((avatar) => avatar.id === selectedAvatarId)
      ? selectedAvatarId
      : (iceConfig.defaultAvatarId || avatars[0].id);
  avatarIdSelect.value = nextAvatarId;
  avatarIdSelect.disabled = avatars.length <= 1;
}

function getStartupMinAudioMs(iceConfig, inputChunkMs) {
  const ratioBased = Math.round((iceConfig.modelSliceMs || 0) * (iceConfig.startupPartialRatio || 0.75));
  return Math.min(
    iceConfig.modelSliceMs || DEFAULT_BOOTSTRAP_FLOOR_MS,
    Math.max(
      inputChunkMs,
      DEFAULT_BOOTSTRAP_FLOOR_MS,
      iceConfig.startupMinAudioMs || 0,
      ratioBased
    )
  );
}

function computeBootstrapTargetMs(connection, inputChunkMs) {
  const sliceMs = Math.max(1, Number(connection.iceConfig.modelSliceMs || 960));
  const startupMinAudioMs = getStartupMinAudioMs(connection.iceConfig, inputChunkMs);
  if (connection.pc && connection.pc.connectionState === "connected") {
    return startupMinAudioMs;
  }
  const optimisticOverlapMs = Math.min(200, Math.max(0, sliceMs - startupMinAudioMs));
  return Math.min(sliceMs, Math.max(startupMinAudioMs, sliceMs - optimisticOverlapMs + BOOTSTRAP_SLACK_MS));
}

async function uploadBootstrapAudio(connection, session, targetDurationMs) {
  if (!bootstrapAudioBuffer || session.stopped || connection.stopped) {
    return;
  }
  const snapshot = await bootstrapAudioBuffer.snapshotBase64(targetDurationMs);
  if (!snapshot || session.stopped || connection.stopped) {
    return;
  }
  const response = await fetch(`${connection.serverUrl}/bootstrap-audio`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      sessionId: session.sessionId,
      peerId: connection.peerId,
      audio: {
        sampleRate: snapshot.sampleRate,
        pcm16Base64: snapshot.pcm16Base64,
      },
    }),
  });
  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(`bootstrap audio failed: ${response.status} ${errorText}`);
  }
  connection.captureMode = snapshot.captureMode;
}

async function waitForConnectionReady(connection, timeoutMs = CONNECTION_READY_TIMEOUT_MS) {
  if (!connection || connection.stopped) {
    return false;
  }
  if (connection.pc && connection.pc.connectionState === "connected") {
    return true;
  }
  try {
    await Promise.race([connection.connectionReadyPromise, delay(timeoutMs).then(() => false)]);
  } catch (_) {
    return false;
  }
  return connection.pc ? connection.pc.connectionState === "connected" : false;
}

async function postSessionStart(serverUrl, peerId, captureMs, inputChunkMs, avatarId) {
  const response = await fetch(`${serverUrl}/session/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ peerId, captureMs, inputChunkMs, avatarId }),
  });
  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(`session start failed: ${response.status} ${errorText}`);
  }
  return response.json();
}

async function postSessionStop(serverUrl, peerId) {
  const response = await fetch(`${serverUrl}/session/stop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ peerId }),
  });
  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(`session stop failed: ${response.status} ${errorText}`);
  }
  return response.json();
}

async function ensureMediaPipeline() {
  if (localStream && bootstrapAudioBuffer) {
    return;
  }
  localStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
    video: false,
  });
  bootstrapAudioBuffer = new BootstrapAudioBuffer(localStream);
  await bootstrapAudioBuffer.start();
  localAudio.srcObject = localStream;
}

function resetWarmupState(overrides = {}) {
  warmupState = {
    preparing: false,
    ready: false,
    error: null,
    serverUrl: null,
    inputChunkMs: null,
    bootstrapTargetMs: null,
    bufferedAudioMs: 0,
    ...overrides,
  };
  updateButtons();
}

function isWarmReadyFor(serverUrl, inputChunkMs) {
  return (
    warmupState.ready &&
    warmupState.serverUrl === serverUrl &&
    warmupState.inputChunkMs === inputChunkMs
  );
}

function isPeerSessionMissingError(error) {
  return (
    error instanceof Error &&
    error.message.includes("session start failed: 404") &&
    error.message.includes("peer session not found")
  );
}

function queueLocalCandidate(connection, candidate) {
  if (connection.stopped) {
    return;
  }
  if (!connection.signalClient) {
    connection.pendingCandidates.push(candidate);
    return;
  }
  connection.signalClient.sendCandidate(candidate).catch((error) => {
    if (!connection.stopped) {
      console.error("failed to send local candidate", error);
    }
  });
}

async function flushPendingLocalCandidates(connection) {
  const pending = [...connection.pendingCandidates];
  connection.pendingCandidates = [];
  for (const candidate of pending) {
    await connection.signalClient.sendCandidate(candidate);
  }
}

async function ensureWarmReady(serverUrl, inputChunkMs, options = {}) {
  const { silent = false } = options;
  if (isWarmReadyFor(serverUrl, inputChunkMs)) {
    return {
      connection: activeConnection,
      bootstrapTargetMs: warmupState.bootstrapTargetMs,
      bufferedAudioMs: warmupState.bufferedAudioMs,
    };
  }
  if (warmupPromise) {
    return warmupPromise;
  }

  warmupState = {
    preparing: true,
    ready: false,
    error: null,
    serverUrl,
    inputChunkMs,
    bootstrapTargetMs: null,
    bufferedAudioMs: 0,
  };
  updateButtons();
  if (!silent) {
    setStatus("Preparing warm connection...", { serverUrl, inputChunkMs });
  }

  warmupPromise = (async () => {
    let connection = null;
    try {
      connection = await ensureConnection(serverUrl);
      const connected = await waitForConnectionReady(connection, PRECONNECT_READY_TIMEOUT_MS);
      if (!connected || !connection.pc || connection.pc.connectionState !== "connected") {
        throw new Error(
          `peer connection not ready for session start (state=${
            connection.pc ? connection.pc.connectionState : "closed"
          })`
        );
      }
      const bootstrapTargetMs = computeBootstrapTargetMs(connection, inputChunkMs);
      const warmBufferReady = await bootstrapAudioBuffer.waitForDuration(bootstrapTargetMs);
      if (!warmBufferReady) {
        throw new Error(`bootstrap buffer did not reach ${bootstrapTargetMs}ms within timeout`);
      }
      const bufferedAudioMs = bootstrapAudioBuffer.currentDurationMs();
      warmupState = {
        preparing: false,
        ready: true,
        error: null,
        serverUrl,
        inputChunkMs,
        bootstrapTargetMs,
        bufferedAudioMs,
      };
      if (!silent && activeSession === null) {
        setStatus("Ready to start.", {
          peerId: connection.peerId,
          signalingMode: connection.signalingMode,
          captureMode: bootstrapAudioBuffer.mode,
          peerConnectionState: pc ? pc.connectionState : "new",
          connectedMs: connection.connectedMs,
          bootstrapTargetMs,
          bufferedAudioMs,
          inferenceWorkers: connection.iceConfig.inferenceWorkers,
          inferenceStepMs: connection.iceConfig.inferenceStepMs,
        });
      }
      updateButtons();
      return { connection, bootstrapTargetMs, bufferedAudioMs };
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error);
      if (connection && (!connection.pc || connection.pc.connectionState !== "connected")) {
        await disconnect(true).catch(() => {});
      }
      resetWarmupState({ error: errorMessage });
      if (!silent) {
        setStatus(`Warm preparation failed: ${errorMessage}`);
      }
      throw error;
    } finally {
      warmupPromise = null;
    }
  })();

  return warmupPromise;
}

async function ensureConnection(serverUrl) {
  if (
    activeConnection &&
    activeConnection.serverUrl === serverUrl &&
    pc &&
    !["closed", "failed"].includes(pc.connectionState)
  ) {
    return activeConnection;
  }
  if (activeConnection) {
    await disconnect(true);
  }

  setStatus("Requesting microphone access...");
  const iceConfig = await loadIceConfig(serverUrl);
  syncAvatarOptions(iceConfig);
  await ensureMediaPipeline();

  remoteStream = new MediaStream();
  remoteVideo.srcObject = remoteStream;

  pc = new RTCPeerConnection({
    iceServers: iceConfig.iceServers,
  });

  senderTrack = localStream.getAudioTracks()[0].clone();
  senderTrack.enabled = false;
  pc.addTrack(senderTrack, new MediaStream([senderTrack]));

  const connection = {
    pc,
    serverUrl,
    iceConfig,
    peerId: null,
    signalClient: null,
    signalingMode: "pending",
    stopped: false,
    closing: false,
    pendingCandidates: [],
    connectedMs: null,
    createdAt: performance.now(),
    captureMode: bootstrapAudioBuffer.mode,
    resolveConnectionReady: null,
  };
  connection.connectionReadyPromise = new Promise((resolve) => {
    connection.resolveConnectionReady = resolve;
  });
  activeConnection = connection;

  pc.onconnectionstatechange = () => {
    if (pc.connectionState === "connected" && connection.connectedMs === null) {
      connection.connectedMs = performance.now() - connection.createdAt;
      if (connection.resolveConnectionReady) {
        connection.resolveConnectionReady(true);
        connection.resolveConnectionReady = null;
      }
    }
    if (!connection.closing && ["failed", "disconnected", "closed"].includes(pc.connectionState)) {
      disconnect(true).catch((error) => {
        setStatus(`Disconnect failed: ${error.message}`);
      });
      return;
    }
    if (activeSession) {
        setStatus("Streaming started.", {
          peerId: connection.peerId,
          sessionId: activeSession.sessionId,
          avatarId: activeSession.avatarId,
          iceMode: iceConfig.iceMode,
          signalingMode: connection.signalingMode,
          captureMode: connection.captureMode,
        peerConnectionState: pc.connectionState,
        connectedMs: connection.connectedMs,
        bootstrapTargetMs: activeSession.bootstrapTargetMs,
      });
    } else {
        setStatus("Peer connection ready.", {
          peerId: connection.peerId,
          iceMode: iceConfig.iceMode,
          signalingMode: connection.signalingMode,
          captureMode: connection.captureMode,
          peerConnectionState: pc.connectionState,
          connectedMs: connection.connectedMs,
          reuseSupported: iceConfig.supportsSessionReuse,
          availableAvatars: iceConfig.avatars?.map((avatar) => avatar.id) || [],
        });
      }
    };

  pc.onicecandidate = (event) => {
    if (event.candidate && !shouldUseIceCandidate(event.candidate.candidate, iceConfig)) {
      return;
    }
    const candidate = event.candidate
      ? {
          candidate: event.candidate.candidate,
          sdpMid: event.candidate.sdpMid,
          sdpMLineIndex: event.candidate.sdpMLineIndex,
        }
      : null;
    queueLocalCandidate(connection, candidate);
  };

  pc.ontrack = (event) => {
    event.streams[0].getTracks().forEach((track) => {
      if (!remoteStream.getTracks().some((existing) => existing.id === track.id)) {
        remoteStream.addTrack(track);
      }
    });
    remoteVideo.play().catch(() => {});
  };

  const offer = await pc.createOffer({
    offerToReceiveAudio: true,
    offerToReceiveVideo: true,
  });
  await pc.setLocalDescription(offer);

  setStatus("Creating persistent peer connection...");
  const response = await fetch(`${serverUrl}/offer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      sdp: offer.sdp,
      type: offer.type,
      autoStartSession: false,
    }),
  });

  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(`offer failed: ${response.status} ${errorText}`);
  }

  const answer = await response.json();
  connection.peerId = answer.peerId;
  connection.signalClient = new PeerSignalClient({
    serverUrl,
    peerId: connection.peerId,
    iceConfig,
    pc,
    onError: (message, error) => {
      if (!connection.stopped) {
        console.error(message, error);
      }
    },
  });
  connection.signalingMode = await connection.signalClient.start();
  await flushPendingLocalCandidates(connection);

  await pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
  await connection.signalClient.markRemoteDescriptionReady();

  updateButtons();
  await waitForConnectionReady(connection);
  return connection;
}

async function start() {
  startBtn.disabled = true;
  const serverUrl = getServerUrlValue();
  const captureMs = Number.parseInt(captureMsInput.value, 10);
  const inputChunkMs = Number.parseInt(inputChunkMsInput.value, 10);
  const avatarId = getAvatarIdValue();

  try {
    if (!isConfiguredServerUrl(serverUrl)) {
      throw new Error("configure a valid server URL before starting");
    }
    let lastError = null;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        const warmReady = await ensureWarmReady(serverUrl, inputChunkMs, { silent: attempt > 0 });
        const connection = warmReady.connection;
        if (!connection.pc || connection.pc.connectionState !== "connected") {
          throw new Error(
            `peer connection not ready for session start (state=${
              connection.pc ? connection.pc.connectionState : "closed"
            })`
          );
        }
        const sessionPayload = await postSessionStart(
          serverUrl,
          connection.peerId,
          captureMs,
          inputChunkMs,
          avatarId
        );
        const bootstrapTargetMs = warmReady.bootstrapTargetMs;
        activeSession = {
          sessionId: sessionPayload.sessionId,
          avatarId: sessionPayload.avatarId || avatarId,
          captureMs,
          inputChunkMs,
          bootstrapTargetMs,
          stopped: false,
          bootstrapUploadTask: null,
        };
        activeSession.bootstrapUploadTask = uploadBootstrapAudio(
          connection,
          activeSession,
          bootstrapTargetMs
        ).catch((error) => {
          if (!activeSession?.stopped) {
            console.error("bootstrap audio upload failed", error);
          }
        });
        if (senderTrack) {
          senderTrack.enabled = true;
        }
        const connected = await waitForConnectionReady(connection);
        setStatus("Streaming started.", {
          peerId: connection.peerId,
          sessionId: activeSession.sessionId,
          avatarId: activeSession.avatarId,
          captureMs,
          inputChunkMs,
          bootstrapTargetMs,
          iceMode: connection.iceConfig.iceMode,
          signalingMode: connection.signalingMode,
          captureMode: connection.captureMode,
          peerConnectionState: pc.connectionState,
          connected,
          connectedMs: connection.connectedMs,
          reconnectAttempted: attempt > 0,
        });
        updateButtons();
        return;
      } catch (error) {
        lastError = error;
        if (!isPeerSessionMissingError(error) || attempt > 0) {
          throw error;
        }
        console.warn("session start hit stale peer, rebuilding connection", error);
        await disconnect(true);
        resetWarmupState();
      }
    }
    if (lastError) {
      throw lastError;
    }
  } catch (error) {
    if (senderTrack) {
      senderTrack.enabled = false;
    }
    console.error(error);
    setStatus(`Start failed: ${error.message}`);
  } finally {
    updateButtons();
  }
}

async function stopSession() {
  const session = activeSession;
  const connection = activeConnection;
  activeSession = null;
  if (!session) {
    updateButtons();
    return;
  }
  session.stopped = true;
  if (senderTrack) {
    senderTrack.enabled = false;
  }
  try {
    if (connection?.peerId) {
      await postSessionStop(connection.serverUrl, connection.peerId);
    }
  } catch (error) {
    console.error("failed to stop session", error);
  }
  if (session.bootstrapUploadTask) {
    await session.bootstrapUploadTask.catch(() => {});
  }
  setStatus("Session stopped. Peer connection kept warm.", {
    peerId: connection?.peerId,
    avatarId: session.avatarId,
    signalingMode: connection?.signalingMode,
    captureMode: connection?.captureMode,
    peerConnectionState: pc ? pc.connectionState : "closed",
    iceMode: connection?.iceConfig?.iceMode,
  });
  if (connection && !connection.stopped) {
    ensureWarmReady(connection.serverUrl, session.inputChunkMs, { silent: true }).catch((error) => {
      console.error("failed to re-warm connection after stop", error);
    });
  }
  updateButtons();
}

async function disconnect(silent = false) {
  const connection = activeConnection;
  const session = activeSession;
  activeConnection = null;
  activeSession = null;

  if (session) {
    session.stopped = true;
  }
  if (connection) {
    connection.stopped = true;
    connection.closing = true;
  }
  if (senderTrack) {
    senderTrack.enabled = false;
  }

  if (session && connection?.peerId) {
    try {
      await postSessionStop(connection.serverUrl, connection.peerId);
    } catch (error) {
      console.error("failed to stop session during disconnect", error);
    }
  }
  if (session?.bootstrapUploadTask) {
    await session.bootstrapUploadTask.catch(() => {});
  }
  if (connection?.signalClient) {
    await connection.signalClient.close().catch(() => {});
  }

  if (pc) {
    pc.ontrack = null;
    pc.onicecandidate = null;
    pc.onconnectionstatechange = null;
    try {
      pc.close();
    } catch (_) {
      // Ignore close races during teardown.
    }
    pc = null;
  }

  if (senderTrack) {
    senderTrack.stop();
    senderTrack = null;
  }
  if (bootstrapAudioBuffer) {
    await bootstrapAudioBuffer.stop();
    bootstrapAudioBuffer = null;
  }
  if (localStream) {
    localStream.getTracks().forEach((track) => track.stop());
    localStream = null;
  }
  if (remoteStream) {
    remoteStream.getTracks().forEach((track) => track.stop());
    remoteStream = null;
  }

  localAudio.srcObject = null;
  remoteVideo.srcObject = null;
  resetWarmupState(silent ? {} : { error: "disconnected" });

  if (!silent) {
    setStatus("Disconnected");
  }
  updateButtons();
}

async function prewarmFromInputs() {
  const serverUrl = getServerUrlValue();
  const inputChunkMs = Number.parseInt(inputChunkMsInput.value, 10);
  if (!isConfiguredServerUrl(serverUrl)) {
    resetWarmupState();
    setStatus("Enter a server URL to prepare the warm connection.");
    return;
  }
  try {
    await ensureWarmReady(serverUrl, inputChunkMs);
  } catch (error) {
    console.error("warm preparation failed", error);
  }
}

function schedulePrewarmFromInputs() {
  if (warmupInputTimer !== null) {
    window.clearTimeout(warmupInputTimer);
  }
  warmupInputTimer = window.setTimeout(() => {
    warmupInputTimer = null;
    prewarmFromInputs().catch(() => {});
  }, 250);
}

startBtn.addEventListener("click", () => {
  start().catch((error) => {
    console.error(error);
    setStatus(`Start failed: ${error.message}`);
    updateButtons();
  });
});

stopBtn.addEventListener("click", () => {
  stopSession().catch((error) => {
    console.error(error);
    setStatus(`Stop failed: ${error.message}`);
    updateButtons();
  });
});

disconnectBtn.addEventListener("click", () => {
  disconnect().catch((error) => {
    console.error(error);
    setStatus(`Disconnect failed: ${error.message}`);
    updateButtons();
  });
});

serverUrlInput.addEventListener("input", schedulePrewarmFromInputs);
inputChunkMsInput.addEventListener("input", schedulePrewarmFromInputs);
captureMsInput.addEventListener("input", updateButtons);
avatarIdSelect.addEventListener("change", updateButtons);

updateButtons();
setStatus("Enter a server URL to prepare the warm connection.");
