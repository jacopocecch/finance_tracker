import pytest
from colors import shade, derive_leaf_colors


class TestShade:
    def test_zero_factor_returns_base(self):
        assert shade("#6B7280", 0.0) == "#6b7280"

    def test_short_hex_expands(self):
        assert shade("#abc", 0.0) == "#aabbcc"

    def test_lighten_moves_toward_white(self):
        assert shade("#000000", 0.5) == "#808080"

    def test_full_lighten_is_white(self):
        assert shade("#123456", 1.0) == "#ffffff"

    def test_darken_moves_toward_black(self):
        assert shade("#ffffff", -0.5) == "#808080"

    def test_full_darken_is_black(self):
        assert shade("#abcdef", -1.0) == "#000000"

    def test_factor_clamped(self):
        assert shade("#102030", 5.0) == "#ffffff"
        assert shade("#102030", -5.0) == "#000000"

    def test_invalid_hex_raises(self):
        with pytest.raises(ValueError):
            shade("nothex", 0.0)


class TestDeriveLeafColors:
    def test_zero_returns_empty(self):
        assert derive_leaf_colors("#6B7280", 0) == []

    def test_single_returns_base(self):
        assert derive_leaf_colors("#6B7280", 1) == ["#6b7280"]

    def test_count_matches(self):
        assert len(derive_leaf_colors("#6B7280", 5)) == 5

    def test_ordered_dark_to_light(self):
        out = derive_leaf_colors("#6B7280", 4)
        assert out == sorted(out)  # darkest (smaller hex) first

    def test_distinct_shades(self):
        out = derive_leaf_colors("#6B7280", 5)
        assert len(set(out)) == 5
