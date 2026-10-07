"""Minimal TN3270 / 3270 data stream helpers for the bundled host simulator.

Only the subset of the protocol that the simulator needs is implemented:
Telnet negotiation of TERMINAL-TYPE, EOR and BINARY (RFC 1576 "TN3270"),
outbound Erase/Write records built from field definitions, and parsing of
inbound Read Modified records.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CODEPAGE = "cp037"

# Telnet
IAC = 0xFF
DONT = 0xFE
DO = 0xFD
WONT = 0xFC
WILL = 0xFB
SB = 0xFA
SE = 0xF0
EOR = 0xEF
OPT_BINARY = 0x00
OPT_TERMINAL_TYPE = 0x18
OPT_EOR = 0x19
TTYPE_IS = 0x00
TTYPE_SEND = 0x01

# 3270 commands, orders and write control characters
CMD_ERASE_WRITE = 0xF5
WCC_RESET_RESTORE = 0xC3  # reset, restore keyboard, reset modified data tags
ORDER_SBA = 0x11  # set buffer address
ORDER_SF = 0x1D  # start field
ORDER_IC = 0x13  # insert cursor

# Field attribute bits (before 6-bit encoding)
ATTR_PROTECTED = 0x20
ATTR_NUMERIC = 0x10
ATTR_INTENSIFIED = 0x08
ATTR_NONDISPLAY = 0x0C

# Attention identifiers
AID_NAMES: dict[int, str] = {
    0x7D: "ENTER",
    0x6D: "CLEAR",
    0x6C: "PA1",
    0x6E: "PA2",
    0x6B: "PA3",
    **{0xF1 + i: f"PF{i + 1}" for i in range(9)},  # PF1-PF9
    0x7A: "PF10",
    0x7B: "PF11",
    0x7C: "PF12",
    **{0xC1 + i: f"PF{i + 13}" for i in range(9)},  # PF13-PF21
    0x4A: "PF22",
    0x4B: "PF23",
    0x4C: "PF24",
}
SHORT_READ_AIDS = {0x6D, 0x6C, 0x6E, 0x6B}  # CLEAR and PA keys send no data

# 6-bit code table used for 12-bit buffer addresses and field attributes.
_CODES = bytes(
    [
        0x40, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F,
        0x50, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0x5A, 0x5B, 0x5C, 0x5D, 0x5E, 0x5F,
        0x60, 0x61, 0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0x6A, 0x6B, 0x6C, 0x6D, 0x6E, 0x6F,
        0xF0, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0x7A, 0x7B, 0x7C, 0x7D, 0x7E, 0x7F,
    ]
)  # fmt: skip


def encode_address(address: int) -> bytes:
    """Encode a buffer address as two bytes (12-bit addressing)."""
    if not 0 <= address < 4096:
        raise ValueError(f"buffer address out of range: {address}")
    return bytes([_CODES[address >> 6], _CODES[address & 0x3F]])


def decode_address(first: int, second: int) -> int:
    """Decode a two byte buffer address (12-bit or 14-bit addressing)."""
    if first & 0xC0 == 0:  # 14-bit binary address
        return (first << 8) | second
    return ((first & 0x3F) << 6) | (second & 0x3F)


def encode_attribute(bits: int) -> int:
    """Encode field attribute bits as a 3270 attribute byte."""
    return _CODES[bits & 0x3F]


@dataclass(frozen=True)
class Text:
    """Protected text whose first character is at a 1-based row/column."""

    row: int
    col: int
    text: str
    bright: bool = False


@dataclass(frozen=True)
class Input:
    """An unprotected input field whose first character is at a 1-based row/column."""

    name: str
    row: int
    col: int
    length: int
    value: str = ""
    hidden: bool = False
    numeric: bool = False
    bright: bool = False


@dataclass
class ScreenDefinition:
    """A full screen: protected text, input fields and the cursor position."""

    items: list[Text | Input]
    cursor_field: str | None = None
    rows: int = 24
    cols: int = 80

    def address(self, row: int, col: int) -> int:
        return (row - 1) * self.cols + (col - 1)

    def inputs(self) -> dict[int, Input]:
        """Input fields keyed by the buffer address of their first character."""
        return {self.address(i.row, i.col): i for i in self.items if isinstance(i, Input)}

    def to_record(self) -> bytes:
        """Build an Erase/Write 3270 record (without Telnet framing)."""
        size = self.rows * self.cols
        out = bytearray([CMD_ERASE_WRITE, WCC_RESET_RESTORE])
        cursor = None
        first_input = None
        for item in self.items:
            start = self.address(item.row, item.col)
            out += bytes([ORDER_SBA]) + encode_address((start - 1) % size)
            if isinstance(item, Text):
                bits = ATTR_PROTECTED | (ATTR_INTENSIFIED if item.bright else 0)
                out += bytes([ORDER_SF, encode_attribute(bits)]) + item.text.encode(CODEPAGE)
                continue
            bits = 0
            if item.hidden:
                bits |= ATTR_NONDISPLAY
            elif item.bright:
                bits |= ATTR_INTENSIFIED
            if item.numeric:
                bits |= ATTR_NUMERIC
            out += bytes([ORDER_SF, encode_attribute(bits)])
            out += item.value[: item.length].encode(CODEPAGE)
            # Terminate the input field with a protected (auto-skip) attribute.
            out += bytes([ORDER_SBA]) + encode_address((start + item.length) % size)
            out += bytes([ORDER_SF, encode_attribute(ATTR_PROTECTED | ATTR_NUMERIC)])
            if first_input is None:
                first_input = start
            if item.name == self.cursor_field:
                cursor = start
        if cursor is None:
            cursor = first_input or 0
        out += bytes([ORDER_SBA]) + encode_address(cursor) + bytes([ORDER_IC])
        return bytes(out)


@dataclass
class InboundRecord:
    """A parsed inbound (terminal to host) record."""

    aid: int
    cursor: int | None = None
    fields: dict[int, str] = field(default_factory=dict)

    @property
    def aid_name(self) -> str:
        return AID_NAMES.get(self.aid, f"AID 0x{self.aid:02X}")


def parse_inbound(record: bytes) -> InboundRecord:
    """Parse a Read Modified inbound record: AID, cursor address, modified fields."""
    if not record:
        raise ValueError("empty inbound record")
    aid = record[0]
    if aid in SHORT_READ_AIDS or len(record) < 3:
        return InboundRecord(aid)
    inbound = InboundRecord(aid, cursor=decode_address(record[1], record[2]))
    i = 3
    while i < len(record):
        if record[i] != ORDER_SBA or i + 2 >= len(record):
            i += 1
            continue
        address = decode_address(record[i + 1], record[i + 2])
        end = record.find(bytes([ORDER_SBA]), i + 3)
        if end < 0:
            end = len(record)
        data = record[i + 3 : end].replace(b"\x00", b"")
        inbound.fields[address] = data.decode(CODEPAGE)
        i = end
    return inbound


def frame_record(record: bytes) -> bytes:
    """Escape IAC bytes and terminate a 3270 record with IAC EOR."""
    return record.replace(bytes([IAC]), bytes([IAC, IAC])) + bytes([IAC, EOR])


class TelnetParser:
    """Incremental Telnet parser producing negotiation events and 3270 records.

    ``feed`` returns a list of events:
    ``("will"|"wont"|"do"|"dont", option)``, ``("sb", payload)`` and
    ``("record", data)`` for every IAC EOR terminated record.
    """

    _VERBS = {WILL: "will", WONT: "wont", DO: "do", DONT: "dont"}

    def __init__(self) -> None:
        self._state = "data"
        self._verb = 0
        self._record = bytearray()
        self._sub = bytearray()

    def feed(self, data: bytes) -> list[tuple[str, object]]:
        events: list[tuple[str, object]] = []
        for byte in data:
            state = self._state
            if state == "data":
                if byte == IAC:
                    self._state = "iac"
                else:
                    self._record.append(byte)
            elif state == "iac":
                self._state = "data"
                if byte == IAC:
                    self._record.append(IAC)
                elif byte == EOR:
                    events.append(("record", bytes(self._record)))
                    self._record.clear()
                elif byte in self._VERBS:
                    self._verb = byte
                    self._state = "option"
                elif byte == SB:
                    self._sub.clear()
                    self._state = "sb"
            elif state == "option":
                events.append((self._VERBS[self._verb], byte))
                self._state = "data"
            elif state == "sb":
                if byte == IAC:
                    self._state = "sb_iac"
                else:
                    self._sub.append(byte)
            elif state == "sb_iac":
                if byte == SE:
                    events.append(("sb", bytes(self._sub)))
                    self._state = "data"
                else:  # IAC IAC inside a subnegotiation is a literal 0xFF
                    self._sub.append(byte)
                    self._state = "sb"
        return events
