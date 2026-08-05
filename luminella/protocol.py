"""Luminella (BRAIN MAGIC) direct serial protocol.

Recovered from Orbital2 Core.app v1.11.0 sourcemaps
(src/main/O2Driver.ts, O2Controller.ts, O2Parser.ts) and verified on hardware.

Wire format
-----------
9600 baud, 8 data bits, EVEN parity, 1 stop bit.
Every logical byte is transmitted as two bytes: <byte> 0x00.
Frames are terminated by ';' (also 0x00-padded).

Outbound
    O  4f 00 00 00 3b 00                    handshake -> device replies "OK;"
    T  54 00 R 00 G 00 B 00 3b 00           glow ring colour
    R  52 00 00 00 3b 00                    reset stick centre position
    M  4d 00 P 00 3b 00                     vibration (Orbital2 only, not Luminella)

Inbound (';'-terminated, 2-byte command tag + fixed payload)
    OK  0 bytes    handshake ack
    JS  4 bytes    'X' <x> 'Y' <y>   stick position, 0x80 = centre
    RE  2 bytes    rotary encoder    (Orbital2 only; Luminella has no RE)
    SW  3 bytes    switch state
    RC  3 bytes    ring capacitance / flat ring touch
"""

import time

import serial

PORT = "/dev/cu.SLAB_USBtoUART"

# Device IDs from DeviceType.ts. Luminella is the "LightModel".
VID_PID = ("3525", "0002")

TERMINATOR = 0x3B  # ';'

# Inbound command tag -> payload length, from O2Parser.ts COMMAND_TABLE.
COMMAND_TABLE = {b"OK": 0, b"JS": 4, b"RE": 2, b"SW": 3, b"RC": 3}


def open_port(port=PORT, timeout=0.05):
    """Open the Luminella serial port with the exact settings O2Driver uses."""
    return serial.Serial(
        port,
        9600,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_EVEN,
        stopbits=serial.STOPBITS_ONE,
        timeout=timeout,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )


def frame(*logical_bytes):
    """Encode logical bytes into the 0x00-padded, ';'-terminated wire frame."""
    out = bytearray()
    for b in logical_bytes:
        out += bytes((b, 0x00))
    out += bytes((TERMINATOR, 0x00))
    return bytes(out)


HELLO = frame(0x4F, 0x00)
RESET_POSITION = frame(0x52, 0x00, 0x00)


def led(r, g, b):
    """Frame that sets the glow ring to an RGB colour."""
    return frame(0x54, r & 0xFF, g & 0xFF, b & 0xFF)


def handshake(ser, timeout=3.0):
    """Send 'O' and wait for the device's "OK;". Returns (ok, raw_bytes)."""
    ser.reset_input_buffer()
    ser.write(HELLO)
    ser.flush()
    deadline = time.time() + timeout
    buf = b""
    while time.time() < deadline:
        buf += ser.read(64)
        if b"OK" in buf:
            return True, buf
    return False, buf


def parse(buf):
    """Split a byte buffer into (tag, payload) frames.

    Mirrors O2Parser._transform: match a known command tag and its fixed
    payload length against the separator; otherwise resynchronise on the next
    ';'. Returns (frames, remainder).
    """
    frames = []
    data = bytes(buf)
    while data:
        tag = data[:2]
        length = COMMAND_TABLE.get(tag)
        if length is not None:
            end = 2 + length
            if len(data) > end and data[end] == TERMINATOR:
                frames.append((tag, data[2:end]))
                data = data[end + 1 :]
                continue
            if len(data) <= end:
                break  # incomplete frame, wait for more bytes

        idx = data.find(bytes((TERMINATOR,)))
        if idx == -1:
            break
        frames.append((data[:2], data[2:idx]))
        data = data[idx + 1 :]
    return frames, data
