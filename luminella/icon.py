"""Menu bar icons: the glow ring, drawn in the colour of the current state.

The emoji this replaces were out of place in a macOS menu bar, and worse,
ambiguous -- error and recording were both a red circle, notification and
transcribing both a purple one. A ring shape shared with the app icon reads as
this app whatever colour it happens to be, and the shape itself now carries
what the emoji could not: a solid ring for settled states, a broken one for
states that want attention.

Beside the ring, while a session is working, sits a mark saying what kind of
work it is -- reading, writing, searching, running something. The shapes follow
the resting indicator in Claude's own design language; they are drawn here
rather than bundled as sprite sheets, so nothing of anyone else's ships inside
a GPLv3 repository, and so the mark can take the state's own colour and read as
one object with the ring.

Images are built in memory rather than written to disk. They used to go to a
tempfile.mkdtemp() directory, which macOS sweeps after a few days -- every icon
then failed to load and the status item fell back to an emoji beside a stale
ring, which is exactly what it looked like: a stray mark that would not go away.
"""

import math

from AppKit import (
    NSBezierPath,
    NSBitmapImageRep,
    NSCalibratedRGBColorSpace,
    NSColor,
    NSGraphicsContext,
    NSImage,
    NSMakeRect,
)

PX_H = 36        # rendered at 2x for retina
PT_H = 18        # displayed height in points
RING_W = 72      # width of the ring half on its own
RADIUS = 12.0
STROKE = 3.6
DOT = 3.4

MARK_FRAMES = 16
# Space between the ring's outer edge and the mark. Deliberately smaller than
# the pale margin at either end of the pill, so the ring and the mark group
# together rather than reading as two things at opposite ends.
MARK_GAP = 11.0
# Every mark is drawn into a box this size, so switching between them never
# changes the width of the status item.
MARK_W = 35.2
MARK_H = 20.0

# Alpha for the parts of a mark that are not the moving one. The pill behind
# them is the same colour at 0.28, so 0.38 composited to 0.55 against a 0.28
# surround -- about 1.5:1, which is to say invisible, and the marks read as
# fewer elements than they have. 0.55 lands near 0.68 and separates from both
# the pill and the lit element.
DIM = 0.55

KINDS = ("dots", "read", "write", "code", "search")


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


def _set(rgb, alpha=1.0):
    r, g, b = rgb
    NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, alpha).set()


def _circle(cx, cy, r):
    NSBezierPath.bezierPathWithOvalInRect_(((cx - r, cy - r), (r * 2, r * 2))).fill()


def _bar(x, y, w, h):
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        ((x, y - h / 2.0), (w, h)), h / 2.0, h / 2.0).fill()


def _wave(phase, i, n):
    """0..1, peaking for one element at a time as the phase travels."""
    return (math.sin(phase - i * (2.0 * math.pi / n)) + 1.0) / 2.0


# ---- the marks -------------------------------------------------------------
#
# Each draws inside (left, cy) .. (left + MARK_W, cy), in the colour given.
# Shapes are chosen to be told apart at 18 points by silhouette, not only by
# motion -- at this size you glance, you do not watch.

def _mark_dots(rgb, left, cy, phase):
    """Three dots whose sizes travel as a wave. The resting indicator."""
    n, r_min, r_max = 3, 2.2, 4.6
    spacing = (MARK_W - r_max * 2) / (n - 1)
    for i in range(n):
        r = r_min + (r_max - r_min) * _wave(phase, i, n)
        _set(rgb)
        _circle(left + r_max + i * spacing, cy, r)


def _mark_read(rgb, left, cy, phase):
    """Three lines of text with the eye travelling down them."""
    widths = (MARK_W, MARK_W * 0.72, MARK_W * 0.88)
    n = len(widths)
    for i, w in enumerate(widths):
        y = cy + (n - 1) / 2.0 * 7.0 - i * 7.0
        lit = _wave(phase, i, n)
        _set(rgb, DIM + (1.0 - DIM) * lit)
        _bar(left, y, w, 3.4)


