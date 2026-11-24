class SyncLabCapture extends AudioWorkletProcessor {
  constructor({ processorOptions: { rooms } }) {
    super();
    this.rooms = rooms;
    this.factor = Math.max(1, Math.round(sampleRate / 3000));
    this.rate = sampleRate / this.factor;
    this.frames = Math.round(this.rate / 4);
    this.batch = new Float32Array(this.frames * rooms);
    this.sum = new Float32Array(rooms);
    this.previous = new Float32Array(rooms);
    this.count = 0;
    this.written = 0;
  }

  process(inputs, outputs) {
    // 所有房间从同一个 AudioContext 取样，输出保持静音，试听走独立的音量节点。
    for (let frame = 0; frame < outputs[0][0].length; frame++) {
      for (let room = 0; room < this.rooms; room++) {
        const mono = inputs[room][0];
        this.sum[room] += mono ? mono[frame] : 0;
      }
      if (++this.count < this.factor) continue;
      for (let room = 0; room < this.rooms; room++) {
        const sample = this.sum[room] / this.factor;
        this.batch[this.written++] = sample - 0.85 * this.previous[room];
        this.previous[room] = sample;
        this.sum[room] = 0;
      }
      this.count = 0;
      if (this.written === this.batch.length) {
        this.port.postMessage({ rate: this.rate, samples: this.batch }, [this.batch.buffer]);
        this.batch = new Float32Array(this.frames * this.rooms);
        this.written = 0;
      }
    }
    return true;
  }
}

registerProcessor("sync-lab-capture", SyncLabCapture);
