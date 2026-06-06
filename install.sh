#!/bin/bash
# VovôAmp v3.3 - Install Script para OrangePi 3 (Armbian)
# Correções: SCHED_RR ARM64, ethernet, IRQ USB, RT priority, latência PipeWire

set -e

echo "==========================================="
echo "   Iniciando Instalação do VovôAmp v3.3"
echo "   Plataforma: OrangePi 3 / Armbian"
echo "==========================================="

# 0. Configurar Hostname
sudo hostnamectl set-hostname vovoamp
grep -q "127.0.0.1 vovoamp" /etc/hosts || echo "127.0.0.1 vovoamp" | sudo tee -a /etc/hosts

# 1. Atualização e Instalação de pacotes do SISTEMA (APT)
echo "[1/8] Instalando dependências do sistema (APT)..."
sudo apt update
sudo apt install -y \
    python3-pip python3-numpy python3-scipy python3-aiohttp \
    pulseaudio-utils bluetooth bluez bluez-tools \
    pipewire pipewire-pulse wireplumber \
    hostapd dnsmasq avahi-daemon \
    libportaudio2 libasound2-dev portaudio19-dev \
    cpufrequtils authbind \
    git autoconf libtool make gcc

# Compilar RNNoise (IA) já que não está no repositório
if [ ! -f /usr/local/lib/librnnoise.so.0 ]; then
    echo "Compilando RNNoise IA..."
    cd /tmp
    git clone https://github.com/xiph/rnnoise.git || true
    cd rnnoise
    ./autogen.sh
    ./configure
    make -j$(nproc)
    sudo make install
    sudo ldconfig
    cd -
fi

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

# 2. Instalação de dependências do PYTHON (PIP)
echo "[2/8] Instalando bibliotecas Python (PIP)..."
sudo pip3 install sounddevice --break-system-packages || \
pip3 install sounddevice --break-system-packages

# 3. Configuração do Bluetooth
echo "[3/8] Configurando Bluetooth para modo de alta performance..."
if [ -f /etc/bluetooth/main.conf ]; then
    sudo sed -i 's/#AutoEnable=false/AutoEnable=true/' /etc/bluetooth/main.conf
    sudo sed -i 's/AutoEnable=false/AutoEnable=true/' /etc/bluetooth/main.conf
    sudo sed -i 's/#DiscoverableTimeout = 0/DiscoverableTimeout = 0/' /etc/bluetooth/main.conf
    sudo sed -i 's/#PairableTimeout = 0/PairableTimeout = 0/' /etc/bluetooth/main.conf
    sudo sed -i 's/#FastConnectable = false/FastConnectable = true/' /etc/bluetooth/main.conf
fi

# Agente bluetooth modo "NoInputNoOutput" (aceita qualquer pareamento)
sudo tee /usr/local/bin/vovoamp-bt-agent.sh << 'EOF'
#!/bin/bash
bt-agent --capability=NoInputNoOutput &
EOF
sudo chmod +x /usr/local/bin/vovoamp-bt-agent.sh
sudo systemctl restart bluetooth

# 4. Configuração do Hotspot Wi-Fi
echo "[4/8] Configurando Hotspot Wi-Fi..."

# Desativa NM na wlan0 para não conflitar com hostapd
sudo nmcli device set wlan0 managed no || true

# Testa se o chipset suporta 5GHz, senão cai para 2.4GHz
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

# Fallback automático para 2.4GHz se 5GHz não funcionar
sudo hostapd -t /etc/hostapd/hostapd.conf 2>&1 | grep -q "Could not set channel" && \
    sudo sed -i 's/hw_mode=a/hw_mode=g/; s/channel=36/channel=6/; s/ieee80211ac=1/ieee80211ac=0/' \
    /etc/hostapd/hostapd.conf && echo "⚠️  Fallback para 2.4GHz (canal 6)" || true

# Dnsmasq
sudo tee /etc/dnsmasq.conf << 'DMEOF'
interface=wlan0
dhcp-range=192.168.4.10,192.168.4.50,255.255.255.0,12h
domain=vovoamp.wifi
address=/vovoamp.wifi/192.168.4.1
server=8.8.8.8
server=8.8.4.4
DMEOF

# IP Estático para wlan0
sudo mkdir -p /etc/network/interfaces.d
sudo tee /etc/network/interfaces.d/wlan0 << 'WLANEOF'
allow-hotplug wlan0
iface wlan0 inet static
    address 192.168.4.1
    netmask 255.255.255.0
WLANEOF

# NAT dinâmico
grep -qxF 'net.ipv4.ip_forward=1' /etc/sysctl.conf || \
    echo 'net.ipv4.ip_forward=1' | sudo tee -a /etc/sysctl.conf
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

