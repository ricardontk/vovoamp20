#!/bin/bash
# Script de inicialização do VovôAmp Headless (via SystemD)
export PORT=80

# Mata instâncias anteriores para evitar conflitos
killall -9 chromium ws_bridge.py Xvfb 2>/dev/null
sleep 1

echo "Iniciando Xvfb..."
Xvfb :99 -screen 0 1024x768x24 &
export DISPLAY=:99
sleep 2

echo "Iniciando ws_bridge.py..."
# Usamos authbind para rodar na porta 80 sem root
authbind --deep python3 /opt/vovoamp/ws_bridge.py &
sleep 2

echo "Iniciando Chromium em background..."
chromium \
  --no-sandbox \
  --disable-gpu \
  --disable-software-rasterizer \
  --disable-dev-shm-usage \
  --autoplay-policy=no-user-gesture-required \
  --use-fake-ui-for-media-stream \
  --allow-file-access-from-files \
  --window-size=1024,768 \
  --app=http://localhost:80/batian.html &

wait
