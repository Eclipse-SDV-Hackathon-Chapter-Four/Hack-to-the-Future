"""Launch file for the Eclipse Muto runtime inside the ros2-hvac container.

Muto is split into three upstream repositories, each of which contributes ROS 2
nodes that are started here under the ``muto`` namespace:

``muto_agent`` (https://github.com/eclipse-muto/agent)
    * ``agent``           entry point; receives stack/twin/command requests and
                          dispatches them to the composer
    * ``gateway``         MQTT bridge to a cloud backend (Eclipse Ditto/Hono).
                          Unused in this offline demo but part of the standard
                          Muto deployment, so it is kept for fidelity.
    * ``commands_plugin`` executes remote commands (e.g. ``ros2 topic list``)

``muto_core`` (https://github.com/eclipse-muto/core)
    * ``core_twin``       digital-twin cache of the vehicle's current stack

``muto_composer`` (https://github.com/eclipse-muto/composer)
    * ``muto_composer``   orchestrates the plugins below for each stack request
    * ``provision_plugin``downloads/verifies stack artifacts
    * ``compose_plugin``  builds the workspace (``colcon build``)
    * ``launch_plugin``   starts/stops the stack's launch file or script

All nodes share the parameters in ``muto.yaml`` plus the vehicle identity
passed as launch arguments by ``start-hvac-stack.sh``.
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

MUTO_PARAMS = "/opt/muto_runtime/muto.yaml"


def _muto_node(package: str, executable: str, name: str, extra_parameters=()) -> Node:
    """Create one Muto node under the ``muto`` namespace with the shared parameters."""
    return Node(
        namespace=LaunchConfiguration("muto_namespace"),
        name=name,
        package=package,
        executable=executable,
        output="screen",
        parameters=[
            MUTO_PARAMS,
            {"namespace": LaunchConfiguration("vehicle_namespace")},
            {"name": LaunchConfiguration("vehicle_name")},
            *extra_parameters,
        ],
    )


def generate_launch_description() -> LaunchDescription:
    arguments = [
        DeclareLaunchArgument("muto_namespace", default_value="muto"),
        DeclareLaunchArgument(
            "vehicle_namespace",
            default_value="org.eclipse.muto.guardian",
            description="Vehicle ID namespace (Ditto thing namespace)",
        ),
        DeclareLaunchArgument(
            "vehicle_name",
            default_value="guardian-hvac",
            description="Vehicle name (Ditto thing name)",
        ),
    ]

    # The composer nodes accept an ignored_packages list; an empty string
    # entry means "ignore nothing" and avoids an empty-array parameter.
    composer_extra = [{"ignored_packages": [""]}]

    nodes = [
        _muto_node("muto_agent", "muto_agent", "agent"),
        _muto_node("muto_agent", "mqtt", "gateway"),
        _muto_node("muto_agent", "commands", "commands_plugin"),
        _muto_node("muto_core", "twin", "core_twin"),
        _muto_node("muto_composer", "muto_composer", "muto_composer", composer_extra),
        _muto_node("muto_composer", "compose_plugin", "compose_plugin", composer_extra),
        _muto_node("muto_composer", "provision_plugin", "provision_plugin", composer_extra),
        _muto_node("muto_composer", "launch_plugin", "launch_plugin", composer_extra),
    ]

    return LaunchDescription(arguments + nodes)
