"""Prepare a reusable invoice background from the clinic's filled reference PNG.

The clinic stationery, watermark, grid and footer are retained pixel-for-pixel.
Only example patient/amount text and its QR are cleared for runtime values.
"""

from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageDraw


def build_template(source: Path, destination: Path) -> None:
    image = Image.open(source).convert("RGB")
    original_pixels = np.asarray(image).copy()
    pixels = np.asarray(image).copy()
    width, height = image.size
    sx, sy = width / 675, height / 977

    def box(x1: int, y1: int, x2: int, y2: int) -> tuple[int, int, int, int]:
        return (round(x1 * sx), round(y1 * sy), round(x2 * sx), round(y2 * sy))

    def clear_field(rect: tuple[int, int, int, int]) -> None:
        """Erase sample values before drawing live passage data in their place."""
        x1, y1, x2, y2 = rect
        ImageDraw.Draw(image).rectangle((x1, y1, x2 - 1, y2 - 1), fill="white")

    # Header data sits on a plain white area in the supplied reference.
    for rect in (
        (96, 220, 220, 237),    # passage number
        (96, 238, 188, 257),    # invoice number
        (59, 257, 139, 276),    # invoice date
        (77, 277, 178, 296),    # patient name
    ):
        clear_field(box(*rect))

    clear_field(box(25, 364, 106, 387))       # prestation name
    clear_field(box(517, 364, 601, 387))      # prestation amount
    # The reference contains an example amount after the printed sentence.
    # Clear the full writable line so no characters from that sample survive.
    clear_field(box(303, 674, 635, 695))      # amount in words

    # Clear the changing amounts while keeping each black grid line intact.
    for y1, y2 in ((553, 569), (573, 589), (593, 609), (613, 628), (632, 648)):
        clear_field(box(516, y1, 622, y2))

    # Restore the exact pink arc behind price fields from the source image.
    # The sample amount text interrupts the arc, so extend each row's surviving
    # pink span across the cleared text instead of approximating a circle.
    pixels = np.asarray(image).copy()
    pink_mask = (original_pixels[:, :, 0] > 235) & (original_pixels[:, :, 1] > 165) & (original_pixels[:, :, 1] < 215) & (original_pixels[:, :, 2] > 170) & (original_pixels[:, :, 2] < 220)
    for rect in ((517, 364, 601, 387), (516, 553, 622, 569), (516, 573, 622, 589), (516, 593, 622, 609), (516, 613, 622, 628), (516, 632, 622, 648)):
        x1, y1, x2, y2 = box(*rect)
        for y in range(y1, y2):
            pink_x = np.flatnonzero(pink_mask[y])
            if pink_x.size:
                left, right = max(x1, int(pink_x.min())), min(x2, int(pink_x.max()) + 1)
                if left < right:
                    pixels[y, left:right] = (250, 190, 194)

    # Restore the gray circular watermark beneath the amount in words.
    x1, y1, x2, y2 = box(303, 674, 635, 695)
    gray_mask = (original_pixels[:, :, 0] > 165) & (original_pixels[:, :, 0] < 210) & (np.abs(original_pixels[:, :, 0].astype(int) - original_pixels[:, :, 1].astype(int)) < 10) & (np.abs(original_pixels[:, :, 1].astype(int) - original_pixels[:, :, 2].astype(int)) < 10)
    for y in range(y1, y2):
        gray_x = np.flatnonzero(gray_mask[y])
        if gray_x.size:
            left, right = max(x1, int(gray_x.min())), min(x2, int(gray_x.max()) + 1)
            if left < right:
                pixels[y, left:right] = (184, 189, 195)

    # The QR code is entirely dynamic and sits on a plain white area.
    x1, y1, x2, y2 = box(522, 216, 604, 296)
    pixels[y1:y2, x1:x2] = 255

    destination.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(pixels, "RGB").save(destination, format="PNG", optimize=True)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: build_invoice_template.py reference.png output.png")
    build_template(Path(sys.argv[1]), Path(sys.argv[2]))
