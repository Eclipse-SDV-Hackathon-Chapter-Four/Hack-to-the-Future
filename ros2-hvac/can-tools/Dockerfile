# syntax=docker/dockerfile:1.7
#
# Tiny CAN command-line container (cansend/candump equivalents) for the
# containerized udp_multicast bus. Compose service: can-tools (profile
# can-tools). Build context is ros2-hvac/ so the DBC contract stays
# single-sourced from the ROS package.
FROM docker.io/library/python:3.12-slim

RUN pip install --no-cache-dir "python-can>=4.2,<5" msgpack cantools

COPY ros2_ws/src/hack_to_the_future_hvac/config/hvac.dbc /usr/local/share/hvac.dbc
COPY can-tools/can_tool.py /usr/local/bin/can_tool.py

ENTRYPOINT ["python", "/usr/local/bin/can_tool.py"]
CMD ["dump"]
