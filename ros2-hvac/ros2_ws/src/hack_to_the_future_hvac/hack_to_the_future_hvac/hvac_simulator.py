"""ROS 2 HVAC simulator node with an optional CAN bridge.

This is the workload that Eclipse Muto deploys into the ``ros2-hvac``
container. It stands in for a vehicle HVAC (heating, ventilation and air
conditioning) controller and is deliberately small so hackathon teams can read
it end to end and replace pieces of it with their own logic.

Responsibilities
----------------
1. **Own the HVAC state.** The four ROS 2 parameters below are the single
   source of truth for the whole stack. Everything else (the Rust bridge, the
   CAN bus, the ``ros2`` CLI) reads and writes *these* parameters:

   ============================  =====  ====================================
   parameter                     type   meaning
   ============================  =====  ====================================
   ``target_temperature_celsius`` int    cabin setpoint, clamped to 16..30
   ``air_conditioning_active``    bool   cooling requested
   ``fan_speed_percent``          int    blower speed, clamped to 0..100
   ``fault_active``               bool   simulated HVAC fault
   ============================  =====  ====================================

2. **Publish diagnostics.** Every ``publish_interval_s`` seconds the node
   publishes a ``diagnostic_msgs/DiagnosticArray`` on ``/diagnostics`` with
   two statuses: ``guardian_hvac/thermal_state`` (HVAC health, becomes ERROR
   when ``fault_active`` is set) and ``guardian_hvac/can_bridge`` (CAN link
   health). The ``ros2_medkit`` gateway turns ERROR statuses into REST
   faults, which is how the Guardian dashboard sees an HVAC fault.

3. **Bridge to CAN (optional).** When ``can_enabled`` is true the node joins
   a CAN bus through `python-can <https://python-can.readthedocs.io/>`_:

   * RX: command frames on ``can_rx_id`` (default ``0x321``) update the
     parameters above.
   * TX: the current state is sent on ``can_tx_id`` (default ``0x320``)
     every ``can_tx_period_s`` seconds.

   The frame layout is defined once in ``config/hvac.dbc`` and encoded and
   decoded through ``cantools``. A hand-written fallback codec with the
   identical layout is used when ``cantools`` is not installed, and the unit
   tests assert both produce the same bytes.

   The transport is chosen by ``can_bustype``/``can_channel`` only, so the
   same code runs against ``udp_multicast`` (pure userspace, works in
   containers on macOS, Windows and Linux) or ``socketcan`` (Linux kernel
   CAN, real hardware).

Configuration precedence
------------------------
``launch argument`` > ``environment variable`` > ``built-in default``.
The environment variables (``CAN_ENABLED``, ``CAN_BUSTYPE``, ``CAN_CHANNEL``,
``CAN_TX_ID``, ``CAN_RX_ID``, ``CAN_TX_PERIOD_S``, ``CAN_RX_POLL_PERIOD_S``,
``CAN_RETRY_PERIOD_S``) are set in ``docker-compose.yml``; the launch
arguments are declared in ``launch/hvac.launch.py``.

Failure philosophy
------------------
CAN problems never crash the node and are never silent: they are logged at
ERROR level, surfaced in the ``guardian_hvac/can_bridge`` diagnostic, and the
bridge reconnects automatically every ``can_retry_period_s`` seconds.
"""

import os
from typing import Optional

from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
import rclpy
from rclpy.node import Node
from rclpy.parameter import Parameter

# Both CAN libraries are optional imports so the node can still run (and
# report the problem via diagnostics) in an image that lacks them.
try:
    import can
except ImportError:  # pragma: no cover - exercised only in broken images
    can = None

try:
    import cantools
except ImportError:  # pragma: no cover - fallback codec is unit tested
    cantools = None


# ─── CAN constants ────────────────────────────────────────────────────────────

#: Largest 11-bit ("standard") CAN identifier. IDs above this are sent and
#: filtered as 29-bit ("extended") identifiers.
CAN_STANDARD_ID_MAX = 0x7FF
#: Largest 29-bit ("extended") CAN identifier.
CAN_EXTENDED_ID_MAX = 0x1FFF_FFFF
#: Upper bound on frames drained per RX poll, so a flooded bus cannot
#: monopolize the ROS executor.
CAN_RX_DRAIN_LIMIT = 64
#: Minimum payload length (bytes) of a valid HVAC frame; see config/hvac.dbc.
HVAC_FRAME_LENGTH = 3

#: Message names in ``config/hvac.dbc``.
DBC_STATE_MESSAGE = "HVAC_STATE"  # simulator -> bus, default ID 0x320
DBC_COMMAND_MESSAGE = "HVAC_COMMAND"  # bus -> simulator, default ID 0x321

