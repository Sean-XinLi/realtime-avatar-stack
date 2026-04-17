import { loadIceConfig, shouldUseIceCandidate } from "./ice_policy.js";
import { PeerSignalClient } from "./signal_client.js";

const PRECONNECT_READY_TIMEOUT_MS = 5000;
const CONNECTION_READY_TIMEOUT_MS = 400;
const INITIAL_ICE_CANDIDATE_TIMEOUT_MS = 1500;
const SILENT_CARRIER_BUFFER_MS = 1000;
const SILENT_CARRIER_AMPLITUDE = 4 / 32768;

function delay(ms) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function summarizeCandidate(candidateLine) {
  if (!candidateLine) {
    return "end-of-candidates";
  }
  const normalized = candidateLine.startsWith("candidate:")
    ? candidateLine.slice("candidate:".length)
    : candidateLine;
  const parts = normalized.trim().split(/\s+/);
  const protocol = parts[2] || "unknown";
  const address = parts[4] || "unknown";
  const typeIndex = parts.indexOf("typ");
  const candidateType = typeIndex >= 0 ? parts[typeIndex + 1] || "unknown" : "unknown";
  return `${protocol}/${candidateType}/${address}`;
}

function createSilentCarrierBuffer(audioContext, durationMs = SILENT_CARRIER_BUFFER_MS) {
  const frameCount = Math.max(1, Math.round((audioContext.sampleRate * durationMs) / 1000));
  const buffer = audioContext.createBuffer(1, frameCount, audioContext.sampleRate);
  const channel = buffer.getChannelData(0);
  // Keep the uplink track alive like a microphone without producing audible output.
  for (let index = 0; index < channel.length; index += 1) {
    channel[index] = (Math.random() * 2 - 1) * SILENT_CARRIER_AMPLITUDE;
  }
  return buffer;
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

function isPeerSessionMissingError(error) {
  return (
    error instanceof Error &&
    error.message.includes("session start failed: 404") &&
    error.message.includes("peer session not found")
  );
}

export class AvatarStreamClient {
  constructor({
    remoteVideo,
    localAudio,
    onStatus,
    onRemoteConfig,
  }) {
    this.remoteVideo = remoteVideo;
    this.localAudio = localAudio;
    this.onStatus = onStatus;
    this.onRemoteConfig = onRemoteConfig;

    this.audioContext = null;
    this.uplinkDestination = null;
    this.monitorGain = null;
    this.masterGain = null;
    this.carrierGain = null;
    this.carrierSource = null;
    this.activeSource = null;
    this.permissionStream = null;

    this.pc = null;
    this.senderTrack = null;
    this.remoteStream = null;
    this.activeConnection = null;
    this.activeSession = null;
    this.playbackFinishedPromise = null;
  }

  get hasActiveConnection() {
    return this.activeConnection !== null;
  }

  get hasActiveSession() {
    return this.activeSession !== null;
  }

  async prepare({ serverUrl }) {
    const connection = await this.#ensureConnection(serverUrl);
    const connected = await this.#waitForConnectionReady(connection, PRECONNECT_READY_TIMEOUT_MS);
    if (!connected || !connection.pc || connection.pc.connectionState !== "connected") {
      throw new Error(
        `peer connection not ready for session start (state=${
          connection.pc ? connection.pc.connectionState : "closed"
        })`
      );
    }
    this.#setStatus("Peer connection ready.", {
      peerId: connection.peerId,
      signalingMode: connection.signalingMode,
      peerConnectionState: connection.pc.connectionState,
      connectedMs: connection.connectedMs,
      iceMode: connection.iceConfig.iceMode,
      hasActiveSession: this.activeSession !== null,
    });
    return {
      connection,
    };
  }

  async startSession({
    serverUrl,
    captureMs,
    inputChunkMs,
    avatarId,
  }) {
    let lastError = null;

    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        const { connection } = await this.prepare({ serverUrl });
        const normalizedAvatarId = avatarId || null;

        if (
          this.activeSession &&
          this.activeSession.captureMs === captureMs &&
          this.activeSession.inputChunkMs === inputChunkMs &&
          this.activeSession.avatarId === normalizedAvatarId
        ) {
          if (this.senderTrack) {
            this.senderTrack.enabled = true;
          }
          this.#setStatus("Persistent session already active.", {
            peerId: connection.peerId,
            sessionId: this.activeSession.sessionId,
            avatarId: this.activeSession.avatarId,
            captureMs: this.activeSession.captureMs,
            inputChunkMs: this.activeSession.inputChunkMs,
            connectedMs: connection.connectedMs,
            reconnectAttempted: attempt > 0,
          });
          return {
            connection,
            session: this.activeSession,
            reused: true,
          };
        }

        if (this.activeSession) {
          await this.stopSession();
        }

        const sessionPayload = await postSessionStart(
          serverUrl,
          connection.peerId,
          captureMs,
          inputChunkMs,
          normalizedAvatarId
        );

        this.activeSession = {
          sessionId: sessionPayload.sessionId,
          avatarId: sessionPayload.avatarId || normalizedAvatarId,
          captureMs,
          inputChunkMs,
        };

        if (this.senderTrack) {
          this.senderTrack.enabled = true;
        }

        this.#setStatus("Persistent session started.", {
          peerId: connection.peerId,
          sessionId: this.activeSession.sessionId,
          avatarId: this.activeSession.avatarId,
          captureMs,
          inputChunkMs,
          signalingMode: connection.signalingMode,
          peerConnectionState: connection.pc.connectionState,
          connectedMs: connection.connectedMs,
          iceMode: connection.iceConfig.iceMode,
          reconnectAttempted: attempt > 0,
        });

        return {
          connection,
          session: this.activeSession,
          reused: false,
        };
      } catch (error) {
        lastError = error;
        if (!isPeerSessionMissingError(error) || attempt > 0) {
          throw error;
        }
        await this.disconnect();
      }
    }

    throw lastError;
  }

  async streamAudio({
    serverUrl,
    captureMs,
    inputChunkMs,
    avatarId,
    audioUrl,
    label,
  }) {
    const audioBuffer = await this.#fetchAudioBuffer(audioUrl);
    const audioDurationMs = Math.round(audioBuffer.duration * 1000);
    this.#setStatus("Fetched synthesized audio segment.", {
      label,
      localAudioMs: audioDurationMs,
      sampleRate: audioBuffer.sampleRate,
    });
    const { connection, session, reused } = await this.startSession({
      serverUrl,
      captureMs,
      inputChunkMs,
      avatarId,
    });

    this.playbackFinishedPromise = this.#playAudioBuffer(audioBuffer);

    const connected = await this.#waitForConnectionReady(connection);
    this.#setStatus("Audio inserted into persistent session.", {
      peerId: connection.peerId,
      sessionId: session.sessionId,
      avatarId: session.avatarId,
      captureMs,
      inputChunkMs,
      connected,
      connectedMs: connection.connectedMs,
      label,
      localAudioMs: audioDurationMs,
      sessionReused: reused,
    });

    await this.playbackFinishedPromise;

    return {
      sessionId: session.sessionId,
      durationMs: audioDurationMs,
    };
  }

  async stopSession() {
    const session = this.activeSession;
    const connection = this.activeConnection;
    this.activeSession = null;
    this.#stopLocalPlayback();

    if (!session) {
      return;
    }
    if (this.senderTrack) {
      this.senderTrack.enabled = false;
    }
    if (connection?.peerId) {
      await postSessionStop(connection.serverUrl, connection.peerId).catch((error) => {
        console.error("failed to stop session", error);
      });
    }
    this.#setStatus("Persistent session stopped. Peer connection kept warm.", {
      peerId: connection?.peerId,
      avatarId: session.avatarId,
      signalingMode: connection?.signalingMode,
      peerConnectionState: this.pc ? this.pc.connectionState : "closed",
    });
  }

  async disconnect() {
    await this.stopSession().catch(() => {});
    const connection = this.activeConnection;
    this.activeConnection = null;

    if (connection) {
      connection.stopped = true;
      connection.closing = true;
    }
    if (connection?.signalClient) {
      await connection.signalClient.close().catch(() => {});
    }
    if (this.pc) {
      this.pc.ontrack = null;
      this.pc.onicecandidate = null;
      this.pc.onconnectionstatechange = null;
      try {
        this.pc.close();
      } catch (_) {
        // Ignore close races during teardown.
      }
      this.pc = null;
    }
    if (this.senderTrack) {
      this.senderTrack.enabled = false;
      this.senderTrack = null;
    }
    if (this.remoteStream) {
      this.remoteStream.getTracks().forEach((track) => track.stop());
      this.remoteStream = null;
    }
    if (this.permissionStream) {
      this.permissionStream.getTracks().forEach((track) => track.stop());
      this.permissionStream = null;
    }
    if (this.remoteVideo) {
      this.remoteVideo.srcObject = null;
    }
    this.#setStatus("Disconnected");
  }

  async #ensureAudioGraph() {
    if (this.audioContext && this.uplinkDestination && this.masterGain && this.carrierSource) {
      await this.audioContext.resume();
      return;
    }

    this.audioContext = new AudioContext();
    this.uplinkDestination = this.audioContext.createMediaStreamDestination();
    this.masterGain = this.audioContext.createGain();
    this.monitorGain = this.audioContext.createGain();
    this.carrierGain = this.audioContext.createGain();
    this.monitorGain.gain.value = 0;

    this.masterGain.connect(this.uplinkDestination);
    this.masterGain.connect(this.monitorGain);
    this.monitorGain.connect(this.audioContext.destination);
    this.carrierGain.connect(this.masterGain);

    const carrierSource = this.audioContext.createBufferSource();
    carrierSource.buffer = createSilentCarrierBuffer(this.audioContext);
    carrierSource.loop = true;
    carrierSource.connect(this.carrierGain);
    carrierSource.start();
    this.carrierSource = carrierSource;

    await this.audioContext.resume();

    if (this.localAudio) {
      this.localAudio.srcObject = this.uplinkDestination.stream;
    }
  }

  async #ensureMediaPermission() {
    if (
      this.permissionStream &&
      this.permissionStream.getAudioTracks().some((track) => track.readyState === "live")
    ) {
      return this.permissionStream;
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error("browser does not support getUserMedia, cannot prepare tailscale peer connection");
    }
    this.#setStatus("Requesting microphone access before creating the peer connection...");
    this.permissionStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });
    return this.permissionStream;
  }

  async #ensureConnection(serverUrl) {
    if (
      this.activeConnection &&
      this.activeConnection.serverUrl === serverUrl &&
      this.pc &&
      !["closed", "failed"].includes(this.pc.connectionState)
    ) {
      return this.activeConnection;
    }

    if (this.activeConnection) {
      await this.disconnect();
    }

    const iceConfig = await loadIceConfig(serverUrl);
    if (typeof this.onRemoteConfig === "function") {
      this.onRemoteConfig(iceConfig);
    }
    this.#setStatus("Preparing remote peer connection...", {
      serverUrl,
      iceMode: iceConfig.iceMode,
      captureMs: iceConfig.captureMs,
      inputChunkMs: iceConfig.inputChunkMs,
      supportsSignalWebSocket: iceConfig.supportsSignalWebSocket,
    });
    if (iceConfig.iceMode === "tailscale") {
      await this.#ensureMediaPermission();
    }

    await this.#ensureAudioGraph();

    this.remoteStream = new MediaStream();
    if (this.remoteVideo) {
      this.remoteVideo.srcObject = this.remoteStream;
    }

    this.pc = new RTCPeerConnection({
      iceServers: iceConfig.iceServers,
    });
    this.senderTrack = this.uplinkDestination.stream.getAudioTracks()[0];
    this.senderTrack.enabled = false;
    this.pc.addTrack(this.senderTrack, this.uplinkDestination.stream);

    const connection = {
      pc: this.pc,
      serverUrl,
      iceConfig,
      peerId: null,
      signalClient: null,
      signalingMode: "pending",
      stopped: false,
      closing: false,
      pendingCandidates: [],
      acceptedLocalCandidateCount: 0,
      filteredLocalCandidates: [],
      localCandidateReadyReason: null,
      connectedMs: null,
      createdAt: performance.now(),
      resolveConnectionReady: null,
      resolveLocalCandidateReady: null,
    };

    connection.connectionReadyPromise = new Promise((resolve) => {
      connection.resolveConnectionReady = resolve;
    });
    connection.localCandidateReadyPromise = new Promise((resolve) => {
      connection.resolveLocalCandidateReady = resolve;
    });
    this.activeConnection = connection;

    this.pc.onconnectionstatechange = () => {
      this.#setStatus("Peer connection state changed.", {
        peerId: connection.peerId,
        signalingMode: connection.signalingMode,
        peerConnectionState: this.pc.connectionState,
      });
      if (this.pc.connectionState === "connected" && connection.connectedMs === null) {
        connection.connectedMs = performance.now() - connection.createdAt;
        if (connection.resolveConnectionReady) {
          connection.resolveConnectionReady(true);
          connection.resolveConnectionReady = null;
        }
      }
      if (!connection.closing && ["failed", "disconnected", "closed"].includes(this.pc.connectionState)) {
        this.disconnect().catch((error) => {
          this.#setStatus(`Disconnect failed: ${error.message}`);
        });
      }
    };

    this.pc.onicecandidate = (event) => {
      if (event.candidate && !shouldUseIceCandidate(event.candidate.candidate, iceConfig)) {
        if (connection.filteredLocalCandidates.length < 5) {
          connection.filteredLocalCandidates.push(summarizeCandidate(event.candidate.candidate));
        }
        return;
      }
      const candidate = event.candidate
        ? {
            candidate: event.candidate.candidate,
            sdpMid: event.candidate.sdpMid,
            sdpMLineIndex: event.candidate.sdpMLineIndex,
          }
        : null;
      if (candidate) {
        connection.acceptedLocalCandidateCount += 1;
      }
      if (!connection.localCandidateReadyReason) {
        connection.localCandidateReadyReason = candidate ? "candidate" : "complete";
        if (connection.resolveLocalCandidateReady) {
          connection.resolveLocalCandidateReady(connection.localCandidateReadyReason);
          connection.resolveLocalCandidateReady = null;
        }
      }
      this.#queueLocalCandidate(connection, candidate);
    };

    this.pc.ontrack = (event) => {
      event.streams[0].getTracks().forEach((track) => {
        if (!this.remoteStream.getTracks().some((existing) => existing.id === track.id)) {
          this.remoteStream.addTrack(track);
        }
      });
      this.#setStatus("Remote media track received.", {
        peerId: connection.peerId,
        kind: event.track?.kind || "unknown",
        trackId: event.track?.id || null,
        remoteTrackCount: this.remoteStream.getTracks().length,
      });
      this.remoteVideo?.play().catch(() => {});
    };

    const offer = await this.pc.createOffer({
      offerToReceiveAudio: true,
      offerToReceiveVideo: true,
    });
    await this.pc.setLocalDescription(offer);
    const localCandidateReadyReason = await this.#waitForInitialLocalCandidate(connection);
    this.#setStatus("Local offer ready. Sending it to the remote stream server...", {
      localCandidateReadyReason,
      acceptedLocalCandidateCount: connection.acceptedLocalCandidateCount,
      filteredLocalCandidates: connection.filteredLocalCandidates,
      iceMode: iceConfig.iceMode,
    });
    const localDescription = this.pc.localDescription;
    if (!localDescription?.sdp || !localDescription?.type) {
      throw new Error("local offer description missing after ICE gathering");
    }
    if (iceConfig.iceMode === "tailscale" && connection.acceptedLocalCandidateCount === 0) {
      const filtered =
        connection.filteredLocalCandidates.length > 0
          ? ` filtered=${connection.filteredLocalCandidates.join(", ")}`
          : "";
      throw new Error(
        `no usable local ICE candidate found for tailscale mode (${localCandidateReadyReason || "timeout"}).` +
          `${filtered} Chrome may be hiding the Tailscale address behind mDNS, so the server never gets a valid candidate pair.`
      );
    }

    const response = await fetch(`${serverUrl}/offer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sdp: localDescription.sdp,
        type: localDescription.type,
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
      pc: this.pc,
      onError: (message, error) => {
        if (!connection.stopped) {
          console.error(message, error);
        }
      },
    });
    connection.signalingMode = await connection.signalClient.start();
    await this.#flushPendingLocalCandidates(connection);

    await this.pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
    this.#setStatus("Remote answer applied. Waiting for media flow...", {
      peerId: connection.peerId,
      signalingMode: connection.signalingMode,
      peerConnectionState: this.pc.connectionState,
    });
    await connection.signalClient.markRemoteDescriptionReady();
    await this.#waitForConnectionReady(connection);
    return connection;
  }

  async #fetchAudioBuffer(audioUrl) {
    await this.#ensureAudioGraph();
    const response = await fetch(audioUrl, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`audio fetch failed: ${response.status}`);
    }
    const arrayBuffer = await response.arrayBuffer();
    return this.audioContext.decodeAudioData(arrayBuffer);
  }

  async #playAudioBuffer(audioBuffer) {
    await this.audioContext.resume();
    this.#stopLocalPlayback();

    const source = this.audioContext.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(this.masterGain);
    this.activeSource = source;

    return new Promise((resolve) => {
      source.onended = () => {
        if (this.activeSource === source) {
          this.activeSource = null;
        }
        if (this.playbackFinishedPromise) {
          this.playbackFinishedPromise = null;
        }
        resolve();
      };
      source.start();
    });
  }

  #stopLocalPlayback() {
    if (!this.activeSource) {
      return;
    }
    try {
      this.activeSource.stop();
    } catch (_) {
      // Ignore double-stop races.
    }
    this.activeSource.disconnect();
    this.activeSource = null;
    this.playbackFinishedPromise = null;
  }

  async #waitForConnectionReady(connection, timeoutMs = CONNECTION_READY_TIMEOUT_MS) {
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

  async #waitForInitialLocalCandidate(connection, timeoutMs = INITIAL_ICE_CANDIDATE_TIMEOUT_MS) {
    if (!connection || connection.stopped) {
      return "stopped";
    }
    if (connection.localCandidateReadyReason) {
      return connection.localCandidateReadyReason;
    }
    try {
      const result = await Promise.race([
        connection.localCandidateReadyPromise,
        delay(timeoutMs).then(() => "timeout"),
      ]);
      return result || "timeout";
    } catch (_) {
      return "timeout";
    }
  }

  #queueLocalCandidate(connection, candidate) {
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

  async #flushPendingLocalCandidates(connection) {
    const pending = [...connection.pendingCandidates];
    connection.pendingCandidates = [];
    for (const candidate of pending) {
      await connection.signalClient.sendCandidate(candidate);
    }
  }

  #setStatus(message, extra = null) {
    if (typeof this.onStatus === "function") {
      this.onStatus(message, extra);
    }
  }
}
