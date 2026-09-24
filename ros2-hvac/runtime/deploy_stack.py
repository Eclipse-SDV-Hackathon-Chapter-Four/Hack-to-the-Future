#!/usr/bin/env python3
"""One-shot publisher that asks Eclipse Muto to start the HVAC stack.

Eclipse Muto is a ROS 2 orchestration framework: a *stack* manifest describes
what to run, and Muto's composer fetches, builds and launches it. In a
production fleet the manifest arrives from a cloud backend (Eclipse Ditto /
Hono via MQTT). In this self-contained demo there is no backend, so this tiny
node plays that role: it reads ``hvac_stack_archive.json`` and publishes it as
a ``MutoAction`` with method ``start`` on ``/muto/stack``, which is exactly
the message the Muto agent would have forwarded from the cloud.

Flow after this message is published (all inside the ros2-hvac container):

    /muto/stack ──► muto_agent ──► muto_composer
                                   ├─ provision_plugin: download the tarball
                                   │    from http://artifact-server:9090 and
                                   │    verify its sha256 checksum
                                   ├─ compose_plugin:   colcon build it under
                                   │    /root/.muto/workspaces/<stack name>/
                                   └─ launch_plugin:    run its run.sh

The node waits ``discovery_wait_s`` seconds first so DDS discovery has found
the Muto nodes (otherwise the message would be published to nobody), then
publishes once and shuts itself down.

Invoked by ``start-hvac-stack.sh``. Parameters:

* ``stack_path``       path of the stack manifest to publish
* ``stack_topic``      Muto stack topic (default ``/muto/stack``)
* ``discovery_wait_s`` delay before publishing
* ``shutdown_delay_s`` delay before exiting, so the message is flushed
"""

import json
from pathlib import Path

from muto_msgs.msg import MutoAction
import rclpy
from rclpy.node import Node


class StackDeployer(Node):
    def __init__(self) -> None:
        super().__init__("hvac_stack_deployer")
        self.declare_parameter("stack_path", "/opt/muto_runtime/hvac_stack_archive.json")
        self.declare_parameter("stack_topic", "/muto/stack")
        self.declare_parameter("discovery_wait_s", 3)
        self.declare_parameter("shutdown_delay_s", 1)

        self.publisher = self.create_publisher(
            MutoAction, str(self.get_parameter("stack_topic").value), 10
        )
        self.published = False
        self.timer = self.create_timer(
            float(self.get_parameter("discovery_wait_s").value), self._publish_stack
        )

    def _publish_stack(self) -> None:
        if self.published:
            return
        self.timer.cancel()

        stack_path = Path(str(self.get_parameter("stack_path").value))
        payload = json.loads(stack_path.read_text(encoding="utf-8"))

        msg = MutoAction()
        msg.method = "start"
        msg.payload = json.dumps(payload)
        self.publisher.publish(msg)
        self.published = True
        self.get_logger().info(f"Published Muto stack from {stack_path}")

        self.shutdown_timer = self.create_timer(
            float(self.get_parameter("shutdown_delay_s").value), self._shutdown
        )

    def _shutdown(self) -> None:
        self.shutdown_timer.cancel()
        rclpy.shutdown()


def main() -> None:
    rclpy.init()
    node = StackDeployer()
    try:
        rclpy.spin(node)
    finally:
        if rclpy.ok():
            rclpy.shutdown()
        node.destroy_node()


if __name__ == "__main__":
    main()
