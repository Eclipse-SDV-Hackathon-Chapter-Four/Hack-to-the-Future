//! `ros2_hvac_bridge`: connects the ROS 2 HVAC simulator to the rest of the
//! Guardian stack, which speaks Eclipse uProtocol over Eclipse Zenoh.
//!
//! It runs inside the `ros2-hvac` container next to the simulator (started
//! last by `ros2-hvac/start-hvac-stack.sh`) and has three jobs:
//!
//! 1. **Commands in.** Subscribe to HVAC commands (`uds/hvac/cmd`, published
//!    by the CDA simulator on behalf of the Guardian) and apply them to the
//!    simulator by writing its ROS 2 parameters.
//! 2. **State out.** Publish the HVAC state as uProtocol events (VSS setpoint,
//!    VSS "AC active", and a combined `hvac/state` event) for the Guardian,
//!    the temperature simulator and the dashboard.
//! 3. **Fault console.** Serve a small web page + JSON API on `PORT` (8093,
//!    host 18081) to inspect the state and inject/clear an HVAC fault.
//!
//! ## Source of truth
//!
//! The ROS 2 parameters on `/hvac_simulator` are the single source of truth.
//! They can change underneath this process at any time: through the CAN
//! bridge inside the simulator, through `ros2 param set`, or through this
//! bridge. Therefore the bridge never pushes its own cached state wholesale;
//! each writer touches only the fields it owns (commands: temperature / AC /
//! fan; fault console: `fault_active`), and a read-back loop polls the
//! parameters every two seconds and republishes the Zenoh events when they
//! changed externally. `write_generation` guards the race between a local
//! write and a concurrent read-back.
//!
//! ## Why shell out to `ros2`?
//!
//! Parameters are read and written by spawning the `ros2 param` CLI. That
//! keeps the bridge free of a Rust ROS 2 client dependency at the cost of a
//! few hundred milliseconds per call, which is fine at this update rate and
//! easy for hackathon teams to reason about.
//!
//! ## Environment
//!
//! * `HOST` / `PORT`            fault console bind address (default 0.0.0.0:8093)
//! * `ZENOH_CONNECT`            Zenoh router endpoint (read by `guardian_sil`)
//! * `ROS2_HVAC_NODE_NAME`      simulator node name hint (default `/hvac_simulator`)
//! * `RUST_LOG`                 tracing filter

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{Html, IntoResponse};
use axum::routing::{get, post};
use axum::{Json, Router};
use guardian_sil::{
    decode_json_payload, hvac_state_uri, make_uri_provider, open_up_transport, publish_json_event,
    uds_hvac_cmd_uri, vss_hvac_active_state_uri, vss_hvac_set_temperature_uri,
    HvacActiveStateEvent, HvacCommand, HvacSetTemperatureEvent, HvacStateEvent,
};
use serde::{Deserialize, Serialize};
use tokio::net::TcpListener;
use tokio::process::Command;
use tokio::sync::Mutex;
use tracing::{info, warn};
use up_rust::{UListener, UMessage, UTransport};

/// Point-in-time view of the HVAC state as served by `/api/state` and used
/// to build the outgoing uProtocol events.
#[derive(Debug, Clone, Serialize)]
struct HvacControllerSnapshot {
    target_temperature_celsius: i8,
    requested_air_conditioning_active: bool,
    effective_air_conditioning_active: bool,
    fan_speed_percent: u8,
    fault_active: bool,
    last_request_id: Option<String>,
    timestamp_ms: u64,
}

/// Bridge-side cache of the simulator's parameters. Kept only so the fault
/// console and the read-back loop can detect changes; the ROS 2 parameters
/// remain authoritative.
#[derive(Debug, Clone, PartialEq, Eq)]
struct HvacControllerState {
    target_temperature_celsius: i8,
    requested_air_conditioning_active: bool,
    fan_speed_percent: u8,
    fault_active: bool,
    last_request_id: Option<String>,
}

