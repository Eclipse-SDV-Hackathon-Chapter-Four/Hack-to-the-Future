# ros2-hvac — ROS 2 HVAC workload template

`ros2-hvac` is the reference workload for the **Hack to the Future** challenge.
It shows, in one container, how a ROS 2 application is packaged, deployed and
operated with Eclipse SDV building blocks, and how it talks to the rest of the
Guardian stack. Teams are expected to copy it, understand it in an afternoon,
and replace parts of it with their own challenge work.

Everything in this directory runs fully containerized on macOS, Windows and
Linux. No ROS 2 installation on the host is needed.

## Five-minute tour

Do this before reading anything else. Commands use `podman-compose`; with
Docker, write `docker compose` instead. Run them from the repository root.

1. **Start the stack** and wait about 30 s:

   ```bash
   podman-compose --profile ros2 -f docker-compose.yml up --build -d
   ```

2. **See the HVAC controller.** Open <http://localhost:18081>. You see the
   setpoint (22 °C), fan, AC and fault state of a ROS 2 node that Eclipse Muto
   just downloaded, built and launched inside the container.

3. **Talk to it over CAN.** Send a command frame (25 °C, fan 15 %, AC on):

   ```bash
   podman-compose --profile can-tools -f docker-compose.yml run --rm can-tools send 321#190F01
   ```

   Reload the page: the values changed. The frame was decoded by the ROS 2
   node using the DBC in `config/hvac.dbc`, written into its parameters, and
   picked up by the web console two seconds later.

4. **Listen to what it says.** Dump the state frames it emits every second:

   ```bash
   podman-compose --profile can-tools -f docker-compose.yml run --rm can-tools dump --count 3 --decode
   ```

