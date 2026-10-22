"""Braille dot canvas for the terminal world map.

The map is drawn on a *dot grid*: every terminal character cell provides
2x4 Braille dots (the Unicode U+2800 block).  This module is the low-level
drawing surface - a flat grid of dots, each dot carrying an integer
*style*.

Style semantics
---------------
``0`` means "empty".  Styles are priority numbers in the range
``0..255``; anything outside that range is rejected with a
``ValueError``.  Higher styles are drawn on top of lower ones: a dot
keeps the maximum style ever set on it, so a bright element (a comet head,
style 6) is never dimmed by a dimmer one (land, style 1).  Style ``0`` is
a no-op - a dot can never be erased.  The style of a character cell is the
maximum over its eight dots - that is what the renderer uses to group
cells into coloured runs.

Performance
-----------
The renderer redraws up to twelve times per second on a grid of a few
hundred thousand dots, so :meth:`Canvas.rows` and
:meth:`Canvas.cell_styles` are computed with bulk byte operations
instead of per-dot Python loops, and both results are cached until the
canvas changes again.
"""

__all__ = ["BITS", "Canvas"]

#: Bit of every dot inside a Braille cell: ``BITS[y][x]`` for ``x`` in
#: ``0..1`` (left/right column) and ``y`` in ``0..3`` (top..bottom row).
BITS = (
    (0x01, 0x08),
    (0x02, 0x10),
    (0x04, 0x20),
    (0x40, 0x80),
)

#: Dots per character cell.
_CELL_WIDTH = 2
_CELL_HEIGHT = 4

#: Style of the land background.
_LAND_STYLE = 1

#: Highest style a dot can carry - styles are stored in a single byte.
_MAX_STYLE = 255


def _check_style(style: int) -> int:
    """Return the style, checking that it fits into one byte.

    Raises:
        ValueError: If the style is outside the range ``0..255``.
    """
    if not 0 <= style <= _MAX_STYLE:
        raise ValueError(f"style must be in the range 0..{_MAX_STYLE}, got {style}")
    return style


def _mask_table(dy: int, dx: int) -> bytes:
    """Return the translation table "non-zero dot -> its Braille bit"."""
    bit = BITS[dy][dx]
    return bytes(bit if value else 0 for value in range(256))


# Translation tables that turn a dot row into the Braille bits of its
# cells.  Indexed by [dot row][dot column].
_MASK_TABLES = tuple(
    tuple(_mask_table(dy, dx) for dx in range(_CELL_WIDTH))
    for dy in range(_CELL_HEIGHT)
)

# Translation table: any non-zero dot becomes the land style.
_LAND_TABLE = bytes(_LAND_STYLE if value else 0 for value in range(256))

# Translation table: an empty dot becomes 0xFF, a drawn dot becomes zero.
_ZERO_TABLE = bytes(0xFF if value == 0 else 0x00 for value in range(256))

# Braille character for every possible dot mask.
_BRAILLE = tuple(chr(0x2800 | mask) for mask in range(256))


