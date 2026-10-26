# Traffic Map — контурная карта: план

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Рисовать береговую линию в один Braille-dot вместо сплошной заливки суши.

**Architecture:** Новая чистая функция `worldmap.coastline()` превращает заполненную маску суши в маску берега по 4-соседству. `ui._Map` кэширует её по viewport, `_render()` передаёт её в `Canvas.blit_land()`. `land_mask()` остаётся заполненной для геометрии и тестов.

**Tech Stack:** Python 3.14, pytest, Pillow, curses.

**Spec:** `docs/superpowers/specs/2026-10-26-outline-map-design.md`

---

## File Structure

| Файл | Ответственность |
|---|---|
| `src/trafficmap/worldmap.py` | маска суши, растеризация, новая функция `coastline()` |
| `src/trafficmap/ui.py` | кэш контура в `_Map`, вызов в `_render()` |
| `tests/test_worldmap.py` | unit-тесты контура |
| `tests/test_ui_snapshot.py` | проверка, что snapshot остаётся картой, а не заливкой |
| `README.md`, `docs/usage.md` | легенда ASCII: `#` — берег |

---

### Task 1: Функция контура

**Files:**
- Modify: `src/trafficmap/worldmap.py`
- Test: `tests/test_worldmap.py`

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/test_worldmap.py`:

```python
from trafficmap.worldmap import coastline


def test_coastline_of_one_land_dot_is_one_dot():
    mask = bytearray(5 * 5)
    mask[2 * 5 + 2] = 1

    edge = coastline(mask, 5, 5)

    assert sum(edge) == 1
    assert edge[2 * 5 + 2] == 1


def test_coastline_of_all_land_is_empty():
    mask = bytearray([1] * (5 * 5))

    assert sum(coastline(mask, 5, 5)) == 0


def test_coastline_keeps_the_inside_of_a_block():
    mask = bytearray(5 * 5)
    for y in range(1, 4):
        for x in range(1, 4):
            mask[y * 5 + x] = 1

    edge = coastline(mask, 5, 5)

    assert sum(edge) == 8
    assert edge[2 * 5 + 2] == 0


def test_coastline_rejects_a_mask_of_another_size():
    with pytest.raises(ValueError):
        coastline(bytearray(4), 3, 3)
```

- [ ] **Step 2: Запустить тесты и увидеть падение**

Run: `.venv/bin/pytest tests/test_worldmap.py -k coastline -v`
Expected: FAIL — `ImportError: cannot import name 'coastline'`.

- [ ] **Step 3: Реализовать функцию**

Добавить в `src/trafficmap/worldmap.py` после `rasterize()`:

```python
def coastline(mask: bytes | bytearray, width: int, height: int) -> bytearray:
    """Return the dots of the mask that touch water.

    A land dot is a coast dot when at least one of its four neighbours
    is water.  The neighbour outside the grid counts as land: a view
    that cuts through a continent must not draw a frame along the edge
    of the screen.

    Args:
        mask: Flat row-major mask, one entry per dot, non-zero for land.
        width: Width of the mask in dots.
        height: Height of the mask in dots.

    Returns:
        A mask of the same size with ``1`` on the coast and ``0``
        elsewhere.

    Raises:
        ValueError: If the size is not positive or the mask length does
            not match the size.
    """
    if width < 1 or height < 1:
        raise ValueError(f"mask size must be positive, got {width}x{height}")
    if len(mask) != width * height:
        raise ValueError(
            f"mask must hold {width * height} dots, got {len(mask)}"
        )
    edge = bytearray(len(mask))
    for y in range(height):
        row = y * width
        for x in range(width):
            index = row + x
            if not mask[index]:
                continue
            if (
                (x > 0 and not mask[index - 1])
                or (x < width - 1 and not mask[index + 1])
                or (y > 0 and not mask[index - width])
                or (y < height - 1 and not mask[index + width])
            ):
                edge[index] = 1
    return edge
