const DEFAULT_ICE_CONFIG = {
  iceMode: "auto",
  iceServers: [{ urls: ["stun:stun.l.google.com:19302"] }],
  iceTailscaleIpv4Prefixes: ["100."],
  iceTailscaleIpv6Prefixes: [],
  sampleRate: 16000,
  captureMs: 20,
  inputChunkMs: 40,
  outputFps: 25,
  modelSliceMs: 960,
  inferenceWorkers: 1,
  inferenceStepMs: 0,
  startupPartialRatio: 0.75,
  startupMinAudioMs: 720,
  avatars: [],
  defaultAvatarId: null,
  supportsSessionReuse: false,
  supportsSignalWebSocket: false,
  signalWebSocketPath: "/ws",
};

function normalizeIceConfig(payload = {}) {
  const iceServers = Array.isArray(payload.iceServers) ? payload.iceServers : DEFAULT_ICE_CONFIG.iceServers;
  const iceTailscaleIpv4Prefixes = Array.isArray(payload.iceTailscaleIpv4Prefixes)
    ? payload.iceTailscaleIpv4Prefixes.filter(Boolean)
    : DEFAULT_ICE_CONFIG.iceTailscaleIpv4Prefixes;
  const iceTailscaleIpv6Prefixes = Array.isArray(payload.iceTailscaleIpv6Prefixes)
    ? payload.iceTailscaleIpv6Prefixes.filter(Boolean)
    : DEFAULT_ICE_CONFIG.iceTailscaleIpv6Prefixes;
  const avatars = Array.isArray(payload.avatars)
    ? payload.avatars
        .filter((avatar) => avatar && typeof avatar.id === "string" && avatar.id.trim())
        .map((avatar) => ({
          id: avatar.id.trim(),
          label:
            typeof avatar.label === "string" && avatar.label.trim()
              ? avatar.label.trim()
              : avatar.id.trim(),
          isDefault: avatar.isDefault === true,
        }))
    : DEFAULT_ICE_CONFIG.avatars;
  const defaultAvatarId =
    typeof payload.defaultAvatarId === "string" && payload.defaultAvatarId.trim()
      ? payload.defaultAvatarId.trim()
      : (avatars.find((avatar) => avatar.isDefault)?.id || avatars[0]?.id || DEFAULT_ICE_CONFIG.defaultAvatarId);

  return {
    iceMode: payload.iceMode === "tailscale" ? "tailscale" : "auto",
    iceServers,
    iceTailscaleIpv4Prefixes,
    iceTailscaleIpv6Prefixes,
    sampleRate: Number.isFinite(payload.sampleRate) ? payload.sampleRate : DEFAULT_ICE_CONFIG.sampleRate,
    captureMs: Number.isFinite(payload.captureMs) ? payload.captureMs : DEFAULT_ICE_CONFIG.captureMs,
    inputChunkMs: Number.isFinite(payload.inputChunkMs) ? payload.inputChunkMs : DEFAULT_ICE_CONFIG.inputChunkMs,
    outputFps: Number.isFinite(payload.outputFps) ? payload.outputFps : DEFAULT_ICE_CONFIG.outputFps,
    modelSliceMs: Number.isFinite(payload.modelSliceMs) ? payload.modelSliceMs : DEFAULT_ICE_CONFIG.modelSliceMs,
    inferenceWorkers: Number.isFinite(payload.inferenceWorkers)
      ? payload.inferenceWorkers
      : DEFAULT_ICE_CONFIG.inferenceWorkers,
    inferenceStepMs: Number.isFinite(payload.inferenceStepMs)
      ? payload.inferenceStepMs
      : DEFAULT_ICE_CONFIG.inferenceStepMs,
    startupPartialRatio: Number.isFinite(payload.startupPartialRatio)
      ? payload.startupPartialRatio
      : DEFAULT_ICE_CONFIG.startupPartialRatio,
    startupMinAudioMs: Number.isFinite(payload.startupMinAudioMs)
      ? payload.startupMinAudioMs
      : DEFAULT_ICE_CONFIG.startupMinAudioMs,
    avatars,
    defaultAvatarId,
    supportsSessionReuse: payload.supportsSessionReuse === true,
    supportsSignalWebSocket: payload.supportsSignalWebSocket === true,
    signalWebSocketPath:
      typeof payload.signalWebSocketPath === "string" && payload.signalWebSocketPath.trim()
        ? payload.signalWebSocketPath
        : DEFAULT_ICE_CONFIG.signalWebSocketPath,
  };
}

export async function loadIceConfig(serverUrl) {
  try {
    const response = await fetch(`${serverUrl}/config`);
    if (!response.ok) {
      throw new Error(`config failed: ${response.status}`);
    }
    return normalizeIceConfig(await response.json());
  } catch (error) {
    console.warn("failed to load ICE config, using defaults", error);
    return { ...DEFAULT_ICE_CONFIG };
  }
}

function parseCandidateLine(candidateLine) {
  if (!candidateLine) {
    return null;
  }
  const normalized = candidateLine.startsWith("candidate:")
    ? candidateLine.slice("candidate:".length)
    : candidateLine;
  const parts = normalized.trim().split(/\s+/);
  if (parts.length < 8) {
    return null;
  }
  const typeIndex = parts.indexOf("typ");
  if (typeIndex < 0 || typeIndex + 1 >= parts.length) {
    return null;
  }
  return {
    protocol: parts[2].toLowerCase(),
    ip: parts[4].toLowerCase(),
    type: parts[typeIndex + 1].toLowerCase(),
  };
}

function ipMatchesPrefixes(ip, prefixes) {
  return prefixes.some((prefix) => ip.startsWith(prefix.toLowerCase()));
}

export function shouldUseIceCandidate(candidateLine, iceConfig) {
  if (!candidateLine) {
    return true;
  }
  if (!iceConfig || iceConfig.iceMode !== "tailscale") {
    return true;
  }
  const parsed = parseCandidateLine(candidateLine);
  if (!parsed) {
    return false;
  }
  if (parsed.protocol !== "udp" || parsed.type !== "host") {
    return false;
  }
  if (ipMatchesPrefixes(parsed.ip, iceConfig.iceTailscaleIpv4Prefixes || [])) {
    return true;
  }
  if (ipMatchesPrefixes(parsed.ip, iceConfig.iceTailscaleIpv6Prefixes || [])) {
    return true;
  }
  return false;
}
