/* LowRAM AI Studio – dictation: hands microphone samples from the audio thread to the page in
 * blocks (never touches the network). "flush" sends what is left when the recording stops. */
class LowramRecorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.block = new Float32Array(2048);
    this.used = 0;
    this.port.onmessage = (ev) => {
      if (ev.data === 'flush') {
        this.port.postMessage({ samples: this.block.slice(0, this.used), last: true });
        this.used = 0;
      }
    };
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) {
      for (let i = 0; i < channel.length; i++) {
        this.block[this.used++] = channel[i];
        if (this.used === this.block.length) {
          this.port.postMessage({ samples: this.block, last: false });
          this.block = new Float32Array(2048);
          this.used = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor('lowram-recorder', LowramRecorder);
