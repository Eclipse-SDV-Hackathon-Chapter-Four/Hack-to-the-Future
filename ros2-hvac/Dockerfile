# syntax=docker/dockerfile:1.7
#
# Image for the ros2-hvac compose service. Two stages:
#
#   rust-build  compiles services/src/bin/ros2_hvac_bridge.rs (the
#               uProtocol/Zenoh <-> ROS 2 bridge and HVAC fault console)
#   runtime     ROS 2 Humble + Eclipse Muto (built from source) + ros2_medkit
#               gateway + python-can/cantools, plus the Muto bootstrap files
#               from runtime/ and the entrypoint start-hvac-stack.sh
#
# The HVAC simulator itself is NOT in this image: Muto downloads it at runtime
# from the artifact-server service (artifacts/hvac_simulator.tar.gz). See
# ros2-hvac/README.md for the component map.

# ── Stage 1: Rust bridge ─────────────────────────────────────────────────────
FROM docker.io/library/rust:1-bookworm AS rust-build
WORKDIR /workspace

COPY Cargo.toml Cargo.lock ./
COPY services/Cargo.toml services/Cargo.toml
COPY services/src services/src

RUN cargo build --target-dir /workspace/target --bin ros2_hvac_bridge \
    && mkdir -p /out \
    && cp /workspace/target/debug/ros2_hvac_bridge /out/ros2_hvac_bridge

# ── Stage 2: ROS 2 runtime ───────────────────────────────────────────────────
FROM docker.io/library/ros:humble-ros-base
ARG ROS_DISTRO=humble

SHELL ["/bin/bash", "-lc"]

# System packages:
#   can-utils, iproute2, kmod   SocketCAN tooling for the Linux host-CAN variant
#   python3-colcon-*, rosdep    needed by Muto's compose plugin to build stacks
#   ros-*-diagnostic-msgs       message type used by the simulator
#   ros-*-rqt*                  ROS 2 GUI inspection tools (X11 forwarding)
#   ros-*-ros2-medkit-gateway   REST diagnostics gateway (ros2_medkit)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        can-utils \
        curl \
        git \
        iproute2 \
        kmod \
        python3-colcon-common-extensions \
        python3-jsonschema \
        python3-pip \
        python3-pytest \
        python3-rosdep \
        python3-requests \
        python3-setuptools \
        python3-vcstool \
        ros-${ROS_DISTRO}-diagnostic-msgs \
        ros-${ROS_DISTRO}-rqt \
        ros-${ROS_DISTRO}-rqt-common-plugins \
        ros-${ROS_DISTRO}-rqt-graph \
        ros-${ROS_DISTRO}-ros2-medkit-gateway \
    && rm -rf /var/lib/apt/lists/*

# CAN libraries for the simulator. apt's python3-can on Jammy is 3.3.2, which
# lacks the cross-platform udp_multicast interface (needs >= 4.0); msgpack is
# its runtime dependency. cantools provides the hvac.dbc frame codec.
RUN pip3 install --no-cache-dir "python-can>=4.2,<5" msgpack cantools

RUN rosdep init 2>/dev/null || true \
    && rosdep update

# Eclipse Muto, built from source: agent (entry point + MQTT gateway), core
# (digital twin), messages (ROS interfaces), composer (provision/build/launch).
WORKDIR /opt/muto_ws/src
RUN git clone --depth 1 https://github.com/eclipse-muto/agent.git \
    && git clone --depth 1 https://github.com/eclipse-muto/core.git \
    && git clone --depth 1 https://github.com/eclipse-muto/messages.git \
    && git clone --depth 1 https://github.com/eclipse-muto/composer.git

WORKDIR /opt/muto_ws
RUN apt-get update \
    && . /opt/ros/${ROS_DISTRO}/setup.sh \
    && rosdep install --from-paths src --ignore-src -r -y --skip-keys="ament_python" \
    && colcon build --merge-install --cmake-args -DCMAKE_BUILD_TYPE=Release

# Muto bootstrap (launch file, parameters, stack manifest, deployer node),
# the container entrypoint, and the Rust bridge.
COPY ros2-hvac/start-hvac-stack.sh /usr/local/bin/start-hvac-stack.sh
COPY ros2-hvac/runtime /opt/muto_runtime
COPY --from=rust-build /out/ros2_hvac_bridge /usr/local/bin/ros2_hvac_bridge

RUN chmod +x \
        /usr/local/bin/start-hvac-stack.sh \
        /usr/local/bin/ros2_hvac_bridge \
        /opt/muto_runtime/deploy_stack.py \
    && mkdir -p /root/.ros2_medkit

ENV RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
    ROS_DOMAIN_ID=0 \
    ROS_DISTRO=humble

ENTRYPOINT ["/usr/local/bin/start-hvac-stack.sh"]