```

Добавить `"coastline"` в `__all__` модуля.

- [ ] **Step 4: Запустить тесты**

Run: `.venv/bin/pytest tests/test_worldmap.py -k coastline -v`
Expected: 4 passed.

- [ ] **Step 5: Закоммитить**

```bash
git add src/trafficmap/worldmap.py tests/test_worldmap.py
git commit -m "feat: coastline mask from the land raster"
```

---

### Task 2: Контур в рендере

**Files:**
- Modify: `src/trafficmap/ui.py`
- Test: `tests/test_ui_snapshot.py`

- [ ] **Step 1: Написать падающий тест на разреженность**

Добавить в `tests/test_ui_snapshot.py`:

```python
def test_snapshot_draws_a_coast_not_a_filled_land(tmp_path):
    text = make_snapshot(tmp_path, "--ascii")
    map_text = "\n".join(text.splitlines()[:-3])
    land_cells = sum(1 for char in map_text if char in "#*+O-")

    assert land_cells > 0, "the map is empty"
    assert land_cells < len(map_text.replace("\n", "")) // 2, (
        "the land is still filled"
    )
```

- [ ] **Step 2: Запустить тест и увидеть падение**

Run: `.venv/bin/pytest tests/test_ui_snapshot.py::test_snapshot_draws_a_coast_not_a_filled_land -v`
Expected: FAIL — заливка занимает больше половины карты.

- [ ] **Step 3: Добавить кэш контура в `_Map`**

В `src/trafficmap/ui.py` импортировать `coastline`:

```python
from trafficmap.worldmap import (
    ...
    coastline,
    ...
)
```

В `_Map.__init__()` добавить:

```python
        self._coast: bytearray | None = None
        self._coast_key: tuple[float, ...] | None = None
```

В `_invalidate()` добавить:

```python
        self._coast = None
        self._coast_key = None
```

После `land_mask()` добавить метод:

```python
    def coastline_mask(self) -> bytearray:
        """Return the coast of the visible part of the world.

        The coast is derived from the filled land mask and cached for
        the same view: it changes only when the view changes, not on
        every frame.

        Returns:
            One byte per dot of the canvas: ``1`` on the coast, ``0``
            elsewhere.
        """
        viewport = self.viewport()
        key = (
            float(self.canvas.width),
            float(self.canvas.height),
            round(viewport.lon_span, 9),
            round(viewport.lat_span, 9),
            round(viewport.center_lat, 6),
            round(viewport.center_lon, 6),
        )
        if self._coast is not None and self._coast_key == key:
            return self._coast
        self._coast = coastline(
            self.land_mask(), self.canvas.width, self.canvas.height
        )
        self._coast_key = key
        return self._coast
```

- [ ] **Step 4: Переключить рендер на контур**

В `_render()` заменить:

```python
    canvas.blit_land(map_.land_mask())
```

на:

```python
    canvas.blit_land(map_.coastline_mask())
```

- [ ] **Step 5: Запустить тест и весь набор**

Run: `.venv/bin/pytest tests/test_ui_snapshot.py -v`
Expected: все тесты проходят, новый тест зелёный.

Run: `.venv/bin/pytest`
Expected: все тесты проходят.

- [ ] **Step 6: Закоммитить**

```bash
git add src/trafficmap/ui.py tests/test_ui_snapshot.py
git commit -m "feat: draw the coast instead of the filled land"
```

---

### Task 3: Документация и проверка кадра

**Files:**
- Modify: `README.md`
- Modify: `docs/usage.md`

- [ ] **Step 1: Обновить легенду README**

В `README.md` заменить строку про ASCII:

```markdown
В ASCII-режиме: `#` — берег, `*` — пир (удалённый адрес) или искра пакета, `+` — голова
```

- [ ] **Step 2: Обновить docs/usage.md**

Заменить `# — суша` на `# — берег` в легенде ASCII.

- [ ] **Step 3: Сделать snapshot и глазами проверить**

Run: `.venv/bin/python -m trafficmap --mock tests/fixtures/mock.jsonl --snapshot /tmp/tmap-outline.txt --frames 30`
Expected: в файле видны контуры материков, а не залитые пятна; статус-бар на месте.

- [ ] **Step 4: Закоммитить**

```bash
git add README.md docs/usage.md
git commit -m "docs: the ascii map draws the coast"
```

---

### Task 4: Финальная проверка

- [ ] **Step 1: Прогнать всё**

Run: `.venv/bin/pytest`
Expected: все тесты проходят.

Run: `.venv/bin/python -m compileall -q src scripts`
Expected: без вывода, код возврата 0.

- [ ] **Step 2: Проверить diff**

Run: `git status -sb && git diff --stat origin/main`
Expected: только ожидаемые файлы, рабочее дерево чистое после коммитов.

- [ ] **Step 3: Запушить**

```bash
git push origin main
```
