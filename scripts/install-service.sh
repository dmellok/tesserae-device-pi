#!/usr/bin/env bash
# Install tesserae-device-pi as a systemd service.
#
# Run as root (or via sudo) ON THE PI after `pip install .` has put the
# tesserae-device-pi binary on PATH (default /usr/local/bin).
#
# The unit is templated for the user that will run the daemon — pick the
# account that has membership in the `gpio` and `spi` groups. The default
# `pi` user typically does.
set -euo pipefail

UNIT_NAME="tesserae-device-pi.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_TEMPLATE="${SCRIPT_DIR}/tesserae-device-pi.service.in"
UNIT_DEST="/etc/systemd/system/${UNIT_NAME}"

SERVICE_USER="${1:-${SUDO_USER:-pi}}"

if [[ ! -f "${UNIT_TEMPLATE}" ]]; then
    echo "missing unit template: ${UNIT_TEMPLATE}" >&2
    exit 1
fi
if [[ "$(id -u)" -ne 0 ]]; then
    echo "this script must be run as root (try: sudo $0 [user])" >&2
    exit 1
fi
if ! command -v tesserae-device-pi >/dev/null 2>&1; then
    echo "tesserae-device-pi binary not found on PATH" >&2
    echo "install it first: pip install ." >&2
    exit 1
fi
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    echo "user ${SERVICE_USER} does not exist" >&2
    exit 1
fi

# The clients this one replaces drive the same panel: stop and disable them so
# two daemons never paint over each other. Their unit files are left in place
# (disabled), so going back is `systemctl enable --now <old unit>`.
for old_unit in tesserae-pi-png-client.service tesserae-pi-bin-client.service; do
    if systemctl list-unit-files "${old_unit}" --no-legend 2>/dev/null | grep -q .; then
        echo "disabling the replaced ${old_unit}"
        systemctl disable --now "${old_unit}" || true
    fi
done

# The venv's python (for the OTA guard and pip) and the service user's cache
# dir, where an update stages its wheel and the guard keeps its record.
CLIENT_BIN="$(readlink -f "$(command -v tesserae-device-pi)")"
VENV_PYTHON="$(dirname "${CLIENT_BIN}")/python"
SERVICE_HOME="$(getent passwd "${SERVICE_USER}" | cut -d: -f6)"
CACHE_DIR="${SERVICE_HOME}/.cache/tesserae-device-pi"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${CACHE_DIR}"
install -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0644 \
    "$("${VENV_PYTHON}" -c 'import tesserae_device_pi.ota_guard as g; print(g.__file__)')" \
    "${CACHE_DIR}/ota-guard.py"

echo "installing ${UNIT_NAME} for user ${SERVICE_USER}"
sed -e "s|@USER@|${SERVICE_USER}|g" \
    -e "s|@PYTHON@|${VENV_PYTHON}|g" \
    -e "s|@CACHE@|${CACHE_DIR}|g" \
    "${UNIT_TEMPLATE}" > "${UNIT_DEST}"
chmod 0644 "${UNIT_DEST}"

systemctl daemon-reload
systemctl enable "${UNIT_NAME}"
systemctl restart "${UNIT_NAME}"
systemctl --no-pager status "${UNIT_NAME}" || true
