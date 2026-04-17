const DEFAULT_BOOTSTRAP_AUDIO_TIMEOUT_MS = 1500;
const WORKLET_NAME = "bootstrap-capture-processor";
const WORKLET_MODULE_URL = new URL("./bootstrap-worklet.js", import.meta.url);

class Int16RingBuffer {
  constructor(capacity) {
    this.capacity = Math.max(1, capacity);
    this.buffer = new Int16Array(this.capacity);
    this.size = 0;
    this.writeIndex = 0;
  }

  clear() {
    this.size = 0;
    this.writeIndex = 0;
  }

  append(samples) {
    const source = samples instanceof Int16Array ? samples : new Int16Array(samples);
    if (!source.length) {
      return;
    }
    if (source.length >= this.capacity) {
      this.buffer.set(source.subarray(source.length - this.capacity));
      this.size = this.capacity;
      this.writeIndex = 0;
      return;
    }

    const endIndex = this.writeIndex + source.length;
    if (endIndex <= this.capacity) {
      this.buffer.set(source, this.writeIndex);
    } else {
      const split = this.capacity - this.writeIndex;
      this.buffer.set(source.subarray(0, split), this.writeIndex);
      this.buffer.set(source.subarray(split), 0);
    }
    this.writeIndex = endIndex % this.capacity;
    this.size = Math.min(this.capacity, this.size + source.length);
  }

  latest(sampleCount) {
    const target = Math.min(Math.max(sampleCount, 0), this.size);
    if (target <= 0) {
      return new Int16Array(0);
    }
    const output = new Int16Array(target);
    const startIndex = (this.writeIndex - target + this.capacity) % this.capacity;
    if (startIndex + target <= this.capacity) {
      output.set(this.buffer.subarray(startIndex, startIndex + target));
      return output;
    }
    const split = this.capacity - startIndex;
    output.set(this.buffer.subarray(startIndex), 0);
    output.set(this.buffer.subarray(0, target - split), split);
    return output;
  }
}

function floatToPcm16(samples) {
  const pcm = new Int16Array(samples.length);
  for (let index = 0; index < samples.length; index += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[index]));
    pcm[index] = clamped < 0 ? Math.round(clamped * 32768) : Math.round(clamped * 32767);
  }
  return pcm;
}

function pcm16ToBase64(samples) {
  const bytes = new Uint8Array(samples.buffer, samples.byteOffset, samples.byteLength);
  let binary = "";
  const chunkSize = 0x8000;
  for (let index = 0; index < bytes.length; index += chunkSize) {
    const slice = bytes.subarray(index, Math.min(index + chunkSize, bytes.length));
    binary += String.fromCharCode(...slice);
  }
  return btoa(binary);
}

async function ensureWorkletModule(audioContext) {
  if (!audioContext.audioWorklet || typeof AudioWorkletNode !== "function") {
    throw new Error("AudioWorklet is not supported in this browser");
  }
  await audioContext.audioWorklet.addModule(WORKLET_MODULE_URL);
}

export class BootstrapAudioBuffer {
  constructor(stream, maxDurationMs = 1600) {
    this.stream = stream;
    this.maxDurationMs = maxDurationMs;
    this.audioContext = null;
    this.source = null;
    this.processor = null;
    this.sink = null;
    this.sampleRate = 0;
    this.ring = null;
    this.closed = false;
    this.mode = "uninitialized";
  }

  async start() {
    this.audioContext = new AudioContext();
    this.sampleRate = this.audioContext.sampleRate;
    this.ring = new Int16RingBuffer(
      Math.max(1, Math.round((this.sampleRate * this.maxDurationMs) / 1000))
    );
    this.source = this.audioContext.createMediaStreamSource(this.stream);
    this.sink = this.audioContext.createGain();
    this.sink.gain.value = 0;
    this.sink.connect(this.audioContext.destination);

    try {
      await ensureWorkletModule(this.audioContext);
      const processor = new AudioWorkletNode(this.audioContext, WORKLET_NAME, {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
      });
      processor.port.onmessage = (event) => {
        if (this.closed || !this.ring) {
          return;
        }
        const samples = event.data?.samples;
        if (samples) {
          this.ring.append(samples);
        }
      };
      this.processor = processor;
      this.mode = "audio-worklet";
    } catch (error) {
      console.warn("AudioWorklet bootstrap capture unavailable, falling back to ScriptProcessor", error);
      const processor = this.audioContext.createScriptProcessor(2048, 1, 1);
      processor.onaudioprocess = (event) => {
        if (this.closed || !this.ring) {
          return;
        }
        this.ring.append(floatToPcm16(event.inputBuffer.getChannelData(0)));
      };
      this.processor = processor;
      this.mode = "script-processor";
    }

    this.source.connect(this.processor);
    this.processor.connect(this.sink);
    await this.audioContext.resume();
  }

  currentDurationMs() {
    if (!this.sampleRate || !this.ring) {
      return 0;
    }
    return (this.ring.size * 1000) / this.sampleRate;
  }

  async waitForDuration(targetDurationMs, timeoutMs = DEFAULT_BOOTSTRAP_AUDIO_TIMEOUT_MS) {
    if (this.closed || !this.ring) {
      return false;
    }
    const targetSamples = Math.max(1, Math.round((this.sampleRate * targetDurationMs) / 1000));
    const deadline = performance.now() + timeoutMs;
    while (!this.closed && this.ring.size < targetSamples && performance.now() < deadline) {
      await new Promise((resolve) => {
        window.setTimeout(resolve, 20);
      });
    }
    return !this.closed && this.ring.size >= targetSamples;
  }

  async snapshotBase64(targetDurationMs, timeoutMs = DEFAULT_BOOTSTRAP_AUDIO_TIMEOUT_MS) {
    if (this.closed || !this.ring) {
      return null;
    }
    await this.waitForDuration(targetDurationMs, timeoutMs);
    if (this.ring.size <= 0) {
      return null;
    }
    const targetSamples = Math.max(1, Math.round((this.sampleRate * targetDurationMs) / 1000));
    const latest = this.ring.latest(targetSamples);
    return {
      sampleRate: this.sampleRate,
      pcm16Base64: pcm16ToBase64(latest),
      durationMs: (latest.length * 1000) / this.sampleRate,
      captureMode: this.mode,
    };
  }

  async stop() {
    this.closed = true;
    if (this.source) {
      this.source.disconnect();
      this.source = null;
    }
    if (this.processor) {
      if ("port" in this.processor && this.processor.port) {
        this.processor.port.onmessage = null;
      }
      if ("onaudioprocess" in this.processor) {
        this.processor.onaudioprocess = null;
      }
      this.processor.disconnect();
      this.processor = null;
    }
    if (this.sink) {
      this.sink.disconnect();
      this.sink = null;
    }
    if (this.audioContext) {
      await this.audioContext.close().catch(() => {});
      this.audioContext = null;
    }
    if (this.ring) {
      this.ring.clear();
      this.ring = null;
    }
  }
}
