# Traffic Map (TUI) — дизайн

**Дата:** 2026-10-21
**Статус:** утверждён
**Проект:** `~/Projects/traffic-map`
**Команда:** `tmap`

## 1. Цель

Показывать живой сетевой трафик этой машины на карте мира прямо в терминале: береговая
линия в Braille-графике, точка «я», удалённые пиры и кометы, летящие по дугам больших
кругов в обе стороны. Никакого браузера, никакого web-сервера.

Успех: запустил `sudo tmap` — через пару секунд видно, как пакеты уходят и приходят,
видно процессы, страны и текущие скорости.

## 2. Не-цели

- Web UI, глобус на three.js, ntopng.
- DPI, разбор payload, SNI, доменные имена (только IP/ASN/PTR).
- Историческая база, графики за сутки, экспорт метрик.
- Кроссплатформенность: только Linux (netlink/`ss`, `/proc`).
- Управление трафиком, шейпинг, блокировки.

## 3. Пользовательский сценарий

1. `sudo tmap` (root нужен, чтобы видеть сокеты всех процессов).
2. В статус-баре `anchor: 203.0.113.7 · RU` — геолокация точки «я».
3. Каждое новое соединение/всплеск трафика порождает комету: зелёная — исходящая,
   голубая — входящая. Комета летит по great circle, за ней гаснущий след.
4. Активные пиры — точки, размер ∝ log(суммарных байт), гаснут через 10 с тишины.
5. Внизу: суммарные ↑/↓, число флоу, стран, топ-процессы.
6. `q` — выход, `space` — пауза (карта замирает, сбор продолжается), `t` — панель
   топ-талкеров, `l` — подписи стран, `1/2/3` — зум, `f` — фильтр по процессу.

## 4. Архитектура

```
┌──────────────────────── collector thread (1 Гц) ────────────────────────┐
│  ss -t -u -a -i -n -p  →  parse_sockets()  →  list[SocketRow]           │
│        │                                              │                 │
│        │                    GeoResolver (MMDB)  ←─────┘                 │
│        ▼                                              ▼                 │
│   FlowTable.update(rows, now) ──► events: Opened / Delta / Closed       │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │ queue.Queue
┌───────────────────────────────────▼─────────────── UI thread (12 fps) ──┐
│  Animator: Comet / Pulse / Peer  →  Canvas (Braille, styles)            │
│  Viewport (equirectangular, zoom) →  curses screen + status bar         │
└─────────────────────────────────────────────────────────────────────────┘
```

Компоненты изолированы: коллектор не знает про curses, рендерер не знает про `ss`,
геолокация не знает ни про то, ни про другое. Всё, что можно посчитать без терминала,
тестируется без терминала.

## 5. Компоненты

### 5.1 `config.py`

```python
@dataclass(frozen=True, slots=True)
class Config:
    fps: int = 12
    poll_ms: int = 1000
    city_db: Path
    asn_db: Path | None
    land_geojson: Path
    cache_dir: Path            # ~/.cache/trafficmap
    ascii: bool = False
    no_anchor: bool = False
    anchor: tuple[float, float] | None = None
    filter_process: str = ""
    mock: Path | None = None
```

Приоритет: CLI > env `TMAP_*` > `~/.config/trafficmap/config.toml` > defaults.
Неизвестные ключи в TOML — ошибка с внятным сообщением.

### 5.2 `geo.py`

```python
@dataclass(frozen=True, slots=True)
class Geo:
    ip: str
    lat: float | None
    lon: float | None
    country: str          # ISO alpha-2 или "--"
    city: str | None
    asn: int | None
    org: str | None

class GeoResolver:
    def lookup(self, ip: str) -> Geo: ...
    def anchor(self) -> Geo | None: ...
```

- MMDB: DB-IP Lite City + ASN. Поля: `country.iso_code`, `city.names.en`,
  `location.latitude/longitude`, `autonomous_system_number/organization`.
