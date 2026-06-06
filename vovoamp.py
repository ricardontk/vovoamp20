
#!/usr/bin/env python3
import os
import sys
import time

# ── PULSEAUDIO / PIPEWIRE SETUP ────────────────────────────
# Usamos o que o sistema nos der, ou o padrão do usuário ricardo (1000)
if not os.getenv("XDG_RUNTIME_DIR"):
    os.environ["XDG_RUNTIME_DIR"] = "/run/user/1000"

PULSE_SERVER = os.getenv("PULSE_SERVER", "unix:/run/user/1000/pulse/native")
os.environ["PULSE_SERVER"] = PULSE_SERVER
# ADICIONE ESTA LINHA AQUI EMBAIXO:
#os.environ["PIPEWIRE_LATENCY"] = "480/48000"
#os.environ["PIPEWIRE_QUANTUM"] = "480/48000" # Trava o motor interno do PipeWire em 10ms
#os.environ["PULSE_LATENCY_MSEC"] = "15"      # Força o servidor Pulse a não criar buffers extras

import asyncio
import json
import logging
import re
import subprocess
import numpy as np
import sounddevice as sd
import json
from aiohttp import web
import aiohttp
import ctypes
from scipy import signal as scipy_signal
import subprocess

def set_hardware_mic_volume(volume_0_to_100):
    # O comando abaixo define o volume do mixer 'Capture' para o valor em porcentagem
    # Ajuste 'Capture' para o nome que você descobriu no comando scontrols
    try:
        subprocess.run(['amixer', 'set', 'Mic', f'{int(volume_0_to_100)}%'], check=True)
    except Exception as e:
        log.error(f"Erro ao ajustar volume do hardware: {e}")
        
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("vovoamp")

SAMPLE_RATE = 48000
BLOCK_SIZE  = 480   # Latência mínima: 10ms de buffering (ideal para tempo real)
CHANNELS    = 1
DTYPE       = 'float32'

# ── ESTADO GLOBAL ──────────────────────────────────────────
state = {
    "running":      False,
    "gain":         1.0,
    "output_volume": 0.8,
    "input_volume": 1.0,
    "hp_enabled":   True,
    "hp_freq":      120,
    "comp_enabled": True,
    "gate_enabled": False,
    "gate_thresh":  -40,
    "level":        0.0,
    "input_device": None,
    "output_device": None,
    "ai_denoise":   False,
    "ai_intensity": 0.85,
}

# ── RNNOISE WRAPPER ────────────────────────────────────────
class RNNoise:
    def __init__(self):
        self.st = None
        try:
            # Tenta carregar de vários lugares comuns
            paths = [
                "/usr/local/lib/librnnoise.so.0",
                "/usr/lib/librnnoise.so.0",
                "librnnoise.so.0"
            ]
            self.lib = None
            for p in paths:
                try:
                    log.info(f"Tentando carregar IA de: {p}")
                    self.lib = ctypes.cdll.LoadLibrary(p)
                    break
                except Exception as ex:
                    log.debug(f"Falha ao carregar {p}: {ex}")
                
            if not self.lib:
                log.warning("❌ IA RNNoise: Biblioteca .so não encontrada. Use sudo ./install.sh para compilar.")
                return

            self.lib.rnnoise_process_frame.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float)]
            self.lib.rnnoise_process_frame.restype = ctypes.c_float
            self.lib.rnnoise_create.restype = ctypes.c_void_p
            self.lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
            self.st = self.lib.rnnoise_create(None)
            log.info("🚀 IA RNNoise (Redução de Ruído) ativada e pronta!")
        except Exception as e:
            log.error(f"❌ Erro crítico ao inicializar IA RNNoise: {e}")
            self.st = None

    def process(self, chunk):
        if not self.st or len(chunk) != 480: return chunk
        # Forçamos float32 para evitar chiado por incompatibilidade de bits
        scaled = (chunk * 32768.0).astype(np.float32)
        in_ptr = scaled.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        self.lib.rnnoise_process_frame(self.st, in_ptr, in_ptr)
        return scaled / 32768.0

    def __del__(self):
        if self.st: self.lib.rnnoise_destroy(self.st)

rnnoise = RNNoise()

stream      = None
ws_clients  = set()
hp_zi       = None
hp_ba       = None
lp_zi       = None
lp_ba       = None
bt_name_cache = {}

# Carrega cache de nomes ao iniciar
def load_bt_cache():
    global bt_name_cache
    try:
        if os.path.exists("/opt/vovoamp/bt_names.json"):
            with open("/opt/vovoamp/bt_names.json") as f:
                bt_name_cache = json.load(f)
    except: pass

def save_bt_cache():
    try:
        with open("/opt/vovoamp/bt_names.json", "w") as f:
            json.dump(bt_name_cache, f)
    except: pass

def preload_system_bt_names():
    """Lê todos os nomes de dispositivos conhecidos pelo BlueZ para o cache"""
    global bt_name_cache
    try:
        # 1. Tenta via bluetoothctl devices (mais rápido e seguro)
        r = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            m = re.match(r'Device ([0-9A-F:]{17})\s+(.+)', line, re.IGNORECASE)
            if m:
                mac, name = m.group(1).upper(), m.group(2).strip()
                if name and name != mac:
                    bt_name_cache[mac] = name
        
        # 2. Tenta via pactl (para fones já conectados agora)
        r = subprocess.run(["pactl", "-s", PULSE_SERVER, "list", "cards"], capture_output=True, text=True, timeout=5)
        # Procura por device.description nos cards
        blocks = r.stdout.split("Card #")
        for b in blocks:
            m_mac = re.search(r'bluez_card\.([0-9A-F_]{17})', b, re.IGNORECASE)
            m_name = re.search(r'device\.description = "(.+)"', b)
            if m_mac and m_name:
                mac = m_mac.group(1).replace("_", ":").upper()
                name = m_name.group(1)
                bt_name_cache[mac] = name
                
        save_bt_cache()
    except Exception as e:
        log.debug(f"Erro no preload BT: {e}")

