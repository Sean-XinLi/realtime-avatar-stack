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
  waitIceComplete: params.get("waitIceComplete") === "1",
};

let pc = null;
let localStream = null;
let remoteStream = null;
let bootstrapAudioBuffer = null;
let startedAt = performance.now();
let finished = false;

const metrics = {
  serverUrl: config.serverUrl,
  captureMs: config.captureMs,
  inputChunkMs: config.inputChunkMs,
  timeoutMs: config.timeoutMs,
  waitIceComplete: config.waitIceComplete,
  userAgent: navigator.userAgent,
};

function sinceStart() {
  return performance.now() - startedAt;
}

function delay(ms) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
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

function waitForIceGatheringComplete(peer) {
  if (peer.iceGatheringState === "complete") {
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    const onStateChange = () => {
      if (peer.iceGatheringState === "complete") {
        peer.removeEventListener("icegatheringstatechange", onStateChange);
        resolve();
      }
    };
    peer.addEventListener("icegatheringstatechange", onStateChange);
  });
}

function watchFirstVideoFrame() {
  return new Promise((resolve) => {
    let resolved = false;
    const finish = (meta = {}) => {
      if (resolved) {
        return;
      }
      resolved = true;
      resolve(meta);
    };

    remoteVideo.addEventListener(
      "loadeddata",
      () => {
        metrics.firstVideoLoadedDataMs = metrics.firstVideoLoadedDataMs || sinceStart();
      },
      { once: true }
    );

    if (typeof remoteVideo.requestVideoFrameCallback === "function") {
      remoteVideo.requestVideoFrameCallback((_, metadata) => {
        finish({
          presentedFrames: metadata.presentedFrames,
          expectedDisplayTime: metadata.expectedDisplayTime,
        });
      });
    }

    window.setTimeout(() => finish({ fallback: "video-frame-timeout" }), config.timeoutMs);
  });
}

async function uploadBootstrapAudio(runState) {
  const snapshot = await runState.bootstrapAudioBuffer.snapshotBase64(runState.bootstrapTargetMs);
  if (!snapshot || runState.stopped) {
    return;
  }
  metrics.bootstrapCapturedMs = snapshot.durationMs;
  metrics.captureMode = snapshot.captureMode;
  const response = await fetch(`${runState.serverUrl}/bootstrap-audio`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      sessionId: runState.sessionId,
      peerId: runState.peerId,
      audio: {
        sampleRate: snapshot.sampleRate,
        pcm16Base64: snapshot.pcm16Base64,
      },
    }),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`bootstrap audio failed: ${response.status} ${text}`);
  }
  const payload = await response.json();
  metrics.bootstrapUploadedMs = sinceStart();
  metrics.bootstrapAcceptedMs = payload.bootstrapAudioMs;
}

async function fetchServerStats(serverUrl, sessionId) {
  const response = await fetch(`${serverUrl}/stats?sessionId=${encodeURIComponent(sessionId)}`);
  if (!response.ok) {
    return null;
  }
  const payload = await response.json();
  return [...(payload.active || []), ...(payload.completed || [])].find(
    (entry) => entry.sessionId === sessionId
  ) || null;
}

async function cleanup(runState) {
  if (runState) {
    runState.stopped = true;
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
  if (runState?.sessionId) {
    const serverStats = await fetchServerStats(runState.serverUrl, runState.sessionId);
    if (serverStats) {
      metrics.serverStats = serverStats;
    }
  }
  const payload = {
    status,
    ...metrics,
    ...extra,
  };
  if (cleanupError) {
    payload.cleanupError = cleanupError;
  }
  resultEl.textContent = JSON.stringify(payload, null, 2);
  console.log("BENCH_RESULT", JSON.stringify(payload));
}

async function run() {
  const runState = {
    pc: null,
    serverUrl: config.serverUrl,
    iceConfig: null,
    peerId: null,
    sessionId: null,
    signalClient: null,
    stopped: false,
    pendingCandidates: [],
    bootstrapAudioBuffer: null,
    bootstrapTargetMs: 0,
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

    pc = new RTCPeerConnection({
      iceServers: iceConfig.iceServers,
    });
    runState.pc = pc;

    const firstFramePromise = watchFirstVideoFrame();

    pc.onconnectionstatechange = () => {
      metrics.connectionState = pc.connectionState;
      if (pc.connectionState === "connected" && metrics.connectedMs === undefined) {
        metrics.connectedMs = sinceStart();
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
    pc.addTrack(senderTrack, new MediaStream([senderTrack]));

    const offer = await pc.createOffer({
      offerToReceiveAudio: true,
      offerToReceiveVideo: true,
    });
    await pc.setLocalDescription(offer);
    metrics.offerCreatedMs = sinceStart();
    if (config.waitIceComplete) {
      await waitForIceGatheringComplete(pc);
      metrics.localIceGatheringCompleteMs = sinceStart();
    }

    const response = await fetch(`${config.serverUrl}/offer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sdp: offer.sdp,
        type: offer.type,
        captureMs: config.captureMs,
        inputChunkMs: config.inputChunkMs,
      }),
    });
    if (!response.ok) {
      throw new Error(`offer failed: ${response.status} ${await response.text()}`);
    }
    const answer = await response.json();
    runState.peerId = answer.peerId;
    runState.sessionId = answer.sessionId;
    metrics.peerId = runState.peerId;
    metrics.sessionId = runState.sessionId;
    metrics.offerResponseMs = sinceStart();

    if (!runState.sessionId) {
      throw new Error("offer response missing sessionId");
    }

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

    runState.bootstrapUploadTask = uploadBootstrapAudio(runState);

    await pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
    await runState.signalClient.markRemoteDescriptionReady();
    metrics.remoteDescriptionSetMs = sinceStart();

    const firstFrame = await firstFramePromise;
    metrics.firstFrameMs = sinceStart();
    metrics.firstFrameMeta = firstFrame;

    await runState.bootstrapUploadTask;
    await delay(500);
    const serverStats = await fetchServerStats(runState.serverUrl, runState.sessionId);
    if (serverStats) {
      metrics.serverStats = serverStats;
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
