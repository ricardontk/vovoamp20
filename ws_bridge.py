import asyncio
import json
import logging
import os
import re
import subprocess
from aiohttp import web

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ws_bridge")

if not os.getenv("XDG_RUNTIME_DIR"):
    os.environ["XDG_RUNTIME_DIR"] = "/run/user/1000"
PULSE_SERVER = os.getenv("PULSE_SERVER", "unix:/run/user/1000/pulse/native")
os.environ["PULSE_SERVER"] = PULSE_SERVER

# Estado atual para sincronizar novos clientes UI que se conectarem
current_state = {
    "gain": 30,
    "hpHz": 120,
    "hpOn": True,
    "compOn": True,
    "gateOn": False,
    "gateThresholdDB": -40,
    "running": False,
    "input_device": None,
    "output_device": None
}

pi_clients = set()
ui_clients = set()
bt_name_cache = {}

# ── BLUETOOTH FUNCS ──
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
    except Exception as e:
        logger.error(f"bt_cmd error: {e}")
        return ""

def parse_bt_devices(output):
    devices = {}
    for m in re.finditer(r'Device ([0-9A-F:]{17})\s+(.+)', output, re.IGNORECASE):
        mac  = m.group(1).upper()
        name = m.group(2).strip()
        if name and name != mac: devices[mac] = name
        elif mac not in devices: devices[mac] = mac
    return [{"mac": k, "name": v} for k, v in devices.items()]

async def get_paired_devices():
    out = await bt_cmd("devices Paired", timeout=5)
    return parse_bt_devices(out)

async def get_connected_devices():
    out = await bt_cmd("devices Connected", timeout=5)
    return parse_bt_devices(out)

async def bt_scan():
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
        await asyncio.sleep(8)
        proc.stdin.write(b"scan off\ndevices\nquit\n")
        await proc.stdin.drain()
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        output = stdout.decode(errors="replace")
        macs = re.findall(r'Device ([0-9A-F:]{17})', output, re.IGNORECASE)
        macs = list({m.upper() for m in macs})
        resolved = {}
        for mac in macs: resolved[mac] = mac # Nomes podem vir depois, no info
        return resolved
    except Exception as e:
        logger.error(f"bt_scan error: {e}")
        return {}

async def get_bt_status():
    paired    = await get_paired_devices()
    connected = await get_connected_devices()
    connected_macs = {d["mac"] for d in connected}
    for d in paired:
        d["connected"] = d["mac"] in connected_macs
    return paired

# ── AUDIO DEVICE FUNCS ──
def _friendly(name):
    n = name
    n = re.sub(r'^alsa_(input|output)\.', '', n)
    n = re.sub(r'\.stereo-fallback.*', '', n)
    n = re.sub(r'^platform-', '', n)
    return n

