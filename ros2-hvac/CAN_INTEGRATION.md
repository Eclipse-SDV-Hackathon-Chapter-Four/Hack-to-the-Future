# ROS 2 HVAC CAN Integration Guide

> Component map, Eclipse project roles and the general development loop are
> in [README.md](README.md). This guide covers only the CAN side.

This guide explains how to exchange CAN frames with the `ros2-hvac` service:

- set HVAC parameters from CAN frames (CAN -> ROS 2 HVAC)
- receive HVAC state frames from ROS 2 HVAC (ROS 2 HVAC -> CAN)

**What you will learn:** how a ROS 2 node joins a CAN bus, how a frame layout
is defined once in a DBC file and shared by every participant, how the same
code moves from a virtual bus to real SocketCAN hardware by changing two
environment variables, and how to observe and test all of it from any laptop.

If you have never used CAN: a frame is a tiny message with an 11-bit
*identifier* (who/what it is about) and up to 8 data bytes. There is no
addressing; every node sees every frame and filters by identifier. A *DBC*
file documents which bits in which frame mean what. Sections 4–6 make this
concrete with the two frames this workload uses.

The **default scenario is fully containerized and runs identically on macOS,
Windows, and Linux** — no kernel modules, no host CAN interfaces, no VM. A
Linux-only variant using real SocketCAN (`vcan0`/`can0`) is described in the
appendix for the openDuT / EDGAR hardware path.

## 1. How it works