- Приватные и служебные диапазоны — явный список сетей (`10/8`, `172.16/12`,
  `192.168/16`, `100.64/10`, `127/8`, `169.254/16`, `fc00::/7`, `fe80::/10`, `::1/128`),
  а не `is_private` (он не покрывает CGNAT стабильно). Такие адреса на карту не идут,
  считаются в счётчик `local`.
- Anchor: публичный IP через `https://api.ipify.org` (fallback `https://ifconfig.me/ip`,
  timeout 2 с), затем `lookup()`. Кэш `cache_dir/anchor.json`, TTL 1 час.
- IP не найден в базе → `lat/lon = None`, точка не рисуется, страна `--`.
- Если сеть недоступна — anchor из конфига; если и его нет — режим «только точки»,
  в статус-баре `anchor: unknown`.

### 5.3 `projection.py`

Равнопромежуточная (equirectangular) проекция. Viewport: центр, ширина/высота в градусах.

```python
def project(lat: float, lon: float, vp: Viewport) -> tuple[int, int] | None
def unproject(x: int, y: int, vp: Viewport) -> tuple[float, float]
def great_circle(a: tuple[float, float], b: tuple[float, float], n: int) -> list[tuple[float, float]]
```

Great circle — slerp по единичным векторам:

```
p(t) = (sin((1-t)Ω)·a + sin(tΩ)·b) / sin Ω,  Ω = acos(a·b)
lat = asin(z), lon = atan2(y, x)
```

При `Ω < 1e-9` — линейная интерполяция. Для антиподов (`sin Ω ≈ 0`) — через
ортогональный вектор. Тесты: Москва→Нью-Йорк, середина пути в Атлантике.

### 5.4 `braille.py`

Точечная сетка, 2×4 точки на знакоместо. Битовая карта:

```python
BITS = ((0x01, 0x08),
        (0x02, 0x10),
        (0x04, 0x20),
        (0x40, 0x80))   # BITS[y][x]
```

`Canvas` хранит `bytearray` стилей на каждую точку (`0` — пусто). Стиль знакоместа —
максимум по его 8 точкам (так комета перекрывает сушу, суша — фон). Методы:
`set`, `line` (Брезенхем), `blit_land`, `cell_styles() -> memoryview`.
Символ: `chr(0x2800 | mask)`.

Стили (числа = приоритет): `1 land`, `2 trail_dim`, `3 trail_mid`, `4 dot`,
`5 trail_bright`, `6 comet_head`, `7 anchor`.

### 5.5 `worldmap.py`

- Источник: Natural Earth 110m land GeoJSON (~200 КБ, public domain).
- Растеризация: Pillow `Image.new("1", (W, H))` + `ImageDraw.polygon` для каждого
  полигона, `getdata()` → `bytearray` 0/1. `W×H` — размер точечной сетки.
- Кэш: `cache_dir/land_{W}x{H}_{mtime}.bin`; при старте/ресайзе сначала кэш,
  иначе растеризация (~30–100 мс).
- Тест: доля суши 25–35 %, центр Африки — суша, центр Тихого океана — вода.

### 5.6 `collector_ss.py`

```python
@dataclass(frozen=True, slots=True)
class SocketRow:
    proto: str                  # "tcp" | "udp"
    local: tuple[str, int]
    remote: tuple[str, int]
    state: str
    pid: int | None
    process: str | None
    bytes_sent: int | None
    bytes_received: int | None
    key: str

def parse_sockets(text: str) -> list[SocketRow]
class SsCollector:
    def poll(self) -> list[SocketRow]
```

- Команда: `ss -t -u -a -i -n -p` (argv, без shell).
- Парсинг: заголовок пропускаем; строка сокета → поля; последующие строки с отступом —
  `bytes_sent:`, `bytes_received:` (tcp_info; у UDP обычно нет — тогда `None`).
- IPv6 `[2001:db8::1]:443`, зоны `%eth0` отбрасываем.
- Отбрасываем: `LISTEN`, remote `0.0.0.0`/`::`/`*`, state `TIME-WAIT` (счётчики уже
  не растут, но закрытие флоу придёт по пропаже сокета).
