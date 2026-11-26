"""Pictures with text for the text-recognition checks – drawn with AppKit and Quartz, so macOS only."""

from __future__ import annotations

from pathlib import Path


def text_png(path: Path, lines: list[str], width: int = 1600, size: int = 56) -> Path:
    """A white PNG with ``lines`` of black text (like a photographed or scanned page)."""
    from AppKit import (  # noqa: PLC0415 - only available on the Mac
        NSBitmapImageRep,
        NSCalibratedRGBColorSpace,
        NSColor,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSGraphicsContext,
        NSMakeRect,
        NSRectFill,
        NSString,
    )

    step = int(size * 1.6)
    height = 120 + len(lines) * step
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(  # noqa: E501
        None, width, height, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0)
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    try:
        NSGraphicsContext.setCurrentContext_(context)
        NSColor.whiteColor().set()
        NSRectFill(NSMakeRect(0, 0, width, height))
        attributes = {NSFontAttributeName: NSFont.systemFontOfSize_(size),
                      NSForegroundColorAttributeName: NSColor.blackColor()}
        y = height - 60 - size
        for line in lines:
            NSString.stringWithString_(line).drawAtPoint_withAttributes_((60, y), attributes)
            y -= step
    finally:
        NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(4, {})  # NSBitmapImageFileTypePNG
    if data is None or not data.writeToFile_atomically_(str(path), True):
        raise RuntimeError(f"could not write {path}")
    return path


def scan_pdf(path: Path, pictures: list[Path], width: float = 595.0) -> Path:
    """A PDF whose pages are only pictures (like a scanner makes them) – no text layer to read."""
    import Quartz  # noqa: PLC0415 - only available on the Mac
    from Foundation import NSURL  # noqa: PLC0415

    context = Quartz.CGPDFContextCreateWithURL(NSURL.fileURLWithPath_(str(path)), None, None)
    if context is None:
        raise RuntimeError(f"could not create {path}")
    for picture in pictures:
        source = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(picture)), None)
        image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None) if source else None
        if image is None:
            raise RuntimeError(f"could not read {picture}")
        height = width * Quartz.CGImageGetHeight(image) / Quartz.CGImageGetWidth(image)
        box = Quartz.CGRectMake(0, 0, width, height)
        Quartz.CGContextBeginPage(context, box)
        Quartz.CGContextDrawImage(context, box, image)
        Quartz.CGContextEndPage(context)
    Quartz.CGPDFContextClose(context)
    return path
