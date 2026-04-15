"""
thumbnail_gen.py — Generates brand-consistent thumbnails for each title variant.

Produces one 1280x720 PNG per title candidate using:
- Pillar-specific gradient background
- Left accent bar in pillar color for visual contrast
- Number hero element when title starts with a digit (e.g. "5 Mistakes…")
- Large white headline text (auto-sized to fit)
- Bottom brand bar with channel name

Output: workspace/thumbnails/thumbnail_{n}.png  (one per title variant, n=0-indexed)

Usage:
    from pipeline.thumbnail_gen import generate_thumbnails
    paths = generate_thumbnails(title_variants, pillar)
"""
import logging
import os
import re
from functools import lru_cache
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

# Bright accent colors per pillar — used for left bar, bottom brand bar, and number hero.
# Saturated and high-contrast against the dark gradient backgrounds above.
PILLAR_ACCENTS = {
    "investing":     (80, 160, 255),   # vivid blue
    "budgeting":     (40, 210, 190),   # teal
    "debt":          (255, 85, 60),    # red-orange
    "tax":           (170, 90, 255),   # purple
    "career_income": (60, 215, 105),   # green
}
DEFAULT_ACCENT = (130, 150, 230)

BRAND_NAME = os.environ.get("CHANNEL_BRAND_NAME", "ClearWealth")

# Height of the bottom brand bar in pixels
_BRAND_BAR_H = 52
# Width of the left accent stripe
_ACCENT_BAR_W = 8


def _make_gradient_image(top_rgb: tuple, bottom_rgb: tuple):
    from PIL import Image
    grad = Image.new("RGB", (1, 2))
    grad.putpixel((0, 0), top_rgb)
    grad.putpixel((0, 1), bottom_rgb)
    return grad.resize((THUMB_W, THUMB_H), Image.Resampling.BILINEAR)


@lru_cache(maxsize=32)
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


def _extract_hero_number(title: str) -> str:
    """
    Return a leading number token from the title for use as a hero element.
    Matches patterns like "5 Mistakes…", "$400/month…", "47% of people…".
    Returns empty string when the title doesn't start with a recognisable number.
    """
    m = re.match(r"^\s*(\$?\d[\d,]*(?:\.\d+)?%?)", title)
    return m.group(1) if m else ""


def _render_thumbnail(title: str, pillar: str, output_path: Path) -> Path:
    from PIL import Image, ImageDraw

    top, bottom = PILLAR_GRADIENTS.get(pillar, DEFAULT_GRADIENT)
    accent = PILLAR_ACCENTS.get(pillar, DEFAULT_ACCENT)

    img = _make_gradient_image(top, bottom)

    # ── Overlay layer (dark content bar + left accent stripe + bottom brand bar) ──
    overlay = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    ov_draw = ImageDraw.Draw(overlay)

    # Left accent stripe — instant visual anchor for the eye
    ov_draw.rectangle(
        [(0, 0), (_ACCENT_BAR_W, THUMB_H)],
        fill=(*accent, 240),
    )

    # Dark content bar for text legibility
    bar_y0 = int(THUMB_H * 0.12)
    bar_y1 = THUMB_H - _BRAND_BAR_H - 6
    ov_draw.rectangle(
        [(_ACCENT_BAR_W, bar_y0), (THUMB_W, bar_y1)],
        fill=(0, 0, 0, 130),
    )

    # Bottom brand bar — solid accent color, full width
    ov_draw.rectangle(
        [(0, THUMB_H - _BRAND_BAR_H), (THUMB_W, THUMB_H)],
        fill=(*accent, 230),
    )

    img = img.convert("RGBA")
    img.alpha_composite(overlay)
    img = img.convert("RGB")
    draw = ImageDraw.Draw(img)

    # ── Number hero element ──────────────────────────────────────────────────
    hero_num = _extract_hero_number(title)
    hero_rendered = False
    if hero_num:
        # Render the leading number large and centered for immediate visual impact.
        hero_font_size = 140
        hero_font = _get_font(hero_font_size)
        hbbox = draw.textbbox((0, 0), hero_num, font=hero_font)
        hw = hbbox[2] - hbbox[0]
        hh = hbbox[3] - hbbox[1]
        hx = (_ACCENT_BAR_W + (THUMB_W - _ACCENT_BAR_W - hw) // 2)
        hy = bar_y0 + 10
        # Shadow
        draw.text((hx + 4, hy + 4), hero_num, font=hero_font, fill=(0, 0, 0, 180))
        draw.text((hx, hy), hero_num, font=hero_font, fill=accent)
        hero_rendered = True
        hero_bottom = hy + hh + 10
        # Title text renders below the number, full width
        title_y0 = hero_bottom
        title_text = re.sub(r"^\s*\$?\d[\d,]*(?:\.\d+)?%?\s*", "", title).strip()
    else:
        title_y0 = bar_y0
        title_text = title

    # ── Headline text ────────────────────────────────────────────────────────
    text_area_h = bar_y1 - title_y0 - 20
    max_w = int(THUMB_W * 0.82)
    font_size = 96 if not hero_rendered else 72
    while font_size > 28:
        font = _get_font(font_size)
        lines = _wrap_text(title_text, font, draw, max_w)
        total_h = sum(draw.textbbox((0, 0), ln, font=font)[3] for ln in lines) + 12 * max(0, len(lines) - 1)
        if total_h <= text_area_h and len(lines) <= 3:
            break
        font_size -= 6

    font = _get_font(font_size)
    lines = _wrap_text(title_text, font, draw, max_w)
    total_h = sum(draw.textbbox((0, 0), ln, font=font)[3] for ln in lines) + 12 * max(0, len(lines) - 1)
    # Centre vertically in remaining space below hero (or full bar)
    y = title_y0 + max(8, (text_area_h - total_h) // 2)

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        lw = bbox[2] - bbox[0]
        x = (_ACCENT_BAR_W + (THUMB_W - _ACCENT_BAR_W - lw) // 2)
        # Drop shadow
        draw.text((x + 3, y + 3), line, font=font, fill=(0, 0, 0, 200))
        draw.text((x, y), line, font=font, fill=(255, 255, 255))
        y += (bbox[3] - bbox[1]) + 12

    # ── Brand bar label ──────────────────────────────────────────────────────
    wm_font = _get_font(30)
    wm_bbox = draw.textbbox((0, 0), BRAND_NAME, font=wm_font)
    wm_w = wm_bbox[2] - wm_bbox[0]
    wm_h = wm_bbox[3] - wm_bbox[1]
    wm_x = THUMB_W - wm_w - 24
    wm_y = THUMB_H - _BRAND_BAR_H + (_BRAND_BAR_H - wm_h) // 2
    draw.text((wm_x, wm_y), BRAND_NAME, font=wm_font, fill=(255, 255, 255))

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
            continue

    logger.info("Generated %d/%d thumbnails", len(paths), len(title_variants))
    return paths