5. **Break it.** Click *Inject HVAC Fault* on the console, then open
   <http://localhost:18080/api/v1/faults>. The fault code
   `GUARDIAN_HVAC_THERMAL_STATE` appears: the node published an ERROR
   diagnostic and ros2_medkit turned it into a REST fault. On the Guardian
   dashboard (<http://localhost:8094>) the Guardian now escalates from HVAC
   cooling to opening the window.

6. **Prove it all works** with one command:

   ```bash
   ./ros2-hvac/test-can-e2e.sh
   ```

You have just exercised every component in the map below. The rest of this
document explains what each one is and how to change it.

## Suggested reading order

| step | read | you will learn |
|---|---|---|
| 1 | [§2 Component map](#2-component-map), [§5 Data flows](#5-data-flows) | what talks to what, and in which direction |
| 2 | `ros2_ws/src/hack_to_the_future_hvac/hack_to_the_future_hvac/hvac_simulator.py` (module docstring first) | how a ROS 2 node owns state as parameters and publishes diagnostics |
| 3 | `launch/hvac.launch.py`, then `docker-compose.yml` service `ros2-hvac` | how configuration flows: compose env → launch args → parameters |
| 4 | `runtime/deploy_stack.py`, `runtime/hvac_stack_archive.json`, `build-hvac-artifact.sh` | how Eclipse Muto deploys a stack from an artifact |
| 5 | `services/src/bin/ros2_hvac_bridge.rs` (module doc first) | how ROS 2 state becomes uProtocol events over Zenoh |
| 6 | `config/hvac.dbc`, `test/test_can_codec.py`, [CAN_INTEGRATION.md](CAN_INTEGRATION.md) | how the CAN contract is defined and tested |
| 7 | [§10 Starting your challenge](#10-starting-your-challenge-from-this-template) | where to make your changes |

Unfamiliar terms (stack, DBC, VSS, DDS, medkit, …) are in the
[glossary](../README.md#glossary) of the main README.

Contents:

1. [What it does](#1-what-it-does)
2. [Component map](#2-component-map)
3. [Role of each Eclipse project](#3-role-of-each-eclipse-project)
4. [Other building blocks](#4-other-building-blocks)
5. [Data flows](#5-data-flows)
6. [Directory guide](#6-directory-guide)
7. [Ports, topics and parameters](#7-ports-topics-and-parameters)
8. [Startup sequence](#8-startup-sequence)
9. [Development loop](#9-development-loop)
10. [Starting your challenge from this template](#10-starting-your-challenge-from-this-template)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. What it does

A ROS 2 node, `/hvac_simulator`, stands in for the vehicle's HVAC (heating,
ventilation, air conditioning) controller. Its state is four ROS 2 parameters:

| parameter | type | meaning |
|---|---|---|
| `target_temperature_celsius` | int | cabin setpoint, clamped 16..30 |
| `air_conditioning_active` | bool | cooling requested |
| `fan_speed_percent` | int | blower speed, clamped 0..100 |
| `fault_active` | bool | simulated HVAC fault |

Three things can change that state, and all three see each other's changes
because the parameters are the single source of truth:

- **The Guardian** (via uProtocol over Zenoh, through the Rust bridge).
- **A CAN bus** (command frames decoded by the node itself).
- **A human** (the fault console web page, or `ros2 param set`).

The node also publishes ROS 2 diagnostics. `ros2_medkit` turns an ERROR
diagnostic into a REST fault, which is how a simulated HVAC fault reaches the
Guardian dashboard and triggers escalation from "cool the cabin" to "open the
window".

## 2. Component map

Everything below the dashed line runs inside the single `ros2-hvac` container.

```
 host ports        18081 (fault console)      18080 (medkit REST)      3000 (medkit web UI)
                        │                          │                          │
 compose network   zenohd:7447  ◄──── uProtocol/Zenoh ────►  guardian, temperature-sim,
                        │                                    dashboard, cda-sim
                        │
 ┌ ros2-hvac ───────────┼──────────────────────────────────────────────────────────────┐
 │                      ▼                                                              │
 │   ros2_hvac_bridge (Rust, axum + up-rust)                                           │
 │     • subscribes uds/hvac/cmd, publishes hvac/state + VSS events                    │
 │     • fault console  GET /  GET /api/state  POST /api/fault                         │
 │     • read/write ROS 2 params via `ros2 param` ──────────────┐                      │
 │                                                              │                      │
 │   Eclipse Muto runtime (agent, twin, composer + plugins)     │                      │
 │     ▲ /muto/stack  ◄── deploy_stack.py (publishes the        │                      │
 │     │                  stack manifest once at boot)          │                      │
 │     └─► provision: GET http://artifact-server:9090/hvac_simulator.tar.gz            │
 │         compose:   colcon build  ~/.muto/workspaces/guardian_hvac_simulator         │
 │         launch:    run.sh ─► ros2 launch hvac.launch.py                             │
 │                                                              ▼                      │
 │   /hvac_simulator (Python, rclpy)          ◄── parameters (single source of truth)  │
 │     • publishes /diagnostics ──────────► ros2_medkit_gateway ──► REST :8080         │
 │     • CAN bridge (python-can + cantools, hvac.dbc)                                  │
 │           RX 0x321 commands ─┐  ┌─ TX 0x320 state every 1 s                         │
 └──────────────────────────────┼──┼──────────────────────────────────────────────────┘
                                ▼  ▼
                    CAN bus: udp_multicast 239.74.163.2 (any OS)  or  socketcan vcan0 (Linux)
                                ▲
                    can-tools container: send 321#160A01 / dump --decode
```

Outside the container, in the same compose file:

| service | role for ros2-hvac |
|---|---|
| `zenohd` | Eclipse Zenoh router; every uProtocol message in the stack goes through it |
| `artifact-server` | plain HTTP server for `artifacts/`, where Muto downloads the HVAC stack from |
| `guardian` | consumes `hvac/state`; decides on HVAC vs. window mitigation |
| `cda-sim` | publishes `uds/hvac/cmd` (the Guardian's HVAC requests, via the CDA path) |
| `temperature-sim` | consumes `hvac/state` to cool the virtual cabin faster when AC is on |
| `dashboard` | shows `hvac/state`, the fault console state and medkit faults on one page |
| `medkit-web-ui` | the upstream ros2_medkit web UI, pointed at `http://localhost:18080` |
| `can-tools` | on-demand `cansend`/`candump` for the containerized CAN bus |
| `ros2-hvac-host-can` | Linux-only variant of `ros2-hvac` on real SocketCAN |

## 3. Role of each Eclipse project

| project | where it appears here | what it does for this workload |
|---|---|---|
| **Eclipse Muto** ([agent](https://github.com/eclipse-muto/agent), [core](https://github.com/eclipse-muto/core), [composer](https://github.com/eclipse-muto/composer), [messages](https://github.com/eclipse-muto/messages)) | built from source in `Dockerfile`; configured by `runtime/muto.launch.py` and `runtime/muto.yaml`; driven by `runtime/deploy_stack.py` | ROS 2 orchestration. Muto receives a *stack* manifest (`runtime/hvac_stack_archive.json`), downloads the referenced tarball, verifies its sha256, builds it with `colcon`, and launches it. In a fleet the manifest arrives from the cloud (Eclipse Ditto/Hono over MQTT via Muto's gateway); here `deploy_stack.py` publishes it locally, so the workload is deployed exactly the way it would be over the air. Muto's *twin* keeps the record of what is deployed. |
| **Eclipse uProtocol** ([up-rust](https://github.com/eclipse-uprotocol/up-rust)) | `services/src/bin/ros2_hvac_bridge.rs`, shared helpers in `services/src/lib.rs` | The application-level messaging contract of the whole Guardian stack: typed events with VSS-style URIs (`Vehicle.Cabin.HVAC.*`), publish/subscribe and RPC that are independent of the transport. The bridge is a uProtocol participant like every other service. |
| **Eclipse Zenoh** ([up-transport-zenoh](https://github.com/eclipse-uprotocol/up-transport-zenoh-rust)) | `zenohd` compose service; `ZENOH_CONNECT` env var | The transport under uProtocol. All services connect to the `zenohd` router; the bridge publishes HVAC state and receives commands through it. |
| **Eclipse SDV** (working group) | the challenge itself | Umbrella under which Muto, uProtocol, Zenoh, openDuT and ThreadX are developed. This template demonstrates their composition. |
| **Eclipse openDuT** | `ros2-hvac-host-can` compose service, appendix of `CAN_INTEGRATION.md` | Test-bench orchestration for real hardware. The Linux SocketCAN variant of this workload is the path to plug the HVAC simulator into an openDuT/EDGAR bench, where CAN frames are forwarded between hosts. Only `CAN_BUSTYPE`/`CAN_CHANNEL` change. |
| **Eclipse ThreadX** | not in this directory (see `threadx-temp-sensor/`) | Mentioned for orientation: the MCU-side temperature sensor of the same stack; it reaches the Guardian over SOME/IP, not through ros2-hvac. |

## 4. Other building blocks

| component | role |
|---|---|
| **ROS 2 Humble** (`rclpy`, `launch`, `diagnostic_msgs`) | runtime for the simulator and for Muto itself. Parameters are the state store, `/diagnostics` is the health channel. |
| **ros2_medkit** (`ros2_medkit_gateway`, `ros2_medkit_web_ui`) | REST gateway over ROS 2. Started with `enable_diagnostic_bridge:=true`, it maps ERROR statuses on `/diagnostics` to faults at `GET /api/v1/faults` (fault code `GUARDIAN_HVAC_THERMAL_STATE`). Also exposes nodes, parameters and topics for inspection. |
| **python-can** + **cantools** | CAN access and the DBC codec inside the simulator. `udp_multicast` makes a real bus semantics (shared medium, arbitration IDs, filters) available with no kernel support. |
| **can-utils** / **SocketCAN** | Linux-only real CAN path (`ros2-hvac-host-can`), interoperable with `cansend`/`candump` and hardware. |
| **axum** + **tokio** | HTTP server and async runtime of the Rust bridge. |

## 5. Data flows

**Guardian asks for cooling**

```
guardian ─RPC─► actuation-adapter ─SOVD REST─► cda-sim ─uProtocol uds/hvac/cmd─► ros2_hvac_bridge
ros2_hvac_bridge ─`ros2 param set`─► /hvac_simulator  (temperature, AC, fan only)
ros2_hvac_bridge ─uProtocol hvac/state + VSS events─► guardian, temperature-sim, dashboard
```

**A CAN device sets the HVAC**

```
can-tools send 321#190F01 ─► /hvac_simulator decodes HVAC_COMMAND via hvac.dbc ─► parameters
ros2_hvac_bridge read-back loop (2 s) sees the change ─► republishes hvac/state
/hvac_simulator TX loop (1 s) ─► HVAC_STATE frame 0x320 ─► every bus participant
```

**Operator injects a fault**

```
browser :18081 ─POST /api/fault─► ros2_hvac_bridge ─`ros2 param set fault_active`─► /hvac_simulator
/hvac_simulator publishes guardian_hvac/thermal_state = ERROR on /diagnostics
ros2_medkit_gateway ─► GET :18080/api/v1/faults shows GUARDIAN_HVAC_THERMAL_STATE
ros2_hvac_bridge publishes effective AC = false ─► guardian escalates to window mitigation
```

Ownership rule that keeps the three flows consistent: **each writer touches
only the fields it owns.** Commands write temperature/AC/fan, the fault
console writes `fault_active`, CAN writes all four (it is "the ECU"). Nobody
pushes a cached full state.

## 6. Directory guide

```
ros2-hvac/
├── README.md                    this file
├── CAN_INTEGRATION.md           CAN how-to: frames, tools, platforms, SocketCAN appendix
├── Dockerfile                   ros2-hvac image (Rust bridge stage + ROS 2/Muto/medkit stage)
├── start-hvac-stack.sh          container entrypoint: Muto ▸ deploy ▸ medkit ▸ bridge
├── build-hvac-artifact.sh       packages the ROS package into artifacts/ and updates the checksum
├── artifact-run.sh              becomes run.sh inside the tarball; what Muto executes
├── test-can-e2e.sh              containerized end-to-end smoke test
├── artifacts/hvac_simulator.tar.gz   the Muto stack archive served by artifact-server
├── runtime/                     Muto bootstrap, copied to /opt/muto_runtime in the image
│   ├── muto.launch.py             starts the Muto nodes (agent, gateway, twin, composer, plugins)
│   ├── muto.yaml                  shared Muto parameters (topic names, vehicle identity)
│   ├── hvac_stack_archive.json    the stack manifest: artifact URL + sha256 + entry script
│   └── deploy_stack.py            publishes the manifest to /muto/stack once at boot
├── can-tools/                   cansend/candump equivalents (python-can) for any host OS
│   ├── Dockerfile
│   └── can_tool.py
└── ros2_ws/src/hack_to_the_future_hvac/     the ROS 2 package (the actual workload)
    ├── package.xml, setup.py, setup.cfg, resource/
    ├── hack_to_the_future_hvac/hvac_simulator.py   the node: parameters, diagnostics, CAN bridge
    ├── launch/hvac.launch.py                       launch args ▸ env vars ▸ defaults
    ├── config/hvac.dbc                             CAN frame contract (HVAC_STATE / HVAC_COMMAND)
    └── test/test_can_codec.py                      unit tests for the codec and DBC consistency
```

`.artifact-stage/` is a git-ignored build intermediate of `build-hvac-artifact.sh` and
mirrors the package; do not edit it.

The stack manifest (`runtime/hvac_stack_archive.json`) is plain JSON and
cannot carry comments; its fields are:

| field | meaning |
|---|---|
| `metadata.name` | stack name; also the workspace directory under `/root/.muto/workspaces/` |
| `metadata.content_type` | `stack/archive`: Muto downloads and builds a tarball |
| `launch.url` | where Muto's provision plugin fetches the tarball |
| `launch.properties.checksum` | sha256 of the tarball, written by `build-hvac-artifact.sh` |
| `launch.properties.launch_file` | the script Muto's launch plugin runs (`run.sh`) |
| `launch.properties.flatten` | extract the tarball at the workspace root |

## 7. Ports, topics and parameters

**Host ports**

| port | service | what |
|---|---|---|
| 18081 | ros2-hvac | HVAC fault console (`/`, `/api/state`, `/api/fault`, `/health`) |
| 18080 | ros2-hvac | ros2_medkit REST API (`/api/v1/faults`, nodes, parameters, ...) |
| 3000 | medkit-web-ui | upstream ros2_medkit web UI (gateway URL `http://localhost:18080`, base `api/v1`) |
| 7447 | zenohd | Zenoh router |

**uProtocol topics used by the bridge** (URIs defined in `services/src/lib.rs`)

| direction | topic | payload |
|---|---|---|
| in | `up/sdv/guardian/uds/hvac/cmd` | `HvacCommand` {request_id, target_temperature_celsius, air_conditioning_active, fan_speed_percent} |
| out | `up/sdv/guardian/hvac/state` | `HvacStateEvent` {target, ac active (effective), fan, fault, timestamp} |
| out | `up/sdv/guardian/vss/Vehicle.Cabin.HVAC.Station.Row1.Left.Temperature` | `HvacSetTemperatureEvent` |
| out | `up/sdv/guardian/vss/Vehicle.Cabin.HVAC.IsAirConditioningActive` | `HvacActiveStateEvent` |

**ROS 2 interfaces**

| kind | name | notes |
|---|---|---|
| node | `/hvac_simulator` | the workload |
| topic | `/diagnostics` | `guardian_hvac/thermal_state`, `guardian_hvac/can_bridge` |
| topic | `/muto/stack`, `/muto/twin`, ... | Muto internals (`runtime/muto.yaml`) |
| params | see section 1 plus `can_*` | `can_tx_id`/`can_rx_id` are strings so `0x320` works |

**CAN** (details in `CAN_INTEGRATION.md`): 11-bit IDs, TX `0x320` state, RX
`0x321` command, 3-byte payload `[temp °C, fan %, flags(bit0 AC, bit1 fault)]`,
transport from `CAN_BUSTYPE`/`CAN_CHANNEL`.

## 8. Startup sequence

What happens when `ros2-hvac` starts (`start-hvac-stack.sh`), with typical
timings on a laptop:

| t | step |
|---|---|
| 0 s | `ros2 launch muto.launch.py`: Muto agent, MQTT gateway, commands plugin, twin, composer and its three plugins start |
| 8 s | `deploy_stack.py` starts, waits 3 s for DDS discovery, publishes `hvac_stack_archive.json` on `/muto/stack`, exits |
| 8 s | `ros2_medkit_gateway` starts serving on :8080 |
| 8 s | `ros2_hvac_bridge` starts (foreground), connects to `zenohd`, serves :8093; its read-back loop warns until the simulator exists |
| ~12 s | Muto provision plugin downloads the tarball from `artifact-server`, verifies the checksum; compose plugin runs `colcon build` |
| ~15 s | Muto launch plugin runs `run.sh` → `ros2 launch hvac.launch.py`; `/hvac_simulator` is up, CAN bridge connected |

The simulator's log is `/root/.muto/workspaces/guardian_hvac_simulator/run.log`
inside the container, not the container's stdout, because Muto owns the
process.

## 9. Development loop

Edit the ROS package under `ros2_ws/src/hack_to_the_future_hvac/`, then:

```bash
./ros2-hvac/build-hvac-artifact.sh                                  # tarball + checksum
podman-compose --profile ros2 -f docker-compose.yml build ros2-hvac # checksum is baked into the image
podman rm -f hack-to-the-future_medkit-web-ui_1 hack-to-the-future_ros2-hvac_1
podman-compose --profile ros2 -f docker-compose.yml up -d --no-deps ros2-hvac medkit-web-ui
```

(`medkit-web-ui` depends on `ros2-hvac`, so podman needs both removed before
recreating. With Docker, `docker compose --profile ros2 up -d --build ros2-hvac`
is enough.)

**Unit tests** run inside the image without deploying anything:

```bash
podman run --rm --entrypoint bash \
  -v ./ros2-hvac/ros2_ws/src/hack_to_the_future_hvac:/pkg:ro \
  localhost/hack-to-the-future/ros2-hvac:stage-ros2 \
  -c "source /opt/ros/humble/setup.bash; cd /pkg && python3 -m pytest -p no:cacheprovider test/ -v"
```

**Try the launch file directly** (no Muto) inside the image:

```bash
podman run --rm --entrypoint bash -e CAN_ENABLED=true -e CAN_BUSTYPE=udp_multicast \
  -v ./ros2-hvac/ros2_ws/src/hack_to_the_future_hvac:/pkg:ro \
  localhost/hack-to-the-future/ros2-hvac:stage-ros2 -c '
  source /opt/ros/humble/setup.bash
  mkdir -p /tmp/ws/src && cp -R /pkg /tmp/ws/src/hack_to_the_future_hvac && cd /tmp/ws
  colcon build && source install/setup.bash
  ros2 launch hack_to_the_future_hvac hvac.launch.py can_tx_id:=0x330'
```

**End-to-end smoke test** against the running stack (any OS):

```bash
./ros2-hvac/test-can-e2e.sh          # COMPOSE_CMD="docker compose" for Docker
```

Changes to the Rust bridge (`services/src/bin/ros2_hvac_bridge.rs`) are
compiled by the image build; no local Rust toolchain is required.

## 10. Starting your challenge from this template

Typical ways teams extend this workload:

- **Add a ROS 2 node or topic.** Put it in the same package, add it to
  `hvac.launch.py`, rebuild the artifact. Muto deploys whatever the tarball
  contains; nothing else changes.
- **Expose new state to the Guardian.** Add a parameter to the simulator,
  read it in the bridge's `read_ros2_hvac_parameters`, add a field to
  `HvacStateEvent` in `services/src/lib.rs`. Consumers pick it up over Zenoh.
- **Accept a new command.** Extend `HvacCommand` and the listener in the
  bridge; write the new field with `ros2 param set` like the others. Respect
  the ownership rule in section 5.
- **Change the CAN contract.** Edit `config/hvac.dbc` *and* the manual codec
  in `hvac_simulator.py`; `test_can_codec.py` fails until they agree. The
  `can-tools dump --decode` output follows the DBC automatically.
- **Move to real hardware.** Use the `ros2-hvac-host-can` service on a Linux
  host or VM and point `CAN_CHANNEL` at `can0`; nothing in the code changes.
- **Deploy a second stack with Muto.** Copy `runtime/hvac_stack_archive.json`,
  give it a new `metadata.name` and `launch.url`, and publish it with
  `deploy_stack.py -p stack_path:=...`. Muto keeps one workspace per stack.

Things intentionally kept simple that a production system would do
differently: the stack manifest is published locally instead of coming from a
cloud backend; the bridge shells out to the `ros2` CLI instead of using a
native client; the HVAC physics is a setpoint, not a thermal model
(`temperature-sim` owns the cabin model).

## 11. Troubleshooting

| symptom | check |
|---|---|
| no `/hvac_simulator` node | `cat /root/.muto/workspaces/guardian_hvac_simulator/run.log` in the container; a launch error (e.g. a parameter type mismatch) shows there, not in `podman logs` |
| Muto never deploys | `artifact-server` must be on the same compose network; checksum in `runtime/hvac_stack_archive.json` must match `artifacts/hvac_simulator.tar.gz` (rerun `build-hvac-artifact.sh` and rebuild the image) |
| bridge logs `failed to read ROS2 HVAC parameters` | normal for the first ~15 s; persistent means the simulator is not running (see above) |
| CAN commands ignored | `guardian_hvac/can_bridge` on `/diagnostics` (`rx_frames`, `connected`); see `CAN_INTEGRATION.md` §10 |
| fault not visible in medkit | `curl localhost:18080/api/v1/faults`; the diagnostic bridge only reports ERROR level, so `fault_active` must be true |
| `podman-compose up --force-recreate ros2-hvac` fails | remove `medkit-web-ui` first; it depends on `ros2-hvac` |
