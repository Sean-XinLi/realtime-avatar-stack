import { BootstrapAudioBuffer } from "./bootstrap_audio.js";
import { loadIceConfig, shouldUseIceCandidate } from "./ice_policy.js";
import { PeerSignalClient } from "./signal_client.js";

const remoteVideo = document.getElementById("remoteVideo");
const resultEl = document.getElementById("result");

const DEFAULT_BOOTSTRAP_FLOOR_MS = 640;
const BOOTSTRAP_SLACK_MS = 80;

const params = new URLSearchParams(window.location.search);
const config = {
  serverUrl: (params.get("serverUrl") || window.location.origin).replace(/\/$/, ""),
  captureMs: Number.parseInt(params.get("captureMs") || "20", 10),
  inputChunkMs: Number.parseInt(params.get("inputChunkMs") || "40", 10),
  timeoutMs: Number.parseInt(params.get("timeoutMs") || "20000", 10),
  sessions: Number.parseInt(params.get("sessions") || "3", 10),
  sessionHoldMs: Number.parseInt(params.get("sessionHoldMs") || "1200", 10),
  sessionGapMs: Number.parseInt(params.get("sessionGapMs") || "300", 10),
};

let pc = null;
let localStream = null;
let remoteStream = null;
let bootstrapAudioBuffer = null;
let startedAt = performance.now();
let finished = false;
let latestPresentedFrames = 0;
let frameCounterArmed = false;

const metrics = {
  serverUrl: config.serverUrl,
  captureMs: config.captureMs,
  inputChunkMs: config.inputChunkMs,
  timeoutMs: config.timeoutMs,
  sessions: config.sessions,
  sessionHoldMs: config.sessionHoldMs,
  sessionGapMs: config.sessionGapMs,
  userAgent: navigator.userAgent,
  sessionMetrics: [],
};

function sinceStart() {
  return performance.now() - startedAt;
}