#: Range limits applied to every value that enters or leaves the node.
TEMPERATURE_MIN_C, TEMPERATURE_MAX_C = 16, 30
FAN_MIN_PERCENT, FAN_MAX_PERCENT = 0, 100

#: Bit positions of the flags byte (byte 2) in the manual codec. Must match
#: the ``air_conditioning_active`` / ``fault_active`` signals in hvac.dbc.
FLAG_AC_ACTIVE = 0x01
FLAG_FAULT_ACTIVE = 0x02


# ─── Frame codec (pure functions, unit tested in test/test_can_codec.py) ─────


def load_can_database(dbc_path: Optional[str] = None):
    """Load the HVAC DBC via cantools, or return ``None`` to use the fallback.

    Lookup order: explicit ``dbc_path`` argument, ``CAN_DBC_PATH`` environment
    variable, then ``config/hvac.dbc`` from the installed package share
    directory. Any failure returns ``None`` instead of raising, because the
    manual codec below is a complete substitute.
    """
    if cantools is None:
        return None
    path = dbc_path or os.getenv("CAN_DBC_PATH")
    if path is None:
        try:
            from ament_index_python.packages import get_package_share_directory

            path = os.path.join(
                get_package_share_directory("hack_to_the_future_hvac"), "config", "hvac.dbc"
            )
        except Exception:
            return None
    try:
        return cantools.database.load_file(path)
    except Exception:
        return None


def clamp_hvac_values(target_temperature: int, fan_speed: int) -> "tuple[int, int]":
    """Clamp setpoint and fan speed to the ranges declared in hvac.dbc."""
    temperature = max(TEMPERATURE_MIN_C, min(TEMPERATURE_MAX_C, int(target_temperature)))
    fan = max(FAN_MIN_PERCENT, min(FAN_MAX_PERCENT, int(fan_speed)))
    return temperature, fan


def encode_hvac_payload(
    db,
    target_temperature: int,
    fan_speed: int,
    hvac_active: bool,
    fault_active: bool,
) -> bytes:
    """Encode HVAC state into the 3-byte ``HVAC_STATE`` payload.

    ``db`` is a cantools database (DBC codec) or ``None`` (manual codec).
    Layout: ``[temperature °C, fan %, flags]`` with flags bit 0 = AC active,
    bit 1 = fault active.
    """
    target_temperature, fan_speed = clamp_hvac_values(target_temperature, fan_speed)
    if db is not None:
        return bytes(
            db.encode_message(
                DBC_STATE_MESSAGE,
                {
                    "target_temperature_celsius": target_temperature,
                    "fan_speed_percent": fan_speed,
                    "air_conditioning_active": int(hvac_active),
                    "fault_active": int(fault_active),
                },
            )
        )
    flags = (FLAG_AC_ACTIVE if hvac_active else 0) | (FLAG_FAULT_ACTIVE if fault_active else 0)
    return bytes([target_temperature, fan_speed, flags])


def decode_hvac_payload(db, data: bytes) -> Optional[dict]:
    """Decode an ``HVAC_COMMAND`` payload into a parameter dict.

    Returns ``None`` for frames shorter than :data:`HVAC_FRAME_LENGTH`.
    Trailing bytes are ignored. Values are clamped, never rejected, so a
    slightly out-of-range command still moves the setpoint to the nearest
    valid value, like a real actuator would.
    """
    if len(data) < HVAC_FRAME_LENGTH:
        return None
    if db is not None:
        decoded = db.decode_message(DBC_COMMAND_MESSAGE, bytes(data[:HVAC_FRAME_LENGTH]))
        target_temperature = int(decoded["target_temperature_celsius"])
        fan_speed = int(decoded["fan_speed_percent"])
        hvac_active = bool(decoded["air_conditioning_active"])
        fault_active = bool(decoded["fault_active"])
    else:
        target_temperature = int(data[0])
        fan_speed = int(data[1])
        flags = int(data[2])
        hvac_active = bool(flags & FLAG_AC_ACTIVE)
        fault_active = bool(flags & FLAG_FAULT_ACTIVE)
    target_temperature, fan_speed = clamp_hvac_values(target_temperature, fan_speed)
    return {
        "target_temperature_celsius": target_temperature,
        "fan_speed_percent": fan_speed,
        "air_conditioning_active": hvac_active,
        "fault_active": fault_active,
    }


# ─── The node ─────────────────────────────────────────────────────────────────


