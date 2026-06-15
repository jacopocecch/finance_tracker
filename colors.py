"""Pure color helpers — no DB access. Macro hue → leaf shades.

A macrocategory owns a base hue; its child categories are rendered as
brightness variations of that hue ("macro tinta, foglia sfuma").
"""
from __future__ import annotations


def _clamp(v: float, lo: float = 0.0, hi: float = 255.0) -> float:
    return max(lo, min(hi, v))


def _parse_hex(base_hex: str) -> tuple[int, int, int]:
    h = base_hex.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise ValueError(f"invalid hex color: {base_hex!r}")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def shade(base_hex: str, factor: float) -> str:
    """Return a brightness-shifted shade of base_hex.

    factor in [-1, 1]: <0 darkens toward black, >0 lightens toward white,
    0 returns the base color. Output is "#rrggbb".
    """
    factor = max(-1.0, min(1.0, factor))
    r, g, b = _parse_hex(base_hex)
    if factor >= 0:
        # lighten toward white
        r = r + (255 - r) * factor
        g = g + (255 - g) * factor
        b = b + (255 - b) * factor
    else:
        # darken toward black
        k = 1.0 + factor  # factor negative → k in [0,1)
        r, g, b = r * k, g * k, b * k
    return "#{:02x}{:02x}{:02x}".format(
        int(round(_clamp(r))), int(round(_clamp(g))), int(round(_clamp(b)))
    )


def derive_leaf_colors(base_hex: str, n: int, spread: float = 0.35) -> list[str]:
    """Spread n leaf shades evenly across [-spread, +spread] brightness.

    For n == 1 returns the base hue unchanged. Darkest first, lightest last.
    """
    if n <= 0:
        return []
    if n == 1:
        return [shade(base_hex, 0.0)]
    step = (2 * spread) / (n - 1)
    return [shade(base_hex, -spread + step * i) for i in range(n)]
