"""Tests for the Braille dot canvas."""

import random

import pytest

from trafficmap.braille import BITS, Canvas


def _reference_rows(canvas: Canvas) -> list[str]:
    """Straightforward per-dot reference implementation of ``rows()``."""
    out = []
    for cy in range(canvas.cell_height):
        chars = []
        for cx in range(canvas.cell_width):
            mask = 0
            for dy in range(4):
                for dx in range(2):
                    if canvas.dot_style(cx * 2 + dx, cy * 4 + dy):
                        mask |= BITS[dy][dx]
            chars.append(chr(0x2800 | mask))
        out.append("".join(chars))
    return out


def _reference_styles(canvas: Canvas) -> list[list[int]]:
    """Straightforward per-dot reference implementation of ``cell_styles()``."""
    out = []
    for cy in range(canvas.cell_height):
        row = []
        for cx in range(canvas.cell_width):
            best = 0
            for dy in range(4):
                for dx in range(2):
                    best = max(best, canvas.dot_style(cx * 2 + dx, cy * 4 + dy))
            row.append(best)
        out.append(row)
    return out


def test_corner_bits():
    c = Canvas(2, 4)
    c.set(0, 0, 1)
    assert chr(0x2801) == c.cell_char(0, 0)
    c.set(1, 3, 1)
    assert chr(0x2881) == c.cell_char(0, 0)


def test_style_priority():
    c = Canvas(2, 4)
    c.set(0, 0, 1)
    c.set(0, 1, 7)
    assert c.cell_style(0, 0) == 7


@pytest.mark.parametrize(
    ("dx", "dy", "bit"),
    [
        (0, 0, 0x01),
        (1, 0, 0x08),
        (0, 1, 0x02),
        (1, 1, 0x10),
        (0, 2, 0x04),
        (1, 2, 0x20),
        (0, 3, 0x40),
        (1, 3, 0x80),
    ],
)
def test_every_dot_maps_to_its_own_bit(dx, dy, bit):
    """Each of the eight dots of a cell contributes exactly one bit."""
    c = Canvas(2, 4)
    c.set(dx, dy, 1)
    assert c.cell_char(0, 0) == chr(0x2800 | bit)
    assert c.cell_style(0, 0) == 1


def test_dimensions_are_rounded_up_to_whole_cells():
    c = Canvas(3, 5)
    assert (c.width, c.height) == (4, 8)
    assert (c.cell_width, c.cell_height) == (2, 2)


def test_dimensions_already_whole_are_kept():
    c = Canvas(8, 12)
    assert (c.width, c.height) == (8, 12)
    assert (c.cell_width, c.cell_height) == (4, 3)


@pytest.mark.parametrize(("width", "height"), [(0, 4), (4, 0), (-2, 4), (4, -1)])
def test_non_positive_size_raises(width, height):
    with pytest.raises(ValueError):
        Canvas(width, height)


def test_out_of_bounds_set_is_ignored():
    c = Canvas(4, 8)
    c.set(-1, 0)
    c.set(0, -1)
    c.set(4, 0)
    c.set(0, 8)
    c.set(999, 999)
    assert c.rows() == [chr(0x2800) * 2] * 2
    assert c.cell_styles() == [[0, 0], [0, 0]]


def test_set_keeps_the_maximum_style():
    c = Canvas(2, 4)
    c.set(0, 0, 5)
    c.set(0, 0, 3)
    assert c.cell_style(0, 0) == 5
    c.set(0, 0, 6)
    assert c.cell_style(0, 0) == 6


def test_set_defaults_to_style_one():
    c = Canvas(2, 4)
    c.set(0, 0)
    assert c.cell_style(0, 0) == 1


def test_line_draws_a_horizontal_run():
    c = Canvas(10, 4)
    c.line(0, 0, 5, 0, 3)
    drawn = [x for x in range(c.width) if c.dot_style(x, 0)]
    assert drawn == [0, 1, 2, 3, 4, 5]
    assert all(c.dot_style(x, 0) == 3 for x in drawn)
    # The run stays inside its own dot row.
    assert all(c.dot_style(x, 1) == 0 for x in range(c.width))


def test_line_is_symmetric():
    forward = Canvas(10, 8)
    backward = Canvas(10, 8)
    forward.line(1, 2, 8, 5, 4)
    backward.line(8, 5, 1, 2, 4)
    assert forward.rows() == backward.rows()
    assert forward.cell_styles() == backward.cell_styles()


def test_line_diagonal_has_the_expected_length():
    c = Canvas(10, 8)
    c.line(0, 0, 5, 5, 2)
    assert (
        sum(1 for y in range(c.height) for x in range(c.width) if c.dot_style(x, y))
        == 6
    )


