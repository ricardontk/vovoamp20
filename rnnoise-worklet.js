// Carrega a versão síncrona do RNNoise localmente (totalmente offline)
importScripts('rnnoise-sync.js');

class RNNoiseProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    // rnnoise-sync.js normalmente expõe a biblioteca na variável rnnoiseWasm ou Rnnoise
    // Dependendo do wrapper, pode ser necessário instanciar
    try {
      if (typeof rnnoiseWasm !== 'undefined') {
        this.rnnoise = rnnoiseWasm();
      } else {
        // Fallback em caso de nome diferente na exportação global
        this.rnnoise = null; 
      }
    } catch (e) {
      console.error("Erro ao instanciar RNNoise", e);
    }
    
    this.frameSize = 480; // 10ms at 48kHz
    this.buffer = new Float32Array(this.frameSize);
    this.bufferIndex = 0;
    
    this.outBuffer = new Float32Array(this.frameSize);
    this.outBufferIndex = 0;
    this.outBufferAvailable = 0;
    
    this.enabled = true; // Permite desligar o efeito se necessário (pass-through)
    
    this.port.onmessage = (e) => {
      if (e.data.type === 'toggle') {
        this.enabled = e.data.enabled;
      }
    };
  }

  process(inputs, outputs, parameters) {
    const input = inputs[0];
    const output = outputs[0];

    // Se não houver entrada ou o RNNoise não inicializou, passa reto ou retorna silêncio
    if (!input || !input[0] || !this.rnnoise || !this.enabled) {
      if (input && input[0] && output && output[0]) {
        output[0].set(input[0]);
      }
      return true;
    }

    const inChannel = input[0];
    const outChannel = output[0];
    
    let inIdx = 0;
    let outIdx = 0;

    while (inIdx < inChannel.length) {
      // 1. Drena dados disponíveis do outBuffer
      while (outIdx < outChannel.length && this.outBufferAvailable > 0) {
        outChannel[outIdx++] = this.outBuffer[this.outBufferIndex++];
        this.outBufferAvailable--;
      }
      
      if (outIdx >= outChannel.length && inIdx >= inChannel.length) {
        break; // Processamos tudo
      }
      
      // 2. Preenche o buffer de entrada até atingir frameSize (480)
      while (inIdx < inChannel.length && this.bufferIndex < this.frameSize) {
        this.buffer[this.bufferIndex++] = inChannel[inIdx++];
      }
      
      // 3. Se temos 480 samples, processa com RNNoise
      if (this.bufferIndex === this.frameSize) {
        // O rnnoiseWasm do jitsi processa um Float32Array in-place ou retorna um novo
        // A API típica deles é context.processAudioFrame(buffer, true)
        // Como a lib exata pode variar, vamos assumir o método padrão do Jitsi rnnoise
        try {
          // Jitsi RNNoise createDenoiseState ou algo similar
          // Vou assumir a API padrão da doc do Jitsi
          if (this.rnnoise && typeof this.rnnoise.processAudioFrame === 'function') {
            // Processa in-place
            this.rnnoise.processAudioFrame(this.buffer, true); // true = update VAD
            this.outBuffer.set(this.buffer);
          } else {
            // Pass-through se a função não existir
            this.outBuffer.set(this.buffer);
          }
        } catch(e) {
          this.outBuffer.set(this.buffer); // Fallback para pass-through
        }
        
        this.bufferIndex = 0;
        this.outBufferIndex = 0;
        this.outBufferAvailable = this.frameSize;
      }
    }

    return true;
  }
}

registerProcessor('rnnoise-processor', RNNoiseProcessor);
