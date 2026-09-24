# Guardian Loop — SDV Building Blocks

> **Who this is for:** you have read the mission in [README.md](README.md) and
> run the reference stack with [Tutorial.md](Tutorial.md). Now you are asking
> *"which existing project do I copy from to build part X?"* This document is
> that map. Skim the two tables first ([overview](#-building-block-overview)
> and [I want to…](#-i-want-to-quick-guide)), then read only the numbered
> sections you need.

The **Guardian Loop** should not be implemented from scratch.

A large part of the functionality needed for this challenge already exists as examples in the Eclipse SDV ecosystem.

The goal is to **reuse existing SDV patterns as building blocks** and combine them into one end-to-end feature:

> **Sense → Communicate → Decide → Actuate → Replace Simulation with Hardware**

The main challenge is integration and portability.

The Guardian business logic should remain unchanged when simulated endpoints are replaced with physical hardware.

---

# 🧱 Building Block Overview

Start with what is already in **this repository**; it is the closest match
to the challenge and runs with one `docker compose up`:

| What you need | In this repository | What to reuse |
|---|---|---|
| Guardian state machine + uProtocol pub/sub + RPC client (Rust) | [`services/src/bin/guardian.rs`](services/src/bin/guardian.rs), shared types and URIs in [`services/src/lib.rs`](services/src/lib.rs) | Working Stage 1–2 Guardian: thresholds, staged mitigation (HVAC first, then window), `/state` HTTP endpoint |
| Simulated sensors publishing VSS events | [`child_presence_sim.rs`](services/src/bin/child_presence_sim.rs), [`temperature_sim.rs`](services/src/bin/temperature_sim.rs) | Minimal publisher pattern with retry; a scripted, closed-loop thermal model |
| Actuation Adapter → CDA → ECU chain (simulated) | [`actuation_adapter.rs`](services/src/bin/actuation_adapter.rs), [`cda_sim.rs`](services/src/bin/cda_sim.rs), [`window_controller_sim.rs`](services/src/bin/window_controller_sim.rs) | uProtocol RPC server, diag/UDS command topics, simulated window ECU with `/state` endpoint |
| Deploying a ROS 2 workload with Eclipse Muto, observing it with ros2_medkit, bridging it to CAN | [`ros2-hvac/`](ros2-hvac/README.md) | Complete template: stack manifest, artifact build, launch args, DBC codec, unit + e2e tests |
| ThreadX sensor + SOME/IP → uProtocol | [`threadx-temp-sensor/`](threadx-temp-sensor/), [`someip_uprot_bridge.rs`](services/src/bin/someip_uprot_bridge.rs) | Stage 4 path: embedded sensor emitting SOME/IP, bridged into the same VSS topic as the simulator |
| Live observation | [`dashboard.rs`](services/src/bin/dashboard.rs) | One page showing every event in the loop; copy its fetch/poll pattern for your own UI |

Then reach for the wider Eclipse SDV ecosystem:

| What you need | Example / Building Block | What to reuse |
|---|---|---|
| uProtocol RPC Client | [Service-to-Signal Horn Client](https://github.com/eclipse-sdv-blueprints/service-to-signal/blob/main/components/horn-client/src/horn_client.rs) | Rust pattern for invoking a uProtocol service |
| uProtocol Service | [Horn Service Kuksa](https://github.com/eclipse-sdv-blueprints/service-to-signal/tree/main/components/horn-service-kuksa) | Example uProtocol service provider |
| Service → Signal → Hardware | [Service-to-Signal Blueprint](https://github.com/eclipse-sdv-blueprints/service-to-signal) | Complete service-to-embedded-actuator flow |
| Embedded Actuator | [Actuator Provider](https://github.com/eclipse-sdv-blueprints/service-to-signal/tree/main/components/actuator-provider) | ESP32 / Zenoh hardware integration |
| Transport-independent uProtocol | [Fleet Management](https://github.com/eclipse-sdv-blueprints/fleet-management) | Same application logic over different transports |
| Vehicle Application + Signals | [Companion Application](https://github.com/eclipse-sdv-blueprints/companion-application) | Vehicle application reading and actuating vehicle signals |
| uService → SOVD → UDS | [Commercial SDV Stack](https://github.com/eclipse-sdv-blueprints/commercial-sdv-stack) | High-level service controlling a classic ECU |
| uService Definition | [Powertrain AsyncAPI](https://github.com/eclipse-sdv-blueprints/commercial-sdv-stack/blob/main/uservices/powertrain/Powertrain-asyncapi.yaml) | Template for defining Guardian / Window services |
| SOVD + OpenBSW | [OpenBSW SOVD Demo](https://github.com/Eclipse-SDV-HackFest-Esslingen-2026/OpenBSW-Playground/tree/main/OpenBSW-SOVD-Demo) | SOVD → DoIP → UDS → OpenBSW |
| ThreadX Sensor ECU | [AZ3166 ThreadX Example](https://github.com/chheis/challenge-threadx-playRemote/blob/4f9cac54efcd383f1cadcedb4aa3c93a97ba9dd0/MXChip/AZ3166/app/main.c#L770) | Temperature sensor running on embedded hardware |
| HPC + MCUs + CAN | [E2E Vehicle Signals](https://github.com/eclipse-sdv-blueprints/e2e-vehicle-signals) | Physical HPC / MCU / CAN / ThreadX integration |
| Software Orchestration | [Software Orchestration Blueprint](https://github.com/eclipse-sdv-blueprints/software-orchestration) | Ankaios / BlueChi workload deployment |
| Dynamic Edge Software | [ROS Racer](https://github.com/eclipse-sdv-blueprints/ros-racer) | Dynamic deployment, OTA and rollback patterns |
| Hazard / Event Processing | [Insurance Blueprint](https://github.com/eclipse-sdv-blueprints/insurance) | Signal monitoring and event detection pattern |

---

# 🧩 How the Pieces Fit Together

The Guardian Loop itself should stay relatively small.

```text
                     ┌─────────────────────┐
                     │    Guardian Loop    │
                     │   Rust / AutoSD     │
                     └──────────┬──────────┘
                                │
             ┌──────────────────┼──────────────────┐
             │                  │                  │
             ▼                  ▼                  ▼

      Child Presence      Cabin Temperature     Vehicle Action
          Service              Service              Service

             │                  │                  │
             │                  │                  ▼
             │                  │             SOVD / CDA
             │                  │                  │
             │                  │                  ▼
             │                  │              DoIP / UDS
             │                  │                  │
             │                  │                  ▼
             │                  │               OpenBSW
             │                  │                  │
             │                  │                  ▼
             │                  │            Window / Fan / Horn
```

The Guardian Loop should know about concepts such as:

```text
ChildPresent
CabinTemperature
OpenWindow
StartFan
SoundAlarm
```

It should **not** know about:

```text
CAN
GPIO
UDS
DoIP
SOME/IP
Serial Ports
ECU DIDs
Hardware Addresses
```

Those details belong to adapters and services.

---

# 1. Child Presence Sensor

Start with a simulated child-presence service.

```text
child-presence-sim
        │
        │ uProtocol Publish
        ▼
ChildPresenceEvent
        │
        ▼
Guardian Loop
```

A simple service definition could look like:

```protobuf
message ChildPresenceEvent {
    bool present = 1;
    float confidence = 2;
    string zone = 3;
    uint64 timestamp_ms = 4;
}
```

For the first milestone, a CLI or small simulator is enough.

Example:

```text
$ child-presence-sim --present true
```

Later the simulator can be replaced by a real sensor.

```text
Simulation
    ↓
Real Child Presence Sensor
```

The Guardian Loop should not change.

### Useful reference

Use the uProtocol event / messaging patterns from:

* [Fleet Management Blueprint](https://github.com/eclipse-sdv-blueprints/fleet-management)

---

# 2. Cabin Temperature Sensor

Start with a virtual temperature publisher.

```text
temperature-sim
      │
      │ uProtocol Publish
      ▼
CabinTemperatureEvent
      │
      ▼
Guardian Loop
```

Example message:

```protobuf
message CabinTemperatureEvent {
    float temperature_celsius = 1;
    uint64 timestamp_ms = 2;
    SensorStatus status = 3;
}
```

The simulator should make it easy to create a dangerous scenario.

Example:

```text
25 °C
28 °C
32 °C
36 °C
40 °C
43 °C
```

---

# 3. Replace the Temperature Simulator with ThreadX Hardware

A physical temperature sensor can be implemented using the **AZ3166** board running **Eclipse ThreadX**.

Useful example:

[AZ3166 ThreadX Example](https://github.com/chheis/challenge-threadx-playRemote/blob/4f9cac54efcd383f1cadcedb4aa3c93a97ba9dd0/MXChip/AZ3166/app/main.c#L770)

The existing example already reads the onboard sensor:

```c
lsm6dsl_data_t lsm6dsl_data = lsm6dsl_data_read();

printf(
    "Temperature: %d\r\n",
    (int)lsm6dsl_data.temperature_degC
);
```

Your task is therefore **not to build the sensor driver**.

Your task is to expose the value to the Guardian architecture.

```text
AZ3166
  │
  │ Eclipse ThreadX
  ▼
Temperature Sensor
  │
  │ SOME/IP / uProtocol integration
  ▼
CabinTemperatureEvent
  │
  ▼
Guardian Loop
```

The service interface should stay identical to the simulated temperature sensor.

---

# 4. Guardian Decision Service

This is the main feature participants should build.

Guardian subscribes to:

```text
Child Presence
Cabin Temperature
Optional Vehicle State
Optional Lock State
Optional elapsed time
```

and calculates the current Guardian state.

For example:

```text
CLEAR
MONITORING
WARNING
CRITICAL
MITIGATING
```

Example logic:

```text
No child
    ↓
CLEAR

Child present
    ↓
MONITORING

Child + elevated temperature
    ↓
WARNING

Child + dangerous temperature
    ↓
CRITICAL

Mitigation successfully started
    ↓
MITIGATING
```

A simplified implementation could look like:

```rust
match guardian_state {
    GuardianState::Clear => {}

    GuardianState::Monitoring => {}

    GuardianState::Warning => {
        alarm_client.warn().await?;
    }

    GuardianState::Critical => {
        window_client.open(25).await?;
        alarm_client.activate().await?;
    }

    GuardianState::Mitigating => {}
}
```

---

# 5. Learn uProtocol RPC from the Horn Client

One of the best starting points for the Guardian service is the existing:

[Horn Client](https://github.com/eclipse-sdv-blueprints/service-to-signal/blob/main/components/horn-client/src/horn_client.rs)

The implementation already demonstrates:

```text
Create request
    ↓
Serialize Protobuf
    ↓
Build uProtocol payload
    ↓
Resolve uProtocol URI
    ↓
Invoke RPC method
    ↓
Process response
```

The important code pattern is:

```rust
self.rpc_client
    .invoke_method(
        resource_uri,
        CallOptions::for_rpc_request(...),
        Some(payload),
    )
    .await
```

Instead of implementing:

```text
activateHorn()
```

Guardian could expose or consume operations such as:

```text
openWindow()
setFanLevel()
activateAlarm()
sendEmergencyNotification()
```

---

# 6. Reuse the Existing Horn Service

The first mitigation action can be implemented almost completely from existing code.

The Service-to-Signal Blueprint already contains:

```text
Guardian
   │
   │ uProtocol RPC
   ▼
Horn Service
   │
   ▼
Kuksa Databroker
   │
   ▼
Zenoh
   │
   ├── Software Horn
   │
   └── ESP32 Actuator
```

Reference:

[Service-to-Signal Blueprint](https://github.com/eclipse-sdv-blueprints/service-to-signal)

The Horn Service implements the COVESA Horn uService.

Reference:

[Horn Service Kuksa](https://github.com/eclipse-sdv-blueprints/service-to-signal/tree/main/components/horn-service-kuksa)

That means a very early Guardian milestone can already be:

```text
Child Present
      +
Temperature Critical
      ↓
Guardian
      ↓
ActivateHorn()
      ↓
Existing Horn Service
```

This gives participants a working end-to-end mitigation path early in the hackathon.

---

# 7. Software Horn → Physical Horn

The Service-to-Signal Blueprint already demonstrates an important principle of this challenge.

Start with:

```text
Horn Service
    ↓
Software Horn
```

Then replace it with:

```text
Horn Service
    ↓
ESP32 Actuator Provider
    ↓
Physical Output
```

Reference:

[Actuator Provider](https://github.com/eclipse-sdv-blueprints/service-to-signal/tree/main/components/actuator-provider)

Guardian does not change.

This is exactly the same pattern expected for the window controller.

---

# 8. Window Control uService

The window should be exposed as a high-level vehicle service.

Guardian should call something like:

```text
SetWindowPosition(
    window = REAR_LEFT,
    position = 25
)
```

Guardian should **not** directly call UDS.

Recommended architecture:

```text
Guardian Loop
      │
      │ uProtocol RPC
      ▼
Window Control uService
      │
      │ SOVD REST
      ▼
OpenSOVD CDA
      │
      │ DoIP / UDS
      ▼
OpenBSW ECU
      │
      ▼
Window Motor
```

---

# 9. Copy the Commercial SDV Stack Pattern

The Commercial SDV Stack already demonstrates almost exactly this architecture.

Existing example:

```text
Fleet Management
       │
       │ uProtocol RPC
       ▼
Powertrain Mode Controller
       │
       │ SOVD HTTP
       ▼
Classic Diagnostic Adapter
       │
       │ UDS
       ▼
Powertrain ECU
```

Reference:

[Commercial SDV Stack](https://github.com/eclipse-sdv-blueprints/commercial-sdv-stack)

Guardian should reuse this architectural pattern:

```text
Guardian
       │
       │ uProtocol RPC
       ▼
Window Control Service
       │
       │ SOVD HTTP
       ▼
Classic Diagnostic Adapter
       │
       │ UDS
       ▼
Window ECU
```

The existing Powertrain uService can also serve as a template:

[Powertrain uService](https://github.com/eclipse-sdv-blueprints/commercial-sdv-stack/tree/main/uservices/powertrain)

The service contract can use the existing AsyncAPI definition as inspiration:

[Powertrain AsyncAPI](https://github.com/eclipse-sdv-blueprints/commercial-sdv-stack/blob/main/uservices/powertrain/Powertrain-asyncapi.yaml)

---

# 10. OpenSOVD + OpenBSW

For the ECU side, use the existing OpenBSW SOVD demo.

Reference:

[OpenBSW SOVD Demo](https://github.com/Eclipse-SDV-HackFest-Esslingen-2026/OpenBSW-Playground/tree/main/OpenBSW-SOVD-Demo)

It already provides:

```text
REST Client
    ↓
SOVD CDA
    ↓
DoIP
    ↓
UDS
    ↓
OpenBSW ECU
```

It also provides:

```text
OpenBSW POSIX / FreeRTOS
SOVD
DoIP
UDS
DIDs
DTCs
Docker setup
Grafana
```

Participants should **extend this example**, not rebuild the diagnostic stack.

For example, add:

```text
WindowPosition
```

as a diagnostic value.

Example:

```text
DID CF20
WindowPosition
0 ... 100 %
```

or implement an appropriate diagnostic routine.

---

# 11. Start with a Simulated Window

The first version can run completely in software.

```text
Guardian
   ↓
Window Control uService
   ↓
CDA
   ↓
OpenBSW POSIX ECU
   ↓
RestBus / simulated window
```

This is the **Software-in-the-Loop** setup.

The Guardian logic can now be fully tested without physical hardware.

---

# 12. Replace the Simulated Window with Hardware

Later replace:

```text
OpenBSW POSIX ECU
      +
Virtual Window
```

with:

```text
OpenBSW / Embedded Controller
      +
Window Motor + CLA
```

The call from Guardian stays:

```text
SetWindowPosition(25%)
```

Only the southbound implementation changes.

---

# 13. HPC Architecture

The Guardian Loop should ultimately run on the supplied AutoSD-based HPC.

The existing E2E Vehicle Signals Blueprint provides a good example of how an HPC interacts with multiple embedded controllers.

Reference:

[E2E Vehicle Signals Blueprint](https://github.com/eclipse-sdv-blueprints/e2e-vehicle-signals)

That blueprint already combines:

```text
Raspberry Pi HPC
Eclipse Ankaios
Kuksa Databroker
CAN Provider
Arduino ECUs
ThreadX ECU
Input devices
Physical outputs
```

Its existing flow looks approximately like:

```text
Input ECU
   ↓
MQTT
   ↓
Bridge
   ↓
Kuksa
   ↓
CAN Provider
   ↓
CAN
   ↓
Actuator ECU
```

Guardian uses the same general architecture, but with a different use case:

```text
Child Sensor
      +
Temperature Sensor
      ↓
Guardian
      ↓
Window / Fan / Alarm
```

---

# 14. Software Orchestration

If teams want to manage Guardian as a dynamically deployed vehicle workload, use:

[Software Orchestration Blueprint](https://github.com/eclipse-sdv-blueprints/software-orchestration)

It contains examples based on:

```text
Eclipse Ankaios
Eclipse BlueChi
```

This can be used for:

```text
Deploy Guardian
Start Guardian
Stop Guardian
Update Guardian
Move Guardian between compute nodes
Restart Guardian
```

This is especially interesting on the AutoSD HPC.

---

# 15. openDuT — Switch the Vehicle Underneath Guardian

openDuT is what ties the challenge together.

Create multiple endpoint implementations.

```text
Temperature
├── temperature-sim
└── AZ3166 + ThreadX

Child Presence
├── child-presence-sim
└── physical sensor

Window
├── OpenBSW POSIX + RestBus
└── OpenBSW / CLA + Window Motor
```

Guardian always sees:

```text
ChildPresence
CabinTemperature
WindowControl
HornControl
```

---

# 🔁 Configuration A — Full SIL

```text
Child Presence Simulator
          │
          ▼
      uProtocol
          │
          ▼
     Guardian Loop
          ▲
          │
      uProtocol
          ▲
          │
Temperature Simulator


Guardian
   │
   ▼
Window uService
   │
   ▼
SOVD CDA
   │
   ▼
OpenBSW POSIX
   │
   ▼
Virtual Window
```

Everything runs in software.

---

# 🔀 Configuration B — Mixed SIL / HIL

Replace the temperature simulator.

```text
AZ3166
ThreadX
Temperature Sensor
      │
      ▼
Guardian
      │
      ▼
Virtual Window
```

Guardian stays unchanged.

---

# 🚗 Configuration C — HIL

Now replace the actuator.

```text
AZ3166
ThreadX
      │
      ▼
Guardian on AutoSD
      │
      ▼
Window uService
      │
      ▼
CDA
      │
      ▼
OpenBSW Embedded Controller
      │
      ▼
Physical Window Motor
```

Again:

> **Guardian stays unchanged.**

---

# ⭐ The Core Hackathon Moment

The most important part of the demo is not the temperature threshold.

It is this:

```text
Virtual Sensor
      ↓
Guardian
      ↓
Virtual Actuator
```

becomes:

```text
Real Sensor
      ↓
Guardian
      ↓
Virtual Actuator
```

and then:

```text
Real Sensor
      ↓
Guardian
      ↓
Real Actuator
```

without changing the Guardian business logic.

openDuT should manage the topology change.

uProtocol should keep the service interfaces stable.

---

# 📂 Where to look in the ecosystem

You should not have to search the whole SDV ecosystem. The two tables in this
document ([overview](#-building-block-overview) and the quick guide below) are
the curated map; every link points at the specific directory or file to read.
When a blueprint has many components, open only the one named in the table.

---

# 🧭 “I Want To…” Quick Guide

| I want to…                             | Start here                                                                                                                                            |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Call another vehicle service from Rust | [Horn Client](https://github.com/eclipse-sdv-blueprints/service-to-signal/blob/main/components/horn-client/src/horn_client.rs)                        |
| Implement a uProtocol service          | [Horn Service](https://github.com/eclipse-sdv-blueprints/service-to-signal/tree/main/components/horn-service-kuksa)                                   |
| Connect a service to hardware          | [Service-to-Signal](https://github.com/eclipse-sdv-blueprints/service-to-signal)                                                                      |
| Control a microcontroller              | [Actuator Provider](https://github.com/eclipse-sdv-blueprints/service-to-signal/tree/main/components/actuator-provider)                               |
| Read the AZ3166 temperature sensor     | [ThreadX Example](https://github.com/chheis/challenge-threadx-playRemote/blob/4f9cac54efcd383f1cadcedb4aa3c93a97ba9dd0/MXChip/AZ3166/app/main.c#L770) |
| Define a new uService                  | [Powertrain AsyncAPI](https://github.com/eclipse-sdv-blueprints/commercial-sdv-stack/blob/main/uservices/powertrain/Powertrain-asyncapi.yaml)         |
| Call an ECU through SOVD               | [Commercial SDV Stack](https://github.com/eclipse-sdv-blueprints/commercial-sdv-stack)                                                                |
| Implement SOVD → UDS → OpenBSW         | [OpenBSW SOVD Demo](https://github.com/Eclipse-SDV-HackFest-Esslingen-2026/OpenBSW-Playground/tree/main/OpenBSW-SOVD-Demo)                            |
| Connect HPCs and embedded ECUs         | [E2E Vehicle Signals](https://github.com/eclipse-sdv-blueprints/e2e-vehicle-signals)                                                                  |
| Deploy workloads dynamically           | [Software Orchestration](https://github.com/eclipse-sdv-blueprints/software-orchestration)                                                            |
| Understand transport portability       | [Fleet Management](https://github.com/eclipse-sdv-blueprints/fleet-management)                                                                        |

---

# 🏁 Recommended Implementation Order

```text
1. Child Presence Simulator
            ↓
2. Temperature Simulator
            ↓
3. Guardian Logic
            ↓
4. Existing Horn uService
            ↓
5. Window Control uService
            ↓
6. OpenSOVD CDA
            ↓
7. OpenBSW simulated ECU
            ↓
8. Deploy Guardian on AutoSD
            ↓
9. Replace temperature-sim with AZ3166
            ↓
10. Replace window-sim with physical controller
            ↓
11. Switch configurations using openDuT
```

Do not start with all physical hardware connected.

Get one complete software loop working first.

---

# 🎯 What Participants Should Build

The challenge is **not** about rebuilding the Eclipse SDV stack.

Participants should focus on the missing Guardian-specific pieces:

```text
Guardian decision logic

ChildPresence service

CabinTemperature service

WindowControl service

Integration configuration

openDuT topology

End-to-end demo
```

Everything else should reuse existing SDV examples wherever possible.

---

# 🚫 Do Not Reinvent These

Do not spend hackathon time implementing:

```text
uProtocol from scratch
UDS from scratch
DoIP from scratch
SOVD from scratch
ThreadX sensor drivers from scratch
a custom vehicle signal broker
a custom workload orchestrator
```

Use the existing Eclipse building blocks.

---

# ✅ Definition of Done

A strong implementation demonstrates:

* [ ] simulated child presence
* [ ] simulated cabin temperature
* [ ] Guardian state machine
* [ ] uProtocol publish / subscribe
* [ ] uProtocol RPC
* [ ] existing Horn service integration
* [ ] Window Control uService
* [ ] SOVD / CDA integration
* [ ] OpenBSW simulated ECU
* [ ] Guardian running on AutoSD
* [ ] AZ3166 / ThreadX replacing the temperature simulator
* [ ] physical actuator replacing the window simulator
* [ ] openDuT switching between at least two configurations
* [ ] **no Guardian business-logic change when endpoints are replaced**

---

# 💡 The Architecture Principle

The challenge can be summarized in one sentence:

> **Build the feature once. Change the vehicle underneath it.**

The sensors may change.

The ECU may change.

The network may change.

The operating system may change.

The test bench may change.

But the Guardian Loop should keep running.

That is the Software-Defined Vehicle idea this challenge is meant to demonstrate.

> **Where we're going, we don't need cables.**
