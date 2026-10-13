#!/bin/sh
# Optional future integration environment. No existing context/files are changed.
set -eu
if [ "${1:-}" != "--create-local" ] || [ "$#" -ne 1 ]; then
  echo 'Usage: sh bootstrap/local.sh --create-local' >&2
  echo 'Creates a NEW local k3d cluster and downloads images/charts. Requires explicit authorization.' >&2
  exit 2
fi
cd "$(dirname "$0")/.."
for command in python3 docker k3d kubectl helm; do
  command -v "$command" >/dev/null || { echo "Missing dependency: $command" >&2; exit 2; }
done
docker info >/dev/null 2>&1 || { echo 'Docker daemon is not available.' >&2; exit 2; }
if [ -e personal/local.json ] || [ -e personal/kubeconfig.local ]; then
  echo 'Refusing to overwrite existing personal configuration.' >&2
  exit 2
fi
k3d cluster list -o json | python3 -c 'import json,sys; sys.exit(2 if any(c["name"]=="imageharbor-lab" for c in json.load(sys.stdin)) else 0)' || {
  echo 'Refusing to reuse an existing imageharbor-lab cluster.' >&2
  exit 2
}
umask 077
mkdir -p personal
k3d cluster create imageharbor-lab --servers 1 --agents 0 \
  --image rancher/k3s:v1.35.9-k3s1 --servers-memory 2g \
  --api-port 127.0.0.1:0 --no-image-volume \
  --k3s-arg '--disable=traefik@server:0' --k3s-arg '--disable=servicelb@server:0' \
  --kubeconfig-update-default=false --kubeconfig-switch-context=false --timeout 180s
k3d kubeconfig get imageharbor-lab > personal/kubeconfig.local
helm upgrade --install imageharbor-traefik traefik \
  --repo https://traefik.github.io/charts --version 41.6.1 \
  --namespace imageharbor-proxy --create-namespace \
  --kubeconfig personal/kubeconfig.local --kube-context k3d-imageharbor-lab \
  --values bootstrap/traefik-values.yaml --wait --timeout 180s
python3 - <<'PY'
import json
from pathlib import Path
config = {
    "instance": "local", "base_domain": "apps.example.test", "backend": "kubectl",
    "state_dir": "../.local/local-cluster", "kubeconfig": "kubeconfig.local",
    "kube_context": "k3d-imageharbor-lab", "routing_enabled": False,
    "require_digest": True, "allow_volume_delete": False, "log_safe_apps": [],
}
Path("personal/local.json").write_text(json.dumps(config, indent=2) + "\n")
PY
echo 'Created private local environment. Routes remain disabled.'
echo 'Use: python3 -m imageharbor --config personal/local.json inventory'