function delay(ms) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function average(values) {
  const filtered = values.filter((value) => Number.isFinite(value));
  if (!filtered.length) {
    return null;
  }
  return filtered.reduce((sum, value) => sum + value, 0) / filtered.length;
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

function computeBootstrapTargetMs(iceConfig, inputChunkMs) {
  const sliceMs = Math.max(1, Number(iceConfig.modelSliceMs || 960));
  const startupMinAudioMs = getStartupMinAudioMs(iceConfig, inputChunkMs);
  const optimisticOverlapMs = Math.min(200, Math.max(0, sliceMs - startupMinAudioMs));
  return Math.min(sliceMs, Math.max(startupMinAudioMs, sliceMs - optimisticOverlapMs + BOOTSTRAP_SLACK_MS));
}

function armVideoFrameCounter() {
  if (frameCounterArmed || typeof remoteVideo.requestVideoFrameCallback !== "function") {
    return;
  }
  frameCounterArmed = true;
  const tick = () => {
    remoteVideo.requestVideoFrameCallback((_, metadata) => {
      latestPresentedFrames = metadata.presentedFrames;
      tick();
    });
  };
  tick();
}

async function waitForPresentedFramesAfter(baseline, timeoutMs) {
  const deadline = performance.now() + timeoutMs;
  while (performance.now() < deadline) {
    if (latestPresentedFrames > baseline) {
      return true;
    }
    await delay(16);
  }
  return false;
}

async function waitForConnectionReady(runState, timeoutMs = 5000) {
  if (pc && pc.connectionState === "connected") {
    return true;
  }
  try {
    await Promise.race([runState.connectionReadyPromise, delay(timeoutMs).then(() => false)]);
  } catch (_) {
    return false;
  }
  return pc ? pc.connectionState === "connected" : false;
}

async function postSessionStart(serverUrl, peerId, captureMs, inputChunkMs) {
  const response = await fetch(`${serverUrl}/session/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ peerId, captureMs, inputChunkMs }),
  });
  if (!response.ok) {
    throw new Error(`session start failed: ${response.status} ${await response.text()}`);
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
    throw new Error(`session stop failed: ${response.status} ${await response.text()}`);
  }
  return response.json();
}

async function uploadBootstrapAudio(runState, sessionMetric) {
  const snapshot = await runState.bootstrapAudioBuffer.snapshotBase64(runState.bootstrapTargetMs);
  if (!snapshot || runState.stopped) {
    return;
  }
  sessionMetric.bootstrapCapturedMs = snapshot.durationMs;
  sessionMetric.captureMode = snapshot.captureMode;
  const response = await fetch(`${runState.serverUrl}/bootstrap-audio`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      peerId: runState.peerId,
      sessionId: sessionMetric.sessionId,
      audio: {
        sampleRate: snapshot.sampleRate,
        pcm16Base64: snapshot.pcm16Base64,
      },
    }),
  });
  if (!response.ok) {
    throw new Error(`bootstrap audio failed: ${response.status} ${await response.text()}`);
  }
  const payload = await response.json();
  sessionMetric.bootstrapUploadedMs = sinceStart() - sessionMetric.sessionStartedAtMs;
  sessionMetric.bootstrapAcceptedMs = payload.bootstrapAudioMs;
}

async function fetchServerSessionStats(sessionId) {
  const response = await fetch(`${config.serverUrl}/stats?sessionId=${encodeURIComponent(sessionId)}`);
  if (!response.ok) {
    return null;
  }
  const payload = await response.json();
  return [...(payload.completed || []), ...(payload.active || [])].find(
    (entry) => entry.sessionId === sessionId
  ) || null;
}

async function waitForCompletedSessionStats(sessionId, timeoutMs = 5000) {
  const deadline = performance.now() + timeoutMs;
  while (performance.now() < deadline) {
    const entry = await fetchServerSessionStats(sessionId);
    if (entry && entry.closed) {
      return entry;
    }
    await delay(100);
  }
  return null;
}

async function cleanup(runState) {
  if (runState) {
    runState.stopped = true;
  }
  if (runState?.activeSessionId && runState?.peerId) {
    try {
      await postSessionStop(runState.serverUrl, runState.peerId);
    } catch (_) {
      // Ignore stop races during teardown.
    }
  }
  if (runState?.signalClient) {
    await runState.signalClient.close().catch(() => {});
  }
  if (pc) {
    pc.onicecandidate = null;
    pc.ontrack = null;
    pc.onconnectionstatechange = null;
    pc.getSenders().forEach((sender) => sender.track && sender.track.stop());
    try {
      pc.close();
    } catch (_) {
      // Ignore close races during teardown.
    }
    pc = null;
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
  remoteVideo.srcObject = null;
}

function buildSummary() {
  const runs = metrics.sessionMetrics;
  const cold = runs[0] || null;
  const warm = runs.slice(1);
  return {
    peerConnectedMs: metrics.connectedMs ?? null,
    coldBrowserFirstFrameMs: cold?.browserFirstFrameMs ?? null,
    warmBrowserFirstFrameAvgMs: average(warm.map((item) => item.browserFirstFrameMs)),
    coldServerFirstGeneratedFrameServedMs: cold?.serverFirstGeneratedFrameServedMs ?? null,
    warmServerFirstGeneratedFrameServedAvgMs: average(warm.map((item) => item.serverFirstGeneratedFrameServedMs)),
    coldServerFirstGeneratedFrameProducedMs: cold?.serverFirstGeneratedFrameProducedMs ?? null,
    warmServerFirstGeneratedFrameProducedAvgMs: average(warm.map((item) => item.serverFirstGeneratedFrameProducedMs)),
  };
}

async function finish(status, extra = {}, runState = null) {
  if (finished) {
    return;
  }
  finished = true;
  let cleanupError = null;
  try {
    await cleanup(runState);
  } catch (error) {
    cleanupError = String(error);
  }
  const payload = {
    status,
    ...metrics,
    summary: buildSummary(),
    ...extra,
  };
  if (cleanupError) {
    payload.cleanupError = cleanupError;
  }
  resultEl.textContent = JSON.stringify(payload, null, 2);
  console.log("BENCH_RESULT", JSON.stringify(payload));
}

async function runWarmSession(runState, sessionIndex) {
  const sessionMetric = {
    sessionIndex,
    baselinePresentedFrames: latestPresentedFrames,
    sessionStartedAtMs: sinceStart(),
    bootstrapTargetMs: runState.bootstrapTargetMs,
  };
  metrics.sessionMetrics.push(sessionMetric);

  const sessionPayload = await postSessionStart(
    runState.serverUrl,
    runState.peerId,
    config.captureMs,
    config.inputChunkMs
  );
  runState.activeSessionId = sessionPayload.sessionId;
  sessionMetric.sessionId = sessionPayload.sessionId;
  sessionMetric.sessionStartAckMs = sinceStart() - sessionMetric.sessionStartedAtMs;

  await uploadBootstrapAudio(runState, sessionMetric);

  const sawFrame = await waitForPresentedFramesAfter(sessionMetric.baselinePresentedFrames, config.timeoutMs);
  sessionMetric.browserFirstFrameMs = sawFrame ? sinceStart() - sessionMetric.sessionStartedAtMs : null;
  sessionMetric.browserFirstFrameTimeout = !sawFrame;

  const liveStats = await fetchServerSessionStats(sessionMetric.sessionId);
  if (liveStats) {
    sessionMetric.serverFirstGeneratedFrameProducedMs = liveStats.firstGeneratedFrameProducedMs;
    sessionMetric.serverFirstGeneratedFrameServedMs = liveStats.firstGeneratedFrameServedMs;
  }

  await delay(config.sessionHoldMs);
  await postSessionStop(runState.serverUrl, runState.peerId);
  runState.activeSessionId = null;

  const completedStats = await waitForCompletedSessionStats(sessionMetric.sessionId);
  if (completedStats) {
    sessionMetric.serverFirstGeneratedFrameProducedMs = completedStats.firstGeneratedFrameProducedMs;
    sessionMetric.serverFirstGeneratedFrameServedMs = completedStats.firstGeneratedFrameServedMs;
    sessionMetric.idleFramesServed = completedStats.idleFramesServed;
    sessionMetric.idleRatio = completedStats.idleRatio;
    sessionMetric.generatedFramesServed = completedStats.generatedFramesServed;
    sessionMetric.startupFastPathUsed = completedStats.startupFastPathUsed;
    sessionMetric.startupAudioMs = completedStats.startupAudioMs;
    sessionMetric.pendingAudioMs = completedStats.pendingAudioMs;
    sessionMetric.workerIndex = completedStats.workerIndex;
    sessionMetric.incrementalDispatches = completedStats.incrementalDispatches;
  }
}

async function run() {
  const runState = {
    pc: null,
    serverUrl: config.serverUrl,
    iceConfig: null,
    peerId: null,
    activeSessionId: null,
    signalClient: null,
    stopped: false,
    pendingCandidates: [],
    bootstrapAudioBuffer: null,
    bootstrapTargetMs: 0,
    connectionReadyPromise: null,
    resolveConnectionReady: null,
  };

  try {
    const iceConfig = await loadIceConfig(config.serverUrl);
    runState.iceConfig = iceConfig;
    runState.bootstrapTargetMs = computeBootstrapTargetMs(iceConfig, config.inputChunkMs);
    metrics.iceMode = iceConfig.iceMode;
    metrics.iceServers = iceConfig.iceServers;
    metrics.bootstrapTargetMs = runState.bootstrapTargetMs;
    metrics.inferenceWorkers = iceConfig.inferenceWorkers;
    metrics.inferenceStepMs = iceConfig.inferenceStepMs;

    localStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });
    metrics.mediaReadyMs = sinceStart();

    bootstrapAudioBuffer = new BootstrapAudioBuffer(localStream);
    runState.bootstrapAudioBuffer = bootstrapAudioBuffer;
    await bootstrapAudioBuffer.start();
    metrics.bootstrapBufferReadyMs = sinceStart();
    metrics.captureMode = bootstrapAudioBuffer.mode;

    remoteStream = new MediaStream();
    remoteVideo.srcObject = remoteStream;
    remoteVideo.addEventListener(
      "loadeddata",
      () => {
        if (metrics.firstVideoLoadedDataMs == null) {
          metrics.firstVideoLoadedDataMs = sinceStart();
        }
      },
      { once: true }
    );
    armVideoFrameCounter();

    pc = new RTCPeerConnection({
      iceServers: iceConfig.iceServers,
    });
    runState.pc = pc;
    runState.connectionReadyPromise = new Promise((resolve) => {
      runState.resolveConnectionReady = resolve;
    });

    pc.onconnectionstatechange = () => {
      metrics.connectionState = pc.connectionState;
      if (pc.connectionState === "connected" && metrics.connectedMs === undefined) {
        metrics.connectedMs = sinceStart();
        if (runState.resolveConnectionReady) {
          runState.resolveConnectionReady(true);
          runState.resolveConnectionReady = null;
        }
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
      if (!runState.signalClient) {
        runState.pendingCandidates.push(candidate);
        return;
      }
      runState.signalClient.sendCandidate(candidate).catch((error) => {
        if (!runState.stopped && !metrics.localCandidateError) {
          metrics.localCandidateError = String(error);
        }
      });
    };

    pc.ontrack = (event) => {
      event.streams[0].getTracks().forEach((track) => {
        if (!remoteStream.getTracks().some((existing) => existing.id === track.id)) {
          remoteStream.addTrack(track);
        }
      });
      remoteVideo.play().catch(() => {});
    };

    const senderTrack = localStream.getAudioTracks()[0].clone();
    senderTrack.enabled = false;
    pc.addTrack(senderTrack, new MediaStream([senderTrack]));

    const offer = await pc.createOffer({
      offerToReceiveAudio: true,
      offerToReceiveVideo: true,
    });
    await pc.setLocalDescription(offer);
    metrics.offerCreatedMs = sinceStart();

    const response = await fetch(`${config.serverUrl}/offer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sdp: offer.sdp,
        type: offer.type,
        autoStartSession: false,
      }),
    });
    if (!response.ok) {
      throw new Error(`offer failed: ${response.status} ${await response.text()}`);
    }
    const answer = await response.json();
    runState.peerId = answer.peerId;
    metrics.peerId = runState.peerId;

    runState.signalClient = new PeerSignalClient({
      serverUrl: runState.serverUrl,
      peerId: runState.peerId,
      iceConfig,
      pc,
      onError: (message, error) => {
        if (!runState.stopped && !metrics.signalError) {
          metrics.signalError = `${message}: ${String(error)}`;
        }
      },
    });
    metrics.signalingMode = await runState.signalClient.start();
    for (const candidate of runState.pendingCandidates) {
      await runState.signalClient.sendCandidate(candidate);
    }
    runState.pendingCandidates = [];

    await pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
    await runState.signalClient.markRemoteDescriptionReady();
    await waitForConnectionReady(runState);

    for (let sessionIndex = 1; sessionIndex <= config.sessions; sessionIndex += 1) {
      await runWarmSession(runState, sessionIndex);
      if (sessionIndex < config.sessions) {
        await delay(config.sessionGapMs);
      }
    }

    await finish("ok", {}, runState);
  } catch (error) {
    await finish(
      "error",
      { error: error instanceof Error ? error.message : String(error) },
      runState
    );
  }
}

run().catch((error) => {
  finish("error", { error: error instanceof Error ? error.message : String(error) }).catch(() => {});
});
