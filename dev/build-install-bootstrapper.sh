#!/usr/bin/env bash
# build-install-bootstrapper.sh
# Builds the bootstrapper integration with PyInstaller (aarch64) and installs it on the remote.
#
# Usage:
#   ./dev/build-install-bootstrapper.sh [REMOTE_HOST] [PIN] [DEV_DOWNLOAD_URL]
#
# If DEV_DOWNLOAD_URL is provided, it is baked into const.py so the bootstrapper
# downloads from that URL instead of GitHub.  Useful for local dev/testing.
#
# Defaults:
#   REMOTE_HOST  = 10.0.0.42
#   PIN          = 8201

set -euo pipefail

REMOTE_HOST="${1:-10.0.0.42}"
PIN="${2:-8201}"
DEV_DOWNLOAD_URL="${3:-}"
REMOTE_USER="web-configurator"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="$(cd "$SCRIPT_DIR/.." && pwd)"

DRIVER_ID="intg_bootstrapper_driver"
INTG_DIR="intg-bootstrapper"
PYTHON_VER="3.11.12-0.3.0"
ARTIFACT_DIR="$WORKSPACE/dist-bootstrapper"
ARCHIVE_NAME="uc-intg-bootstrapper.tar.gz"
CONST_FILE="$WORKSPACE/${INTG_DIR}/const.py"
CONST_BACKUP="${CONST_FILE}.bak"

# ---- Optionally patch DEV_DOWNLOAD_URL into const.py ----------------------
if [ -n "$DEV_DOWNLOAD_URL" ]; then
    echo "==> Patching DEV_DOWNLOAD_URL into const.py: ${DEV_DOWNLOAD_URL}"
    cp "$CONST_FILE" "$CONST_BACKUP"
    # Replace the getenv line so the default is the dev URL
    sed -i.tmp "s|DEV_DOWNLOAD_URL: str = os.getenv(\"UC_DEV_DOWNLOAD_URL\", \"\")|DEV_DOWNLOAD_URL: str = os.getenv(\"UC_DEV_DOWNLOAD_URL\", \"${DEV_DOWNLOAD_URL}\")|" "$CONST_FILE"
    rm -f "${CONST_FILE}.tmp"
fi

# Restore const.py on exit (success or failure)
restore_const() {
    if [ -f "$CONST_BACKUP" ]; then
        mv "$CONST_BACKUP" "$CONST_FILE"
        echo "==> Restored original const.py"
    fi
}
trap restore_const EXIT

echo "==> Building bootstrapper for aarch64 using PyInstaller Docker image..."

cd "$WORKSPACE"

rm -rf "$WORKSPACE/dist/${DRIVER_ID}" "$WORKSPACE/build/${DRIVER_ID}"

docker run --rm --name builder \
    --platform linux/arm64 \
    --user="$(id -u):$(id -g)" \
    -v "$WORKSPACE":/workspace \
    "docker.io/unfoldedcircle/r2-pyinstaller:${PYTHON_VER}" \
    bash -c "
      cd /workspace && \
      python -m pip install -q -r requirements.txt && \
      pyinstaller --clean -y --onedir --name ${DRIVER_ID} \
        ${INTG_DIR}/driver.py
    "

echo "==> Packaging archive..."
rm -rf "$ARTIFACT_DIR"
mkdir -p "$ARTIFACT_DIR/bin"

# Move PyInstaller output into bin/, rename the executable to 'driver'
mv "$WORKSPACE/dist/${DRIVER_ID}"/* "$ARTIFACT_DIR/bin/"
mv "$ARTIFACT_DIR/bin/${DRIVER_ID}" "$ARTIFACT_DIR/bin/driver"

# Copy driver.json to archive root (also placed in bin/ by remote during install)
cp "$WORKSPACE/${INTG_DIR}/driver.json" "$ARTIFACT_DIR/driver.json"

tar czf "$WORKSPACE/${ARCHIVE_NAME}" -C "$ARTIFACT_DIR" .

echo "==> Archive created: ${ARCHIVE_NAME} ($(du -sh "$WORKSPACE/${ARCHIVE_NAME}" | cut -f1))"

# ---- Delete existing driver if present ------------------------------------
echo "==> Checking for existing installation of ${DRIVER_ID} on remote..."

INSTANCES=$(curl --silent --insecure \
    "https://${REMOTE_HOST}/api/intg/instances?driver_id=${DRIVER_ID}" \
    --user "${REMOTE_USER}:${PIN}" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    items = data.get('items', data) if isinstance(data, dict) else data
    print('\\n'.join(i['id'] for i in items if isinstance(i, dict)))
except Exception:
    pass
" 2>/dev/null || true)

for INSTANCE_ID in $INSTANCES; do
    echo "   Deleting instance: ${INSTANCE_ID}"
    curl --silent --insecure -X DELETE \
        "https://${REMOTE_HOST}/api/intg/instances/${INSTANCE_ID}" \
        --user "${REMOTE_USER}:${PIN}" > /dev/null
done

DRIVER_EXISTS=$(curl --silent --insecure -o /dev/null -w "%{http_code}" \
    "https://${REMOTE_HOST}/api/intg/drivers/${DRIVER_ID}" \
    --user "${REMOTE_USER}:${PIN}")

if [ "$DRIVER_EXISTS" = "200" ]; then
    echo "   Deleting driver: ${DRIVER_ID}"
    curl --silent --insecure -X DELETE \
        "https://${REMOTE_HOST}/api/intg/drivers/${DRIVER_ID}" \
        --user "${REMOTE_USER}:${PIN}" > /dev/null
fi

echo "==> Installing on remote: https://${REMOTE_HOST}..."
curl --silent --show-error --fail --insecure \
    --location "https://${REMOTE_HOST}/api/intg/install" \
    --user "${REMOTE_USER}:${PIN}" \
    --form "file=@\"${WORKSPACE}/${ARCHIVE_NAME}\"" \
    | python3 -m json.tool

echo ""
echo "Done. Bootstrapper installed on https://${REMOTE_HOST}"
if [ -n "$DEV_DOWNLOAD_URL" ]; then
    echo "DEV_DOWNLOAD_URL baked in: ${DEV_DOWNLOAD_URL}"
fi