# Reexecuta NAT quando USB tethering é plugado
echo 'ACTION=="add", SUBSYSTEM=="net", RUN+="/usr/local/bin/nat-setup.sh"' | \
    sudo tee /etc/udev/rules.d/99-vovoamp-nat.rules
sudo udevadm control --reload-rules

sudo systemctl unmask hostapd
sudo systemctl enable hostapd dnsmasq

# 5. Preparação das pastas
echo "[5/8] Preparando diretórios (/opt/vovoamp)..."
sudo mkdir -p /opt/vovoamp/static/lib
sudo chown -R $USER:$USER /opt/vovoamp

if [ -f vovoamp.py ]; then cp vovoamp.py /opt/vovoamp/; fi
if [ -f index.html ]; then cp index.html /opt/vovoamp/static/; fi

echo "📦 Baixando bibliotecas para modo offline..."
sudo -u ricardo curl -L -o /opt/vovoamp/static/lib/qrcode.min.js \
    https://raw.githubusercontent.com/davidshimjs/qrcodejs/master/qrcode.min.js || true
sudo -u ricardo curl -L -o /opt/vovoamp/static/lib/font-awesome.css \
    https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css || true

# 6. Otimizações de latência do sistema
echo "[6/8] Aplicando otimizações de latência..."

# CPU em modo performance permanente
echo 'GOVERNOR="performance"' | sudo tee /etc/default/cpufrequtils

# Parâmetros de memória (swappiness era 100 no Armbian padrão — ruim para áudio)
sudo sed -i '/vm.swappiness/d' /etc/sysctl.conf
sudo sed -i '/vm.dirty_ratio/d' /etc/sysctl.conf
sudo sed -i '/vm.dirty_background_ratio/d' /etc/sysctl.conf
sudo sed -i '/kernel.sched_rt_runtime_us/d' /etc/sysctl.conf
cat << 'SYSCTL' | sudo tee -a /etc/sysctl.conf
vm.swappiness=10
vm.dirty_ratio=4
vm.dirty_background_ratio=2
# Libera quota RT do cgroup (necessário com CONFIG_RT_GROUP_SCHED=y no kernel Armbian)
kernel.sched_rt_runtime_us=-1
SYSCTL
sudo sysctl -p

# Limites real-time para o usuário ricardo
sudo tee /etc/security/limits.d/vovoamp.conf << 'EOF'
ricardo    -    rtprio     95
ricardo    -    memlock    unlimited
ricardo    -    nice       -20
@audio     -    rtprio     95
@audio     -    memlock    unlimited
EOF
sudo usermod -aG audio ricardo

# USB autosuspend desativado para microfone USB
sudo tee /etc/udev/rules.d/90-usb-audio-nosuspend.rules << 'EOF'
ACTION=="add", SUBSYSTEM=="usb", ATTR{bDeviceClass}=="01", \
  TEST=="power/autosuspend", ATTR{power/autosuspend}="-1"
EOF

# IRQs em threads separadas (reduz jitter USB — requer reboot)
grep -q 'threadirqs' /boot/armbianEnv.txt || \
    echo 'extraargs=threadirqs' | sudo tee -a /boot/armbianEnv.txt


# 7. Instalação dos serviços
echo "[7/8] Instalando scripts de inicialização e serviços systemd..."

# Script principal de áudio
sudo tee /usr/local/bin/vovoamp-audio-start.sh << 'EOF'
#!/bin/bash
export XDG_RUNTIME_DIR=/run/user/1000
export HOME=/home/ricardo

mkdir -p /run/user/1000/pulse
chown -R ricardo:ricardo /run/user/1000
rm -f /run/user/1000/pipewire-0 /run/user/1000/pipewire-0.lock
rm -f /run/user/1000/pipewire-0-manager /run/user/1000/pipewire-0-manager.lock
rm -f /run/user/1000/pulse/native

# Bluetooth: desativa scan passivo para liberar antena
hciconfig hci0 noscan 2>/dev/null || true

# Wi-Fi: desativa power save
iw dev wlan0 set power_save off 2>/dev/null || true

# IP fixo do Hotspot
ip addr flush dev wlan0 2>/dev/null || true
ip addr add 192.168.4.1/24 dev wlan0 2>/dev/null || true
ip link set wlan0 up 2>/dev/null || true

# CPU em modo performance
echo "performance" | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor > /dev/null 2>&1 || true

# Libera quota RT do cgroup (necessário com CONFIG_RT_GROUP_SCHED=y)
echo -1 > /proc/sys/kernel/sched_rt_runtime_us 2>/dev/null || true

# Afinidade da IRQ do microfone USB → core 2 (detecta automaticamente)
USB_IRQ=$(grep "xhci-hcd:usb1" /proc/interrupts 2>/dev/null | cut -d: -f1 | tr -d ' ')
[ -n "$USB_IRQ" ] && echo 4 > /proc/irq/$USB_IRQ/smp_affinity 2>/dev/null && \
    echo "IRQ $USB_IRQ (USB audio) → core 2" || true

