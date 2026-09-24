"""Unit tests for the HVAC CAN frame codec.

What is covered:

* value clamping to the ranges declared in ``config/hvac.dbc``
* the built-in (manual) encoder/decoder, including short and oversized frames
* that the DBC codec (cantools) and the manual codec agree byte-for-byte, so
  the two implementations cannot drift apart silently
* the default frame IDs in the DBC (0x320 state, 0x321 command)
* a round trip over python-can's ``virtual`` bus

The tests import ``hvac_simulator`` directly, which needs ``rclpy`` and
``diagnostic_msgs``, so run them inside the ros2-hvac image (see README §9):

    podman run --rm --entrypoint bash \
      -v ./ros2-hvac/ros2_ws/src/hack_to_the_future_hvac:/pkg:ro \
      localhost/hack-to-the-future/ros2-hvac:stage-ros2 \
      -c "source /opt/ros/humble/setup.bash; cd /pkg && python3 -m pytest -p no:cacheprovider test/ -v"

DBC-dependent tests skip automatically when cantools is not installed.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hack_to_the_future_hvac.hvac_simulator import (  # noqa: E402
    clamp_hvac_values,
    decode_hvac_payload,
    encode_hvac_payload,
    load_can_database,
)

DBC_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "hvac.dbc")


@pytest.fixture
def dbc():
    db = load_can_database(DBC_PATH)
    if db is None:
        pytest.skip("cantools not available or DBC not loadable")
    return db


def test_clamp_ranges():
    assert clamp_hvac_values(22, 50) == (22, 50)
    assert clamp_hvac_values(5, -10) == (16, 0)
    assert clamp_hvac_values(99, 250) == (30, 100)


def test_encode_manual_layout():
    assert encode_hvac_payload(None, 22, 10, True, False) == bytes([0x16, 0x0A, 0x01])
    assert encode_hvac_payload(None, 24, 40, True, True) == bytes([0x18, 0x28, 0x03])
    assert encode_hvac_payload(None, 16, 0, False, False) == bytes([0x10, 0x00, 0x00])


def test_encode_manual_clamps():
    assert encode_hvac_payload(None, 99, 250, False, True) == bytes([0x1E, 0x64, 0x02])
    assert encode_hvac_payload(None, 0, -1, False, False) == bytes([0x10, 0x00, 0x00])


def test_decode_manual():
    command = decode_hvac_payload(None, bytes([0x18, 0x28, 0x03]))
    assert command == {
        "target_temperature_celsius": 24,
        "fan_speed_percent": 40,
        "air_conditioning_active": True,
        "fault_active": True,
    }


def test_decode_manual_clamps():
    command = decode_hvac_payload(None, bytes([0xFF, 0xFF, 0x00]))
    assert command["target_temperature_celsius"] == 30
    assert command["fan_speed_percent"] == 100


def test_decode_short_frame_rejected():
    assert decode_hvac_payload(None, b"") is None
    assert decode_hvac_payload(None, bytes([0x16, 0x0A])) is None


def test_decode_ignores_trailing_bytes():
    command = decode_hvac_payload(None, bytes([0x16, 0x0A, 0x01, 0xAA, 0xBB]))
    assert command["target_temperature_celsius"] == 22


def test_roundtrip_manual():
    for temp in (16, 22, 30):
        for fan in (0, 55, 100):
            for ac in (False, True):
                for fault in (False, True):
                    payload = encode_hvac_payload(None, temp, fan, ac, fault)
                    command = decode_hvac_payload(None, payload)
                    assert command == {
                        "target_temperature_celsius": temp,
                        "fan_speed_percent": fan,
                        "air_conditioning_active": ac,
                        "fault_active": fault,
                    }


def test_dbc_matches_manual_encoding(dbc):
    """The hand-packed layout and the hvac.dbc contract must agree bit-for-bit."""
    for temp in (16, 22, 30):
        for fan in (0, 55, 100):
            for ac in (False, True):
                for fault in (False, True):
                    manual = encode_hvac_payload(None, temp, fan, ac, fault)
                    from_dbc = encode_hvac_payload(dbc, temp, fan, ac, fault)
                    assert from_dbc == manual, (temp, fan, ac, fault)


def test_dbc_matches_manual_decoding(dbc):
    for payload in (bytes([0x16, 0x0A, 0x01]), bytes([0x18, 0x28, 0x03]), bytes([0x1E, 0x64, 0x00])):
        assert decode_hvac_payload(dbc, payload) == decode_hvac_payload(None, payload)


def test_dbc_default_frame_ids(dbc):
    assert dbc.get_message_by_name("HVAC_STATE").frame_id == 0x320
    assert dbc.get_message_by_name("HVAC_COMMAND").frame_id == 0x321


def test_virtual_bus_roundtrip(dbc):
    can = pytest.importorskip("can")
    with can.Bus(interface="virtual", channel="codec-test") as tx_bus, can.Bus(
        interface="virtual", channel="codec-test"
    ) as rx_bus:
        payload = encode_hvac_payload(dbc, 27, 42, True, False)
        tx_bus.send(can.Message(arbitration_id=0x321, data=payload, is_extended_id=False))
        msg = rx_bus.recv(timeout=2.0)
        assert msg is not None
        assert msg.arbitration_id == 0x321
        command = decode_hvac_payload(dbc, msg.data)
        assert command == {
            "target_temperature_celsius": 27,
            "fan_speed_percent": 42,
            "air_conditioning_active": True,
            "fault_active": False,
        }