def test_line_clips_out_of_bounds_endpoints():
    c = Canvas(10, 4)
    c.line(-5, 0, 3, 0, 1)
    drawn = [x for x in range(c.width) if c.dot_style(x, 0)]
    assert drawn == [0, 1, 2, 3]


def test_line_of_a_single_point():
    c = Canvas(4, 4)
    c.line(2, 2, 2, 2, 3)
    assert c.dot_style(2, 2) == 3
    assert (
        sum(1 for y in range(c.height) for x in range(c.width) if c.dot_style(x, y))
        == 1
    )


def test_blit_land_paints_empty_dots():
    c = Canvas(4, 4)
    mask = bytearray(c.width * c.height)
    mask[0] = 1
    mask[5] = 1
    c.blit_land(mask)
    assert c.dot_style(0, 0) == 1
    assert c.dot_style(1, 1) == 1
    assert c.dot_style(2, 0) == 0


def test_blit_land_never_lowers_an_existing_style():
    c = Canvas(4, 4)
    c.set(0, 0, 5)
    mask = bytearray(c.width * c.height)
    mask[0] = 1
    mask[1] = 1
    c.blit_land(mask)
    assert c.dot_style(0, 0) == 5
    assert c.dot_style(1, 0) == 1
    assert c.cell_style(0, 0) == 5


def test_blit_land_accepts_bytes_and_bytearray():
    mask = bytes(4 * 4)
    c = Canvas(4, 4)
    c.blit_land(mask)
    assert c.rows() == [chr(0x2800) * 2]


@pytest.mark.parametrize("length", [0, 1, 15, 17])
def test_blit_land_with_wrong_length_raises(length):
    c = Canvas(4, 4)
    with pytest.raises(ValueError):
        c.blit_land(bytearray(length))


def test_cell_char_of_an_empty_cell():
    c = Canvas(2, 4)
    assert c.cell_char(0, 0) == chr(0x2800)


@pytest.mark.parametrize(("cx", "cy"), [(-1, 0), (0, -1), (1, 0), (0, 1)])
def test_cell_outside_the_canvas_raises(cx, cy):
    c = Canvas(2, 4)
    with pytest.raises(IndexError):
        c.cell_char(cx, cy)
    with pytest.raises(IndexError):
        c.cell_style(cx, cy)


