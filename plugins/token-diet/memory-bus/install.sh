#!/usr/bin/env bash
# Deploy memory-bus to the server (Qdrant + Ollama host). Run from the Mac:
#   bash memory-bus/install.sh            # rsync + venv + unit + start
#   bash memory-bus/install.sh --migrate  # ... then migrate legacy collections
# Additive only: new dir /opt/memory-bus, new unit memory-bus.service. Touches nothing else.
set -euo pipefail
HOST=${MB_SSH_HOST:-${2:-}}
[[ -z "$HOST" ]] && { echo "usage: MB_SSH_HOST=root@host bash install.sh [--migrate]"; exit 2; }
SRC=$(cd "$(dirname "$0")" && pwd)
MIGRATE=0; [[ "${1:-}" == "--migrate" ]] && MIGRATE=1

echo "== rsync code -> $HOST:/opt/memory-bus"
ssh -o BatchMode=yes "$HOST" 'mkdir -p /opt/memory-bus && chmod 750 /opt/memory-bus'
rsync -az --delete --exclude '.venv' --exclude '__pycache__' --exclude 'tests' --exclude 'mac' --exclude 'client' --exclude 'hooks' \
  "$SRC/" "$HOST:/opt/memory-bus/"

ssh -o BatchMode=yes "$HOST" 'set -euo pipefail
cd /opt/memory-bus
if [ ! -x .venv/bin/python ]; then
  # system site-packages: reuse torch 2.10 + sentence-transformers 5.2.3 + qdrant-client 1.17 already on the box
  uv venv --system-site-packages --python /usr/bin/python3 .venv
fi
# Only install into the venv what the system site-packages do not already satisfy. `uv pip install`
# resolves without looking at system site-packages, so an unconditional install pulls a second
# torch/transformers stack (5.6 GB) that breaks the system torchvision (observed 2026-09-16).
if .venv/bin/python -c "import fastapi, uvicorn, pydantic, qdrant_client, sentence_transformers" 2>/dev/null; then
  echo "== deps satisfied by system site-packages; skipping uv pip install"
else
  echo "== installing missing deps into venv"
  uv pip install --python .venv/bin/python -r requirements.txt
fi
HF_HUB_OFFLINE=1 .venv/bin/python -c "from sentence_transformers import SentenceTransformer as S; m=S(\"sentence-transformers/all-MiniLM-L6-v2\",device=\"cpu\"); print(\"model ok dim\", m.get_sentence_embedding_dimension())"
install -m 644 systemd/memory-bus.service /etc/systemd/system/memory-bus.service
systemctl daemon-reload
systemctl enable --now memory-bus.service
for i in $(seq 1 60); do curl -sf http://127.0.0.1:8791/health >/dev/null && break; sleep 2; done
curl -s http://127.0.0.1:8791/health | python3 -m json.tool | head -40
'

if [[ $MIGRATE -eq 1 ]]; then
  echo "== migrating legacy collections (llm=off; re-embeds ~3.4k texts, ~1-2 min)"
  ssh -o BatchMode=yes "$HOST" 'cd /opt/memory-bus && HF_HUB_OFFLINE=1 MB_LLM_MODE=off .venv/bin/python migrate.py'
fi
echo "== done. Mac side: memory-bus tunnel && memory-bus health"