class Canvas:
    """A grid of Braille dots with a per-dot style.

    Args:
        width: Requested width in dots; rounded up to a multiple of two.
        height: Requested height in dots; rounded up to a multiple of four.

    Raises:
        ValueError: If width or height is smaller than one.
    """

    def __init__(self, width: int, height: int) -> None:
        if width < 1 or height < 1:
            raise ValueError(f"canvas size must be positive, got {width}x{height}")
        self.width = width + (-width % _CELL_WIDTH)
        self.height = height + (-height % _CELL_HEIGHT)
        self._dots = bytearray(self.width * self.height)
        self._cache: tuple[list[str], list[list[int]]] | None = None

    @property
    def cell_width(self) -> int:
        """Number of character cells per row."""
        return self.width // _CELL_WIDTH

    @property
    def cell_height(self) -> int:
        """Number of character rows."""
        return self.height // _CELL_HEIGHT

    def clear(self) -> None:
        """Erase every dot."""
        self._dots[:] = bytes(len(self._dots))
        self._cache = None

    def set(self, x: int, y: int, style: int = 1) -> None:
        """Set the style of one dot.

        The dot keeps the maximum style ever set on it, so a dimmer
        element never overwrites a brighter one.  Style ``0`` is a no-op:
        a dot can never be erased.  Coordinates outside the grid are
        ignored.

        Args:
            x: Dot column.
            y: Dot row.
            style: Style of the dot, in the range ``0..255``.

        Raises:
            ValueError: If the style is outside the range ``0..255``.
        """
        _check_style(style)
        if 0 <= x < self.width and 0 <= y < self.height:
            index = y * self.width + x
            if style > self._dots[index]:
                self._dots[index] = style
                self._cache = None

    def dot_style(self, x: int, y: int) -> int:
        """Return the style of one dot (0 outside the grid)."""
        if 0 <= x < self.width and 0 <= y < self.height:
            return self._dots[y * self.width + x]
        return 0

    def line(self, x0: int, y0: int, x1: int, y1: int, style: int = 1) -> None:
        """Draw a straight line with Bresenham's algorithm.

        Every point of the line gets ``style``; points outside the grid
        are clipped away.  Style ``0`` is a no-op: a dot can never be
        erased.

        Args:
            x0: Column of the first point.
            y0: Row of the first point.
            x1: Column of the last point.
            y1: Row of the last point.
            style: Style of the line, in the range ``0..255``.

        Raises:
            ValueError: If the style is outside the range ``0..255``.
        """
        _check_style(style)
        grid = self._dots
        width = self.width
        height = self.height
        dx = abs(x1 - x0)
        sx = 1 if x0 < x1 else -1
        dy = -abs(y1 - y0)
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        # Bresenham never leaves the bounding box of its endpoints, so a
        # line with both ends inside the canvas needs no clipping.
        inside = (
            0 <= x0 < width
            and 0 <= y0 < height
            and 0 <= x1 < width
            and 0 <= y1 < height
        )
        changed = False
        while True:
            # Inlined Canvas.set: this is the hottest path of the renderer.
            if inside or (0 <= x0 < width and 0 <= y0 < height):
                index = y0 * width + x0
                if style > grid[index]:
                    grid[index] = style
                    changed = True
            if x0 == x1 and y0 == y1:
                break
            err2 = 2 * err
            if err2 >= dy:
                err += dy
                x0 += sx
            if err2 <= dx:
                err += dx
                y0 += sy
        if changed:
            self._cache = None

    def blit_land(self, mask: bytes | bytearray) -> None:
        """Paint land pixels from a flat mask of 0/1 values.

        The mask is row-major and holds one entry per dot, so its length
        must be ``width * height``.  A land pixel gives style 1, but never
        lowers an existing style - land is the background.

        Args:
            mask: Flat sequence of 0/1 land flags, one per dot.  Every
                value must fit into a style byte (``0..255``).

        Raises:
            ValueError: If the mask length does not match the canvas, or
                if a mask value is outside the range ``0..255``.
        """
        size = self.width * self.height
        if len(mask) != size:
            raise ValueError(f"land mask must hold {size} dots, got {len(mask)}")
        try:
            land_mask = bytes(mask)
        except ValueError as exc:
            raise ValueError(
                f"land mask values must be in the range 0..{_MAX_STYLE}"
            ) from exc
        # Only empty dots take the land style: the result is the current
        # grid OR-ed with (land AND empty-dots).
        land = int.from_bytes(land_mask.translate(_LAND_TABLE), "big")
        empty = int.from_bytes(self._dots.translate(_ZERO_TABLE), "big")
        dots = int.from_bytes(self._dots, "big")
        self._dots = bytearray(((land & empty) | dots).to_bytes(size, "big"))
        self._cache = None

    def cell_char(self, cx: int, cy: int) -> str:
        """Return the Braille character of one cell.

        An empty cell is rendered as U+2800, so every cell keeps its
        place in the fixed-width grid.

        Args:
            cx: Cell column.
            cy: Cell row.

        Raises:
            IndexError: If the cell is outside the canvas.
        """
        self._check_cell(cx, cy)
        grid = self._dots
        width = self.width
        x0 = cx * _CELL_WIDTH
        y0 = cy * _CELL_HEIGHT
        mask = 0
        for dy in range(_CELL_HEIGHT):
            base = (y0 + dy) * width + x0
            if grid[base]:
                mask |= BITS[dy][0]
            if grid[base + 1]:
                mask |= BITS[dy][1]
        return _BRAILLE[mask]

    def cell_style(self, cx: int, cy: int) -> int:
        """Return the maximum dot style of one cell (0 when it is empty).

        Args:
            cx: Cell column.
            cy: Cell row.

        Raises:
            IndexError: If the cell is outside the canvas.
        """
        self._check_cell(cx, cy)
        grid = self._dots
        width = self.width
        x0 = cx * _CELL_WIDTH
        y0 = cy * _CELL_HEIGHT
        best = 0
        for dy in range(_CELL_HEIGHT):
            base = (y0 + dy) * width + x0
            for dx in range(_CELL_WIDTH):
                value = grid[base + dx]
                if value > best:
                    best = value
        return best

    def cell_styles(self) -> list[list[int]]:
        """Return the style of every cell, one list per character row.

        The result is a fresh copy of the cached styles, so the caller
        may modify it without corrupting the canvas.
        """
        return [list(row) for row in self._rendered()[1]]

    def rows(self) -> list[str]:
        """Return the canvas as text, one string per character row.

        Every line holds exactly ``cell_width`` Braille characters.  The
        result is a fresh copy of the cached rows, so the caller may
        modify it without corrupting the canvas.
        """
        return list(self._rendered()[0])

    def _check_cell(self, cx: int, cy: int) -> None:
        """Reject cell coordinates outside the canvas."""
        if not (0 <= cx < self.cell_width and 0 <= cy < self.cell_height):
            raise IndexError(
                f"cell {cx},{cy} is outside the {self.cell_width}x"
                f"{self.cell_height} cell grid"
            )

    def _rendered(self) -> tuple[list[str], list[list[int]]]:
        """Return the cached ``(rows, cell styles)`` pair."""
        cache = self._cache
        if cache is None:
            cache = self._compute()
            self._cache = cache
        return cache

    def _compute(self) -> tuple[list[str], list[list[int]]]:
        """Build the text rows and the per-cell styles from the dots."""
        width = self.width
        grid = self._dots
        rows: list[str] = []
        styles: list[list[int]] = []
        for cy in range(self.cell_height):
            base = cy * _CELL_HEIGHT * width
            dot_rows = [
                grid[base + dy * width : base + (dy + 1) * width]
                for dy in range(_CELL_HEIGHT)
            ]
            rows.append("".join([_BRAILLE[mask] for mask in _row_masks(dot_rows)]))
            styles.append(list(_row_styles(dot_rows)))
        return rows, styles


def _row_masks(dot_rows: list[bytes | bytearray]) -> bytes:
    """Return the Braille bit mask of every cell of one character row.

    Args:
        dot_rows: The four dot rows of the character row.

    Returns:
        One byte per cell, holding the eight dot bits of that cell.
    """
    acc = 0
    for dy, row in enumerate(dot_rows):
        left_table, right_table = _MASK_TABLES[dy]
        acc |= int.from_bytes(row[0::2].translate(left_table), "big")
        acc |= int.from_bytes(row[1::2].translate(right_table), "big")
    return acc.to_bytes(len(dot_rows[0]) // _CELL_WIDTH, "big")


def _row_styles(dot_rows: list[bytes | bytearray]) -> bytes:
    """Return the maximum dot style of every cell of one character row.

    Every cell of the row owns eight dots: the left and the right
    column of the four dot rows.  Slicing the two columns apart leaves
    eight byte strings that hold one dot slot per cell, and the maximum
    of those eight values is the style of the cell.

    Args:
        dot_rows: The four dot rows of the character row.

    Returns:
        One byte per cell, holding the maximum style of that cell.
    """
    columns = []
    for row in dot_rows:
        columns.append(row[0::2])
        columns.append(row[1::2])
    return bytes(map(max, *columns))
