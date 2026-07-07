"""Inset each full-bleed iconset PNG by 10% into a transparent canvas, so the
built .icns / Dock icon matches macOS's standard proportions (content ~80%)."""
import os
import sys

from AppKit import (NSBitmapImageRep, NSCalibratedRGBColorSpace,
                    NSGraphicsContext, NSImage)
from Foundation import NSMakeRect

FRAC = 0.10
d = sys.argv[1]
for name in sorted(os.listdir(d)):
    if not name.endswith(".png"):
        continue
    path = os.path.join(d, name)
    src = NSImage.alloc().initWithContentsOfFile_(path)
    rep0 = src.representations()[0]
    w, h = int(rep0.pixelsWide()), int(rep0.pixelsHigh())
    rep = NSBitmapImageRep.alloc().\
        initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, w, h, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0)
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)
    pad = round(w * FRAC)
    src.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(pad, pad, w - 2 * pad, h - 2 * pad),
        NSMakeRect(0, 0, 0, 0), 2, 1.0)          # 2 = SourceOver, whole source
    NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(4, {})   # 4 = PNG
    ok = data.writeToFile_atomically_(path, True)
    print("  %-22s %dx%d pad=%d -> %s" % (name, w, h, pad, "ok" if ok else "FAIL"))
print("done")
