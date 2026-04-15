#!/usr/bin/env bash
# Install / refresh the OCI metrics bridge under /opt/script (Ubuntu/Debian VM).
# Run as root after cloning this repository.
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo $0"
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="/opt/script"

install -d -m 0755 "${TARGET}/logs"
rsync -a --delete \
  --exclude venv \
  --exclude logs \
  --exclude env \
  "${REPO_ROOT}/opt/script/" "${TARGET}/"

chmod 0755 "${TARGET}/run-with-otel.sh" 2>/dev/null || true
chmod 0644 "${TARGET}/oci_metrics_bridge_vm.py" 2>/dev/null || true

if [[ ! -d "${TARGET}/venv" ]]; then
  python3 -m venv "${TARGET}/venv"
fi
"${TARGET}/venv/bin/pip" install --upgrade pip
"${TARGET}/venv/bin/pip" install --no-cache-dir -r "${TARGET}/requirements.txt"
if ! "${TARGET}/venv/bin/python" -c "import oci, requests" 2>/dev/null; then
  echo "pip retry: oci import failed; reinstalling oci wheel"
  "${TARGET}/venv/bin/pip" install --force-reinstall --no-cache-dir "oci>=2.126.0"
fi
"${TARGET}/venv/bin/python" -c "import oci, requests; print('venv ok')"

if [[ ! -f "${TARGET}/env" ]]; then
  install -m 0600 "${TARGET}/env.example" "${TARGET}/env"
  echo "Created ${TARGET}/env from env.example — edit with secrets and OCI compartment OCID."
fi

echo "Install complete."
echo "Next:"
echo "  1) Install OCI CLI config + API key: ${TARGET}/../.oci/config (see README)."
echo "  2) Edit ${TARGET}/env"
echo "  3) Test: ${TARGET}/run-with-otel.sh"
echo "  4) Optional cron: cp ${TARGET}/cron.example /etc/cron.d/oci-metrics-bridge"