load_bt_cache()
preload_system_bt_names()

# ── REDE (HOTSPOT INFO) ────────────────────────────────────
def get_network_info():
    """Lê SSID e senha do hostapd.conf e IPs das interfaces"""
    info = {"ssid": "vovoamp", "password": "", "wlan_ip": "", "eth_ip": "", "ipv6": ""}
    try:
        with open("/etc/hostapd/hostapd.conf") as f:
            for line in f:
                line = line.strip()
                if line.startswith("ssid="):
                    info["ssid"] = line.split("=", 1)[1]
                elif line.startswith("wpa_passphrase="):
                    info["password"] = line.split("=", 1)[1]
    except Exception:
        pass
    # Tenta IPv4 do Hotspot (wlan0 ou uap0)
    for iface in ["wlan0", "uap0"]:
        try:
            r = subprocess.run(["ip", "-4", "addr", "show", iface], capture_output=True, text=True, timeout=2)
            m = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', r.stdout)
            if m: 
                info["wlan_ip"] = m.group(1)
                break
        except: pass

    # Tenta IPv4 do Cabo (end0 ou eth0)
    for iface in ["end0", "eth0"]:
        try:
            r = subprocess.run(["ip", "-4", "addr", "show", iface], capture_output=True, text=True, timeout=2)
            m = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', r.stdout)
            if m: 
                info["eth_ip"] = m.group(1)
                break
        except: pass

    # Tenta IPv6 Global (procura em todas as interfaces)
    try:
        r = subprocess.run(["ip", "-6", "addr", "show"], capture_output=True, text=True, timeout=2)
        # Procura por scope global (IPv6 real de internet)
        m = re.search(r'inet6 ([0-9a-f:]+)/\d+ scope global', r.stdout)
        if m: info["ipv6"] = m.group(1)
    except: pass
    return info

def get_system_stats():
    """Lê temperatura, CPU e RAM do sistema"""
    stats = {"temp": 0, "cpu": 0, "ram": 0, "freq": 0}
    try:
        # Temperatura (miliCelsius -> Celsius)
        if os.path.exists("/sys/class/thermal/thermal_zone0/temp"):
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                stats["temp"] = round(int(f.read()) / 1000, 1)
        
        # Carga da CPU (Load Average 1min)
        with open("/proc/loadavg") as f:
            stats["cpu"] = round(float(f.read().split()[0]) * 100 / 4, 1) # 4 núcleos
        
        # Memória RAM
        with open("/proc/meminfo") as f:
            lines = f.readlines()
            total = int(lines[0].split()[1])
            # MemAvailable (mais preciso que Free)
            avail = [l for l in lines if "MemAvailable" in l]
            if avail:
                free = int(avail[0].split()[1])
            else:
                free = int(lines[1].split()[1])
            stats["ram"] = round((total - free) / total * 100, 1)
            
        # Frequência Atual (Hz -> MHz)
        if os.path.exists("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"):
            with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq") as f:
                stats["freq"] = int(int(f.read()) / 1000)
    except Exception as e:
        log.warning(f"Erro ao ler estatísticas do sistema: {e}")
    return stats

# ── FILTROS ────────────────────────────────────────────────
def make_highpass(freq, fs):
    nyq  = fs / 2.0
    norm = max(0.001, min(freq / nyq, 0.999))
    b, a = scipy_signal.butter(4, norm, btype='high')
    return b, a

def make_lowpass(freq, fs):
    nyq  = fs / 2.0
    norm = max(0.001, min(freq / nyq, 0.999))
    b, a = scipy_signal.butter(4, norm, btype='low')
    return b, a

def reset_filters():
    global hp_zi, hp_ba, lp_zi, lp_ba
    # Detecta a frequência atual (se é HFP ou normal)
    fs = 16000 if "headset" in str(state["input_device"]) or "handsfree" in str(state["input_device"]) else SAMPLE_RATE
    
    # Filtro de Graves (High-Pass)
    b_h, a_h = make_highpass(state["hp_freq"], fs)
    hp_ba = (b_h, a_h)
    hp_zi = scipy_signal.lfilter_zi(b_h, a_h) * 0.0
    
    # Filtro de Chiado/Denoiser (Low-Pass)
    # No HFP cortamos em 6kHz, no normal em 10kHz para limpar o hiss do OrangePi
    lp_freq = 6000 if fs <= 16000 else 10000
    b_l, a_l = make_lowpass(lp_freq, fs)
    lp_ba = (b_l, a_l)
    lp_zi = scipy_signal.lfilter_zi(b_l, a_l) * 0.0