- Процесс: первая запись `users:(("name",pid=123,fd=4))`.
- Без root `ss` покажет только свои сокеты — коллектор это переживает, UI показывает
  жёлтый баннер `no root: only your sockets`.

### 5.7 `flows.py`

Два уровня: сокеты (для дельт) и флоу (для карты).

```python
@dataclass(slots=True)
class Flow:
    key: str                # f"{remote_ip}|{pid}|{proto}"
    remote: Geo
    process: str
    pid: int | None
    proto: str
    port: int
    tx: int
    rx: int
    tx_rate: float
    rx_rate: float
    first_seen: float
    last_seen: float
    state: str

class FlowTable:
    def update(self, rows: list[SocketRow], now: float) -> list[FlowEvent]
    def stats(self) -> Stats
```

- Дельты: `delta = max(0, new - prev)`; если счётчик уменьшился (сокет переиспользован) —
  считать `new`, пометить `reset`.
- Закрытие сокета: пропал из выдачи → удалить; флоу без сокетов и без дельт
  `3 × poll_ms` → `Closed`.
- `stats()`: суммарные `tx_rate`/`rx_rate`, число флоу, `Counter` стран, топ-5
  `процесс:порт` по rate, счётчик `local` (приватные адреса).
- Порог показа: флоу с `tx+rx == 0` и возрастом > 5 с не рисуется (UDP-«висяки»).

### 5.8 `anim.py`

- `Comet(path, born, ttl=1.2, kind, weight=1)`: голова в `t = (now-born)/ttl`,
  след — 12 предыдущих точек с затуханием.
- Спавн: `Opened` → 1 комета; `Delta` → комета, если `rate > 32 КБ/с`, не чаще
  одной на флоу за 300 мс. Направление: `tx` → out (зелёная), `rx` → in (голубая).
- `Peer(geo, weight, last_seen)`: живёт 10 с, стиль по `log2(bytes)`.
- `Pulse`: короткие/UDP-флоу — вспышка без полёта.
- Ограничение: не более 200 активных комет, старые вытесняются.

### 5.9 `ui.py`

- `curses.wrapper`, `curs_set(0)`, `timeout(1000 // fps)`, `nodelay`.
- Поток коллектора + `queue.Queue`; UI-поток только потребляет события.
- Раскладка: карта `H-3` строк, 3 строки статуса. При `W < 80` или `H < 24` —
  сообщение «terminal too small».
- Отрисовка: по строкам группируем одинаковые стили в runs, `addstr` на run,
  а не на клетку (иначе 7680 вызовов на кадр).
- Ресайз: `KEY_RESIZE` → пересчёт viewport и land-маски.
- `--snapshot PATH --frames N`: отрисовать N кадров с мок-временем и записать
  последний кадр в файл (для тестов и CI, без TTY).
- `--dump PATH`: headless-режим, пишет события `opened|updated|closed` в JSONL
  с геоданными, без curses; выход по `Ctrl-C`.

### 5.10 `collector_mock.py`

JSONL, по строке на тик:

```json
{"t": 0.0, "sockets": [{"proto": "tcp", "local": ["10.0.0.2", 51000],
 "remote": ["1.1.1.1", 443], "state": "ESTAB", "pid": 100,
 "process": "curl", "bytes_sent": 1200, "bytes_received": 9000}]}
```

Позволяет крутить демо и тесты без root и без сети.

## 6. Экран