impl Default for HvacControllerState {
    fn default() -> Self {
        Self {
            target_temperature_celsius: 22,
            requested_air_conditioning_active: false,
            fan_speed_percent: 0,
            fault_active: false,
            last_request_id: None,
        }
    }
}

impl HvacControllerState {
    /// A faulted HVAC cannot cool: the effective state masks the request.
    /// This is what the Guardian sees and what makes it escalate to window
    /// mitigation when a fault is injected.
    fn effective_air_conditioning_active(&self) -> bool {
        self.requested_air_conditioning_active && !self.fault_active
    }

    fn snapshot(&self) -> HvacControllerSnapshot {
        HvacControllerSnapshot {
            target_temperature_celsius: self.target_temperature_celsius,
            requested_air_conditioning_active: self.requested_air_conditioning_active,
            effective_air_conditioning_active: self.effective_air_conditioning_active(),
            fan_speed_percent: self.fan_speed_percent,
            fault_active: self.fault_active,
            last_request_id: self.last_request_id.clone(),
            timestamp_ms: now_ms(),
        }
    }
}

/// Shared state handed to the axum handlers, the uProtocol listener and the
/// read-back loop.
#[derive(Clone)]
struct AppState {
    hvac_state: Arc<Mutex<HvacControllerState>>,
    transport: Arc<dyn UTransport>,
    ros2_node_name: String,
    // Bumped on every locally-initiated write (command / fault toggle) so the
    // read-back loop can discard a stale parameter dump that raced with it.
    write_generation: Arc<AtomicU64>,
}

// The ROS 2 parameters on /hvac_simulator are the single source of truth for
// HVAC state; they can change underneath us via the CAN bridge or ros2 CLI.
#[derive(Debug, Clone, PartialEq, Eq)]
struct RosHvacParameters {
    target_temperature_celsius: i8,
    air_conditioning_active: bool,
    fan_speed_percent: u8,
    fault_active: bool,
}

/// Body of `POST /api/fault`.
#[derive(Debug, Deserialize)]
struct FaultToggleRequest {
    fault_active: bool,
}

/// uProtocol listener for `uds/hvac/cmd` (`HvacCommand` JSON payloads).
struct HvacCommandListener {
    app: AppState,
}

