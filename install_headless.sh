#!/bin/bash
# VovôAmp Headless - Install Script (Hotspot + NAT + Headless WebAudio)

set -e

echo "==========================================="
echo "   Iniciando Instalação do VovôAmp Headless"
echo "   Plataforma: OrangePi 3 / Armbian"
echo "==========================================="

# 0. Configurar Hostname
sudo hostnamectl set-hostname vovoamp
grep -q "127.0.0.1 vovoamp" /etc/hosts || echo "127.0.0.1 vovoamp" | sudo tee -a /etc/hosts

# 1. Atualização e Instalação de pacotes (APT)
echo "[1/6] Instalando dependências do sistema..."
sudo apt update
sudo apt install -y \
    python3-pip python3-aiohttp \
    pulseaudio-utils bluetooth bluez bluez-tools \
    pipewire pipewire-pulse wireplumber \
    hostapd dnsmasq avahi-daemon \
    xvfb chromium \
    authbind

# Authbind para porta 80
sudo touch /etc/authbind/byport/80
sudo chmod 777 /etc/authbind/byport/80

# WirePlumber headless bluetooth
sudo mkdir -p /etc/wireplumber/wireplumber.conf.d/
sudo tee /etc/wireplumber/wireplumber.conf.d/51-bluetooth-headless.conf << 'WPEOF'
wireplumber.profiles = {
  main = {
    support.reserve-device = disabled
    monitor.alsa.reserve-device = disabled
    monitor.bluez.seat-monitoring = disabled
    support.logind = disabled
  }
}
monitor.bluez.properties = {
  bluez5.roles = [ a2dp_sink a2dp_source hsp_hs hsp_ag hfp_hf hfp_ag ]
  bluez5.codecs = [ sbc_xq sbc ]
  bluez5.enable-sbc-xq = true
  bluez5.hfphsp-backend = native
  bluez5.auto-connect = [ hfp_hf hsp_hs a2dp_sink ]
}
WPEOF

# D-Bus permissões para Bluetooth
sudo tee /etc/dbus-1/system.d/vovoamp-bluetooth.conf << 'DBUSEOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
  "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy user="ricardo">
    <allow own="org.bluez"/>
    <allow send_destination="org.bluez"/>
    <allow send_interface="org.bluez.Agent1"/>
    <allow send_interface="org.bluez.MediaEndpoint1"/>
    <allow send_interface="org.bluez.MediaPlayer1"/>
    <allow send_interface="org.bluez.Profile1"/>
    <allow send_interface="org.freedesktop.DBus.ObjectManager"/>
    <allow send_interface="org.freedesktop.DBus.Properties"/>
  </policy>
</busconfig>
DBUSEOF

# 2. Configuração do Bluetooth
echo "[2/6] Configurando Bluetooth..."
if [ -f /etc/bluetooth/main.conf ]; then
    sudo sed -i 's/#AutoEnable=false/AutoEnable=true/' /etc/bluetooth/main.conf
    sudo sed -i 's/AutoEnable=false/AutoEnable=true/' /etc/bluetooth/main.conf
    sudo sed -i 's/#DiscoverableTimeout = 0/DiscoverableTimeout = 0/' /etc/bluetooth/main.conf
    sudo sed -i 's/#PairableTimeout = 0/PairableTimeout = 0/' /etc/bluetooth/main.conf
    sudo sed -i 's/#FastConnectable = false/FastConnectable = true/' /etc/bluetooth/main.conf
fi

# Agente bluetooth automático
sudo tee /usr/local/bin/vovoamp-bt-agent.sh << 'EOF'
#!/bin/bash
bt-agent --capability=NoInputNoOutput &
EOF
sudo chmod +x /usr/local/bin/vovoamp-bt-agent.sh
sudo systemctl restart bluetooth

# 3. Configuração do Hotspot Wi-Fi e NAT Dinâmico
echo "[3/6] Configurando Hotspot Wi-Fi e Compartilhamento de Internet..."

sudo nmcli device set wlan0 managed no || true