CAN frames are carried by python-can's
[`udp_multicast`](https://python-can.readthedocs.io/en/stable/interfaces/udp_multicast.html)
interface: every bus participant joins a UDP multicast group (default
`239.74.163.2`, port 43113) inside the container network. This is a pure
userspace transport, so it works wherever containers run — including the
Linux VMs that Docker Desktop and Podman Machine use on macOS and Windows.

The HVAC simulator selects its transport purely via environment variables
(`CAN_BUSTYPE`, `CAN_CHANNEL`), so the same code runs unmodified against
`udp_multicast` (demo, any OS) or `socketcan` (Linux hardware path).

Instead of host `cansend`/`candump` (Linux-only tools), a small `can-tools`
container provides equivalents that speak the same transport.

## 2. Prerequisites (all platforms)

- A container engine + compose:
  - **macOS**: Podman Desktop (`podman machine init && podman machine start`)
    or Docker Desktop
  - **Windows**: Podman Desktop or Docker Desktop (WSL2 backend recommended)
  - **Linux**: podman + podman-compose, or docker + docker compose
- This repository checked out

Nothing else. No `vcan`, no `can-utils`, no Vagrant.

> The commands below use `podman-compose`. With Docker, substitute
> `docker compose` (e.g. `docker compose --profile ros2 up --build`).

## 3. Start the stack

From the repository root:

```bash
podman-compose --profile ros2 -f docker-compose.yml up --build
```

The `ros2-hvac` service starts with CAN enabled by default:

| Variable | Default | Meaning |
|---|---|---|
| `CAN_ENABLED` | `"true"` | enable the CAN bridge in the simulator |
| `CAN_BUSTYPE` | `udp_multicast` | python-can interface type |
| `CAN_CHANNEL` | `239.74.163.2` | multicast group (or CAN device for socketcan) |
| `CAN_TX_PERIOD_S` | `"1.0"` | period of outgoing state frames |
| `CAN_RX_POLL_PERIOD_S` | `"0.05"` | how often incoming frames are drained |
| `CAN_RETRY_PERIOD_S` | `"5.0"` | reconnect interval after a bus failure |
| `CAN_TX_ID` | `"0x320"` | state frame ID (simulator -> bus) |
| `CAN_RX_ID` | `"0x321"` | command frame ID (bus -> simulator) |

IDs above `0x7FF` are automatically sent/filtered as 29-bit extended IDs;
invalid IDs disable the bridge with an error in the log.

The simulator's own log goes to the deployed Muto workspace, not the
container stdout. Confirm the bridge is up with:

```bash
podman-compose --profile ros2 -f docker-compose.yml exec ros2-hvac \
  grep "CAN bridge" /root/.muto/workspaces/guardian_hvac_simulator/run.log
```

```
[hvac_simulator]: CAN bridge enabled on udp_multicast:239.74.163.2
```

If the bus is unavailable at startup (or fails mid-run), the simulator logs
an error and keeps retrying every `CAN_RETRY_PERIOD_S` seconds — it recovers
automatically once the bus is reachable.

## 4. CAN frame contract

The canonical, machine-checked contract is
[`ros2_ws/src/hack_to_the_future_hvac/config/hvac.dbc`](ros2_ws/src/hack_to_the_future_hvac/config/hvac.dbc)
(messages `HVAC_STATE` / `HVAC_COMMAND`). The simulator encodes/decodes
through it via `cantools`, and the unit tests assert the layout below matches
the DBC bit-for-bit. Use `dump --decode` (section 6) to see frames decoded by
signal name.

11-bit standard IDs, 3-byte payload:

- RX frame ID `0x321` (bus -> simulator): updates ROS 2 parameters
- TX frame ID `0x320` (simulator -> bus): current HVAC state

Payload bytes (both directions):

- byte 0: `target_temperature_celsius` (clamped 16..30)
- byte 1: `fan_speed_percent` (clamped 0..100)
- byte 2: flags bitfield
  - bit 0 (`0x01`): `air_conditioning_active`
  - bit 1 (`0x02`): `fault_active`

Incoming frames are drained every `CAN_RX_POLL_PERIOD_S` (50 ms default), so
commands apply near-instantly. If several command frames arrive in one poll
window, only the newest is applied — like a real actuator following the most
recent setpoint. Frames with other IDs are filtered out at the bus level.

## 5. Send commands to the HVAC simulator

In a second terminal, use the `can-tools` container (`send` takes
`cansend`-compatible `<ID>#<HEXDATA>` syntax).

Example: target=22 °C (0x16), fan=10 % (0x0A), AC on, no fault:

```bash
podman-compose --profile can-tools -f docker-compose.yml run --rm can-tools send 321#160A01
```

Example: target=24 °C (0x18), fan=40 % (0x28), AC on, fault active:

```bash
podman-compose --profile can-tools -f docker-compose.yml run --rm can-tools send 321#182803
```

Verify the parameters changed inside the simulator container:

```bash
podman-compose --profile ros2 -f docker-compose.yml exec ros2-hvac \
  bash -lc "ros2 param get /hvac_simulator target_temperature_celsius && \
            ros2 param get /hvac_simulator fan_speed_percent && \
            ros2 param get /hvac_simulator air_conditioning_active && \
            ros2 param get /hvac_simulator fault_active"
```

You can also check via the medkit REST API from the host:
`http://localhost:18080/api/v1/`.

## 6. Watch HVAC state frames

```bash
podman-compose --profile can-tools -f docker-compose.yml run --rm can-tools dump
```

Expected output — ID `320` frames exactly at `CAN_TX_PERIOD_S` (1 s default),
prefixed with the receive timestamp:

```
(1790173931.845605)  239.74.163.2  320   [3]  16 0A 01
(1790173932.845664)  239.74.163.2  320   [3]  16 0A 01
```

`dump` accepts `--count N` (exit after N frames), `--timeout S`, and
`--decode`, which renders known frames by signal name using the `hvac.dbc`
contract:

```
(1790174708.490776)  239.74.163.2  320   [3]  16 0A 00  HVAC_STATE: target_temperature_celsius=22 fan_speed_percent=10 air_conditioning_active=0 fault_active=0
```

## 7. State synchronization (single source of truth)

The ROS 2 parameters on `/hvac_simulator` are the single source of truth for
HVAC state. All command sources write to them and read from them:

- **CAN** (`CAN_RX_ID` frames) writes the commanded fields directly.
- **uProtocol/Zenoh HVAC commands** (via the Rust bridge) write only the
  fields they carry: target temperature, AC active, fan speed.
- **The fault console** (`http://localhost:18081`, also used by the
  dashboard) writes only `fault_active`.

The Rust bridge reads the parameters back every 2 s, so changes made via CAN
show up in `GET http://localhost:18081/api/state`, in the fault console UI,
and are republished as Zenoh state events for the Guardian and
temperature-sim. No source overwrites fields it does not own.

## 8. CAN health diagnostics

The simulator publishes a `guardian_hvac/can_bridge` status on
`/diagnostics` (alongside the existing `guardian_hvac/thermal_state`):

- `OK` — bridge connected (or intentionally disabled by configuration)
- `ERROR` — python-can missing, invalid CAN ID configuration, or bus
  disconnected (with automatic reconnect in progress)

Values include `interface`, `channel`, `connected`, `rx_frames`,
`rx_ignored`, `tx_frames`, `errors`, and `last_rx_age_s`. Inspect with:

```bash
podman-compose --profile ros2 -f docker-compose.yml exec ros2-hvac \
  bash -lc "ros2 topic echo /diagnostics --once"
```

or through the medkit gateway / web UI (`http://localhost:3000`).

## 9. Platform notes

### macOS

- Works with both Podman (`podman machine`) and Docker Desktop; all CAN
  traffic stays inside the engine's Linux VM, so the host needs no CAN
  support at all.
- The SocketCAN appendix below does **not** apply to the macOS host itself —
  if you need real SocketCAN, use a Linux VM (see the repository
  `Vagrantfile`).

### Windows

- Use Docker Desktop or Podman Desktop with the WSL2 backend.
- Run the compose commands from PowerShell, CMD, or a WSL2 shell — the
  workflow is identical.
- As on macOS, SocketCAN is not available on the Windows host; the
  containerized udp_multicast path is the supported scenario.

### Linux

- The containerized scenario works out of the box, same commands as above.
- Additionally, Linux hosts can run the SocketCAN variant (appendix below)
  for interop with real CAN hardware, `can-utils`, or openDuT/EDGAR.

## 10. Troubleshooting

- **First stop: the `guardian_hvac/can_bridge` diagnostic** (section 8) — it
  tells you whether the bridge is connected and whether frames are flowing
  (`rx_frames`/`tx_frames` counters).
- **`CAN is enabled but python-can is not installed`** in the simulator log:
  the image is stale — rebuild with `--build` (python-can >= 4.2 is installed
  via pip in the Dockerfile).
- **`send` succeeds but parameters don't change**: commands apply within
  ~`CAN_RX_POLL_PERIOD_S` (50 ms); confirm the "CAN bridge enabled" line in
  the simulator `run.log` (see section 3) and that you sent to `CAN_RX_ID`
  (`321`).
- **`Failed to initialize CAN bridge ... retrying`** in the simulator log:
  the bus wasn't reachable; the bridge reconnects automatically every
  `CAN_RETRY_PERIOD_S` — fix the transport (channel/interface) and it
  recovers without a restart.
- **`dump` times out**: confirm `CAN_ENABLED=true` on `ros2-hvac` and that
  `can-tools` uses the same `CAN_CHANNEL` and compose network as `ros2-hvac`
  (both join the default `hack-to-the-future-net` — UDP multicast only
  reaches containers on the same bridge network).
- **Frames of other services appear in `dump`**: expected — udp_multicast is
  a shared bus, exactly like real CAN; filter by ID.

## 11. Development and testing

After changing the simulator package
(`ros2-hvac/ros2_ws/src/hack_to_the_future_hvac`):

```bash
./ros2-hvac/build-hvac-artifact.sh     # rebuilds the tarball AND updates the
                                       # checksum in runtime/hvac_stack_archive.json
podman-compose --profile ros2 -f docker-compose.yml build ros2-hvac
```

Simulator parameters follow the precedence *launch argument > environment
variable > built-in default* (see `launch/hvac.launch.py`). The CAN IDs are
string parameters so both `0x320` and `800` are accepted from every source.

**Unit tests** (frame codec, clamping, DBC contract consistency, virtual-bus
roundtrip) run inside the container:

```bash
podman run --rm --entrypoint bash \
  -v ./ros2-hvac/ros2_ws/src/hack_to_the_future_hvac:/pkg:ro \
  localhost/hack-to-the-future/ros2-hvac:stage-ros2 \
  -c "source /opt/ros/humble/setup.bash; cd /pkg && python3 -m pytest test/ -v"
```

**End-to-end smoke test** against a running stack (any OS; set
`COMPOSE_CMD="docker compose"` for Docker):

```bash
podman-compose --profile ros2 -f docker-compose.yml up -d
./ros2-hvac/test-can-e2e.sh
```

It verifies: simulator deployed with CAN enabled, CAN command → ROS
parameters, state frames on the bus with the commanded payload, fault flag
via CAN, and state reset.

## 12. Exercises for teams

Each exercise touches one layer and can be verified with the tools above.

1. **Read a frame by hand.** Run `dump` without `--decode`, pick one `320`
   frame and decode its three bytes using section 4. Check your answer with
   `dump --decode`.
2. **Change the bus address space.** Set `CAN_TX_ID`/`CAN_RX_ID` to `0x330`
   and `0x331` in `docker-compose.yml`, restart, and make `send` still work.
   Then try an extended ID such as `0x18DA10F1` and watch the `can_bridge`
   diagnostic.
3. **Add a signal.** Add a `recirculation_active` flag (bit 2 of byte 2) to
   both messages in `config/hvac.dbc`, to the manual codec in
   `hvac_simulator.py`, and as a new ROS 2 parameter. Run the unit tests
   until the DBC and manual codecs agree, then rebuild the artifact and verify
   with `dump --decode`.
4. **Break the transport.** Point `CAN_CHANNEL` of `ros2-hvac` at a different
   multicast group than `can-tools`, observe what the diagnostic and the log
   say, then fix it without restarting the simulator (hint: the supervisor
   timer reconnects; `ros2 param set` can change the channel).
5. **Go to real hardware (Linux only).** Follow the appendix, then bridge
   `vcan0` to a USB CAN adapter with `cangw` or point `CAN_CHANNEL` at `can0`.

## Appendix: Linux host SocketCAN variant (`ros2-hvac-host-can`)

For the openDuT / EDGAR path, a Linux host can run the simulator against a
real SocketCAN interface. This variant uses `network_mode: host` and is
**Linux-only**.

1. Create and bring up `vcan0` on the host:

   ```bash
   sudo modprobe vcan
   sudo ip link add dev vcan0 type vcan 2>/dev/null || true
   sudo ip link set vcan0 up
   ip -details link show vcan0
   ```

   (The repository `Vagrantfile` already provisions `vcan` and `can_gw`.)

2. Start the host-CAN service:

   ```bash
   podman-compose --profile ros2-host-can -f docker-compose.yml up --build ros2-hvac-host-can
   ```

3. Use the native tools on the host:

   ```bash
   cansend vcan0 321#160A01
   candump vcan0
   ```

Deployment model:

- Step 1 (single-host test): ROBOT + testee use host networking and host `vcan0`.
- Step 2 (distributed): each side uses local `vcan0`; EDGAR forwards CAN
  frames between hosts.

No simulator code changes are needed between the containerized scenario and
either step — only `CAN_BUSTYPE`/`CAN_CHANNEL` differ.

Troubleshooting (SocketCAN):

- `No such device` for `vcan0`: create and bring up `vcan0` on the host.
- `Operation not permitted` on the CAN socket: the container needs
  `NET_ADMIN`/`NET_RAW` (already set on `ros2-hvac-host-can`).