def _mark_write(rgb, left, cy, phase):
    """A nib running along a line, leaving it behind."""
    t = (phase % (2.0 * math.pi)) / (2.0 * math.pi)
    _set(rgb, DIM)
    _bar(left, cy - 6.0, MARK_W, 3.0)                     # the line being written on
    travel = MARK_W - 6.0
    _set(rgb)
    _bar(left, cy - 6.0, max(3.0, travel * t), 3.0)       # the part already written
    _circle(left + travel * t + 1.5, cy + 1.5, 3.4)       # the nib, held above it


def _mark_code(rgb, left, cy, phase):
    """Two chevrons facing out, breathing in turn. Angular, so it cannot be
    mistaken for the dots at a glance."""
    path_w, inset = 7.0, 3.0
    for i, direction in enumerate((-1, 1)):
        lit = _wave(phase, i, 2)
        _set(rgb, DIM + (1.0 - DIM) * lit)
        x = left + MARK_W / 2.0 + direction * (inset + path_w)
        tip = x + direction * path_w * 0.9
        p = NSBezierPath.bezierPath()
        p.setLineWidth_(3.0)
        p.setLineCapStyle_(1)
        p.setLineJoinStyle_(1)
        p.moveToPoint_((x, cy + 6.5))
        p.lineToPoint_((tip, cy))
        p.lineToPoint_((x, cy - 6.5))
        p.stroke()


def _mark_search(rgb, left, cy, phase):
    """A dot sweeping around a ring: looking over a field."""
    cx = left + MARK_W / 2.0
    rx, ry = MARK_W / 2.0 - 4.0, MARK_H / 2.0 - 4.0
    _set(rgb, DIM)
    ring = NSBezierPath.bezierPathWithOvalInRect_(
        NSMakeRect(cx - rx, cy - ry, rx * 2, ry * 2))
    ring.setLineWidth_(2.4)
    ring.stroke()
    _set(rgb)
    _circle(cx + rx * math.cos(phase), cy + ry * math.sin(phase), 3.6)


MARKS = {
    "dots": _mark_dots,
    "read": _mark_read,
    "write": _mark_write,
    "code": _mark_code,
    "search": _mark_search,
}


# ---- the icon --------------------------------------------------------------

def render(rgb, gap=True, phase=None, kind="dots"):
    """Draw one status item image. Returns an NSImage sized in points.

    phase is None for the ring alone, or an angle in radians to show the
    working mark beside it; kind picks which mark.
    """
    colour = _visible(rgb)
    cx, cy = RING_W / 2.0, PX_H / 2.0

    # The pale margin the ring already sits in, reused as the margin after the
    # mark so both ends of the pill match. Fixing a width for the mark instead
    # left 14pt of empty pill between ring and mark and only 3pt after it,
    # which read as a mark crammed into the right cap.
    margin = cx - (RADIUS + STROKE / 2.0)
    mark_left = cx + RADIUS + STROKE / 2.0 + MARK_GAP
    width = RING_W if phase is None else int(round(mark_left + MARK_W + margin))

    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, width, PX_H, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0
    )
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(context)

    # Background: the state's own colour, pale and translucent, filling the
    # whole item as a rounded pill. A tinted field this size registers in the
    # corner of the eye where a thin ring alone did not.
    _set(colour, 0.28)
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        ((0, 0), (width, PX_H)), cy, cy
    ).fill()

    _set(colour)
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
    _circle(cx, cy, DOT)

    if phase is not None:
        MARKS.get(kind, _mark_dots)(colour, mark_left, cy, phase)

    NSGraphicsContext.restoreGraphicsState()

    image = NSImage.alloc().initWithSize_((width / 2.0, PT_H))
    image.addRepresentation_(rep)
    return image


def frames(rgb, kind, gap=False):
    """A full cycle of one mark, as a list of images."""
    return [
        render(rgb, gap=gap, phase=2.0 * math.pi * i / MARK_FRAMES, kind=kind)
        for i in range(MARK_FRAMES)
    ]


def render_states(states):
    """One image per state, for the states that do not animate."""
    return {
        name: render(spec["color"], gap=spec.get("mode") == "blink")
        for name, spec in states.items()
    }
