from __future__ import annotations

from datetime import datetime

import pytest

from green_screen_agent.simulator import CustomerStore, DemoApplication
from green_screen_agent.simulator.datastream import (
    EOR,
    IAC,
    ORDER_IC,
    ORDER_SBA,
    ORDER_SF,
    InboundRecord,
    Input,
    ScreenDefinition,
    TelnetParser,
    Text,
    decode_address,
    encode_address,
    encode_attribute,
    frame_record,
    parse_inbound,
)

from conftest import DEMO_SECRET, DEMO_USER

AID_ENTER, AID_CLEAR, AID_PF3, AID_PF5, AID_PF7, AID_PF8 = 0x7D, 0x6D, 0xF3, 0xF5, 0xF7, 0xF8


def test_address_encoding_round_trip():
    for address in range(4096):
        assert decode_address(*encode_address(address)) == address
    assert encode_address(0) == b"\x40\x40"
    assert encode_address(80) == b"\xc1\x50"  # row 2, column 1
    assert decode_address(0x07, 0x7F) == 0x077F  # 14-bit address
    with pytest.raises(ValueError):
        encode_address(4096)


def test_screen_record_layout():
    screen = ScreenDefinition([Text(1, 2, "HELLO"), Input("name", 3, 10, 5, value="AB")], cursor_field="name")
    record = screen.to_record()
    assert record[:2] == b"\xf5\xc3"  # Erase/Write, WCC reset + restore keyboard
    assert bytes([ORDER_SBA]) + encode_address(0) + bytes([ORDER_SF, encode_attribute(0x20)]) in record
    assert "HELLO".encode("cp037") in record
    input_start = screen.address(3, 10)
    assert bytes([ORDER_SF, encode_attribute(0)]) + "AB".encode("cp037") in record
    # Input field is terminated by a protected attribute after its last character.
    assert bytes([ORDER_SBA]) + encode_address(input_start + 5) + bytes([ORDER_SF]) in record
    assert record.endswith(bytes([ORDER_SBA]) + encode_address(input_start) + bytes([ORDER_IC]))
    assert set(screen.inputs()) == {input_start}


def test_parse_inbound_read_modified():
    record = (
        bytes([AID_ENTER]) + encode_address(5)
        + bytes([ORDER_SBA]) + encode_address(170) + "abc".encode("cp037")
        + bytes([ORDER_SBA]) + encode_address(250) + b"\x00" + "x".encode("cp037")
    )
    inbound = parse_inbound(record)
    assert inbound.aid_name == "ENTER"
    assert inbound.cursor == 5
    assert inbound.fields == {170: "abc", 250: "x"}


def test_parse_inbound_short_read():
    inbound = parse_inbound(bytes([AID_CLEAR]))
    assert inbound.aid_name == "CLEAR"
    assert inbound.fields == {}
    assert parse_inbound(bytes([AID_PF3]) + encode_address(0)).aid_name == "PF3"


def test_telnet_parser_handles_split_negotiation_and_records():
    data = (
        bytes([IAC, 0xFD, 0x18])  # DO TERMINAL-TYPE
        + bytes([IAC, 0xFA, 0x18, 0x00]) + b"IBM-DYNAMIC" + bytes([IAC, 0xF0])
        + frame_record(b"\x7d\xff\x01")
    )
    parser = TelnetParser()
    events = []
    for i in range(len(data)):  # feed one byte at a time
        events += parser.feed(data[i:i + 1])
    assert events == [("do", 0x18), ("sb", b"\x18\x00IBM-DYNAMIC"), ("record", b"\x7d\xff\x01")]


def test_frame_record_escapes_iac():
    assert frame_record(b"\x01\xff\x02") == b"\x01\xff\xff\x02" + bytes([IAC, EOR])


class DemoDriver:
    """Drive DemoApplication directly with field values by name."""

    def __init__(self) -> None:
        self.store = CustomerStore()
        self.app = DemoApplication(self.store, clock=lambda: datetime(2026, 1, 2, 3, 4, 5))
        self.screen = self.app.start()

    def send(self, aid: int, values: dict[str, str] | None = None) -> DemoApplication:
        by_name = {field.name: address for address, field in self.screen.inputs().items()}
        fields = {by_name[name]: value for name, value in (values or {}).items()}
        self.screen = self.app.handle(InboundRecord(aid, cursor=0, fields=fields))
        return self.app

    def sign_on(self) -> None:
        self.send(AID_ENTER, {"userid": DEMO_USER, "password": DEMO_SECRET})
        assert self.app.state == "menu"


def test_demo_sign_on_rejects_bad_password():
    demo = DemoDriver()
    app = demo.send(AID_ENTER, {"userid": DEMO_USER, "password": "WRONG"})
    assert app.state == "signon"
    assert "Invalid userid or password" in app.message
    app = demo.send(AID_ENTER, {"userid": DEMO_USER.lower(), "password": DEMO_SECRET.lower()})
    assert app.state == "menu"
    assert app.message == "Sign-on complete. Welcome, DEMO."


def test_demo_customer_update_validation():
    demo = DemoDriver()
    demo.sign_on()
    demo.send(AID_ENTER, {"option": "1"})
    app = demo.send(AID_ENTER, {"custno": "12"})
    assert app.message == "Customer number must be 6 digits."
    app = demo.send(AID_ENTER, {"custno": "100003"})
    assert app.customer is not None and app.customer.status == "HOLD"
    app = demo.send(AID_PF5, {"status": "BOGUS"})
    assert "Invalid status" in app.message
    app = demo.send(AID_PF5, {"phone": "555-0100", "status": "active"})
    assert app.message == "Customer 100003 updated."
    assert demo.store.get("100003").status == "ACTIVE"
    assert demo.store.get("100003").phone == "555-0100"


def test_demo_list_paging_and_sign_off():
    demo = DemoDriver()
    demo.sign_on()
    app = demo.send(AID_ENTER, {"option": "2"})
    assert app.state == "list"
    assert demo.send(AID_PF7).message == "Top of list."
    demo.send(AID_PF8)
    demo.send(AID_PF8)
    assert app.page == 2
    assert demo.send(AID_PF8).message == "Bottom of list."
    demo.send(AID_PF3)
    app = demo.send(AID_PF3)
    assert (app.state, app.message) == ("signon", "Sign-off complete.")
    app = demo.send(AID_PF3)
    assert app.closed