def apply_filters(block):
    global hp_zi, lp_zi
    out = block.copy()
    
    # 1. RNNoise IA (Primeiro de tudo para pegar o som "puro" e dinâmico)
    # Apenas em 48kHz para melhor estabilidade
    if state["ai_denoise"] and rnnoise.st and len(out) >= 480:
        dry = out.copy()
        # RNNoise precisa de frames de 480 amostras
        for i in range(0, (len(out) // 480) * 480, 480):
            out[i:i+480] = rnnoise.process(out[i:i+480])
        
        # # Mixagem entre Som Limpo e Som Original (Intensidade)
        intensity = state["ai_intensity"]
        out = (out * intensity) + (dry * (1.0 - intensity))

    # # 2. Filtro de Graves (Corta rumble do sinal já limpo)
    if state["hp_enabled"] and hp_ba is not None:
        out, hp_zi[:] = scipy_signal.lfilter(hp_ba[0], hp_ba[1], out, zi=hp_zi)

    # # 3. Filtro de Chiado (Denoiser de alta frequência sempre ativo)
    if lp_ba is not None:
        out, lp_zi[:] = scipy_signal.lfilter(lp_ba[0], lp_ba[1], out, zi=lp_zi)

    # # 4. Compressor (Nivela o volume da voz já processada)
    if state["comp_enabled"]:
        threshold = 0.1
        ratio     = 8.0
        abs_out   = np.abs(out)
        mask      = abs_out > threshold
        out[mask] = np.sign(out[mask]) * (threshold + (abs_out[mask] - threshold) / ratio)

    # # ── GANHO E LIMITAÇÃO ──
    # # Aplicamos um ganho base 100% maior (dobro) para garantir volume máximo.
    effective_gain = state["gain"] * 2.0

    # # Compensação automática: a IA (RNNoise) reduz o volume ao remover o ruído.
    # # Aplicamos um boost extra de até 100% (+6dB) proporcional à intensidade da IA.
    if state["ai_denoise"]:
        effective_gain *= (1.0 + 1.0 * state["ai_intensity"])
    
    out = out * effective_gain

    # # ── SOFT LIMITER (Melhora qualidade em volumes altos) ──
	    # # Impede que o som "clipe" (distorça) de forma agressiva
    # # Limitador suave mais leve para processadores ARM
    out = np.where(out > 0.9, 0.9 + (out - 0.9) / 4, out)
    out = np.where(out < -0.9, -0.9 + (out + -0.9) / 4, out)

    rms = float(np.sqrt(np.mean(out ** 2)))
    db  = 20 * np.log10(rms + 1e-9)
    if state["gate_enabled"] and db < state["gate_thresh"]:
        out = out * 0.0
    
    # # Aplica volume de saída final
    out = out * state["output_volume"]
    
    out = np.clip(out, -1.0, 1.0)
    state["level"] = min(float(np.sqrt(np.mean(block ** 2))) * 6, 1.0)
    return out

#── STREAM DE ÁUDIO ────────────────────────────────────────
def audio_callback(indata, outdata, frames, time, status):
    if status:
        log.warning(f"Audio: {status}")
    mono = indata[:, 0] if indata.ndim > 1 else indata.flatten()
    processed = apply_filters(mono)
    if outdata.ndim > 1:
        outdata[:, 0] = processed
        if outdata.shape[1] > 1:
            outdata[:, 1] = processed
    else:
        outdata[:] = processed.reshape(outdata.shape)

def _friendly(name):
    n = name
    n = re.sub(r'^alsa_(input|output)\.', '', n)
    n = re.sub(r'\.stereo-fallback.*', '', n)
    n = re.sub(r'^platform-', '', n)
    return n

def get_audio_devices():
    """Lista dispositivos de áudio via pactl, com nomes amigáveis e re-tentativa automática."""
    for attempt in range(3):
        devs = []
        try:
            # 1. Mapa de MAC -> Nome via bluetoothctl (mais confiável para o nome do usuário)
            bt_names = {}
            try:
                r = subprocess.run(
                    ["bluetoothctl", "devices", "Connected"], # Lista todos conhecidos para garantir nome
                    capture_output=True, text=True, timeout=5
                )
                for line in r.stdout.strip().split("\n"):
                    m = re.match(r'Device ([0-9A-F:]{17})\s+(.+)', line, re.IGNORECASE)
                    if m:
                        mac = m.group(1).upper().replace(":", "_")
                        bt_names[mac] = m.group(2).strip()
            except Exception:
                pass

            def resolve_name(pulse_name):
                # Procura MAC no nome (ex: bluez_output.FC_2E_A9_56_D9_97.1)
                m = re.search(r'bluez_(?:sink|source|output|input)\.([0-9A-F_]{17})', pulse_name, re.IGNORECASE)
                if m:
                    mac_clean = m.group(1).upper()
                    mac_std = mac_clean.replace("_", ":")
                    # 1. Tenta nomes vindos do bluetoothctl agora
                    if mac_clean in bt_names: 
                        name = bt_names[mac_clean]
                        bt_name_cache[mac_std] = name
                        return name
                    # 2. Tenta nomes no nosso cache persistente
                    if mac_std in bt_name_cache: return bt_name_cache[mac_std]
                return _friendly(pulse_name)

            # 2. Entradas (Sources)
            r = subprocess.run(
                ["pactl", "-s", PULSE_SERVER, "list", "sources", "short"],
                capture_output=True, text=True, timeout=5, check=True
            )
            for line in r.stdout.strip().split("\n"):
                if not line.strip(): continue
                parts = line.split("\t")
                if len(parts) < 2: continue
                name = parts[1].strip()
                if "monitor" in name.lower(): continue
                friendly = resolve_name(name)
                icon = "🎧" if "bluez" in name else "🎤"
                devs.append({
                    "id": f"source:{name}",
                    "name": f"{icon} {friendly}",
                    "inputs": 2,
                    "outputs": 0,
                    "type": "input"
                })

            # 3. Saídas (Sinks)
            r = subprocess.run(
                ["pactl", "-s", PULSE_SERVER, "list", "sinks", "short"],
                capture_output=True, text=True, timeout=5, check=True
            )
            for line in r.stdout.strip().split("\n"):
                if not line.strip(): continue
                parts = line.split("\t")
                if len(parts) < 2: continue
                name = parts[1].strip()
                friendly = resolve_name(name)
                icon = "🎧" if "bluez" in name else "🔊"
                devs.append({
                    "id": f"sink:{name}",
                    "name": f"{icon} {friendly}",
                    "inputs": 0,
                    "outputs": 2,
                    "type": "output"
                })
                
        except Exception as e:
            import traceback
            err_detail = ""
            if hasattr(e, 'stderr') and e.stderr:
                err_detail = f" | Detalhe: {e.stderr}"
            log.warning(f"Tentativa {attempt+1} falhou: {e}{err_detail}")
            if attempt < 2:
                # AUTOCURA: Se o PipeWire travar, damos um choque nele
                log.info("Tentando reiniciar o sistema de som (autocura)...")
                subprocess.run(["sudo", "-u", "ricardo", "XDG_RUNTIME_DIR=/run/user/1000", "systemctl", "--user", "restart", "pipewire", "pipewire-pulse"], capture_output=True)
                time.sleep(2) # Espera o som acordar
            else:
                log.error("Todas as tentativas de listar dispositivos falharam.")
                log.debug(traceback.format_exc())
    
    if devs: return devs
    return []

def start_stream():
    global stream
    if stream: stop_stream()
    reset_filters()
    try:
        in_dev_id  = state["input_device"]
        out_dev_id = state["output_device"]

        pulse_source = None
        pulse_sink = None
        
        # Detecta se estamos usando Bluetooth HFP (Microfone) para ajustar a frequência
        using_hfp = False
        if isinstance(in_dev_id, str) and "bluez" in in_dev_id and ("headset" in in_dev_id or "handsfree" in in_dev_id):
            using_hfp = True
        
        # No modo HFP (Voz), 16kHz é muito mais estável que 48kHz
        current_rate = 16000 if using_hfp else SAMPLE_RATE
        log.info(f"Frequência de operação selecionada: {current_rate} Hz {'(Modo HFP)' if using_hfp else ''}")

        if isinstance(in_dev_id, str) and ":" in in_dev_id:
            _, pulse_source = in_dev_id.split(":", 1)
        if isinstance(out_dev_id, str) and ":" in out_dev_id:
            _, pulse_sink = out_dev_id.split(":", 1)

        # Configura PipeWire/PulseAudio
        if pulse_source:
            os.environ["PULSE_SOURCE"] = pulse_source
            subprocess.run(["pactl", "-s", PULSE_SERVER, "set-default-source", pulse_source], check=False)
        if pulse_sink:
            os.environ["PULSE_SINK"] = pulse_sink
            subprocess.run(["pactl", "-s", PULSE_SERVER, "set-default-sink", pulse_sink], check=False)

        # Busca dispositivos sounddevice
        in_idx = None
        out_idx = None
        try:
            devices = sd.query_devices()
            for i, d in enumerate(devices):
                if "pulse" in d["name"].lower():
                    in_idx = i
                    out_idx = i
                    break
        except Exception as e:
            log.warning(f"Erro ao buscar dispositivos sounddevice: {e}")

        log.info(f"Configurando Stream: In={pulse_source} Out={pulse_sink} | Rate={current_rate}")

        stream = sd.Stream(
            samplerate = current_rate,
            blocksize  = BLOCK_SIZE if current_rate == 48000 else BLOCK_SIZE // 3,
            dtype      = DTYPE,
            channels   = CHANNELS,
            device     = (in_idx, out_idx),
            callback   = audio_callback,
            latency    = 'low',
            
           #prime_output_buffers = False,
        )
        stream.start()
        state["running"] = True
        log.info("Stream iniciado ✓")
        return True, "ok"
    except Exception as e:
        state["running"] = False
        log.error(f"Erro stream: {e}")
        return False, str(e)

def stop_stream():
    global stream, _rnn_buffer
    _rnn_buffer = np.zeros(0, dtype='float32')
    if stream:
        try: stream.stop()
        except: pass
        try: stream.close()
        except: pass
        stream = None
    state["running"] = False
    state["level"]   = 0.0

# ── BLUETOOTH (AUXILIARES) ──────────────────────────────────
def strip_ansi(text):
    """Remove códigos de cores ANSI do terminal para o log ficar limpo"""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)

async def bt_cmd(cmd, timeout=8):
    try:
        proc = await asyncio.create_subprocess_exec(
            'bluetoothctl',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        cmds = f"{cmd}\nquit\n"
        stdout, _ = await asyncio.wait_for(
            proc.communicate(input=cmds.encode()), timeout=timeout
        )
        return stdout.decode(errors='replace')
    except asyncio.TimeoutError:
        return ""
    except Exception as e:
        log.error(f"bt_cmd error: {e}")
        return ""

async def bt_get_name(mac, timeout=4):
    """Resolve o nome real de um dispositivo via bluetoothctl info"""
    try:
        proc = await asyncio.create_subprocess_exec(
            'bluetoothctl',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        cmds = f"info {mac}\nquit\n"
        stdout, _ = await asyncio.wait_for(
            proc.communicate(input=cmds.encode()), timeout=timeout
        )
        output = stdout.decode(errors='replace')
        # Tenta Name primeiro
        m = re.search(r'\bName:\s+(.+)', output)
        if m:
            name = m.group(1).strip()
            if name and name != mac and not re.match(r'^[0-9A-F:_-]{17}$', name, re.IGNORECASE):
                return name
        # Fallback para Alias
        m = re.search(r'\bAlias:\s+(.+)', output)
        if m:
            alias = m.group(1).strip()
            # Alias no formato FC-2E-A9-56-D9-97 é inútil, ignorar
            if alias and not re.match(r'^[0-9A-F:_-]{17}$', alias, re.IGNORECASE):
                return alias
        return None
    except Exception:
        return None

async def bt_scan():
    """Escaneia dis	positivos BT por 10s e resolve nomes via info"""
    try:
        proc = await asyncio.create_subprocess_exec(
            'bluetoothctl',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        cmds = b"power on\nagent on\ndefault-agent\nscan on\n"
        proc.stdin.write(cmds)
        await proc.stdin.drain()
        await asyncio.sleep(20)
        proc.stdin.write(b"scan off\ndevices\nquit\n")
        await proc.stdin.drain()
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        output = stdout.decode(errors="replace")

        macs = re.findall(r'Device ([0-9A-F:]{17})', output, re.IGNORECASE)
        macs = list({m.upper() for m in macs})
        resolved = {}
        for mac in macs:
            # Tenta cache primeiro
            if mac in bt_name_cache:
                name = bt_name_cache[mac]
            else:
                name = await bt_get_name(mac)
                if name: 
                    bt_name_cache[mac] = name
                    save_bt_cache()
            resolved[mac] = name if name else mac
        return resolved
    except Exception as e:
        log.error(f"bt_scan error: {e}")
        return {}

def parse_bt_devices(output):
    devices = {}
    for m in re.finditer(r'Device ([0-9A-F:]{17})\s+(.+)', output, re.IGNORECASE):
        mac  = m.group(1).upper()
        name = m.group(2).strip()
        if name and name != mac and not re.match(r'^[0-9A-F:]{17}$', name, re.IGNORECASE):
            devices[mac] = name
        elif mac not in devices:
            devices[mac] = mac
    return [{"mac": k, "name": v} for k, v in devices.items()]

async def get_paired_devices():
    """Lista pareados com nomes resolvidos via info"""
    out = await bt_cmd("devices Paired", timeout=5)
    devices = parse_bt_devices(out)
    for d in devices:
        if d["name"] == d["mac"]:
            # Tenta cache primeiro
            if d["mac"] in bt_name_cache:
                d["name"] = bt_name_cache[d["mac"]]
            else:
                name = await bt_get_name(d["mac"])
                if name: 
                    d["name"] = name
                    bt_name_cache[d["mac"]] = name
                    save_bt_cache()
    return devices

async def get_connected_devices():
    out = await bt_cmd("devices Connected", timeout=5)
    return parse_bt_devices(out)

async def bt_get_info(mac, timeout=5):
    try:
        proc = await asyncio.create_subprocess_exec(
            'bluetoothctl', stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(input=f"info {mac}\nquit\n".encode()), timeout=timeout
        )
        output = stdout.decode(errors='replace')
        battery = None
        m = re.search(r'Battery Percentage.*?\((\d+)\)', output)
        if not m: m = re.search(r'bluetooth\.battery = "(\d+)%"', output)
        if m: battery = int(m.group(1))
        return {"battery": battery}
    except Exception:
        return {"battery": None}

async def bt_get_active_profile(mac):
    try:
        card = "bluez_card." + mac.replace(":", "_")
        r = subprocess.run(
            ["pactl", "-s", PULSE_SERVER, "list", "cards"],
            capture_output=True, text=True, timeout=5
        )
        in_card = False
        for line in r.stdout.split("\n"):
            if card in line: in_card = True
            if in_card and "Active Profile:" in line:
                profile = line.split("Active Profile:")[-1].strip()
                if "a2dp" in profile.lower(): return "a2dp"
                if "handsfree" in profile.lower() or "hfp" in profile.lower(): return "hfp"
                return profile
        return None
    except Exception:
        return None

async def get_bt_status():
    """Retorna pareados com flag connected, battery e active_profile"""
    paired    = await get_paired_devices()
    connected = await get_connected_devices()
    connected_macs = {d["mac"] for d in connected}
    for d in paired:
        d["connected"] = d["mac"] in connected_macs
        if d["connected"]:
            info = await bt_get_info(d["mac"])
            d["battery"] = info.get("battery")
            d["active_profile"] = await bt_get_active_profile(d["mac"])
        else:
            d["battery"] = None
            d["active_profile"] = None
    return paired

# ── ROTAS HTTP ─────────────────────────────────────────────
async def route_index(request):
    return web.FileResponse('/opt/vovoamp/static/index.html')

async def route_status(request):
    network = get_network_info()
    stats   = get_system_stats()
    return web.json_response({
        **state,
        "level":   round(state["level"], 3),
        "devices": get_audio_devices(),
        "network": network,
        "stats":   stats,
    })

async def route_start(request):
    ok, msg = start_stream()
    asyncio.create_task(broadcast_state())
    return web.json_response({"ok": ok, "msg": msg})

async def route_stop(request):
    stop_stream()
    asyncio.create_task(broadcast_state())
    return web.json_response({"ok": True})

async def route_set(request):
    data = await request.json()
    if "input_volume" in data:
        new_vol = float(data["input_volume"])
        state["input_volume"] = new_vol
        
        # Converte para 0-100 se o seu slider for 0.0-1.0
        volume_percent = new_vol * 100 
        
        # Chama a função que controla o hardware
        set_hardware_mic_volume(volume_percent)
        
    log.info(f"SET: {data}")
    changed_hp = False
    if "gain"          in data: state["gain"]          = float(max(1.0, min(data["gain"], 10.0)))
    if "input_volume"  in data: state["input_volume"]  = float(max(0.0, min(data["input_volume"], 1.5)))
    if "output_volume" in data: state["output_volume"] = float(max(0.0, min(data["output_volume"], 1.0)))
    if "hp_enabled"    in data: state["hp_enabled"]    = bool(data["hp_enabled"])
    if "comp_enabled" in data: state["comp_enabled"] = bool(data["comp_enabled"])
    if "gate_enabled" in data: state["gate_enabled"] = bool(data["gate_enabled"])
    if "gate_thresh"  in data: state["gate_thresh"]  = int(max(-70, min(data["gate_thresh"], -10)))
    if "input_device" in data: state["input_device"] = data["input_device"]
    if "output_device"in data: state["output_device"]= data["output_device"]
    if "ai_denoise"   in data: state["ai_denoise"]   = bool(data["ai_denoise"])
    if "ai_intensity" in data: state["ai_intensity"] = float(max(0.0, min(data["ai_intensity"], 1.0)))
    if "hp_freq" in data:
        new = int(max(60, min(data["hp_freq"], 300)))
        if new != state["hp_freq"]:
            state["hp_freq"] = new
            changed_hp = True
    if changed_hp:
        reset_filters()
    
    # Sincroniza todos os clientes conectados
    asyncio.create_task(broadcast_state())
    
    return web.json_response({"ok": True})

async def route_restart(request):
    if state["running"]:
        stop_stream()
        await asyncio.sleep(0.3)
    ok, msg = start_stream()
    asyncio.create_task(broadcast_state())
    return web.json_response({"ok": ok, "msg": msg})

# ── ROTAS BLUETOOTH ────────────────────────────────────────
async def route_bt_scan(request):
    log.info("Iniciando scan Bluetooth...")
    # Garante que o cache de nomes do sistema foi lido antes do scan
    preload_system_bt_names()
    resolved = await bt_scan()
    paired   = await get_paired_devices()
    paired_macs = {d["mac"] for d in paired}
    devices = [
        {"mac": mac, "name": name, "paired": mac in paired_macs}
        for mac, name in resolved.items()
    ]
    log.info(f"Scan concluído: {len(devices)} dispositivos")
    await broadcast_bt_status()
    return web.json_response({"ok": True, "devices": devices})

async def route_bt_paired(request):
    devices = await get_bt_status()
    return web.json_response({"ok": True, "devices": devices})

async def route_bt_pair(request):
    data = await request.json()
    mac  = data.get("mac", "")
    if not re.match(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$', mac):
        return web.json_response({"ok": False, "msg": "MAC inválido"})
    
    log.info(f"Iniciando pareamento forçado com {mac}...")
    try:
        # Comando unificado e mais robusto via bluetoothctl
        # Usamos -- para enviar comandos diretos e evitar o prompt interativo poluído
        cmd_pair = f"bluetoothctl --timeout 15 pair {mac}"
        proc = await asyncio.create_subprocess_shell(
            cmd_pair,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        out = strip_ansi(stdout.decode(errors="replace") + stderr.decode(errors="replace"))
        
        # Passo 2: Trust e Connect (Essencial para persistência)
        await bt_cmd(f"trust {mac}", timeout=5)
        out_conn = await bt_cmd(f"connect {mac}", timeout=10)
        
        full_log = out + out_conn
        ok = "successful" in full_log.lower() or "already" in full_log.lower()
        
        if ok:
            log.info(f"✓ Pareamento com {mac} concluído!")
            await broadcast_bt_status()
            return web.json_response({"ok": True, "msg": "Pareado e conectado!"})
        else:
            # Filtra apenas o erro relevante, ignorando poluição de outros devices
            relevant_err = "\n".join([l for l in out.splitlines() if mac in l or "fail" in l.lower() or "error" in l.lower()])
            log.error(f"Falha no pareamento: {relevant_err}")
            return web.json_response({"ok": False, "msg": relevant_err if relevant_err else "Falha no pareamento. Verifique o modo de busca do fone."})
            
    except Exception as e:
        log.error(f"Pair error: {e}")
        return web.json_response({"ok": False, "msg": str(e)})

async def route_bt_connect(request):
    data = await request.json()
    mac  = data.get("mac", "")
    if not re.match(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$', mac):
        return web.json_response({"ok": False, "msg": "MAC inválido"})
    
    log.info(f"Conectando a {mac} com autorização total...")
    # Garante que o dispositivo é confiável (necessário para Echo Dot)
    await bt_cmd(f"trust {mac}")
    
    # Tenta conectar
    out = await bt_cmd(f"connect {mac}", timeout=10)
    ok  = "successful" in out.lower() or "already" in out.lower()
    
    # Se falhou, tenta resetar o link
    if not ok:
        await bt_cmd(f"disconnect {mac}")
        await asyncio.sleep(1)
        out = await bt_cmd(f"connect {mac}", timeout=10)
        ok  = "successful" in out.lower() or "already" in out.lower()

    await broadcast_bt_status()
    return web.json_response({"ok": ok, "msg": "Conectado!" if ok else out[-200:]})

async def route_bt_disconnect(request):
    data = await request.json()
    mac  = data.get("mac", "")
    out  = await bt_cmd(f"disconnect {mac}", timeout=6)
    ok   = "successful" in out.lower()
    await broadcast_bt_status()
    return web.json_response({"ok": ok, "msg": "Desconectado!" if ok else out[-200:]})

async def route_bt_remove(request):
    data = await request.json()
    mac  = data.get("mac", "")
    out  = await bt_cmd(f"remove {mac}", timeout=6)
    ok   = "successful" in out.lower()
    await broadcast_bt_status()
    return web.json_response({"ok": ok, "msg": "Removido!" if ok else out[-200:]})

async def route_bt_profile(request):
    """POST /bt/profile — alterna perfil via pactl (melhor para PipeWire)"""
    data = await request.json()
    mac = data.get("mac", "")
    profile_type = data.get("profile", "a2dp_sink") 
    
    card_name = "bluez_card." + mac.replace(":", "_")
    log.info(f"Alterando perfil de {card_name} para tipo {profile_type}...")

    try:
        # 1. Listar perfis reais do card para achar o nome exato
        r = subprocess.run(["pactl", "-s", PULSE_SERVER, "list", "cards"], capture_output=True, text=True, timeout=5)
        
        target_card_info = ""
        for block in r.stdout.split("Card #"):
            if card_name in block:
                target_card_info = block
                break
        
        if not target_card_info:
            return web.json_response({"ok": False, "msg": "Fone não encontrado no sistema de som."})

        # 2. Achar o nome real do perfil baseado em palavras-chave (Suporte Universal)
        real_profile = None
        profiles_found = re.findall(r'^\s+([a-z0-9_-]+):', target_card_info, re.MULTILINE)
        
        log.info(f"Perfis detectados no dispositivo: {profiles_found}")

        if "a2dp" in profile_type.lower():
            # Ordem de preferência para MÚSICA
            keywords_a2dp = ["a2dp-sink", "a2dp_sink", "audio-gateway", "a2dp"]
            for kw in keywords_a2dp:
                for p in profiles_found:
                    if kw in p.lower():
                        real_profile = p
                        break
                if real_profile: break
        else:
            # Ordem de preferência para VOZ (Microfone)
            # Priorizamos mSBC (mais clareza) -> Handsfree -> Headset -> Gateway
            keywords_hfp = ["msbc", "headset-head-unit", "handsfree-head-unit", "hands-free", "hfp", "hsp", "audio-gateway"]
            for kw in keywords_hfp:
                for p in profiles_found:
                    if kw in p.lower():
                        real_profile = p
                        break
                if real_profile: break

        # Se nada foi encontrado, tenta o primeiro perfil que não seja 'off'
        if not real_profile and profiles_found:
            real_profile = [p for p in profiles_found if p != "off"][0]

        log.info(f"Perfil universal selecionado: {real_profile}")

        if not real_profile:
            real_profile = "a2dp-sink" if "a2dp" in profile_type.lower() else "headset-head-unit"

        log.info(f"Perfil real encontrado: {real_profile}")
        res = subprocess.run(["pactl", "-s", PULSE_SERVER, "set-card-profile", card_name, real_profile], capture_output=True, text=True)
        
        if res.returncode == 0:
            await broadcast_bt_status()
            return web.json_response({"ok": True})
        else:
            return web.json_response({"ok": False, "msg": res.stderr.strip()})
            
    except Exception as e:
        log.error(f"Erro ao trocar perfil: {e}")
        return web.json_response({"ok": False, "msg": str(e)})

async def broadcast_state():
    """Envia o estado atual das configurações para todos os clientes WS de forma imediata"""
    global ws_clients
    if not ws_clients:
        return
    try:
        # Criamos o pacote de sincronização
        payload = {
            "state": state,
            "stats": get_system_stats()
        }
        msg = json.dumps(payload)
        dead = set()
        for ws in ws_clients:
            try: await ws.send_str(msg)
            except: dead.add(ws)
        ws_clients -= dead
    except Exception as e:
        log.warning(f"broadcast_state error: {e}")

async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    ws_clients.add(ws)
    
    # Envia o estado inicial completo imediatamente
    try:
        await ws.send_str(json.dumps({
            "state": state,
            "bt_devices": await get_bt_status(),
            "stats": get_system_stats()
        }))
        
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.ERROR:
                break
    finally:
        ws_clients.discard(ws)
    return ws

async def broadcast_bt_status():
    global ws_clients
    if not ws_clients:
        return
    try:
        devices = await get_bt_status()
        msg = json.dumps({"bt_devices": devices})
        dead = set()
        for ws in ws_clients:
            try: await ws.send_str(msg)
            except: dead.add(ws)
        ws_clients -= dead
    except Exception as e:
        log.warning(f"broadcast_bt error: {e}")

async def broadcast_level():
    global ws_clients
    while True:
        await asyncio.sleep(0.05)
        if ws_clients and state["running"]:
            msg  = json.dumps({"level": round(state["level"], 3)})
            dead = set()
            for ws in ws_clients:
                try:
                    await ws.send_str(msg)
                except:
                    dead.add(ws)
            ws_clients -= dead

async def broadcast_stats():
    """Envia temperatura e uso de CPU periodicamente"""
    global ws_clients
    while True:
        await asyncio.sleep(2.0)
        if ws_clients:
            stats = get_system_stats()
            msg = json.dumps({"stats": stats})
            dead = set()
            for ws in ws_clients:
                try:
                    await ws.send_str(msg)
                except:
                    dead.add(ws)
            ws_clients -= dead

# ── AUTO CONNECT ───────────────────────────────────────────
async def auto_connect_bt():
    """Conecta automaticamente dispositivos pareados e trusted — igual celular."""
    await asyncio.sleep(15)
    last_connected = set()
    while True:
        try:
            paired    = await get_paired_devices()
            connected = await get_connected_devices()
            connected_macs = {d["mac"] for d in connected}

            for d in paired:
                if d["mac"] not in connected_macs:
                    log.info(f"Auto-conectando {d['name']} ({d['mac']})...")
                    out = await bt_cmd(f"connect {d['mac']}", timeout=8)
                    if "successful" in out.lower():
                        log.info(f"✓ {d['name']} conectado automaticamente!")

            # Notifica WS apenas se status mudou
            if connected_macs != last_connected:
                last_connected = connected_macs
                await broadcast_bt_status()

        except Exception as e:
            log.warning(f"Auto-connect BT error: {e}")
        await asyncio.sleep(20)

# ── APP ────────────────────────────────────────────────────
async def set_bt_volume(volume_pct=150):
    """Força volume alto em todos os sinks Bluetooth ativos"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pactl", "-s", PULSE_SERVER, "list", "sinks", "short",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        out = stdout.decode(errors='replace')
        for line in out.splitlines():
            if "bluez_output" in line:
                sink_name = line.split()[1]
                await asyncio.create_subprocess_exec(
                    "pactl", "-s", PULSE_SERVER, "set-sink-volume", sink_name, f"{volume_pct}%"
                )
                log.info(f"Volume BT {sink_name} → {volume_pct}%")
    except Exception as e:
        log.warning(f"set_bt_volume error: {e}")

async def bt_volume_watcher():
    """Monitora novos sinks BT e força volume alto"""
    known = set()
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "pactl", "-s", PULSE_SERVER, "list", "sinks", "short",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            out = stdout.decode(errors='replace')
            current = set()
            for line in out.splitlines():
                if "bluez_output" in line:
                    sink = line.split()[1]
                    current.add(sink)
                    if sink not in known:
                        log.info(f"Novo sink BT detectado: {sink} — forçando volume 150%")
                        await asyncio.sleep(2)  # Aguarda o sink estabilizar
                        await asyncio.create_subprocess_exec(
                            "pactl", "-s", PULSE_SERVER, "set-sink-volume", sink, "150%"
                        )
            known = current
        except Exception as e:
            log.warning(f"bt_volume_watcher error: {e}")
        await asyncio.sleep(10) # Otimização: checa volume a cada 10s em vez de 5s


async def on_startup(app):
    # Garante que o Bluetooth está ligado no início
    await bt_cmd("power on")
    await set_bt_volume(150)
    asyncio.create_task(broadcast_level())
    asyncio.create_task(broadcast_stats())
    asyncio.create_task(auto_connect_bt())
    log.info("VovôAmp iniciado — http://vovoamp.wifi")

def create_app():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.router.add_get ('/',               route_index)
    app.router.add_get ('/status',         route_status)
    app.router.add_post('/start',          route_start)
    app.router.add_post('/stop',           route_stop)
    app.router.add_post('/set',            route_set)
    app.router.add_post('/restart',        route_restart)
    app.router.add_get ('/ws',             ws_handler)
    app.router.add_post('/bt/scan',        route_bt_scan)
    app.router.add_get ('/bt/paired',      route_bt_paired)
    app.router.add_post('/bt/pair',        route_bt_pair)
    app.router.add_post('/bt/connect',     route_bt_connect)
    app.router.add_post('/bt/disconnect',  route_bt_disconnect)
    app.router.add_post('/bt/remove',      route_bt_remove)
    app.router.add_post('/bt/profile',     route_bt_profile)
    app.router.add_static('/static',       '/opt/vovoamp/static')
    return app

if __name__ == "__main__":
    try:
        # Aumenta a prioridade do processo para tempo real (evita picotados)
        os.nice(-20)
    # Força o Kernel do Linux a tratar o VovôAmp como thread de Tempo Real (SCHED_RR = 2)
    # Usamos a prioridade 99 (máxima do sistema de áudio)
        SYS_sched_setscheduler = 156  # ID da syscall para Linux ARM64
        ctypes.CDLL('libc.so.6').syscall(SYS_sched_setscheduler, 0, 2, ctypes.byref(ctypes.c_int(99)))
        log.info("🚀 Prioridade máxima Real-Time (SCHED_RR) injetada no Kernel!")
    except Exception as e:
        log.warning(f"Não foi possível aplicar prioridade Real-Time: {e}")
    
    log.info("=" * 50)
    log.info("  VovôAmp v3.0 — OrangePi 3")
    log.info("=" * 50)
    app = create_app()
    web.run_app(app, host=["0.0.0.0", "::"], port=80)
