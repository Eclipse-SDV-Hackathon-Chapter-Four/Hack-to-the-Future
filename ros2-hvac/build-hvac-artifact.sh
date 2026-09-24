#!/usr/bin/env bash
# Package the ROS 2 HVAC simulator as an Eclipse Muto stack archive.
#
# Run this after every change to ros2_ws/src/hack_to_the_future_hvac. It:
#
#   1. copies the package plus artifact-run.sh (as run.sh) into .artifact-stage/
#   2. tars it into artifacts/hvac_simulator.tar.gz, which the artifact-server
#      compose service serves at http://artifact-server:9090/
#   3. writes the tarball's sha256 into runtime/hvac_stack_archive.json, the
#      manifest deploy_stack.py publishes; Muto refuses an artifact whose
#      checksum does not match.
#
# Because runtime/ is baked into the ros2-hvac image, rebuild the image
# afterwards:  podman-compose --profile ros2 -f docker-compose.yml build ros2-hvac
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARTIFACT_DIR="${ROOT_DIR}/artifacts"
STAGE_DIR="${ROOT_DIR}/.artifact-stage/hvac_simulator"
PACKAGE_SRC="${ROOT_DIR}/ros2_ws/src/hack_to_the_future_hvac"
ARTIFACT_PATH="${ARTIFACT_DIR}/hvac_simulator.tar.gz"
ARCHIVE_JSON="${ROOT_DIR}/runtime/hvac_stack_archive.json"

rm -rf "${STAGE_DIR}"
mkdir -p "${STAGE_DIR}/src" "${ARTIFACT_DIR}"

cp -R "${PACKAGE_SRC}" "${STAGE_DIR}/src/hack_to_the_future_hvac"
find "${STAGE_DIR}" -name __pycache__ -type d -prune -exec rm -rf {} +
cp "${ROOT_DIR}/artifact-run.sh" "${STAGE_DIR}/run.sh"
chmod +x "${STAGE_DIR}/run.sh"

tar -C "${STAGE_DIR}" -czf "${ARTIFACT_PATH}" .

CHECKSUM="$(shasum -a 256 "${ARTIFACT_PATH}" 2>/dev/null || sha256sum "${ARTIFACT_PATH}")"
CHECKSUM="${CHECKSUM%% *}"
echo "${CHECKSUM}"

python3 - "${ARCHIVE_JSON}" "${CHECKSUM}" <<'PY'
import json
import sys

path, checksum = sys.argv[1], sys.argv[2]
with open(path) as f:
    doc = json.load(f)
doc["launch"]["properties"]["checksum"] = checksum
with open(path, "w") as f:
    json.dump(doc, f, indent=2)
    f.write("\n")
print(f"updated checksum in {path}")
PY
