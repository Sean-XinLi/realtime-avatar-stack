import { shouldUseIceCandidate } from "./ice_policy.js";

const REMOTE_CANDIDATE_POLL_MS = 200;

function toSignalWebSocketUrl(serverUrl, signalWebSocketPath = "/ws", peerId = "") {
  const wsUrl = new URL(signalWebSocketPath, `${serverUrl.replace(/\/$/, "")}/`);
  wsUrl.protocol = wsUrl.protocol === "https:" ? "wss:" : "ws:";
  wsUrl.searchParams.set("peerId", peerId);
  return wsUrl.toString();
}

async function postCandidate(serverUrl, peerId, candidate) {
  const response = await fetch(`${serverUrl}/candidate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ peerId, candidate }),
  });
  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(`candidate failed: ${response.status} ${errorText}`);
  }
}

export class PeerSignalClient {
  constructor({ serverUrl, peerId, iceConfig, pc, onError = null }) {
    this.serverUrl = serverUrl.replace(/\/$/, "");
    this.peerId = peerId;
    this.iceConfig = iceConfig;
    this.pc = pc;
    this.onError = onError;
    this.closed = false;
    this.remoteDescriptionReady = false;
    this.remoteCandidatesComplete = false;
    this.pendingRemoteCandidates = [];
    this.candidateSendChain = Promise.resolve();
    this.remoteApplyChain = Promise.resolve();
    this.pollTask = null;
    this.ws = null;
    this.mode = "initializing";
  }

  async start() {
    if (this.iceConfig?.supportsSignalWebSocket) {
      try {
        await this.#connectWebSocket();
        this.mode = "websocket";
        return this.mode;
      } catch (error) {
        this.#reportError("signal websocket unavailable, falling back to HTTP polling", error);
      }
    }
    this.mode = "http";
    this.pollTask = this.#pollRemoteCandidates().catch((error) => {
      if (!this.closed) {
        this.#reportError("remote candidate polling failed", error);
      }
    });
    return this.mode;
  }

  async sendCandidate(candidate) {
    if (this.closed) {
      return;
    }
    if (this.mode === "websocket" && this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "candidate", candidate }));
      return;
    }
    this.candidateSendChain = this.candidateSendChain
      .then(() => postCandidate(this.serverUrl, this.peerId, candidate))
      .catch((error) => {
        if (!this.closed) {
          this.#reportError("failed to send local candidate", error);
        }
      });
  }

  async markRemoteDescriptionReady() {
    this.remoteDescriptionReady = true;
    await this.remoteApplyChain.catch(() => {});
    for (const candidate of this.pendingRemoteCandidates) {
      await this.pc.addIceCandidate(candidate);
    }
    this.pendingRemoteCandidates = [];
    if (this.remoteCandidatesComplete) {
      await this.pc.addIceCandidate(null);
    }
  }

  async close() {
    this.closed = true;
    if (this.ws) {
      try {
        this.ws.close();
      } catch (_) {
        // Ignore close races during teardown.
      }
      this.ws = null;
    }
    if (this.pollTask) {
      await this.pollTask.catch(() => {});
      this.pollTask = null;
    }
    await this.candidateSendChain.catch(() => {});
    await this.remoteApplyChain.catch(() => {});
  }

  async #connectWebSocket() {
    const websocketUrl = toSignalWebSocketUrl(
      this.serverUrl,
      this.iceConfig?.signalWebSocketPath || "/ws",
      this.peerId
    );
    await new Promise((resolve, reject) => {
      const ws = new WebSocket(websocketUrl);
      this.ws = ws;
      const cleanup = () => {
        ws.removeEventListener("open", handleOpen);
        ws.removeEventListener("error", handleError);
      };
      const handleOpen = () => {
        cleanup();
        resolve();
      };
      const handleError = () => {
        cleanup();
        reject(new Error("websocket connection failed"));
      };
      ws.addEventListener("open", handleOpen, { once: true });
      ws.addEventListener("error", handleError, { once: true });
    });

    this.ws.addEventListener("message", (event) => {
      this.remoteApplyChain = this.remoteApplyChain
        .then(async () => {
          const payload = JSON.parse(event.data);
          await this.#handleSignalMessage(payload);
        })
        .catch((error) => {
          if (!this.closed) {
            this.#reportError("signal websocket message handling failed", error);
          }
        });
    });
    this.ws.addEventListener("close", () => {
      if (this.closed || this.mode !== "websocket" || this.remoteCandidatesComplete) {
        return;
      }
      this.mode = "http";
      this.pollTask = this.#pollRemoteCandidates().catch((error) => {
        if (!this.closed) {
          this.#reportError("remote candidate polling failed after websocket close", error);
        }
      });
    });
  }

  async #handleSignalMessage(payload) {
    if (!payload || typeof payload !== "object") {
      return;
    }
    if (payload.type === "snapshot" || payload.type === "candidates") {
      const candidates = Array.isArray(payload.candidates) ? payload.candidates : [];
      for (const candidate of candidates) {
        await this.#applyRemoteCandidate(candidate);
      }
      if (payload.complete) {
        this.remoteCandidatesComplete = true;
        if (this.remoteDescriptionReady) {
          await this.pc.addIceCandidate(null);
        }
      }
      return;
    }
    if (payload.type === "error") {
      this.#reportError("signal websocket error", payload.error || "unknown error");
    }
  }

  async #applyRemoteCandidate(candidate) {
    const candidateLine = candidate?.candidate;
    if (!candidateLine || !shouldUseIceCandidate(candidateLine, this.iceConfig)) {
      return;
    }
    if (!this.remoteDescriptionReady) {
      this.pendingRemoteCandidates.push(candidate);
      return;
    }
    await this.pc.addIceCandidate(candidate);
  }

  async #pollRemoteCandidates() {
    let cursor = 0;
    let complete = false;
    const seen = new Set();
    while (!this.closed && !complete) {
      const response = await fetch(
        `${this.serverUrl}/candidates?${new URLSearchParams({
          peerId: this.peerId,
          cursor: String(cursor),
        }).toString()}`
      );
      if (response.status === 404 && this.closed) {
        return;
      }
      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`remote candidates failed: ${response.status} ${errorText}`);
      }
      const payload = await response.json();
      const candidates = Array.isArray(payload.candidates) ? payload.candidates : [];
      for (const candidate of candidates) {
        const key = `${candidate.candidate}|${candidate.sdpMid}|${candidate.sdpMLineIndex}`;
        if (seen.has(key)) {
          continue;
        }
        seen.add(key);
        await this.#applyRemoteCandidate(candidate);
      }
      cursor = Number.isFinite(payload.nextCursor) ? payload.nextCursor : cursor;
      complete = Boolean(payload.complete);
      if (complete) {
        this.remoteCandidatesComplete = true;
        if (this.remoteDescriptionReady) {
          await this.pc.addIceCandidate(null);
        }
        return;
      }
      await new Promise((resolve) => window.setTimeout(resolve, REMOTE_CANDIDATE_POLL_MS));
    }
  }

  #reportError(message, error) {
    if (typeof this.onError === "function") {
      this.onError(message, error);
    }
  }
}