class HvacSimulator(Node):
    """HVAC controller stand-in: parameters + diagnostics + optional CAN bridge."""

    def __init__(self) -> None:
        super().__init__("hvac_simulator")

        self._declare_parameters()

        self.diagnostic_publisher = self.create_publisher(DiagnosticArray, "/diagnostics", 10)

        # CAN bridge state. ``_can_bus`` is None whenever we are disconnected;
        # the supervisor timer reconnects. ``_can_fault_reason`` holds a
        # permanent configuration error (missing library, bad IDs) that a
        # reconnect cannot fix, and is surfaced verbatim in diagnostics.
        self._can_bus: Optional["can.BusABC"] = None
        self._can_db = None
        self._can_timers = []
        self._can_fault_reason: Optional[str] = None
        self._can_rx_frames = 0
        self._can_rx_ignored = 0
        self._can_tx_frames = 0
        self._can_errors = 0
        self._can_last_rx_time_s: Optional[float] = None
        self._can_start()

        interval_s = float(self.get_parameter("publish_interval_s").value)
        self.timer = self.create_timer(interval_s, self._tick)

    def _declare_parameters(self) -> None:
        """Declare all node parameters.

        HVAC state parameters have fixed defaults (launch arguments override
        them). CAN parameters default to the ``CAN_*`` environment variables so
        the compose file can configure the transport without touching launch
        files. The CAN IDs are declared as *strings* so hex (``"0x320"``) and
        decimal (``"800"``) both work from every source; see :meth:`_can_id`.
        """
        self.declare_parameter("publish_interval_s", 2.0)
        self.declare_parameter("target_temperature_celsius", 22)
        self.declare_parameter("air_conditioning_active", False)
        self.declare_parameter("fan_speed_percent", 0)
        self.declare_parameter("fault_active", False)

        self.declare_parameter("can_enabled", _env_bool("CAN_ENABLED", False))
        self.declare_parameter("can_bustype", os.getenv("CAN_BUSTYPE", "socketcan"))
        self.declare_parameter("can_channel", os.getenv("CAN_CHANNEL", "vcan0"))
        self.declare_parameter("can_tx_period_s", _env_float("CAN_TX_PERIOD_S", 1.0))
        self.declare_parameter("can_rx_poll_period_s", _env_float("CAN_RX_POLL_PERIOD_S", 0.05))
        self.declare_parameter("can_retry_period_s", _env_float("CAN_RETRY_PERIOD_S", 5.0))
        self.declare_parameter("can_tx_id", os.getenv("CAN_TX_ID", "0x320"))
        self.declare_parameter("can_rx_id", os.getenv("CAN_RX_ID", "0x321"))

    def destroy_node(self):
        for timer in self._can_timers:
            timer.cancel()
        self._can_close()
        return super().destroy_node()

    # ── Current state helpers ────────────────────────────────────────────────

    def _hvac_state(self) -> "tuple[int, bool, int, bool]":
        """Return ``(target_temperature, ac_active, fan_speed, fault_active)``."""
        return (
            int(self.get_parameter("target_temperature_celsius").value),
            bool(self.get_parameter("air_conditioning_active").value),
            int(self.get_parameter("fan_speed_percent").value),
            bool(self.get_parameter("fault_active").value),
        )

    def _now_s(self) -> float:
        return self.get_clock().now().nanoseconds / 1_000_000_000.0

    # ── Diagnostics ──────────────────────────────────────────────────────────

    def _tick(self) -> None:
        """Periodic publisher of the two DiagnosticStatus entries."""
        diag_msg = DiagnosticArray()
        diag_msg.header.stamp = self.get_clock().now().to_msg()
        diag_msg.status.append(self._thermal_status())
        diag_msg.status.append(self._can_bridge_status())
        self.diagnostic_publisher.publish(diag_msg)

    def _thermal_status(self) -> DiagnosticStatus:
        """``guardian_hvac/thermal_state``: OK idle, WARN cooling, ERROR fault.

        ``ros2_medkit`` maps the ERROR level to a REST fault with code
        ``GUARDIAN_HVAC_THERMAL_STATE``; that is what the Guardian dashboard
        shows when a fault is injected.
        """
        target_temperature, hvac_active, fan_speed, fault_active = self._hvac_state()
        if fault_active:
            level, message = DiagnosticStatus.ERROR, "HVAC fault simulated"
        elif hvac_active:
            level, message = DiagnosticStatus.WARN, "HVAC cooling active"
        else:
            level, message = DiagnosticStatus.OK, "HVAC idle"
        return DiagnosticStatus(
            level=level,
            name="guardian_hvac/thermal_state",
            message=message,
            hardware_id="guardian-hvac-sim",
            values=[
                KeyValue(key="target_temperature_celsius", value=str(target_temperature)),
                KeyValue(key="air_conditioning_active", value=str(hvac_active).lower()),
                KeyValue(key="fan_speed_percent", value=str(fan_speed)),
                KeyValue(key="fault_active", value=str(fault_active).lower()),
            ],
        )

    def _can_bridge_status(self) -> DiagnosticStatus:
        """``guardian_hvac/can_bridge``: link health plus frame counters.

        OK when connected or intentionally disabled; ERROR when python-can is
        missing, the IDs are invalid, or the bus is disconnected (a reconnect
        is always in progress in that last case).
        """
        if not bool(self.get_parameter("can_enabled").value):
            level, message = DiagnosticStatus.OK, "CAN bridge disabled by configuration"
        elif self._can_fault_reason is not None:
            level, message = DiagnosticStatus.ERROR, self._can_fault_reason
        elif self._can_bus is None:
            level, message = DiagnosticStatus.ERROR, "CAN bus disconnected; reconnecting"
        else:
            level, message = DiagnosticStatus.OK, "CAN bridge connected"

        last_rx_age = ""
        if self._can_last_rx_time_s is not None:
            last_rx_age = f"{self._now_s() - self._can_last_rx_time_s:.1f}"

        return DiagnosticStatus(
            level=level,
            name="guardian_hvac/can_bridge",
            message=message,
            hardware_id="guardian-hvac-sim",
            values=[
                KeyValue(key="interface", value=str(self.get_parameter("can_bustype").value)),
                KeyValue(key="channel", value=str(self.get_parameter("can_channel").value)),
                KeyValue(key="connected", value=str(self._can_bus is not None).lower()),
                KeyValue(key="rx_frames", value=str(self._can_rx_frames)),
                KeyValue(key="rx_ignored", value=str(self._can_rx_ignored)),
                KeyValue(key="tx_frames", value=str(self._can_tx_frames)),
                KeyValue(key="errors", value=str(self._can_errors)),
                KeyValue(key="last_rx_age_s", value=last_rx_age),
            ],
        )

    # ── CAN bridge: lifecycle ────────────────────────────────────────────────

    def _can_id(self, name: str) -> int:
        """Parse a CAN ID parameter; ``int(x, 0)`` accepts ``0x320`` and ``800``."""
        return int(str(self.get_parameter(name).value), 0)

    def _can_start(self) -> None:
        """Validate configuration, open the bus and start the three CAN timers.

        Timers (all on the default single-threaded executor, so no locking is
        needed anywhere in this class):

        * RX poll every ``can_rx_poll_period_s`` (drain incoming commands)
        * TX every ``can_tx_period_s`` (send the current state)
        * supervisor every ``can_retry_period_s`` (reconnect if disconnected)
        """
        if not bool(self.get_parameter("can_enabled").value):
            return
        if can is None:
            self._can_fault_reason = "python-can not installed; CAN bridge disabled"
            self.get_logger().error(
                "CAN is enabled but python-can is not installed; "
                "CAN bridge is DISABLED. Install python-can>=4.2 (see ros2-hvac/Dockerfile)."
            )
            return

        rx_id = self._can_id("can_rx_id")
        tx_id = self._can_id("can_tx_id")
        if not self._can_ids_valid(rx_id, tx_id):
            return

        self._can_db = load_can_database()
        codec = "hvac.dbc via cantools" if self._can_db is not None else "built-in packing"
        self.get_logger().info(f"CAN frame codec: {codec}")

        self._can_connect()

        # Lower bounds keep a typo in the config from spinning the executor.
        rx_poll_period_s = max(0.01, float(self.get_parameter("can_rx_poll_period_s").value))
        tx_period_s = max(0.1, float(self.get_parameter("can_tx_period_s").value))
        retry_period_s = max(1.0, float(self.get_parameter("can_retry_period_s").value))
        self._can_timers = [
            self.create_timer(rx_poll_period_s, self._can_rx_poll),
            self.create_timer(tx_period_s, self._can_tx),
            self.create_timer(retry_period_s, self._can_supervise),
        ]

    def _can_ids_valid(self, rx_id: int, tx_id: int) -> bool:
        """Reject IDs outside 0..0x1FFFFFFF; records the reason for diagnostics."""
        for name, value in (("can_rx_id", rx_id), ("can_tx_id", tx_id)):
            if not 0 <= value <= CAN_EXTENDED_ID_MAX:
                self._can_fault_reason = f"invalid {name} 0x{value:X}; CAN bridge disabled"
                self.get_logger().error(
                    f"Invalid {name} 0x{value:X} (must be 0..0x{CAN_EXTENDED_ID_MAX:X}); "
                    "CAN bridge is DISABLED."
                )
                return False
        return True

    def _can_connect(self) -> None:
        """Open the bus with a hardware-style filter on the command ID.

        The filter means frames from other participants on a shared bus never
        reach Python at all; ``rx_ignored`` only counts frames that passed the
        filter but were malformed.
        """
        channel = str(self.get_parameter("can_channel").value)
        bustype = str(self.get_parameter("can_bustype").value)
        rx_id = self._can_id("can_rx_id")
        rx_extended = rx_id > CAN_STANDARD_ID_MAX
        can_filters = [
            {
                "can_id": rx_id,
                "can_mask": CAN_EXTENDED_ID_MAX if rx_extended else CAN_STANDARD_ID_MAX,
                "extended": rx_extended,
            }
        ]
        try:
            self._can_bus = can.Bus(interface=bustype, channel=channel, can_filters=can_filters)
            self.get_logger().info(f"CAN bridge enabled on {bustype}:{channel}")
        except Exception as exc:
            self._can_bus = None
            retry_period_s = max(1.0, float(self.get_parameter("can_retry_period_s").value))
            self.get_logger().error(
                f"Failed to initialize CAN bridge on {bustype}:{channel}: {exc}; "
                f"retrying every {retry_period_s:.0f}s"
            )

    def _can_supervise(self) -> None:
        """Supervisor timer: reconnect whenever the bus handle is gone."""
        if self._can_bus is None:
            self._can_connect()

    def _can_fail(self, context: str, exc: Exception) -> None:
        """Handle an I/O error: count it, log it, drop the bus for the supervisor."""
        self._can_errors += 1
        self.get_logger().error(f"{context}: {exc}; resetting CAN bridge")
        self._can_close()

    def _can_close(self) -> None:
        if self._can_bus is not None:
            try:
                self._can_bus.shutdown()
            except Exception:
                pass
            self._can_bus = None

    # ── CAN bridge: data path ────────────────────────────────────────────────

    def _can_rx_poll(self) -> None:
        """Drain pending command frames and apply the newest one.

        "Latest frame wins": if several commands arrived since the last poll
        only the most recent is applied, like a real actuator following the
        most recent setpoint. Applying it means writing the ROS parameters,
        which is the same thing the Rust bridge or ``ros2 param set`` would do.
        """
        if self._can_bus is None:
            return
        rx_id = self._can_id("can_rx_id")

        latest = None
        try:
            for _ in range(CAN_RX_DRAIN_LIMIT):
                msg = self._can_bus.recv(timeout=0.0)
                if msg is None:
                    break
                if msg.arbitration_id != rx_id or len(msg.data) < HVAC_FRAME_LENGTH:
                    self._can_rx_ignored += 1
                    continue
                latest = msg
                self._can_rx_frames += 1
                self._can_last_rx_time_s = self._now_s()
        except Exception as exc:
            self._can_fail("CAN receive failed", exc)
            return

        if latest is None:
            return

        command = decode_hvac_payload(self._can_db, latest.data)
        if command is None:
            return

        self.set_parameters(
            [
                Parameter(
                    "target_temperature_celsius",
                    Parameter.Type.INTEGER,
                    command["target_temperature_celsius"],
                ),
                Parameter("fan_speed_percent", Parameter.Type.INTEGER, command["fan_speed_percent"]),
                Parameter(
                    "air_conditioning_active",
                    Parameter.Type.BOOL,
                    command["air_conditioning_active"],
                ),
                Parameter("fault_active", Parameter.Type.BOOL, command["fault_active"]),
            ]
        )

    def _can_tx(self) -> None:
        """Send the current state as one ``HVAC_STATE`` frame."""
        if self._can_bus is None:
            return

        target_temperature, hvac_active, fan_speed, fault_active = self._hvac_state()
        tx_id = self._can_id("can_tx_id")
        payload = encode_hvac_payload(self._can_db, target_temperature, fan_speed, hvac_active, fault_active)

        try:
            self._can_bus.send(
                can.Message(
                    arbitration_id=tx_id,
                    data=payload,
                    is_extended_id=tx_id > CAN_STANDARD_ID_MAX,
                )
            )
            self._can_tx_frames += 1
        except Exception as exc:
            self._can_fail("CAN send failed", exc)


# ─── Environment helpers ──────────────────────────────────────────────────────


def _env_bool(key: str, default: bool) -> bool:
    """Read a boolean env var; accepts 1/true/yes/on (case-insensitive)."""
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(key: str, default: float) -> float:
    """Read a float env var, falling back to ``default`` on parse errors."""
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def main() -> None:
    rclpy.init()
    node = HvacSimulator()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()
