"""Launch file for the HVAC simulator node.

This is the entry point Eclipse Muto runs when it deploys the ``hvac_simulator``
stack archive (see ``ros2-hvac/artifact-run.sh``). It exists to do one thing:
turn *launch arguments* into *node parameters*, with a sensible fallback chain.

Precedence per parameter (highest first):

1. launch argument, e.g. ``ros2 launch hack_to_the_future_hvac hvac.launch.py can_channel:=vcan0``
2. environment variable (``CAN_*``), as set in ``docker-compose.yml``
3. built-in default below

Only the CAN parameters have an environment-variable fallback; the HVAC state
parameters are plain launch arguments. The node itself applies the same
defaults, so this file can be bypassed entirely with ``ros2 run``.
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import EnvironmentVariable, LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def _arg(name: str, default: str, env_var: str = None) -> DeclareLaunchArgument:
    """Declare a launch argument, optionally defaulting to an environment variable."""
    if env_var is not None:
        return DeclareLaunchArgument(
            name, default_value=EnvironmentVariable(env_var, default_value=default)
        )
    return DeclareLaunchArgument(name, default_value=default)


def _param(name: str, value_type) -> ParameterValue:
    """Bind a launch argument to a typed node parameter.

    ``value_type`` must match the type the node declares the parameter with,
    otherwise rclpy rejects the override at startup. In particular the CAN IDs
    are *strings* (so ``"0x320"`` works); ``int`` would fail on hex input.
    """
    return ParameterValue(LaunchConfiguration(name), value_type=value_type)


def generate_launch_description() -> LaunchDescription:
    # Boolean values must be the literal strings "true" / "false".
    arguments = [
        # HVAC state (the single source of truth for the whole stack)
        _arg("publish_interval_s", "2.0"),
        _arg("target_temperature_celsius", "22"),
        _arg("air_conditioning_active", "false"),
        _arg("fan_speed_percent", "0"),
        _arg("fault_active", "false"),
        # CAN bridge (environment variables come from docker-compose.yml)
        _arg("can_enabled", "false", "CAN_ENABLED"),
        _arg("can_bustype", "socketcan", "CAN_BUSTYPE"),
        _arg("can_channel", "vcan0", "CAN_CHANNEL"),
        _arg("can_tx_period_s", "1.0", "CAN_TX_PERIOD_S"),
        _arg("can_rx_poll_period_s", "0.05", "CAN_RX_POLL_PERIOD_S"),
        _arg("can_retry_period_s", "5.0", "CAN_RETRY_PERIOD_S"),
        _arg("can_tx_id", "0x320", "CAN_TX_ID"),
        _arg("can_rx_id", "0x321", "CAN_RX_ID"),
    ]

    parameters = {
        "publish_interval_s": _param("publish_interval_s", float),
        "target_temperature_celsius": _param("target_temperature_celsius", int),
        "air_conditioning_active": _param("air_conditioning_active", bool),
        "fan_speed_percent": _param("fan_speed_percent", int),
        "fault_active": _param("fault_active", bool),
        "can_enabled": _param("can_enabled", bool),
        "can_bustype": _param("can_bustype", str),
        "can_channel": _param("can_channel", str),
        "can_tx_period_s": _param("can_tx_period_s", float),
        "can_rx_poll_period_s": _param("can_rx_poll_period_s", float),
        "can_retry_period_s": _param("can_retry_period_s", float),
        "can_tx_id": _param("can_tx_id", str),
        "can_rx_id": _param("can_rx_id", str),
    }

    hvac_node = Node(
        package="hack_to_the_future_hvac",
        executable="hvac_simulator",
        name="hvac_simulator",
        output="screen",
        parameters=[parameters],
    )

    return LaunchDescription(arguments + [hvac_node])