# Para processos anteriores
pkill -u ricardo pipewire 2>/dev/null || true
pkill -u ricardo wireplumber 2>/dev/null || true
sleep 2

# Agente de pareamento automático do Bluetooth
/usr/local/bin/vovoamp-bt-agent.sh &

# PipeWire
runuser -u ricardo -- env \
  XDG_RUNTIME_DIR=/run/user/1000 \
  HOME=/home/ricardo \
  DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket \
  /usr/bin/pipewire &
PW_PID=$!
sleep 2
chrt -r -p 80 $PW_PID 2>/dev/null && echo "pipewire ($PW_PID) → SCHED_RR 80" || true

# WirePlumber
runuser -u ricardo -- env \
  XDG_RUNTIME_DIR=/run/user/1000 \
  HOME=/home/ricardo \
  DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket \
  WIREPLUMBER_PROFILE=main-systemwide \
  /usr/bin/wireplumber &
WP_PID=$!
sleep 3
chrt -r -p 75 $WP_PID 2>/dev/null && echo "wireplumber ($WP_PID) → SCHED_RR 75" || true

# PipeWire-Pulse
runuser -u ricardo -- env \
  XDG_RUNTIME_DIR=/run/user/1000 \
  HOME=/home/ricardo \
  DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket \
  /usr/bin/pipewire-pulse &
PP_PID=$!
sleep 2
chrt -r -p 70 $PP_PID 2>/dev/null && echo "pipewire-pulse ($PP_PID) → SCHED_RR 70" || true

sleep infinity
EOF
sudo chmod +x /usr/local/bin/vovoamp-audio-start.sh

# Serviço de áudio
sudo tee /etc/systemd/system/vovoamp-audio.service << 'EOF'
[Unit]
Description=VovoAmp Audio Stack (PipeWire)
After=bluetooth.service dbus.service
Wants=bluetooth.service

[Service]
Type=simple
Nice=-20
LimitRTPRIO=95
LimitMEMLOCK=infinity
ExecStart=/usr/local/bin/vovoamp-audio-start.sh
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Servidor web
sudo tee /etc/systemd/system/vovoamp.service << 'EOF'
[Unit]
Description=VovôAmp Servidor Web
After=network.target vovoamp-audio.service
Wants=vovoamp-audio.service
BindsTo=vovoamp-audio.service

[Service]
Type=simple
User=ricardo
WorkingDirectory=/opt/vovoamp
# Aguarda PipeWire estar pronto (máx 30s) em vez de sleep fixo
ExecStartPre=/bin/bash -c 'for i in $(seq 1 30); do pactl -s unix:/run/user/1000/pulse/native info &>/dev/null && exit 0; sleep 1; done; exit 1'
ExecStart=/usr/bin/authbind --deep /usr/bin/python3 /opt/vovoamp/vovoamp.py
Restart=always
RestartSec=15
Environment=PYTHONUNBUFFERED=1
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=PULSE_SERVER=unix:/run/user/1000/pulse/native

[Install]
WantedBy=multi-user.target
EOF

# Configurações de baixa latência PipeWire / WirePlumber para o usuário ricardo
sudo -u ricardo mkdir -p /home/ricardo/.config/wireplumber/wireplumber.conf.d/
sudo -u ricardo mkdir -p /home/ricardo/.config/pipewire/pipewire.conf.d/

# Codecs Bluetooth — SBC-XQ primeiro (faststream removido: não suportado pelo fone)
sudo -u ricardo tee /home/ricardo/.config/wireplumber/wireplumber.conf.d/10-bluetooth-lowlatency.conf << 'EOF'
monitor.bluez.properties = {
    bluez5.enable-sbc-xq = true
    bluez5.enable-msbc = true
    bluez5.enable-hw-volume = true
    bluez5.codecs = [ sbc_xq sbc ]
}
EOF


# 8. Finalização
echo "[8/8] Ativando e iniciando serviços..."
sudo systemctl daemon-reload
sudo systemctl enable avahi-daemon
sudo systemctl enable vovoamp-audio.service
sudo systemctl enable vovoamp.service
sudo systemctl restart avahi-daemon
sudo systemctl restart vovoamp-audio.service
sudo systemctl restart vovoamp.service

# Garante que ethernet é gerenciada pelo NetworkManager
sudo nmcli device set end0 managed yes
sudo nmcli device connect end0 || true

echo ""
echo "==========================================="
echo "   VovôAmp v3.3 Instalado com Sucesso!     "
echo "   Acesse: http://vovoamp.wifi             "
echo "   ⚠️  Reboot necessário para threadirqs   "
echo "==========================================="
