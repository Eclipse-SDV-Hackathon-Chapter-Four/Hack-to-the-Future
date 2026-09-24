#!/usr/bin/env bash
# Container entrypoint for the ros2-hvac service (see ros2-hvac/README.md).
#
# Starts four things in one container:
#
#   1. Eclipse Muto runtime      ros2 launch muto.launch.py    (background)
#   2. Stack deployer            deploy_stack.py               (one-shot)
#      -> Muto downloads hvac_simulator.tar.gz from artifact-server, builds
#         it and launches the HVAC simulator node (/hvac_simulator).
#   3. ros2_medkit gateway       REST diagnostics on :8080     (background)
#   4. ros2_hvac_bridge          uProtocol/Zenoh <-> ROS 2 parameters, fault
#                                console on :8093              (foreground)
#
# The bridge runs in the foreground (exec) so the container's lifetime is tied
# to it; the trap tears down the background processes on exit.
#
# Environment (all optional):
#   MUTO_VEHICLE_NAMESPACE / MUTO_VEHICLE_NAME  vehicle identity for Muto
#   MUTO_BOOTSTRAP_DELAY_S       seconds to let Muto start before deploying
#   MUTO_DEPLOY_DISCOVERY_WAIT_S seconds deploy_stack.py waits for discovery
#   CAN_BRINGUP_VCAN=true        create/bring up a vcan interface named
#                                $CAN_CHANNEL (Linux SocketCAN variant only;
#                                needs NET_ADMIN, see ros2-hvac-host-can)
#   CAN_*                        forwarded to the simulator via its launch file
set -eo pipefail

source /opt/ros/${ROS_DISTRO}/setup.bash
source /opt/muto_ws/install/setup.bash

mkdir -p "${HOME}/.ros2_medkit"

# Optional Linux-only virtual CAN interface. The default containerized
# scenario uses python-can's udp_multicast and needs none of this.
CAN_CHANNEL="${CAN_CHANNEL:-vcan0}"
if [[ "${CAN_BRINGUP_VCAN:-false}" == "true" ]]; then
  modprobe vcan 2>/dev/null || true
  if ! ip link show "${CAN_CHANNEL}" >/dev/null 2>&1; then
    ip link add dev "${CAN_CHANNEL}" type vcan || true
  fi
  ip link set "${CAN_CHANNEL}" up || true
fi

# 1. Eclipse Muto runtime (agent, twin, composer + plugins).
ros2 launch /opt/muto_runtime/muto.launch.py \
  vehicle_namespace:="${MUTO_VEHICLE_NAMESPACE:-org.eclipse.muto.guardian}" \
  vehicle_name:="${MUTO_VEHICLE_NAME:-guardian-hvac}" &
MUTO_PID=$!

sleep "${MUTO_BOOTSTRAP_DELAY_S:-8}"

# 2. Ask Muto to start the HVAC stack (plays the role of the cloud backend).
python3 /opt/muto_runtime/deploy_stack.py \
  --ros-args \
  -p stack_path:=/opt/muto_runtime/hvac_stack_archive.json \
  -p discovery_wait_s:="${MUTO_DEPLOY_DISCOVERY_WAIT_S:-3.0}" &
DEPLOY_PID=$!

# 3. ros2_medkit REST gateway; the diagnostic bridge turns /diagnostics ERROR
#    statuses into faults at /api/v1/faults.
ros2 launch ros2_medkit_gateway bringup.launch.py \
  enable_diagnostic_bridge:=true \
  server_host:=0.0.0.0 &
MEDKIT_PID=$!

cleanup() {
  kill "${DEPLOY_PID}" "${MEDKIT_PID}" "${MUTO_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# 4. uProtocol/Zenoh <-> ROS 2 bridge and HVAC fault console (foreground).
exec /usr/local/bin/ros2_hvac_bridge