def get_audio_devices():
    devs = []
    try:
        r = subprocess.run(["pactl", "-s", PULSE_SERVER, "list", "sources", "short"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.strip().split("\n"):
            if not line.strip(): continue
            parts = line.split("\t")
            if len(parts) < 2: continue
            name = parts[1].strip()
            if "monitor" in name.lower(): continue
            icon = "🎧" if "bluez" in name else "🎤"
            devs.append({"id": f"source:{name}", "name": f"{icon} {_friendly(name)}", "type": "input"})
            
        r = subprocess.run(["pactl", "-s", PULSE_SERVER, "list", "sinks", "short"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.strip().split("\n"):
            if not line.strip(): continue
            parts = line.split("\t")
            if len(parts) < 2: continue
            name = parts[1].strip()
            icon = "🎧" if "bluez" in name else "🔊"
            devs.append({"id": f"sink:{name}", "name": f"{icon} {_friendly(name)}", "type": "output"})
    except Exception as e:
        logger.error(f"Erro ao listar audios: {e}")
    return devs

def set_audio_device(dev_type, dev_id):
    if not dev_id: return
    try:
        real_id = dev_id.split(":", 1)[1] if ":" in dev_id else dev_id
        if dev_type == "input":
            subprocess.run(["pactl", "-s", PULSE_SERVER, "set-default-source", real_id], check=False)
        else:
            subprocess.run(["pactl", "-s", PULSE_SERVER, "set-default-sink", real_id], check=False)
    except Exception as e:
        logger.error(f"Erro ao mudar device {dev_id}: {e}")

# ── WEBSOCKET ──
async def broadcast_ui(data):
    for ui_ws in list(ui_clients):
        if not ui_ws.closed: await ui_ws.send_json(data)

async def broadcast_pi(data):
    for pi_ws in list(pi_clients):
        if not pi_ws.closed: await pi_ws.send_json(data)

async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    client_type = request.query.get("type", "ui")
    if client_type == "pi":
        pi_clients.add(ws)
        logger.info("Cliente PI conectado.")
        await ws.send_json({"type": "state_sync", "state": current_state})
    else:
        ui_clients.add(ws)
        logger.info("Cliente UI conectado.")
        await ws.send_json({"type": "state_sync", "state": current_state})
        # Ao conectar a UI, envia os devices
        devices = get_audio_devices()
        bt_status = await get_bt_status()
        await ws.send_json({"type": "audio_devices", "devices": devices})
        await ws.send_json({"type": "bt_status", "devices": bt_status})

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                data = json.loads(msg.data)
                
                if client_type == "pi":
                    if data.get("type") in ["vu", "status", "log"]:
                        if data.get("type") == "status":
                            current_state["running"] = data.get("running", False)
                        await broadcast_ui(data)
                
                else: # Mensagens da UI
                    msg_type = data.get("type")
                    if msg_type == "config":
                        current_state.update(data.get("state", {}))
                        await broadcast_pi(data)
                        await broadcast_ui(data) # sync other UIs
                        
                    elif msg_type == "action":
                        await broadcast_pi(data)
                        
                    elif msg_type == "req_audio_devices":
                        devs = get_audio_devices()
                        await ws.send_json({"type": "audio_devices", "devices": devs})
                        
                    elif msg_type == "set_audio_device":
                        dtype = data.get("device_type")
                        did = data.get("device_id")
                        if dtype == "input": current_state["input_device"] = did
                        else: current_state["output_device"] = did
                        set_audio_device(dtype, did)
                        await broadcast_pi({"type": "action", "action": "restart"}) # Manda o Pi reiniciar audio
                        
                    elif msg_type == "bt_scan":
                        res = await bt_scan()
                        paired = await get_paired_devices()
                        pmacs = {p["mac"] for p in paired}
                        devs = [{"mac": k, "name": v, "paired": k in pmacs} for k,v in res.items()]
                        await ws.send_json({"type": "bt_scan_res", "devices": devs})
                        
                    elif msg_type == "bt_action":
                        action = data.get("action")
                        mac = data.get("mac")
                        if action == "pair":
                            await bt_cmd(f"pair {mac}\ntrust {mac}\nconnect {mac}", 15)
                        elif action == "connect":
                            await bt_cmd(f"connect {mac}")
                        elif action == "disconnect":
                            await bt_cmd(f"disconnect {mac}")
                        elif action == "forget":
                            await bt_cmd(f"remove {mac}")
                        
                        bt_status = await get_bt_status()
                        await broadcast_ui({"type": "bt_status", "devices": bt_status})
                        devs = get_audio_devices()
                        await broadcast_ui({"type": "audio_devices", "devices": devs})
                        
            elif msg.type == web.WSMsgType.ERROR:
                pass
    finally:
        if client_type == "pi" and ws in pi_clients: pi_clients.remove(ws)
        elif ws in ui_clients: ui_clients.remove(ws)

    return ws

async def index_handler(request):
    return web.FileResponse('./ui.html')

app = web.Application()
app.add_routes([
    web.get('/ws', websocket_handler),
    web.get('/', index_handler),
    web.static('/', './')
])

if __name__ == '__main__':
    port = int(os.getenv('PORT', 80))
    logger.info(f"Iniciando ponte WebSocket em http://0.0.0.0:{port}")
    web.run_app(app, host='0.0.0.0', port=port)
