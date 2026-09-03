#!/usr/bin/env bash
# Install systemd drop-in so Ollama on Ubuntu matches Windows VRAM behavior:
# q8 KV, flash-attn, single slot, CUDA unified memory (overflow → system RAM).
set -euo pipefail
DEST=/etc/systemd/system/ollama.service.d
mkdir -p "$DEST"
cat > "$DEST/aether.conf" <<'EOF'
[Service]
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="GGML_CUDA_ENABLE_UNIFIED_MEMORY=1"
EOF
systemctl daemon-reload
systemctl restart ollama
# Wait until API is back
for _ in $(seq 1 40); do
  if curl -sf -m 1 http://127.0.0.1:11434/api/tags >/dev/null; then
    exit 0
  fi
  sleep 0.25
done
exit 1
