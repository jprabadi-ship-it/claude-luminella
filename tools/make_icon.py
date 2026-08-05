#!/usr/bin/env python3
"""Render the app icon: the glow ring, drawn with Quartz.

Produces build/Luminella.iconset and build/Luminella.icns.
"""

import math
import os
import subprocess
import sys

import Quartz
from Quartz import CGColorSpaceCreateDeviceRGB, CGBitmapContextCreate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICONSET = os.path.join(ROOT, "build", "Luminella.iconset")
ICNS = os.path.join(ROOT, "build", "Luminella.icns")

SIZES = [16, 32, 64, 128, 256, 512, 1024]


def draw(size):
    cs = CGColorSpaceCreateDeviceRGB()
    ctx = CGBitmapContextCreate(
        None, size, size, 8, size * 4, cs, Quartz.kCGImageAlphaPremultipliedLast
    )
    s = size / 1024.0

    # Rounded dark body, matching macOS icon geometry.
    inset = 100 * s
    rect = Quartz.CGRectMake(inset, inset, size - 2 * inset, size - 2 * inset)
    path = Quartz.CGPathCreateWithRoundedRect(rect, 185 * s, 185 * s, None)
    Quartz.CGContextAddPath(ctx, path)
    Quartz.CGContextSetRGBFillColor(ctx, 0.07, 0.08, 0.11, 1.0)
    Quartz.CGContextFillPath(ctx)

    cx = cy = size / 2.0

    # The glow ring: a cyan-to-amber sweep, drawn as short arc segments so the
    # gradient follows the circle rather than the frame.
    radius = 250 * s
    width = 78 * s
    steps = 180
    Quartz.CGContextSetLineWidth(ctx, width)
    Quartz.CGContextSetLineCap(ctx, Quartz.kCGLineCapRound)
    for i in range(steps):
        t = i / float(steps - 1)
        # cyan (0,160,200) -> amber (255,140,0)
        r = (0 + (255 - 0) * t) / 255.0
        g = (160 + (140 - 160) * t) / 255.0
        b = (200 + (0 - 200) * t) / 255.0
        a0 = math.radians(130 + 300 * (i / float(steps)))
        a1 = math.radians(130 + 300 * ((i + 1.4) / float(steps)))
        Quartz.CGContextSetRGBStrokeColor(ctx, r, g, b, 1.0)
        Quartz.CGContextBeginPath(ctx)
        Quartz.CGContextAddArc(ctx, cx, cy, radius, a0, a1, 0)
        Quartz.CGContextStrokePath(ctx)

    # Centre dot: the stick.
    Quartz.CGContextSetRGBFillColor(ctx, 0.85, 0.88, 0.93, 1.0)
    Quartz.CGContextFillEllipseInRect(
        ctx, Quartz.CGRectMake(cx - 78 * s, cy - 78 * s, 156 * s, 156 * s)
    )

    return Quartz.CGBitmapContextCreateImage(ctx)


def write_png(image, path):
    url = Quartz.CFURLCreateWithFileSystemPath(None, path, Quartz.kCFURLPOSIXPathStyle, False)
    dest = Quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
    Quartz.CGImageDestinationAddImage(dest, image, None)
    Quartz.CGImageDestinationFinalize(dest)


def main():
    os.makedirs(ICONSET, exist_ok=True)
    cache = {}
    for size in SIZES:
        cache[size] = draw(size)

    # iconset naming: icon_<pt>x<pt>[@2x].png
    for pt in (16, 32, 128, 256, 512):
        write_png(cache[pt], os.path.join(ICONSET, f"icon_{pt}x{pt}.png"))
        write_png(cache[pt * 2], os.path.join(ICONSET, f"icon_{pt}x{pt}@2x.png"))

    subprocess.run(["iconutil", "-c", "icns", ICONSET, "-o", ICNS], check=True)
    print("wrote", ICNS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
