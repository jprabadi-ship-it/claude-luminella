"""Menu bar icons: the glow ring, drawn in the colour of the current state.

The emoji this replaces were out of place in a macOS menu bar, and worse,
ambiguous -- error and recording were both a red circle, notification and
transcribing both a purple one. A ring shape shared with the app icon reads as
this app whatever colour it happens to be, and the shape itself now carries
what the emoji could not: a solid ring for settled states, a broken one for
states that want attention.

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
)

PX_H = 36        # rendered at 2x for retina
PT_H = 18        # displayed height in points
RING_W = 72      # width of the ring half on its own
RADIUS = 12.0
STROKE = 3.6
DOT = 3.4

# Working mark: three dots whose sizes travel as a wave, after the resting
# indicator in Claude's own design language. Drawn here rather than bundled as
# sprite sheets -- the shape is the reference, the colour stays the state's own
# so the ring and the mark read as one object.
MARK_DOTS = 3
MARK_SPACING = 13.0
MARK_R_MIN = 2.2
MARK_R_MAX = 4.6
MARK_FRAMES = 16
# Space between the ring's outer edge and the first dot. Deliberately smaller
# than the pale margin at either end of the pill, so the ring and the dots
# group together rather than reading as two things at opposite ends.
MARK_GAP = 11.0


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


def _draw_mark(x0, cy, phase):
    """Three dots, sizes travelling as a wave from left to right."""
    for i in range(MARK_DOTS):
        angle = phase - i * (2.0 * math.pi / MARK_DOTS)
        swing = (math.sin(angle) + 1.0) / 2.0          # 0..1
        r = MARK_R_MIN + (MARK_R_MAX - MARK_R_MIN) * swing
        cx = x0 + i * MARK_SPACING
        NSBezierPath.bezierPathWithOvalInRect_(
            ((cx - r, cy - r), (r * 2, r * 2))
        ).fill()


def render(rgb, gap=True, phase=None):
    """Draw one status item image. Returns an NSImage sized in points.

    phase is None for the ring alone, or an angle in radians to show the
    working mark beside it.
    """
    # The pale margin the ring already sits in, reused as the margin after the
    # last dot so both ends of the pill match. Fixing a width for the mark
    # instead left 14pt of empty pill between ring and dots and only 3pt after
    # them, which read as dots crammed into the right cap.
    margin = RING_W / 2.0 - (RADIUS + STROKE / 2.0)
    mark_x0 = RING_W / 2.0 + RADIUS + STROKE / 2.0 + MARK_GAP + MARK_R_MAX
    if phase is None:
        width = RING_W
    else:
        last = mark_x0 + (MARK_DOTS - 1) * MARK_SPACING + MARK_R_MAX
        width = int(round(last + margin))
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, width, PX_H, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0
    )
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(context)

    r, g, b = _visible(rgb)
    cx, cy = RING_W / 2.0, PX_H / 2.0

    # Background: the state's own colour, pale and translucent, filling the
    # whole item as a rounded pill. A tinted field this size registers in the
    # corner of the eye where a thin ring alone did not.
    NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 0.28).set()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        ((0, 0), (width, PX_H)), cy, cy
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

    NSBezierPath.bezierPathWithOvalInRect_(
        ((cx - DOT, cy - DOT), (DOT * 2, DOT * 2))
    ).fill()

    if phase is not None:
        _draw_mark(mark_x0, cy, phase)

    NSGraphicsContext.restoreGraphicsState()

    image = NSImage.alloc().initWithSize_((width / 2.0, PT_H))
    image.addRepresentation_(rep)
    return image


def render_states(states, marked=()):
    """Build every state's image. Returns {state: NSImage | [NSImage, ...]}.

    Blinking states get the broken ring and steady ones the closed ring, so
    the shape says "waiting on you" even before the colour registers. States
    named in `marked` get a list of frames instead of a single image, for the
    working mark to animate through.
    """
    images = {}
    for name, spec in states.items():
        gap = spec.get("mode") == "blink"
        if name in marked:
            images[name] = [
                render(spec["color"], gap=gap,
                       phase=2.0 * math.pi * i / MARK_FRAMES)
                for i in range(MARK_FRAMES)
            ]
        else:
            images[name] = render(spec["color"], gap=gap)
    return images
