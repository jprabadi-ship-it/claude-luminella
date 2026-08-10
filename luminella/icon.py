"""Menu bar icons: the glow ring, drawn in the colour of the current state.

The emoji this replaces were out of place in a macOS menu bar, and worse,
ambiguous -- error and recording were both a red circle, notification and
transcribing both a purple one. A ring shape shared with the app icon reads as
this app whatever colour it happens to be, and the shape itself now carries
what the emoji could not: a solid ring for settled states, a broken one for
states that want attention.
"""

import math
import os
import tempfile

from AppKit import (
    NSBezierPath,
    NSBitmapImageRep,
    NSCalibratedRGBColorSpace,
    NSColor,
    NSGraphicsContext,
    NSPNGFileType,
)

PX_W = 72        # rendered at 2x for retina; twice as wide as tall so the
PX_H = 36        # glow has somewhere to spill and the item stands out
PT_W = 36        # displayed size in points
PT = 18
RADIUS = 12.0
STROKE = 3.6
DOT = 3.4


def _visible(rgb):
    """Lift a colour to something readable against a menu bar.

    Ring colours are chosen to look right on the device, where a dim blue is
    calm and legible. At 18 points against a translucent bar the same value is
    nearly invisible, so scale it up while keeping the hue.
    """
    r, g, b = [max(0, min(255, int(c))) for c in rgb]
    peak = max(r, g, b)
    if peak == 0:
        return (0.45, 0.45, 0.45)          # "off" reads as grey, not nothing
    if peak < 235:
        scale = 235.0 / peak
        r, g, b = (min(255, c * scale) for c in (r, g, b))
    return (r / 255.0, g / 255.0, b / 255.0)


def render(rgb, path, gap=True):
    """Draw one ring icon to a PNG file."""
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, PX_W, PX_H, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0
    )
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(context)

    r, g, b = _visible(rgb)
    cx, cy = PX_W / 2.0, PX_H / 2.0

    # Background: the state's own colour, pale and translucent, filling the
    # whole item as a rounded pill. A tinted field this size registers in the
    # corner of the eye where a thin ring alone did not.
    NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 0.28).set()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        ((0, 0), (PX_W, PX_H)), cy, cy
    ).fill()

    NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 1.0).set()

    ring = NSBezierPath.bezierPath()
    ring.setLineWidth_(STROKE)
    ring.setLineCapStyle_(1)  # round
    if gap:
        # Same 300-degree sweep as the app icon, so the two read as one family.
        ring.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
            (cx, cy), RADIUS, 125.0, 65.0
        )
    else:
        ring.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
            (cx, cy), RADIUS, 0.0, 360.0
        )
    ring.stroke()

    dot = NSBezierPath.bezierPathWithOvalInRect_(
        ((cx - DOT, cy - DOT), (DOT * 2, DOT * 2))
    )
    dot.fill()

    NSGraphicsContext.restoreGraphicsState()

    data = rep.representationUsingType_properties_(NSPNGFileType, {})
    data.writeToFile_atomically_(path, True)
    return path


def render_states(states):
    """Render every state's icon once. Returns {state: png path}.

    Blinking states get the broken ring and steady ones the closed ring, so
    the shape says "waiting on you" even before the colour registers.
    """
    directory = tempfile.mkdtemp(prefix="luminella-icons-")
    paths = {}
    for name, spec in states.items():
        gap = spec.get("mode") == "blink"
        paths[name] = render(spec["color"], os.path.join(directory, name + ".png"), gap=gap)
    return paths
