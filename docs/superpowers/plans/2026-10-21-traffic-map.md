# Traffic Map (TUI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Показать живой трафик машины на Braille-карте мира в терминале: кометы по great circle, пиры, скорости, процессы.

**Architecture:** Отдельный поток опрашивает `ss -t -u -a -i -n -p` раз в секунду, переводит сокеты в дельты байт, обогащает GeoIP из DB-IP Lite и отдаёт события через очередь. UI-поток на curses рисует карту (Natural Earth → Pillow → Braille), анимирует кометы и статус-бар. Коллектор, гео и рендер не знают друг о друге и тестируются по отдельности.

**Tech Stack:** Python 3.14, curses (stdlib), Pillow, maxminddb, pytest.

**Spec:** `docs/superpowers/specs/2026-10-21-traffic-map-design.md`

---

## File Structure

| Файл | Ответственность |
|---|---|
| `pyproject.toml` | пакет `trafficmap`, entry point `tmap`, зависимости |
| `Makefile` | venv, assets, run, test, snapshot |
| `scripts/fetch_assets.py` | DB-IP Lite City/ASN + Natural Earth 110m |
| `src/trafficmap/config.py` | Config, TOML/env/CLI |
| `src/trafficmap/projection.py` | Viewport, equirect, great circle |
| `src/trafficmap/braille.py` | Canvas, биты, стили |
| `src/trafficmap/worldmap.py` | GeoJSON → маска суши + кэш |
| `src/trafficmap/geo.py` | Geo, GeoResolver, anchor, приватные сети |
| `src/trafficmap/collector_ss.py` | парсер `ss`, запуск опроса |
| `src/trafficmap/collector_mock.py` | JSONL-реплей |
| `src/trafficmap/flows.py` | FlowTable, дельты, события, Stats |
| `src/trafficmap/anim.py` | Comet / Peer |
| `src/trafficmap/ui.py` | curses, раскладка, клавиши, snapshot |
| `src/trafficmap/__main__.py` | сборка зависимостей, запуск |
| `tests/` | unit + snapshot |

---

### Task 0: Скелет проекта

**Files:**
- Create: `pyproject.toml`, `Makefile`, `.gitignore`, `src/trafficmap/__init__.py`, `src/trafficmap/__main__.py`, `tests/test_version.py`

- [ ] **Step 1: Создать `pyproject.toml`**

```toml
[project]
name = "trafficmap"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["pillow>=10.0", "maxminddb>=2.5"]

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
tmap = "trafficmap.__main__:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

- [ ] **Step 2: Создать `Makefile`** (табы обязательны)

```make
PY := .venv/bin/python
PIP := .venv/bin/pip

venv:
	python3 -m venv .venv
	$(PIP) install -U pip
	$(PIP) install -e '.[dev]'

assets:
	$(PY) scripts/fetch_assets.py

test:
	.venv/bin/pytest

run:
	$(PY) -m trafficmap

snapshot:
	$(PY) -m trafficmap --mock tests/fixtures/mock.jsonl --snapshot /tmp/tmap.txt --frames 30

