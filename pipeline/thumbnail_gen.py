"""
thumbnail_gen.py — Generates brand-consistent thumbnails for each title variant.

Produces one 1280x720 PNG per title candidate using:
- Pillar-specific gradient background
- Large white headline text (auto-sized to fit)
- Small "ClearWealth" brand watermark in corner

Output: workspace/thumbnails/thumbnail_{n}.png  (one per title variant, n=0-indexed)

Usage:
    from pipeline.thumbnail_gen import generate_thumbnails
    paths = generate_thumbnails(title_variants, pillar)
"""
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

THUMB_W = 1280
THUMB_H = 720
OUTPUT_DIR = Path("workspace/thumbnails")

# Pillar → (top_color, bottom_color) brand gradients matching Shorts palette
PILLAR_GRADIENTS = {
    "investing":     ((15, 15, 45),   (30, 60, 120)),
    "budgeting":     ((10, 30, 40),   (20, 80, 100)),
    "debt":          ((45, 10, 10),   (100, 25, 25)),
    "tax":           ((25, 10, 45),   (60, 25, 110)),
    "career_income": ((10, 30, 15),   (25, 80, 40)),
}
DEFAULT_GRADIENT = ((15, 15, 25), (40, 40, 80))

BRAND_NAME = os.environ.get("CHANNEL_BRAND_NAME", "ClearWealth")


def _make_gradient_image(top_rgb: tuple, bottom_rgb: tuple):
    from PIL import Image
    img = Image.new("RGB", (THUMB_W, THUMB_H))
    pixels = img.load()
    for y in range(THUMB_H):
        t = y / max(THUMB_H - 1, 1)
        r = int(top_rgb[0] + (bottom_rgb[0] - top_rgb[0]) * t)
        g = int(top_rgb[1] + (bottom_rgb[1] - top_rgb[1]) * t)
        b = int(top_rgb[2] + (bottom_rgb[2] - top_rgb[2]) * t)
        for x in range(THUMB_W):
            pixels[x, y] = (r, g, b)
    return img


def _get_font(size: int):
    from PIL import ImageFont
    _asset_font = Path(__file__).parent / "assets" / "brand_font.ttf"
    candidates = [
        str(_asset_font),
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Arial Unicode.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    return ImageFont.load_default(size=size)


def _wrap_text(text: str, font, draw, max_width: int) -> list[str]:
    """Wrap text to fit within max_width pixels."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        test = (current + " " + word).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _render_thumbnail(title: str, pillar: str, output_path: Path) -> Path:
    from PIL import Image, ImageDraw

    top, bottom = PILLAR_GRADIENTS.get(pillar, DEFAULT_GRADIENT)
    img = _make_gradient_image(top, bottom)
    draw = ImageDraw.Draw(img)

    # Draw semi-transparent overlay bar for text legibility
    bar_y0, bar_y1 = int(THUMB_H * 0.15), int(THUMB_H * 0.80)
    overlay = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    ov_draw = ImageDraw.Draw(overlay)
    ov_draw.rectangle([(60, bar_y0), (THUMB_W - 60, bar_y1)], fill=(0, 0, 0, 120))
    img = img.convert("RGBA")
    img.alpha_composite(overlay)
    img = img.convert("RGB")
    draw = ImageDraw.Draw(img)

    # Auto-size headline to fill ~85% of the bar width
    max_w = int(THUMB_W * 0.82)
    font_size = 100
    while font_size > 32:
        font = _get_font(font_size)
        lines = _wrap_text(title, font, draw, max_w)
        total_h = sum(draw.textbbox((0, 0), ln, font=font)[3] for ln in lines) + 14 * max(0, len(lines) - 1)
        if total_h <= (bar_y1 - bar_y0 - 40) and len(lines) <= 3:
            break
        font_size -= 6

    font = _get_font(font_size)
    lines = _wrap_text(title, font, draw, max_w)
    total_h = sum(draw.textbbox((0, 0), ln, font=font)[3] for ln in lines) + 14 * max(0, len(lines) - 1)
    y = bar_y0 + ((bar_y1 - bar_y0 - total_h) // 2)

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        lw = bbox[2] - bbox[0]
        x = (THUMB_W - lw) // 2
        # Drop shadow
        draw.text((x + 3, y + 3), line, font=font, fill=(0, 0, 0, 180))
        draw.text((x, y), line, font=font, fill=(255, 255, 255))
        y += (bbox[3] - bbox[1]) + 14

    # Brand watermark bottom-right
    wm_font = _get_font(32)
    draw.text((THUMB_W - 220, THUMB_H - 50), BRAND_NAME, font=wm_font, fill=(180, 200, 255))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(output_path), "PNG")
    return output_path


def generate_thumbnails(
    title_variants: list[str],
    pillar: str,
    output_dir: Path = OUTPUT_DIR,
) -> list[Path]:
    """
    Render one thumbnail PNG per title variant.
    Returns list of output paths (same order as title_variants).
    """
    if not title_variants:
        logger.warning("No title variants provided — skipping thumbnail generation")
        return []

    paths: list[Path] = []
    for i, title in enumerate(title_variants):
        out = output_dir / f"thumbnail_{i:02d}.png"
        try:
            _render_thumbnail(title, pillar, out)
            logger.info("Thumbnail %d rendered: %s ('%s')", i, out.name, title[:50])
            paths.append(out)
        except Exception as exc:
            logger.error("Thumbnail %d failed for '%s': %s", i, title[:50], exc)
            raise RuntimeError(
                f"Thumbnail generation failed for variant {i} ('{title[:50]}'): {exc}"
            ) from exc

    logger.info("Generated %d/%d thumbnails", len(paths), len(title_variants))
    return paths