sudo tee /etc/hostapd/hostapd.conf << 'HOEOF'
interface=wlan0
driver=nl80211
ssid=VovoAmp-5G
hw_mode=a
channel=36
ieee80211n=1
ieee80211ac=1
wmm_enabled=1
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=vovoamp123
wpa_key_mgmt=WPA-PSK
wpa_pairwise=CCMP
rsn_pairwise=CCMP
HOEOF

sudo hostapd -t /etc/hostapd/hostapd.conf 2>&1 | grep -q "Could not set channel" && \
    sudo sed -i 's/hw_mode=a/hw_mode=g/; s/channel=36/channel=6/; s/ieee80211ac=1/ieee80211ac=0/' \
    /etc/hostapd/hostapd.conf && echo "⚠️  Fallback para 2.4GHz (canal 6)" || true

sudo tee /etc/dnsmasq.conf << 'DMEOF'
interface=wlan0
dhcp-range=192.168.4.10,192.168.4.50,255.255.255.0,12h
domain=vovoamp.wifi
address=/vovoamp.wifi/192.168.4.1
server=8.8.8.8
server=8.8.4.4
DMEOF

sudo mkdir -p /etc/network/interfaces.d
sudo tee /etc/network/interfaces.d/wlan0 << 'WLANEOF'
allow-hotplug wlan0
iface wlan0 inet static
    address 192.168.4.1
    netmask 255.255.255.0
WLANEOF

# NAT dinâmico
grep -qxF 'net.ipv4.ip_forward=1' /etc/sysctl.conf || echo 'net.ipv4.ip_forward=1' | sudo tee -a /etc/sysctl.conf
sudo sysctl -w net.ipv4.ip_forward=1

sudo tee /usr/local/bin/nat-setup.sh << 'NATEOF'
#!/bin/bash
sysctl -w net.ipv4.ip_forward=1 > /dev/null
iptables -t nat -F POSTROUTING 2>/dev/null
iptables -F FORWARD 2>/dev/null
for iface in end0 eth0 usb0 $(ls /sys/class/net/ | grep enx 2>/dev/null); do
    if ip link show "$iface" 2>/dev/null | grep -q "state UP" && \
       ip addr show "$iface" 2>/dev/null | grep -q "inet "; then
        iptables -t nat -A POSTROUTING -o "$iface" -j MASQUERADE
        iptables -A FORWARD -i "$iface" -o wlan0 -m state --state RELATED,ESTABLISHED -j ACCEPT
        iptables -A FORWARD -i wlan0 -o "$iface" -j ACCEPT
        echo "NAT ativo via $iface"
        break
    fi
done
NATEOF
sudo chmod +x /usr/local/bin/nat-setup.sh
sudo /usr/local/bin/nat-setup.sh

echo 'ACTION=="add", SUBSYSTEM=="net", RUN+="/usr/local/bin/nat-setup.sh"' | sudo tee /etc/udev/rules.d/99-vovoamp-nat.rules
sudo udevadm control --reload-rules

sudo systemctl unmask hostapd
sudo systemctl enable hostapd dnsmasq

# 4. Preparação das pastas
echo "[4/6] Preparando diretórios (/opt/vovoamp)..."
sudo mkdir -p /opt/vovoamp
sudo chown -R $USER:$USER /opt/vovoamp

# Copia os arquivos gerados do headless para /opt/vovoamp
cp ws_bridge.py /opt/vovoamp/ || true
cp ui.html /opt/vovoamp/ || true
cp batian.html /opt/vovoamp/ || true
cp rnnoise-worklet.js /opt/vovoamp/ || true
cp rnnoise-sync.js /opt/vovoamp/ || true
cp start_headless.sh /opt/vovoamp/ || true

chmod +x /opt/vovoamp/start_headless.sh

# 5. Instalação dos serviços
echo "[5/6] Instalando serviços systemd (Pipewire + Headless)..."

# Script principal de áudio (mantém o PipeWire rodando invisível)
sudo tee /usr/local/bin/vovoamp-audio-start.sh << 'EOF'
#!/bin/bash
export XDG_RUNTIME_DIR=/run/user/1000
export HOME=/home/ricardo