```
╔══════════════════════════════════════════════════════════════════════╗
║   ·   ░░▒▒▓▓   ·    ·        ·      ✦────○          ·       ·        ║
║     ░▒▓▓▓▓▓▓▒░   ·     ·   ╲  ╱        ·    ·                       ║
║  ●═══════════════════════✦  ╳  ·                    ✦                ║
║   ·   ░░▒▓▓▓▒░   ·    ╱  ╲      ·        ·                          ║
╠══════════════════════════════════════════════════════════════════════╣
║ ↑ 2.4 MB/s   ↓ 11.8 MB/s   flows 37   peers 12   RU DE NL US         ║
║ firefox:443 ↓9.1M · curl:22 ↑1.2M · spotify:443 ↓1.4M               ║
║ anchor 203.0.113.7 · RU · geo 10ms · q quit  ? help               ║
╚══════════════════════════════════════════════════════════════════════╝
```

Цвета: суша — тёмно-синий, anchor — белый, out — зелёный (3 яркости), in — голубой
(3 яркости), текст — серый, warning — жёлтый. При `--ascii`: `#` суша, `*` пир,
`+` комета, `-` след.

## 7. Ошибки и деградация

| Ситуация | Поведение |
|---|---|
| Нет `assets/*.mmdb` | сообщение + `make assets`, exit 2 |
| Нет GeoJSON | сообщение + `make assets`, exit 2 |
| Нет TTY | `TUI requires a terminal`, exit 2 |
| `ss` упал | баннер, retry с backoff 1→5 с, последние данные остаются |
| Нет root | жёлтый баннер, работают только свои сокеты |
| Anchor не резолвится | точки без дуг, `anchor: unknown` |
| IP не в базе | страна `--`, точка не рисуется, счётчик unknown |
| Терминал < 80×24 | сообщение, выход по `q` |
| Битый TOML | путь и номер строки, exit 2 |

## 8. Производительность

- Кадр ≤ 60 мс при 200×60 знакомест (400×240 точек).
- CPU ≤ 10 % одного ядра при 12 fps, RSS ≤ 80 МБ.
- Старт ≤ 1 с при тёплом кэше суши.
- `ss`-опрос ≤ 30 мс на типичном десктопе (сотни сокетов).

## 9. Тестирование

- `test_projection.py`: известные координаты, great circle, антиподы, клиппинг.
- `test_braille.py`: точные биты для (0,0), (1,0), (0,3), (1,3); приоритет стилей.
- `test_worldmap.py`: доля суши, Африка/Тихий океан; кэш читается повторно.
- `test_ss_parser.py`: фикстура с IPv4, IPv6, UDP, без процесса, с зоной.
- `test_flows.py`: дельты, reset счётчика, закрытие, агрегация по пиру, stats.
- `test_geo.py`: приватные диапазоны, anchor из кэша, отсутствие IP в базе.
- `test_ui_snapshot.py`: `--mock --snapshot` даёт непустой кадр с Braille и статусом.
- Ручная проверка: `sudo tmap` на живом трафике, ресайз, пауза, зум.

## 10. Зависимости и лицензии

- Python ≥ 3.11 (на машине 3.14.7), venv.
- `pillow` — растеризация суши.
- `maxminddb` — чтение MMDB.
- `pytest` — dev.
- DB-IP Lite — CC BY 4.0, атрибуция в README и на экране помощи.
- Natural Earth — public domain.

## 11. Этапы

| Этап | Результат | Приёмка |
|---|---|---|
| M0 | скелет, venv, ассеты | `tmap --version`, базы скачаны |
| M1 | статическая карта | Braille-мир, ресайз, `q` |
| M2 | сбор + гео | `--dump` с реальными флоу, тесты зелёные |
| M3 | анимация | кометы, статус-бар, 12 fps |
| M4 | полировка | фильтры, ascii, no-root баннер, README, systemd |
| M5 (backlog) | UDP/eBPF, SYN-сниффер, автообновление базы | по желанию |

## 12. Риски

- **UDP-объём не виден** через `ss`: DNS/QUIC только «мигают». M5.
- **Короткие соединения** между опросами 1 Гц могут не попасть. M5.
- **Геолокация города** неточна (датацентры, VPN). Поэтому в подписях страна + ASN.
- **Root** обязателен для полной картины.
- **Braille-шрифт** обязателен; `--ascii` как фолбэк.