#[async_trait]
impl UListener for HvacCommandListener {
    async fn on_receive(&self, message: UMessage) {
        match decode_json_payload::<HvacCommand>(&message) {
            Ok(cmd) => {
                self.app.write_generation.fetch_add(1, Ordering::SeqCst);
                let snapshot = {
                    let mut guard = self.app.hvac_state.lock().await;
                    guard.target_temperature_celsius = cmd.target_temperature_celsius;
                    guard.requested_air_conditioning_active = cmd.air_conditioning_active;
                    guard.fan_speed_percent = cmd.fan_speed_percent.min(100);
                    guard.last_request_id = Some(cmd.request_id.clone());
                    guard.snapshot()
                };

                // Write only the fields this command carries; fault_active is
                // owned by other sources (CAN, fault console) and must not be
                // clobbered here.
                if let Err(err) = sync_command_parameters(&self.app.ros2_node_name, &snapshot).await {
                    warn!("failed to sync ROS2 HVAC parameters after command: {}", err);
                }

                if let Err(err) = publish_hvac_state(&self.app.transport, &snapshot).await {
                    warn!("failed to publish HVAC state after command: {}", err);
                } else {
                    info!(
                        "HVAC command applied request_id={} target={}C active={} fan={} fault={}",
                        cmd.request_id,
                        snapshot.target_temperature_celsius,
                        snapshot.effective_air_conditioning_active,
                        snapshot.fan_speed_percent,
                        snapshot.fault_active
                    );
                }
            }
            Err(err) => warn!("failed to decode HVAC command: {}", err),
        }
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_target(false)
        .with_env_filter(
            std::env::var("RUST_LOG")
                .unwrap_or_else(|_| "ros2_hvac_bridge=info,reqwest=warn,info".to_string()),
        )
        .init();

    let host = std::env::var("HOST").unwrap_or_else(|_| "0.0.0.0".to_string());
    let port = std::env::var("PORT").unwrap_or_else(|_| "8093".to_string());
    let addr = format!("{}:{}", host, port);
    let uri_provider = make_uri_provider("ros2-hvac-bridge", 0x9501, 0x01);
    let transport = open_up_transport(uri_provider).await?;
    let app_state = AppState {
        hvac_state: Arc::new(Mutex::new(HvacControllerState::default())),
        transport: transport.clone(),
        ros2_node_name: std::env::var("ROS2_HVAC_NODE_NAME")
            .unwrap_or_else(|_| "/hvac_simulator".to_string()),
        write_generation: Arc::new(AtomicU64::new(0)),
    };

    transport
        .register_listener(
            &uds_hvac_cmd_uri(),
            None,
            Arc::new(HvacCommandListener {
                app: app_state.clone(),
            }),
        )
        .await?;

    let initial_snapshot = app_state.hvac_state.lock().await.snapshot();
    publish_hvac_state(&transport, &initial_snapshot).await?;

    // Read-back loop: the ROS 2 parameters are the source of truth. Pull them
    // periodically so state set via CAN (or ros2 CLI) is reflected in
    // /api/state, the fault console, and the Zenoh state events.
    let sync_app = app_state.clone();
    let sync_transport = transport.clone();
    tokio::spawn(async move {
        let mut reported_read_failure = false;
        loop {
            let generation_before = sync_app.write_generation.load(Ordering::SeqCst);
            match read_ros2_hvac_parameters(&sync_app.ros2_node_name).await {
                Ok(params) => {
                    reported_read_failure = false;
                    if sync_app.write_generation.load(Ordering::SeqCst) != generation_before {
                        // A local command raced with this dump; its values may
                        // be stale. Skip and re-read next cycle.
                        continue;
                    }
                    let (changed, snapshot) = {
                        let mut guard = sync_app.hvac_state.lock().await;
                        let changed = guard.target_temperature_celsius
                            != params.target_temperature_celsius
                            || guard.requested_air_conditioning_active
                                != params.air_conditioning_active
                            || guard.fan_speed_percent != params.fan_speed_percent
                            || guard.fault_active != params.fault_active;
                        guard.target_temperature_celsius = params.target_temperature_celsius;
                        guard.requested_air_conditioning_active = params.air_conditioning_active;
                        guard.fan_speed_percent = params.fan_speed_percent;
                        guard.fault_active = params.fault_active;
                        (changed, guard.snapshot())
                    };
                    if changed {
                        info!(
                            "ROS2 HVAC parameters changed externally: target={}C active={} fan={} fault={}; republishing state",
                            snapshot.target_temperature_celsius,
                            snapshot.effective_air_conditioning_active,
                            snapshot.fan_speed_percent,
                            snapshot.fault_active
                        );
                        if let Err(err) = publish_hvac_state(&sync_transport, &snapshot).await {
                            warn!("failed to publish HVAC state after read-back: {}", err);
                        }
                    }
                }
                Err(err) => {
                    if !reported_read_failure {
                        warn!("failed to read ROS2 HVAC parameters (will keep retrying): {}", err);
                        reported_read_failure = true;
                    }
                }
            }

            tokio::time::sleep(std::time::Duration::from_secs(2)).await;
        }
    });

    let app = Router::new()
        .route("/", get(index))
        .route("/health", get(health))
        .route("/api/state", get(get_state))
        .route("/api/fault", post(set_fault))
        .with_state(app_state);

    let listener = TcpListener::bind(&addr).await?;
    info!("ROS2 HVAC bridge UI listening on {}", addr);
    info!("ros2_medkit diagnostics remain available on port 18080; configuration sync is disabled because the gateway has no writable /configurations backend for hvac_simulator");

    axum::serve(listener, app).await?;
    Ok(())
}

/// `GET /` — the fault console page (embedded HTML below).
async fn index() -> impl IntoResponse {
    Html(INDEX_HTML)
}

/// `GET /health` — liveness probe.
async fn health() -> StatusCode {
    StatusCode::OK
}

/// `GET /api/state` — current snapshot (also polled by the Guardian dashboard).
async fn get_state(State(app): State<AppState>) -> Json<HvacControllerSnapshot> {
    let guard = app.hvac_state.lock().await;
    Json(guard.snapshot())
}

/// `POST /api/fault {"fault_active": bool}` — inject or clear the simulated
/// HVAC fault. Writes only `fault_active` on the simulator.
async fn set_fault(
    State(app): State<AppState>,
    Json(payload): Json<FaultToggleRequest>,
) -> Result<Json<HvacControllerSnapshot>, StatusCode> {
    app.write_generation.fetch_add(1, Ordering::SeqCst);
    let snapshot = {
        let mut guard = app.hvac_state.lock().await;
        guard.fault_active = payload.fault_active;
        guard.snapshot()
    };

    // Write only fault_active; the other parameters may have been set via CAN
    // and must not be reset from our (possibly stale) local state.
    if let Err(err) = sync_fault_parameter(&app.ros2_node_name, snapshot.fault_active).await {
        warn!("failed to sync ROS2 HVAC parameters after fault toggle: {}", err);
    }

    publish_hvac_state(&app.transport, &snapshot)
        .await
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

    Ok(Json(snapshot))
}

/// Publish the three outgoing uProtocol events for one snapshot:
/// VSS `Vehicle.Cabin.HVAC.Station.Row1.Left.Temperature`,
/// VSS `Vehicle.Cabin.HVAC.IsAirConditioningActive`, and the combined
/// `hvac/state` event consumed by the Guardian, temperature-sim and dashboard.
async fn publish_hvac_state(
    transport: &Arc<dyn UTransport>,
    snapshot: &HvacControllerSnapshot,
) -> Result<(), up_rust::UStatus> {
    publish_json_event(
        transport.clone(),
        vss_hvac_set_temperature_uri(),
        &HvacSetTemperatureEvent {
            temperature_celsius: snapshot.target_temperature_celsius,
            timestamp_ms: snapshot.timestamp_ms,
        },
    )
    .await?;

    publish_json_event(
        transport.clone(),
        vss_hvac_active_state_uri(),
        &HvacActiveStateEvent {
            active: snapshot.effective_air_conditioning_active,
            timestamp_ms: snapshot.timestamp_ms,
        },
    )
    .await?;

    publish_json_event(
        transport.clone(),
        hvac_state_uri(),
        &HvacStateEvent {
            target_temperature_celsius: snapshot.target_temperature_celsius,
            air_conditioning_active: snapshot.effective_air_conditioning_active,
            fan_speed_percent: snapshot.fan_speed_percent,
            fault_active: snapshot.fault_active,
            timestamp_ms: snapshot.timestamp_ms,
        },
    )
    .await
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// Write the command-owned parameters (temperature, AC, fan) to the simulator.
/// Note that the *effective* AC state is written, so a faulted HVAC reports
/// AC off on the ROS 2 side as well.
async fn sync_command_parameters(
    node_name_hint: &str,
    snapshot: &HvacControllerSnapshot,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let node_name = resolve_ros2_node_name(node_name_hint).await?;
    run_ros2_param_set(
        &node_name,
        "target_temperature_celsius",
        &snapshot.target_temperature_celsius.to_string(),
    )
    .await?;
    run_ros2_param_set(
        &node_name,
        "air_conditioning_active",
        if snapshot.effective_air_conditioning_active {
            "true"
        } else {
            "false"
        },
    )
    .await?;
    run_ros2_param_set(
        &node_name,
        "fan_speed_percent",
        &snapshot.fan_speed_percent.to_string(),
    )
    .await?;
    Ok(())
}

/// Write only `fault_active` to the simulator.
async fn sync_fault_parameter(
    node_name_hint: &str,
    fault_active: bool,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let node_name = resolve_ros2_node_name(node_name_hint).await?;
    run_ros2_param_set(
        &node_name,
        "fault_active",
        if fault_active { "true" } else { "false" },
    )
    .await
}

/// Read the four HVAC parameters back from the simulator via
/// `ros2 param dump`, parsing its flat `key: value` YAML output.
async fn read_ros2_hvac_parameters(
    node_name_hint: &str,
) -> Result<RosHvacParameters, Box<dyn std::error::Error + Send + Sync>> {
    let node_name = resolve_ros2_node_name(node_name_hint).await?;
    let output = Command::new("ros2")
        .args(["param", "dump", &node_name])
        .output()
        .await?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        return Err(format!("ros2 param dump {} failed: {}", node_name, stderr).into());
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    let mut target: Option<i8> = None;
    let mut active: Option<bool> = None;
    let mut fan: Option<u8> = None;
    let mut fault: Option<bool> = None;

    for line in stdout.lines() {
        let Some((key, value)) = line.trim().split_once(':') else {
            continue;
        };
        let value = value.trim();
        match key.trim() {
            "target_temperature_celsius" => target = value.parse().ok(),
            "air_conditioning_active" => active = value.parse().ok(),
            "fan_speed_percent" => fan = value.parse().ok(),
            "fault_active" => fault = value.parse().ok(),
            _ => {}
        }
    }

    match (target, active, fan, fault) {
        (Some(target_temperature_celsius), Some(air_conditioning_active), Some(fan_speed_percent), Some(fault_active)) => {
            Ok(RosHvacParameters {
                target_temperature_celsius,
                air_conditioning_active,
                fan_speed_percent,
                fault_active,
            })
        }
        _ => Err(format!(
            "ros2 param dump {} missing expected HVAC parameters (got target={:?} active={:?} fan={:?} fault={:?})",
            node_name, target, active, fan, fault
        )
        .into()),
    }
}

/// `ros2 param set <node> <param> <value>`, surfacing stdout/stderr on failure.
async fn run_ros2_param_set(
    node_name: &str,
    param_name: &str,
    value: &str,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let output = Command::new("ros2")
        .args(["param", "set", node_name, param_name, value])
        .output()
        .await?;

    if output.status.success() {
        return Ok(());
    }

    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();

    Err(format!(
        "ros2 param set {} {} {} failed: {} {}",
        node_name, param_name, value, stdout, stderr
    )
    .into())
}

/// Find the simulator node: exact match on the hint first, otherwise the
/// first node whose name ends with it (tolerates a namespace prefix).
async fn resolve_ros2_node_name(
    node_name_hint: &str,
) -> Result<String, Box<dyn std::error::Error + Send + Sync>> {
    let output = Command::new("ros2").args(["node", "list"]).output().await?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        return Err(format!("ros2 node list failed: {}", stderr).into());
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    let nodes: Vec<&str> = stdout.lines().map(str::trim).filter(|line| !line.is_empty()).collect();

    if nodes.iter().any(|node| *node == node_name_hint) {
        return Ok(node_name_hint.to_string());
    }

    let bare_hint = node_name_hint.trim_start_matches('/');
    if let Some(node) = nodes.iter().find(|node| node.trim_start_matches('/').ends_with(bare_hint)) {
        return Ok((*node).to_string());
    }

    Err(format!("node not found from hint {}. visible nodes: {}", node_name_hint, nodes.join(", ")).into())
}

/// The fault console page. Polls `/api/state` every 1.5 s and posts to
/// `/api/fault`; kept inline so the bridge is a single self-contained binary.
const INDEX_HTML: &str = r#"<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Guardian HVAC Fault Console</title>
  <style>
    :root {
      --bg: #f4efe6;
      --panel: rgba(255, 252, 246, 0.88);
      --ink: #1b241f;
      --muted: #5f6b63;
      --accent: #0e8c61;
      --danger: #cc4b37;
      --border: rgba(27, 36, 31, 0.12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: Georgia, "Times New Roman", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(14, 140, 97, 0.14), transparent 30%),
        radial-gradient(circle at bottom right, rgba(204, 75, 55, 0.15), transparent 28%),
        linear-gradient(135deg, #efe6d7 0%, #f7f4ed 48%, #e2ebdf 100%);
      display: grid;
      place-items: center;
      padding: 24px;
    }
    .panel {
      width: min(860px, 100%);
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 28px;
      box-shadow: 0 24px 80px rgba(30, 40, 34, 0.12);
      overflow: hidden;
      backdrop-filter: blur(18px);
    }
    .hero {
      padding: 28px 30px 18px;
      border-bottom: 1px solid var(--border);
      background: linear-gradient(120deg, rgba(14, 140, 97, 0.08), rgba(255,255,255,0));
    }
    h1 {
      margin: 0 0 8px;
      font-size: clamp(2rem, 4vw, 3.1rem);
      line-height: 0.95;
      letter-spacing: -0.04em;
    }
    .subtitle {
      margin: 0;
      color: var(--muted);
      max-width: 56ch;
      font-size: 1rem;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 14px;
      padding: 22px 24px 12px;
    }
    .card {
      border: 1px solid var(--border);
      border-radius: 20px;
      padding: 16px 18px;
      background: rgba(255,255,255,0.55);
    }
    .label {
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      color: var(--muted);
      margin-bottom: 10px;
    }
    .value {
      font-size: 2rem;
      line-height: 1;
    }
    .value.small {
      font-size: 1.15rem;
    }
    .actions {
      padding: 12px 24px 28px;
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
    }
    button {
      border: 0;
      border-radius: 999px;
      padding: 14px 18px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      transition: transform 140ms ease, opacity 140ms ease;
    }
    button:hover { transform: translateY(-1px); }
    .ok { background: var(--accent); color: white; }
    .fault { background: var(--danger); color: white; }
    .status-pill {
      padding: 10px 14px;
      border-radius: 999px;
      background: rgba(27, 36, 31, 0.06);
      color: var(--muted);
    }
    .status-pill strong { color: var(--ink); }
  </style>
</head>
<body>
  <main class="panel">
    <section class="hero">
      <h1>Guardian HVAC</h1>
      <p class="subtitle">ROS2 HVAC controller state over Zenoh and up-rust. Use this panel to inject an HVAC fault and watch the guardian escalate from setpoint control to window mitigation.</p>
    </section>
    <section class="grid">
      <article class="card"><div class="label">Target Temperature</div><div class="value" id="target">--</div></article>
      <article class="card"><div class="label">Effective AC State</div><div class="value small" id="active">--</div></article>
      <article class="card"><div class="label">Fan Speed</div><div class="value" id="fan">--</div></article>
      <article class="card"><div class="label">Fault State</div><div class="value small" id="fault">--</div></article>
    </section>
    <section class="actions">
      <button class="fault" id="injectFault">Inject HVAC Fault</button>
      <button class="ok" id="clearFault">Clear Fault</button>
      <div class="status-pill">Last request: <strong id="requestId">none</strong></div>
    </section>
  </main>
  <script>
    async function fetchState() {
      const response = await fetch('/api/state');
      const data = await response.json();
      document.getElementById('target').textContent = `${data.target_temperature_celsius}°C`;
      document.getElementById('active').textContent = data.effective_air_conditioning_active ? 'ACTIVE' : 'INACTIVE';
      document.getElementById('fan').textContent = `${data.fan_speed_percent}%`;
      document.getElementById('fault').textContent = data.fault_active ? 'FAULTED' : 'HEALTHY';
      document.getElementById('requestId').textContent = data.last_request_id || 'none';
    }

    async function setFault(faultActive) {
      await fetch('/api/fault', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fault_active: faultActive })
      });
      await fetchState();
    }

    document.getElementById('injectFault').addEventListener('click', () => setFault(true));
    document.getElementById('clearFault').addEventListener('click', () => setFault(false));
    fetchState();
    setInterval(fetchState, 1500);
  </script>
</body>
</html>
"#;