mkdir -p /run/user/1000/pulse
chown -R ricardo:ricardo /run/user/1000
rm -f /run/user/1000/pipewire-0 /run/user/1000/pipewire-0.lock
rm -f /run/user/1000/pipewire-0-manager /run/user/1000/pipewire-0-manager.lock
rm -f /run/user/1000/pulse/native

hciconfig hci0 noscan 2>/dev/null || true
iw dev wlan0 set power_save off 2>/dev/null || true

ip addr flush dev wlan0 2>/dev/null || true
ip addr add 192.168.4.1/24 dev wlan0 2>/dev/null || true
ip link set wlan0 up 2>/dev/null || true

pkill -u ricardo pipewire 2>/dev/null || true
pkill -u ricardo wireplumber 2>/dev/null || true
sleep 2

/usr/local/bin/vovoamp-bt-agent.sh &

runuser -u ricardo -- env XDG_RUNTIME_DIR=/run/user/1000 HOME=/home/ricardo DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket /usr/bin/pipewire &
sleep 2
runuser -u ricardo -- env XDG_RUNTIME_DIR=/run/user/1000 HOME=/home/ricardo DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket WIREPLUMBER_PROFILE=main-systemwide /usr/bin/wireplumber &
sleep 3
runuser -u ricardo -- env XDG_RUNTIME_DIR=/run/user/1000 HOME=/home/ricardo DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket /usr/bin/pipewire-pulse &

# Forçar 100Mbps para evitar falha de auto-negociação (LED amarelo/verde piscando)
ethtool -s end0 speed 100 duplex full autoneg off 2>/dev/null || true
sleep 2

# Reativar NAT após o link subir estável
/usr/local/bin/nat-setup.sh 2>/dev/null || true

sleep infinity
EOF
sudo chmod +x /usr/local/bin/vovoamp-audio-start.sh

# Serviço de Áudio
sudo tee /etc/systemd/system/vovoamp-audio.service << 'EOF'
[Unit]
Description=VovoAmp Audio Stack (PipeWire)
After=bluetooth.service dbus.service
Wants=bluetooth.service

[Service]
Type=simple
ExecStart=/usr/local/bin/vovoamp-audio-start.sh
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Serviço Headless (Chromium + ws_bridge)
sudo tee /etc/systemd/system/vovoamp-headless.service << 'EOF'
[Unit]
Description=VovôAmp Headless (Chromium + Bridge)
After=network.target vovoamp-audio.service
Wants=vovoamp-audio.service
BindsTo=vovoamp-audio.service

[Service]
Type=simple
User=ricardo
WorkingDirectory=/opt/vovoamp
ExecStartPre=/bin/bash -c 'for i in $(seq 1 30); do pactl -s unix:/run/user/1000/pulse/native info &>/dev/null && exit 0; sleep 1; done; exit 1'
ExecStart=/opt/vovoamp/start_headless.sh
Restart=always
RestartSec=15
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=PULSE_SERVER=unix:/run/user/1000/pulse/native

[Install]
WantedBy=multi-user.target
EOF

# Codecs Bluetooth
sudo -u ricardo mkdir -p /home/ricardo/.config/wireplumber/wireplumber.conf.d/
sudo -u ricardo tee /home/ricardo/.config/wireplumber/wireplumber.conf.d/10-bluetooth-lowlatency.conf << 'EOF'
monitor.bluez.properties = {
    bluez5.enable-sbc-xq = true
    bluez5.enable-msbc = true
    bluez5.enable-hw-volume = true
    bluez5.codecs = [ sbc_xq sbc ]
}
EOF

# 6. Finalização
echo "[6/6] Ativando e iniciando serviços..."
sudo systemctl daemon-reload
sudo systemctl enable avahi-daemon
sudo systemctl enable vovoamp-audio.service
sudo systemctl enable vovoamp-headless.service
sudo systemctl restart avahi-daemon
sudo systemctl restart vovoamp-audio.service
sudo systemctl restart vovoamp-headless.service

sudo nmcli device set end0 managed yes
sudo nmcli device connect end0 || true

echo ""
echo "==========================================="
echo "   VovôAmp Headless Instalado com Sucesso! "
echo "   Conecte no Wi-Fi: VovoAmp-5G            "
echo "   Acesse: http://vovoamp.wifi             "
echo "==========================================="
