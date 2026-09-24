#!/usr/bin/env bash
# End-to-end smoke test for the containerized HVAC CAN scenario.
#
# Exercises the real deployed stack (Muto-deployed simulator, udp_multicast
# CAN bus, can-tools container) and checks every hop of the CAN data flow
# described in README.md §5:
#
#   1. ros2-hvac container is running
#   2. Muto deployed the simulator and its CAN bridge came up (run.log)
#   3. a command frame on 0x321 changes the ROS 2 parameters
#   4. state frames on 0x320 are emitted and carry the commanded payload
#   5. the fault flag can be set via CAN
#   6. state can be reset via CAN
#
# Works on macOS / Windows (WSL2/git-bash) / Linux — requires only a running
# stack:  podman-compose --profile ros2 -f docker-compose.yml up -d
#
# Usage:  ./ros2-hvac/test-can-e2e.sh
#   COMPOSE_CMD overrides the compose command (default: podman-compose;
#   use COMPOSE_CMD="docker compose" for Docker).
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="${COMPOSE_CMD:-podman-compose}"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.yml"

RUN_TOOLS=(${COMPOSE} --profile can-tools -f "${COMPOSE_FILE}" run --rm can-tools)
EXEC_HVAC=(${COMPOSE} --profile ros2 -f "${COMPOSE_FILE}" exec ros2-hvac)

FAILURES=0

step() { printf '\n== %s ==\n' "$1"; }
pass() { printf 'PASS: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1"; FAILURES=$((FAILURES + 1)); }

param_get() {
  "${EXEC_HVAC[@]}" bash -lc "source /opt/ros/humble/setup.bash >/dev/null 2>&1 && timeout 10 ros2 param get /hvac_simulator $1" 2>/dev/null | tr -d '\r'
}

step "stack reachable"
if "${EXEC_HVAC[@]}" true >/dev/null 2>&1; then
  pass "ros2-hvac container is running"
else
  fail "ros2-hvac container not running; start with: ${COMPOSE} --profile ros2 -f docker-compose.yml up -d"
  exit 1
fi

step "simulator deployed with CAN bridge"
if "${EXEC_HVAC[@]}" grep -q "CAN bridge enabled" /root/.muto/workspaces/guardian_hvac_simulator/run.log 2>/dev/null; then
  pass "CAN bridge enabled in simulator log"
else
  fail "CAN bridge not enabled (check run.log in the container)"
fi

step "CAN command -> ROS parameters (25C / 15% / AC on)"
"${RUN_TOOLS[@]}" send 321#190F01 >/dev/null 2>&1
DEADLINE=$((SECONDS + 20))
RX_OK=0
while [ ${SECONDS} -lt ${DEADLINE} ]; do
  if param_get target_temperature_celsius | grep -q "25"; then
    RX_OK=1
    break
  fi
  sleep 2
done
if [ ${RX_OK} -eq 1 ] \
  && param_get fan_speed_percent | grep -q "15" \
  && param_get air_conditioning_active | grep -qi "true"; then
  pass "parameters updated from CAN frame"
else
  fail "parameters did not reflect CAN frame 321#190F01"
fi

step "HVAC state frames on the bus (TX 0x320)"
DUMP="$("${RUN_TOOLS[@]}" dump --count 3 --timeout 10 2>/dev/null)"
if [ "$(printf '%s\n' "${DUMP}" | grep -c ' 320 ')" -ge 3 ]; then
  pass "received 3 state frames"
else
  fail "expected 3 frames with ID 320, got: ${DUMP}"
fi
if printf '%s\n' "${DUMP}" | grep -q "19 0F 01"; then
  pass "state frames reflect the commanded values"
else
  fail "state frames do not contain payload 19 0F 01"
fi

step "fault flag via CAN"
"${RUN_TOOLS[@]}" send 321#190F03 >/dev/null 2>&1
sleep 2
if param_get fault_active | grep -qi "true"; then
  pass "fault_active set via CAN"
else
  fail "fault_active not set"
fi

step "reset to defaults"
"${RUN_TOOLS[@]}" send 321#160A00 >/dev/null 2>&1
sleep 2
if param_get fault_active | grep -qi "false"; then
  pass "state reset"
else
  fail "state not reset"
fi

printf '\n'
if [ ${FAILURES} -eq 0 ]; then
  echo "ALL CHECKS PASSED"
else
  echo "${FAILURES} CHECK(S) FAILED"
  exit 1
fi
