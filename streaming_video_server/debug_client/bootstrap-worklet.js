class BootstrapCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const channels = inputs[0];
    if (!channels || channels.length === 0 || !channels[0] || channels[0].length === 0) {
      return true;
    }
    const source = channels[0];
    const pcm = new Int16Array(source.length);
    for (let index = 0; index < source.length; index += 1) {
      const clamped = Math.max(-1, Math.min(1, source[index]));
      pcm[index] = clamped < 0 ? Math.round(clamped * 32768) : Math.round(clamped * 32767);
    }
    this.port.postMessage({ samples: pcm }, [pcm.buffer]);
    return true;
  }
}

registerProcessor("bootstrap-capture-processor", BootstrapCaptureProcessor);
