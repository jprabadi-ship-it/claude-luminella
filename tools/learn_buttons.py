"""Capture and decode Luminella input frames so buttons can be identified.

Run it, then press each switch one at a time. Every decoded frame is printed
with a timestamp so presses can be told apart by the gaps between them.
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from luminella import protocol

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 45.0


def main():
    ser = protocol.open_port()
    ok, raw = protocol.handshake(ser)
    print(f"handshake: {'OK' if ok else 'NO REPLY'}  raw={raw!r}", flush=True)
    if not ok:
        print("device did not answer -- is Orbital2 Core running and holding the port?")

    # Dim white so it is obvious the capture is live.
    ser.write(protocol.led(40, 40, 40))
    ser.flush()

    print(f"\ncapturing {DURATION:.0f}s -- press the switches one at a time\n", flush=True)
    start = time.time()
    buf = b""
    seen = {}
    while time.time() - start < DURATION:
        buf += ser.read(256)
        frames, buf = protocol.parse(buf)
        for tag, payload in frames:
            if tag == b"JS":
                continue  # stick position streams constantly; too noisy here
            t = time.time() - start
            key = (tag, payload)
            seen[key] = seen.get(key, 0) + 1
            print(
                f"[{t:6.2f}s] {tag.decode('ascii', 'replace'):2}  "
                f"{payload.hex(' ') or '-':11}  {payload!r}",
                flush=True,
            )

    ser.write(protocol.led(0, 0, 0))
    ser.flush()
    ser.close()

    print("\n=== distinct non-JS frames ===", flush=True)
    for (tag, payload), n in sorted(seen.items()):
        print(f"  {tag.decode('ascii', 'replace'):2}  {payload.hex(' '):11}  x{n}", flush=True)


if __name__ == "__main__":
    main()