.PHONY: venv assets test run snapshot
```

- [ ] **Step 3: Создать `.gitignore`**

```
.venv/
__pycache__/
*.egg-info/
assets/*.mmdb
assets/land.geojson
.pytest_cache/
```

- [ ] **Step 4: Минимальный `__init__.py` и `__main__.py`**

```python
# src/trafficmap/__init__.py
__version__ = "0.1.0"
```

```python
# src/trafficmap/__main__.py
import sys
from trafficmap import __version__

def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if "--version" in argv:
        print(f"tmap {__version__}")
        return 0
    print("not implemented", file=sys.stderr)
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Тест версии**

```python
# tests/test_version.py
from trafficmap import __version__
from trafficmap.__main__ import main

def test_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"tmap {__version__}"
```

- [ ] **Step 6: Установить, прогнать, закоммитить**

```bash
cd ~/Projects/traffic-map
git config user.name "deb" && git config user.email "deb@localhost"   # локально, если не задан
make venv && .venv/bin/pytest && .venv/bin/tmap --version
git add -A && git commit -m "chore: project skeleton"
```

Ожидается: `1 passed`, `tmap 0.1.0`.

---

### Task 1: Конфиг

**Files:** Create `src/trafficmap/config.py`, `tests/test_config.py`

- [ ] **Step 1: Тест приоритетов**

```python
def test_cli_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TMAP_FPS", "20")
    cfg = load_config(["--fps", "30", "--city-db", str(tmp_path / "c.mmdb")])
    assert cfg.fps == 30

def test_unknown_toml_key_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('fps = 12\nnope = 1\n')
    with pytest.raises(ValueError, match="nope"):
        load_config([], path=p)
```

- [ ] **Step 2: Реализовать**

```python
DEFAULT_CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "trafficmap"
ASSETS = Path(__file__).resolve().parents[2] / "assets"

@dataclass(frozen=True, slots=True)
class Config:
    fps: int = 12
    poll_ms: int = 1000
    city_db: Path = ASSETS / "dbip-city-lite.mmdb"
    asn_db: Path | None = ASSETS / "dbip-asn-lite.mmdb"
    land_geojson: Path = ASSETS / "land.geojson"
    cache_dir: Path = DEFAULT_CACHE
    ascii: bool = False
    no_anchor: bool = False
    anchor: tuple[float, float] | None = None
    filter_process: str = ""
    mock: Path | None = None
    snapshot: Path | None = None
    frames: int = 0
    dump: Path | None = None
```

`load_config(argv, path=None)`: читает TOML (`tomllib`), проверяет набор ключей по
`dataclasses.fields`, накладывает env `TMAP_<FIELD>`, затем argparse-оверрайды.
`--anchor "55.75,37.61"` парсится в tuple. `fps` и `poll_ms` — положительные.

- [ ] **Step 3: Прогнать**

```bash
.venv/bin/pytest tests/test_config.py -v
```

- [ ] **Step 4: Коммит** `feat: configuration loading`

---

### Task 2: Проекция и great circle

**Files:** Create `src/trafficmap/projection.py`, `tests/test_projection.py`

- [ ] **Step 1: Тесты**

```python
def test_center_maps_to_center():
    vp = Viewport(width=360, height=180)
    assert project(0, 0, vp) == (180, 90)

def test_outside_viewport_is_none():
    vp = Viewport(width=360, height=180, lon_span=90, lat_span=45)
    assert project(0, 120, vp) is None

def test_great_circle_endpoints():
    pts = great_circle((55.75, 37.61), (40.71, -74.0), 32)
    assert len(pts) == 32
    assert abs(pts[0][0] - 55.75) < 1e-6 and abs(pts[-1][1] + 74.0) < 1e-6
    mid = pts[16]
    assert 55 < mid[0] < 70 and -40 < mid[1] < -20   # над Атлантикой

def test_antipodes_do_not_crash():
    pts = great_circle((0, 0), (0, 180), 10)
    assert len(pts) == 10 and all(-90 <= la <= 90 for la, _ in pts)
```

Конвенция индексов: `x = int((dlon + lon_span/2) / lon_span * (width - 1) + 0.5)`,
`y = int((lat_span/2 - (lat - center_lat)) / lat_span * (height - 1) + 0.5)`,
с клампом в `[0, width-1]` / `[0, height-1]`. `dlon` нормализуется в `[-180, 180]`.

- [ ] **Step 2: Реализовать** по формулам спеки (`project` — floor-индексы, клиппинг;
  `great_circle` — slerp, антиподы через ортогональную ось). `Viewport` получает
  `zoom` не нужно: центрирование и `lon_span/lat_span` достаточно.

- [ ] **Step 3: Прогнать и закоммитить**

```bash
.venv/bin/pytest tests/test_projection.py -v
git add src/trafficmap/projection.py tests/test_projection.py
git commit -m "feat: equirectangular projection and great circles"
```

---

### Task 3: Braille-канвас

**Files:** Create `src/trafficmap/braille.py`, `tests/test_braille.py`

- [ ] **Step 1: Тесты точных битов**

```python
def test_corner_bits():
    c = Canvas(2, 4)
    c.set(0, 0, 1); assert chr(0x2801) == c.cell_char(0, 0)
    c.set(1, 3, 1); assert chr(0x2881) == c.cell_char(0, 0)

def test_style_priority():
    c = Canvas(2, 4)
    c.set(0, 0, 1); c.set(0, 1, 7)
    assert c.cell_style(0, 0) == 7
```

- [ ] **Step 2: Реализовать** `BITS`, `Canvas.set/line/blit_land/cell_char/cell_style`.
  `line` — Брезенхем, стиль у всех точек. Границы — молча игнорировать.

- [ ] **Step 3: Прогнать и закоммитить**

```bash
.venv/bin/pytest tests/test_braille.py -v
git add ... && git commit -m "feat: braille canvas"
```

---

### Task 4: Карта мира и ассеты

**Files:** Create `src/trafficmap/worldmap.py`, `scripts/fetch_assets.py`,
`tests/test_worldmap.py`

- [ ] **Step 1: Скрипт ассетов**

```python
URLS = {
    "land.geojson": "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_land.geojson",
    "dbip-city-lite.mmdb": "https://download.db-ip.com/free/dbip-city-lite-{ym}.mmdb.gz",
    "dbip-asn-lite.mmdb": "https://download.db-ip.com/free/dbip-asn-lite-{ym}.mmdb.gz",
}
```

`{ym}` — текущий месяц `YYYY-MM`; при 404 пробовать предыдущий, максимум 3 назад.
Скачивать в `assets/` через `urllib.request` (timeout 30 с), `.gz` распаковывать
`gzip`, проверять, что файл не пустой. Печатать размер и путь.

- [ ] **Step 2: Тесты растеризации**

```python
def test_land_fraction_and_known_points(sample_geojson):
    mask = rasterize(sample_geojson, 80, 40)
    assert 0.2 < sum(mask) / len(mask) < 0.4
    assert mask[20 * 80 + 40] == 1        # Африка
    assert mask[20 * 80 + 2] == 0         # Тихий океан

def test_cache_roundtrip(tmp_path, sample_geojson):
    a = load_land(tmp_path, "land.geojson", 80, 40)
    b = load_land(tmp_path, "land.geojson", 80, 40)
    assert a == b
```

`sample_geojson` — фикстура из двух полигонов (Африка и Южная Америка), чтобы тест
не зависел от скачанного файла.

- [ ] **Step 3: Реализовать** `rasterize(geojson, w, h) -> bytearray` (Pillow `polygon`,
  внешние кольца, клиппинг координат) и `load_land(cache_dir, path, w, h)` с кэшем
  `land_{w}x{h}_{mtime_ns}.bin`.

- [ ] **Step 4: Скачать ассеты, прогнать, закоммитить**

```bash
make assets && .venv/bin/pytest tests/test_worldmap.py -v
git add ... && git commit -m "feat: world land raster with cache"
```

---

### Task 5: GeoIP и anchor

**Files:** Create `src/trafficmap/geo.py`, `tests/test_geo.py`

- [ ] **Step 1: Тесты**

```python
@pytest.mark.parametrize("ip", ["10.1.2.3", "172.19.0.5", "192.168.1.1",
                                "100.64.0.7", "127.0.0.1", "169.254.1.1",
                                "fd00::1", "fe80::1", "::1"])
def test_local_ranges(ip):
    assert is_local(ip)

def test_public_ip_is_not_local():
    assert not is_local("1.1.1.1")

def test_anchor_from_cache(tmp_path, monkeypatch):
    # записать anchor.json с будущим expires_at и lat/lon
    # resolver.anchor() == Geo(...) без сетевого вызова
```

- [ ] **Step 2: Реализовать** явный список сетей (`LOCAL_NETS`), `Geo`, `GeoResolver`
  (`maxminddb.open_database`, кэш `dict`), `anchor()` с файловым кэшем TTL 3600 с и
  HTTP-fallback `api.ipify.org` → `ifconfig.me/ip`. Все сетевые ошибки — `None`,
  без исключений наверх. Логирование — в stderr, не в TUI.

- [ ] **Step 3: Прогнать и закоммитить**

```bash
.venv/bin/pytest tests/test_geo.py -v
git add ... && git commit -m "feat: geoip resolver and anchor"
```

---

### Task 6: Парсер `ss`

**Files:** Create `src/trafficmap/collector_ss.py`, `tests/fixtures/ss_tinup.txt`,
`tests/test_ss_parser.py`

- [ ] **Step 1: Фикстура** — вывод `ss -t -u -a -i -n -p`, где есть: IPv4 TCP с
  `bytes_sent`/`bytes_received`, IPv6 `[2001:db8::1]:443`, UDP без счётчиков,
  сокет без процесса, `LISTEN`, `*:*`.

- [ ] **Step 2: Тесты**

```python
def test_parses_ipv4_with_process(fixture_text):
    rows = parse_sockets(fixture_text)
    r = next(r for r in rows if r.remote[1] == 443)
    assert r.process == "curl" and r.bytes_sent == 1200 and r.bytes_received == 9000

def test_parses_ipv6(fixture_text):
    r = next(r for r in parse_sockets(fixture_text) if ":" in r.remote[0])
    assert r.remote == ("2001:db8::1", 443)

def test_skips_listen_and_wildcard(fixture_text):
    rows = parse_sockets(fixture_text)
    assert all(r.state != "LISTEN" for r in rows)
    assert all(r.remote[0] not in {"0.0.0.0", "::", "*"} for r in rows)
```

- [ ] **Step 3: Реализовать** `_split_addr`, `parse_sockets`, `SsCollector.poll`
  (argv `["ss", "-t", "-u", "-a", "-i", "-n", "-p"]`, `subprocess.run(timeout=5)`,
  `check=False`; при ненулевом коде — `RuntimeError` с stderr).

- [ ] **Step 4: Прогнать и закоммитить**

```bash
.venv/bin/pytest tests/test_ss_parser.py -v
git add ... && git commit -m "feat: ss socket collector"
```

---

### Task 7: FlowTable и статистика

**Files:** Create `src/trafficmap/flows.py`, `tests/test_flows.py`

- [ ] **Step 1: Тесты**

```python
def test_delta_and_rate(resolver):
    t = FlowTable(resolver, poll_ms=1000)
    t.update([row(bytes_sent=1000, bytes_received=500)], now=0.0)
    ev = t.update([row(bytes_sent=3000, bytes_received=1500)], now=1.0)
    upd = [e for e in ev if isinstance(e, Updated)][0]
    assert upd.dtx == 2000 and upd.drx == 1000
    assert upd.flow.tx_rate == pytest.approx(2000)

def test_counter_reset_does_not_go_negative(resolver):
    t = FlowTable(resolver, poll_ms=1000)
    t.update([row(bytes_sent=5000)], now=0.0)
    ev = t.update([row(bytes_sent=100)], now=1.0)
    assert all(getattr(e, "dtx", 0) >= 0 for e in ev)

def test_flow_closes_after_three_idle_polls(resolver):
    t = FlowTable(resolver, poll_ms=1000)
    key = t.update([row()], now=0.0)[0].flow.key
    t.update([], now=1.0); t.update([], now=2.0); t.update([], now=3.0)
    ev = t.update([], now=4.5)
    assert any(isinstance(e, Closed) and e.key == key for e in ev)

def test_local_peers_are_counted_not_mapped(resolver):
    t = FlowTable(resolver, poll_ms=1000)
    t.update([row(remote_ip="10.0.0.5")], now=0.0)
    assert t.stats().local == 1 and len(t.flows()) == 0
```

- [ ] **Step 2: Реализовать** `Flow`, `Opened/Updated/Closed`, `SocketState`,
  `FlowTable.update/stats/flows`. Ключ флоу `f"{ip}|{pid}|{proto}"`; ключ сокета —
  `f"{proto}|{lip}:{lport}|{rip}:{rport}|{pid}"`. Новый сокет задаёт baseline без
  дельты. EMA скорости: `0.5*new + 0.5*old`.

- [ ] **Step 3: Реализовать headless-режим `--dump PATH`**

`--dump` не поднимает curses: цикл `poll → update → запись JSONL` с интервалом
`poll_ms`, выход по `Ctrl-C`. Формат строки:

```json
{"t": 12.0, "type": "updated", "ip": "1.1.1.1", "port": 443, "proto": "tcp",
 "process": "curl", "pid": 100, "tx": 2000, "rx": 1000,
 "lat": 37.4, "lon": -122.0, "country": "US"}
```

Поля `lat/lon/country` — из `Geo` (могут быть `null`). `type` — `opened|updated|closed`.
Флоу с `tx+rx == 0` и возрастом > 5 с в `flows()` не попадают.

- [ ] **Step 4: Прогнать, проверить dump, закоммитить**

```bash
.venv/bin/pytest tests/test_flows.py -v
sudo .venv/bin/tmap --dump /tmp/flows.jsonl   # 10 секунд, затем Ctrl-C
head -3 /tmp/flows.jsonl
git add ... && git commit -m "feat: flow aggregation, rates, dump mode"
```

---

### Task 8: Анимация

**Files:** Create `src/trafficmap/anim.py`, `tests/test_anim.py`

- [ ] **Step 1: Тесты**

```python
def test_comet_position_over_time():
    c = Comet(great_circle((0, 0), (0, 90), 20), born=0.0, ttl=1.0)
    assert c.head(0.0) == (0.0, 0.0)
    assert c.head(1.0) == (0.0, 90.0)

def test_comet_expires():
    c = Comet(great_circle((0, 0), (0, 90), 20), born=0.0, ttl=1.0)
    assert c.expired(1.1)

def test_animator_caps_comets():
    a = Animator(max_comets=3)
    for i in range(10):
        a.spawn(kind="out", path=great_circle((0, 0), (0, 90), 20), now=float(i))
    assert len(a.comets) == 3
```

- [ ] **Step 2: Реализовать** `Comet.head(now)` (индекс по `t`, clip), `trail`
  (12 точек назад), `expired`, `Animator.spawn/prune/draw(canvas, viewport, now)`,
  `Peer` с TTL 10 с. Политика спавна: `Opened` → 1 комета; `Updated` →
  комета при `rate > 32 КБ/с`, не чаще одной на флоу за 300 мс; направление по
  большему из `dtx`/`drx`. Лимит 200 комет, старые вытесняются. `draw` кладёт
  голову стилем 6, след — 5/3/2 по возрасту, пиров — стилем 4, anchor — 7.

- [ ] **Step 3: Прогнать и закоммитить**

```bash
.venv/bin/pytest tests/test_anim.py -v
git add ... && git commit -m "feat: comet animation model"
```

---

### Task 9: TUI и snapshot

**Files:** Create `src/trafficmap/ui.py`, `src/trafficmap/collector_mock.py`,
`tests/fixtures/mock.jsonl`, `tests/test_ui_snapshot.py`; modify `__main__.py`

- [ ] **Step 1: Мок-данные** — 6 тиков по 1 с: `curl`→`1.1.1.1:443`, `firefox`→
  `142.250.0.1:443`, `ssh`→`203.0.113.7:22`, один UDP-флоу, один локальный пир.

- [ ] **Step 2: Snapshot-тест**

```python
def test_snapshot_contains_map_and_stats(tmp_path):
    out = tmp_path / "frame.txt"
    rc = main(["--mock", "tests/fixtures/mock.jsonl", "--snapshot", str(out),
               "--frames", "10", "--no-anchor"])
    assert rc == 0
    text = out.read_text()
    assert any("\u2800" <= ch <= "\u28ff" for ch in text)
    assert "flows" in text and "MB/s" in text
```

- [ ] **Step 3: Реализовать UI**

Ключевые части:

```python
def run(cfg: Config, events: queue.Queue) -> int:
    if not sys.stdout.isatty():
        print("TUI requires a terminal", file=sys.stderr)
        return 2
    return curses.wrapper(_loop, cfg, events)

def _loop(stdscr, cfg, events):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(max(1, 1000 // cfg.fps))
    ...
```

Цикл: забрать все события из очереди, обновить `Animator`, отрисовать кадр.
Отрисовка карты — `Canvas` → `cells()` → сборка runs по стилю → `addstr`.
Статус-бар: `↑ {rate} ↓ {rate} flows N peers M countries …`; вторая строка —
топ-3 процесса; третья — anchor/geo/warning/help.

Клавиши: `q`, `space`, `t`, `l`, `1/2/3`, `f`, `?`. Зум меняет `lon_span/lat_span`
и пересчитывает land-маску (кэш по размеру, поэтому быстро). При `W < 80` или
`H < 24` — по центру `terminal too small: need 80x24`, кадры не рисуются.

`--snapshot`: не требует TTY, гоняет `frames` кадров с мок-временем, пишет последний
кадр как обычный текст (без ANSI), возвращает 0.

- [ ] **Step 4: Прогнать snapshot, затем живой запуск**

```bash
.venv/bin/pytest tests/test_ui_snapshot.py -v
.venv/bin/tmap --mock tests/fixtures/mock.jsonl
sudo .venv/bin/tmap        # глазами: кометы, ресайз, пауза
```

- [ ] **Step 5: Коммит** `feat: curses UI and snapshot mode`

---

### Task 10: Полировка и документация

**Files:** Create `README.md`, `trafficmap.service`, `docs/usage.md`; modify `ui.py`, `geo.py`

- [ ] **Step 1: `--ascii`** — заменить глифы карты на `#`, пиры на `*`, кометы на `+`,
  след на `-`; snapshot-тест с `--ascii` не должен содержать Braille.
- [ ] **Step 2: no-root баннер** — `os.geteuid() != 0` → жёлтая строка
  `no root: only your sockets`; ~~в `--dump` добавить поле~~ (в дампе такого поля нет).
- [ ] **Step 3: systemd unit** — `User=root`, `ExecStart=~/Projects/traffic-map/.venv/bin/tmap`,
  `Restart=on-failure`, `StandardInput=tty`, `TTYPath=/dev/tty1` (документировать,
  что проще запускать из shell).
- [ ] **Step 4: README** — установка, `make assets`, запуск, клавиши, ограничения,
  атрибуция DB-IP (CC BY 4.0), troubleshooting (Braille-шрифт, root, нет базы).
- [ ] **Step 5: Финальные проверки**

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q src
.venv/bin/tmap --mock tests/fixtures/mock.jsonl --snapshot /tmp/f.txt --frames 30
grep -c $'\u28' /tmp/f.txt
```

Ожидается: все тесты passed, compileall без ошибок, snapshot непустой.

- [ ] **Step 6: Коммит** `docs: usage, ascii fallback, no-root mode`

---

# Phase 2: пакетный уровень, зум, точные координаты

Решения пользователя (2026-10-21): сбор пакетов — **tshark отдельным процессом**;
координаты — **таблица ASN + детерминированный разброс**; без прав — **откат на `ss`
с постоянным предупреждением**, карта не пустеет.

## Task 11: Пакетный сборщик (`tshark`)

**Files:** `src/trafficmap/collector_pcap.py`, `tests/test_collector_pcap.py`,
`src/trafficmap/config.py` (`capture`, `iface`), `src/trafficmap/ui.py`
(`_FallbackCollector`, `_CollectorThread`).

- `PcapCollector` запускает `tshark -i <iface|default_interface()> -n -l -T fields`
  с полями `frame.time_epoch, ip.src, ip.dst, ipv6.src, ipv6.dst, ip.proto,
  ipv6.nxt, ip.len, ipv6.plen, frame.len, tcp.srcport, tcp.dstport,
  udp.srcport, udp.dstport`; stdout читается построчно в фоновом потоке,
  stderr — для диагностики.
- Свои адреса — из `ip -j addr show` (плюс loopback); src свой → исходящий,
  dst свой → входящий, оба свои → локальный. Размер IPv6 берётся из
  `ipv6.plen`, протокол — из `ipv6.nxt`, иначе из `frame.len`/`ip.proto`.
- Кумулятивные счётчики по 5-кортежу, `poll()` отдаёт те же `SocketRow`, что и
  `SsCollector`; простой > `IDLE_TIMEOUT = 60 с` вытесняет кортеж.
- `drain_bursts()` отдаёт всплески по пиру с `age` (секунд с последнего пакета)
  для искр; счётчики пакетов — для `pkt/s`.
- Имя процесса — из `ss`-таблицы `local_port → (pid, process)`.
- `NO_PRIVILEGES` и `MISSING_TSHARK` — точные тексты с командой `setcap` /
  советом установить `tshark`; `PcapCollector` бросает `RuntimeError`.
- `make_collector`: `capture = "pcap"` (по умолчанию) оборачивается в
  `_FallbackCollector`; при ошибке старта/первого опроса — `ss` и постоянное
  предупреждение в статус-баре (`capture error: ... (sockets only)`).
  `capture = "ss"` — сразу сокеты, без ошибки захвата.

## Task 12: Искры пакетов (Pulse)

- `anim.py`: `Pulse` (TTL 0.7 с, затухание 0.55 с), `STYLE_PULSE = 9` и
  `STYLE_PULSE_DIM = 8`, пул 400; `Animator.spawn_pulse(geo, weight, now)`;
  пакет ≥ 8 KiB — крупная искра (2×2).
- `ui.py`: `_spawn_pulses` из `drain_bursts()`; `pkt/s` — EMA с TTL 2 с
  (`PACKET_RATE_TTL`), формат `834 pkt/s` / `12.4k pkt/s`; ASCII — `*`,
  цвет — magenta (13).

## Task 13: Непрерывный зум и резкая карта

- `_Map.zoom` — float 1…120 (`MIN_ZOOM`, `MAX_ZOOM`), шаг 1.25; пресеты
  `1/2/3` = ×1/×3/×12 (охват 360×180 → 120×60 → 30×15).
- `+`/`=` и `-`/`_` — шаг, `0` — мир; колесо — `curses.mousemask` +
  `curses.getmouse`, `BUTTON4/5_PRESSED`, зум вокруг точки под курсором
  (`zoom_by(factor, cursor)`), центр хранится в `_Map.center`.
- `worldmap.rasterize(geojson, width, height, viewport=None)` рисует полигоны
  в кадре вьюпорта (без клампинга, чтобы не размазывать край); вьюпорт через
  антимеридиан рисуется тремя сдвигами (±360°). `load_geojson()` вынесен.
- `_Map.land_mask`: мир (охват ≥ 360×180) — из кэша/растра, иначе — растрирование
  под вьюпорт; кэш ключуется размером, центром и охватом.

## Task 14: Точные координаты (таблица ASN + разброс)

- `src/trafficmap/asn_coords.py`: `Place(city, country, lat, lon)`;
  `ANYCAST_POPS` — 10 сетей, которые отвечают из многих городов
  (Cloudflare, Google, Akamai ×2, Fastly, Meta, X, Netflix, Telegram, GitHub),
  у каждой — список точек присутствия; `nearest_place` выбирает ближайшую
  к дому, `anycast_place(asn, home)` — вход для резолвера.
- `GeoResolver.set_home(lat, lon)`; `_lookup_public` подменяет место для
  anycast-ASN; адрес самой машины (`_anchor_ip`) не подменяется — он там, где
  база говорит.
- `SCATTER_DEGREES = 0.15`: `_spread(geo, ip)` сдвигает адрес по sha1(ip) —
  стабильно между кадрами и запусками; по умолчанию выключен, UI включает
  (`GeoResolver(..., scatter=True)`), чтобы точка «я» и тесты оставались точными.
- Документация: разброс — способ рисования, а не точность; для anycast —
  ближайшая точка присутствия (догадка, зато не другой континент).

---

## Self-Review

- **Покрытие спеки:** конфиг (T1), проекция (T2), Braille (T3), карта/ассеты (T4),
  гео/anchor (T5), `ss` (T6), флоу/статистика (T7), анимация (T8), UI/snapshot (T9),
  ascii/no-root/доки (T10), пакетный сборщик (T11), искры (T12), зум (T13),
  координаты (T14).
- **Плейсхолдеров нет:** все шаги содержат команды, код или точные требования.
- **Согласованность типов:** `SocketRow.remote: tuple[str,int]`, `Geo`, `Flow.key`,
  `Opened/Updated/Closed` используются одинаково в T6–T9. `Canvas` API
  (`set/line/blit_land/cell_char/cell_style`) совпадает в T3 и T9. `Viewport`
  (`width/height/center_lat/center_lon/lon_span/lat_span`) — в T2, T8, T9.
- **Обратная совместимость:** `PcapCollector` реализует тот же `poll() -> list[SocketRow]`,
  поэтому `FlowTable` и аниматор не меняются; `--capture ss` остаётся рабочим режимом
  без привилегий.
- **Пробелы:** eBPF-учёт per-process остаётся вне плана (нет bpftrace/bpftool в системе).
