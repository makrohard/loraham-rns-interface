"""The direct-SPI interface's limits towards RNS: a bounded outbound queue and a fixed
hardware MTU that leaves room for the IFAC bytes."""


def test_the_outbound_queue_is_bounded():
    """A packet may wait MAX_DUTY_HOLD seconds for duty allowance; unbounded, a
    chatty local client fills memory on a Pi Zero."""
    from collections import deque

    from loraham_rns.interface import LoRaSPIInterface

    iface = LoRaSPIInterface.__new__(LoRaSPIInterface)
    iface._tx_queue, iface._tx_dropped = deque(), 0
    iface._tx_event = type("E", (), {"set": lambda self: None})()
    iface.online, iface.max_packet = True, 255

    limit = LoRaSPIInterface.MAX_QUEUE_PACKETS
    for _ in range(limit + 10):
        LoRaSPIInterface.process_outgoing(iface, b"x" * 32)
    assert len(iface._tx_queue) == limit
    assert iface._tx_dropped == 10, "dropped packets must be counted, not silent"


def test_the_hardware_mtu_is_declared_as_fixed():
    """One RNS packet goes in one LoRa payload (255 B max). RNS's base MTU is 500, and
    it only honours an interface's HW_MTU when FIXED_MTU or AUTOCONFIGURE_MTU is set —
    otherwise links keep the 500 B default and oversized packets were dropped here."""
    from loraham_rns.interface import LoRaSPIInterface
    from loraham_rns.radio import MAX_PAYLOAD

    assert LoRaSPIInterface.FIXED_MTU is True
    assert LoRaSPIInterface.AUTOCONFIGURE_MTU is False, \
        "our limit is the radio FIFO, not the bitrate table"
    assert MAX_PAYLOAD == 255


def test_the_mtu_leaves_room_for_the_ifac_bytes():
    """RNS appends the IFAC in Transport.transmit(), AFTER packing: advertising the
    full 255 made a legal 255-byte packet leave as 263 and get dropped here."""
    from loraham_rns.interface import LoRaSPIInterface
    from loraham_rns.radio import MAX_PAYLOAD

    assert LoRaSPIInterface.DEFAULT_IFAC_SIZE == 8
    assert MAX_PAYLOAD - LoRaSPIInterface.DEFAULT_IFAC_SIZE == 247