def test_rows_shape():
    c = Canvas(8, 12)
    rows = c.rows()
    assert len(rows) == c.height // 4
    assert all(len(row) == c.width // 2 for row in rows)


def test_cell_styles_shape():
    c = Canvas(8, 12)
    styles = c.cell_styles()
    assert len(styles) == c.height // 4
    assert all(len(row) == c.width // 2 for row in styles)
    assert all(isinstance(value, int) for row in styles for value in row)


def test_rows_and_cell_styles_agree():
    c = Canvas(6, 8)
    c.set(0, 0, 3)
    c.line(4, 5, 6, 6, 6)
    styles = c.cell_styles()
    rows = c.rows()
    for cy, row in enumerate(rows):
        for cx, char in enumerate(row):
            assert styles[cy][cx] == c.cell_style(cx, cy)
            # A cell with style 0 is empty, a cell with dots is not.
            assert (char == chr(0x2800)) == (styles[cy][cx] == 0)


def test_clear_erases_everything():
    c = Canvas(4, 4)
    c.set(0, 0, 3)
    c.clear()
    assert c.rows() == [chr(0x2800) * 2]
    assert c.cell_styles() == [[0, 0]]


def test_cache_follows_the_dots():
    c = Canvas(4, 4)
    assert c.rows() == [chr(0x2800) * 2]
    c.set(0, 0, 2)
    assert c.rows() == [chr(0x2801) + chr(0x2800)]
    assert c.cell_styles() == [[2, 0]]


def test_fast_paths_match_the_reference_implementation():
    """The bulk byte operations must agree with a per-dot implementation."""
    rng = random.Random(20261021)
    c = Canvas(37, 21)
    for _ in range(2000):
        c.set(rng.randrange(c.width), rng.randrange(c.height), rng.randrange(0, 8))
    assert c.rows() == _reference_rows(c)
    assert c.cell_styles() == _reference_styles(c)


def test_fast_paths_match_the_reference_with_land():
    rng = random.Random(7)
    c = Canvas(20, 12)
    mask = bytearray(rng.choice((0, 1)) for _ in range(c.width * c.height))
    c.blit_land(mask)
    for _ in range(200):
        c.set(rng.randrange(c.width), rng.randrange(c.height), rng.randrange(1, 8))
    assert c.rows() == _reference_rows(c)
    assert c.cell_styles() == _reference_styles(c)


def test_large_canvas_is_consistent():
    c = Canvas(400, 240)
    c.line(0, 0, 399, 239, 6)
    mask = bytearray(c.width * c.height)
    for i in range(0, len(mask), 7):
        mask[i] = 1
    c.blit_land(mask)
    styles = c.cell_styles()
    assert len(styles) == 60
    assert all(len(row) == 200 for row in styles)
    # The line starts in the first cell, so its first dot is lit.
    assert ord(c.rows()[0][0]) & BITS[0][0]


def test_high_style_values_are_kept():
    """Styles are priority numbers, not indexes: 200 must survive."""
    c = Canvas(4, 4)
    c.set(0, 0, 200)
    c.set(2, 2, 7)
    assert c.cell_style(0, 0) == 200
    assert c.cell_style(1, 0) == 7
    assert c.cell_styles() == [[200, 7]]


def test_clear_resets_the_styles():
    c = Canvas(4, 4)
    c.set(0, 0, 200)
    c.clear()
    c.set(1, 1, 3)
    assert c.cell_styles() == [[3, 0]]


def test_land_is_drawn_over_an_empty_canvas():
    c = Canvas(4, 4)
    mask = bytearray(c.width * c.height)
    mask[0] = 1
    c.blit_land(mask)
    assert c.rows() == [chr(0x2801) + chr(0x2800)]
    assert c.cell_styles() == [[1, 0]]


def test_land_under_a_bright_dot():
    c = Canvas(4, 4)
    c.set(0, 0, 6)
    mask = bytearray(c.width * c.height)
    mask[0] = 1
    c.blit_land(mask)
    assert c.dot_style(0, 0) == 6
    assert c.cell_style(0, 0) == 6


def _bresenham(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    """The points of a Bresenham line, used by the model."""
    dx = abs(x1 - x0)
    sx = 1 if x0 < x1 else -1
    dy = -abs(y1 - y0)
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    points = []
    while True:
        points.append((x0, y0))
        if x0 == x1 and y0 == y1:
            return points
        err2 = 2 * err
        if err2 >= dy:
            err += dy
            x0 += sx
        if err2 <= dx:
            err += dx
            y0 += sy


def _model_rows(model, width, height):
    """Text rows expected from an independent model of the dots."""
    out = []
    for cy in range(height // 4):
        chars = []
        for cx in range(width // 2):
            mask = 0
            for dy in range(4):
                for dx in range(2):
                    if model.get((cx * 2 + dx, cy * 4 + dy), 0):
                        mask |= BITS[dy][dx]
            chars.append(chr(0x2800 | mask))
        out.append("".join(chars))
    return out


def _model_styles(model, width, height):
    """Cell styles expected from an independent model of the dots."""
    out = []
    for cy in range(height // 4):
        row = []
        for cx in range(width // 2):
            best = 0
            for dy in range(4):
                for dx in range(2):
                    best = max(best, model.get((cx * 2 + dx, cy * 4 + dy), 0))
            row.append(best)
        out.append(row)
    return out


def test_random_operations_match_a_model():
    """set/line/blit_land/clear must keep the canvas and the model equal."""
    rng = random.Random(4242)
    for _ in range(30):
        canvas = Canvas(rng.randrange(1, 20), rng.randrange(1, 20))
        model: dict[tuple[int, int], int] = {}

        def put(x: int, y: int, style: int) -> None:
            if 0 <= x < canvas.width and 0 <= y < canvas.height:
                if style > model.get((x, y), 0):
                    model[(x, y)] = style

        for _ in range(40):
            choice = rng.random()
            if choice < 0.45:
                x = rng.randrange(-3, canvas.width + 3)
                y = rng.randrange(-3, canvas.height + 3)
                style = rng.randrange(0, 9)
                canvas.set(x, y, style)
                put(x, y, style)
            elif choice < 0.75:
                x0 = rng.randrange(-3, canvas.width + 3)
                y0 = rng.randrange(-3, canvas.height + 3)
                x1 = rng.randrange(-3, canvas.width + 3)
                y1 = rng.randrange(-3, canvas.height + 3)
                style = rng.randrange(1, 9)
                canvas.line(x0, y0, x1, y1, style)
                for x, y in _bresenham(x0, y0, x1, y1):
                    put(x, y, style)
            elif choice < 0.92:
                mask = bytearray(
                    rng.choice((0, 1)) for _ in range(canvas.width * canvas.height)
                )
                canvas.blit_land(mask)
                for index, value in enumerate(mask):
                    if value:
                        put(index % canvas.width, index // canvas.width, 1)
            else:
                canvas.clear()
                model.clear()

            assert canvas.rows() == _model_rows(model, canvas.width, canvas.height)
            assert canvas.cell_styles() == _model_styles(
                model, canvas.width, canvas.height
            )
