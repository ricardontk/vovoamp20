#!/bin/bash
# Script de inicialização do VovôAmp Headless
# Para rodar no Armbian (OrangePi 3 LTS)

# Mata instâncias anteriores para evitar conflitos
killall -9 chromium-browser chromium ws_bridge.py Xvfb 2>/dev/null
sleep 1

# Garante que as dependências necessárias estejam presentes
# sudo apt-get install xvfb chromium-browser python3-aiohttp -y

echo "Iniciando Xvfb..."
Xvfb :99 -screen 0 1024x768x24 &
export DISPLAY=:99
sleep 2

echo "Iniciando ws_bridge.py..."
python3 ws_bridge.py &
sleep 2

echo "Iniciando Chromium em background..."
chromium-browser \
  --no-sandbox \
  --disable-gpu \
  --disable-software-rasterizer \
  --disable-dev-shm-usage \
  --autoplay-policy=no-user-gesture-required \
  --use-fake-ui-for-media-stream \
  --allow-file-access-from-files \
  --window-size=1024,768 \
  --app=http://localhost:8080/batian.html &

echo "================================================="
echo " VovôAmp Headless iniciado!"
echo " Acesse a interface em: http://<IP_DA_ORANGE_PI>:8080/ui.html"
echo "================================================="
wait
