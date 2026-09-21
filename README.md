# Internet Speed Tester

Однофайловый CLI-скрипт на Python, который замеряет скорость интернет-соединения последовательными HTTP-запросами и печатает результат в терминал с «фронтендоподобным» UI на библиотеке [`rich`](https://github.com/Textualize/rich).

Особенности:
- **Одна команда — и запустилось.** Никаких `venv`, `pip install` вручную и настройки окружения не требуется: скрипт сам поставит `rich` при первом запуске.
- **Только stdlib для HTTP.** Замер живой, без магии `requests`/`httpx`.
- **Живой UI.** Прогресс-бар, растущая таблица запросов, спарклайн истории скоростей, финальная панель с рейтингом связи.
- **Полноценная типизация + dataclass** для результатов, все хелперы чистые.
- **Покрыто pytest'ом** (57 тестов, локальный http-сервер на loopback — не нужно интернета).

---

## Требования

- **Python 3.7+** (проверено на 3.7 — 3.13).
- Работает на macOS / Linux / Windows.
- `rich` — ставится автоматически при первом запуске. Хотите поставить заранее — `pip install -r requirements.txt`.

---

## Установка и запуск

```bash
git clone <репозиторий>
cd "AD Robot"
python3 speed_test.py "https://speed.cloudflare.com/__down?bytes=10000000"
```

Скрипт сделает **10 последовательных GET-запросов** к URL, замерит время каждого, посчитает суммарный объём и агрегированную скорость в MB/s, и нарисует живой отчёт.

### Варианты вызова

```bash
# Интерактивный режим — скрипт сам спросит URL
python3 speed_test.py

# Кастомное число запросов (например, 20)
python3 speed_test.py https://host/big.jpg -n 20

# Справка
python3 speed_test.py --help
```

### Хорошие URL для теста

| URL | Что отдаёт |
|---|---|
| `https://speed.cloudflare.com/__down?bytes=10000000` | Ровно 10 000 000 байт, стабильно, не кэшируется |
| `https://speed.hetzner.de/10MB.bin` | 10 MB бинарник от Hetzner |
| `https://speed.hetzner.de/100MB.bin` | 100 MB бинарник (осторожно на медленном канале) |
| `https://upload.wikimedia.org/wikipedia/commons/6/6e/Solar_sys.jpg` | Тяжёлая картинка с Википедии |

Для честного замера файл должен быть **≥ 5 MB** — иначе TCP slow-start, хендшейк и буферы доминируют над самим throughput.

---

## Структура проекта

```
.
├── speed_test.py           # весь исполняемый код
├── tests/
│   └── test_speed_test.py  # pytest-набор (unit + integration на loopback)
├── requirements.txt        # runtime: rich
├── requirements-dev.txt    # + pytest
├── pytest.ini              # конфиг pytest (testpaths, pythonpath)
├── .gitignore              # Python/venv/IDE/OS
└── README.md
```

---

## Тесты

Установка зависимостей и запуск:

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
```

Ожидаемый вывод — **57 passed**. Все интеграционные тесты HTTP-слоя работают на loopback (`127.0.0.1` на свободном порту, поднимаемый через `http.server.ThreadingHTTPServer` фикстурой) — внешнего интернета не требуется.

Что покрыто:
- **Форматтеры** (`humanize_bytes`, `humanize_speed`, `sparkline`, `gauge_bar`, `rate_speed`, `speed_dots`, `_clean_url_error`) — граничные случаи, пороги, длины, диапазоны символов.
- **Датакласс `RequestResult`** — дефолты для failure-случая, `speed_bps`, frozen-инвариант.
- **HTTP-слой** (`download_once`, `perform_measurement`) — успешный 200, 404 → `HTTPError`, connection-refused → `RequestResult(ok=False)`.
- **CLI** (`parse_args`, `resolve_url`) — валидные URL, невалидные схемы/netloc, интерактивный prompt через `monkeypatch`.
- **Оркестрация** (`run_measurements`, `main`) — правильное число результатов, exit-code 0/1.

Если у вас в окружении есть глючные pytest-плагины от других пакетов (у меня было `web3.tools.pytest_ethereum`), запустите с отключённой автозагрузкой:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest
```

---

## Как устроен код

Файл: [`speed_test.py`](./speed_test.py). Разбит на девять пронумерованных секций.

### Секция 1. Bootstrap — автоустановка `rich` (`speed_test.py:19-73`)

```python
def _bootstrap() -> None:
    try:
        import rich
        return
    except ImportError:
        pass
    # три попытки pip install с capture_output
```

Проверяем, есть ли `rich`. Если нет — пробуем три варианта установки по очереди:

1. `pip install rich` — стандартный случай.
2. `pip install --user rich` — если системный питон без прав.
3. `pip install --break-system-packages rich` — для «managed» интерпретаторов (Debian/Ubuntu ≥ 23.04, Homebrew, PEP 668).

Каждая попытка выполняется через `subprocess.run(..., capture_output=True)` — вывод pip сохраняется в `last_output`. После каждой успешной установки вызывается `_refresh_import_paths()` (добавляет user site-packages в `sys.path` + `importlib.invalidate_caches()`), потом делается повторный `import rich`. Если ни одна попытка не завершилась успехом — печатается полный лог pip и `sys.exit(1)`.

**Важно:** `_bootstrap()` вызывается **до** любого `from rich import ...` — иначе интерпретатор упал бы на импорте.

### Секция 2. Импорты (`speed_test.py:87-108`)

Стандартная библиотека:
- `urllib.request`, `urllib.error`, `urllib.parse` — HTTP-клиент, без `requests`/`httpx`.
- `ssl.create_default_context` — системные корневые сертификаты.
- `time.perf_counter` — монотонный высокоточный таймер.
- `statistics.mean`, `statistics.median` — агрегаты.
- `dataclasses.dataclass` — для `RequestResult`.
- `argparse`, `re` — парсинг аргументов и очистка сообщений об ошибках.

Плюс `from rich import ...` для UI.

### Секция 3. Константы (`speed_test.py:113-125`)

Все «ручки» в одном месте. Магические числа не растеканы по коду:

```python
DEFAULT_REQUEST_COUNT = 10
REQUEST_TIMEOUT_SEC = 60.0
CHUNK_SIZE = 64 * 1024
UI_REFRESH_HZ = 10
TABLE_GAUGE_WIDTH = 24
FINAL_GAUGE_WIDTH = 42
SPARKLINE_WIDTH = 24
ERROR_MSG_MAX_LEN = 32
SPARK_CHARS = " ▁▂▃▄▅▆▇█"
USER_AGENT = "speed_test.py/1.0 (+python)"
```

### Секция 4. Датакласс `RequestResult` (`speed_test.py:131-146`)

Заменяет разноключевой `dict`. `frozen=True` → неизменяемый, безопасно передавать между потоками/функциями:

```python
@dataclass(frozen=True)
class RequestResult:
    ok: bool
    elapsed_sec: float = 0.0
    n_bytes: int = 0
    status: int = 0
    error: str = ""

    @property
    def speed_bps(self) -> float:
        if not self.ok or self.elapsed_sec <= 0:
            return 0.0
        return self.n_bytes / self.elapsed_sec
```

`speed_bps` инкапсулирует деление «байт / секунды» и защищает от zero-division и от бессмысленных «скоростей» для упавших запросов.

### Секция 5. Форматирующие хелперы (`speed_test.py:151-217`)

Чистые функции, каждая делает одну вещь:

- **`humanize_bytes(n)`** — `1234567` → `"1.18 MB"`. Идёт по единицам `B/KB/MB/GB`, делит на 1024 пока не влезет, форматирует с двумя знаками.
- **`humanize_speed(bps)`** — вход в байтах/сек. Если ≥ 0.01 MB/s → печатает как MB/s, иначе KB/s (чтобы не увидеть `0.00 MB/s` при медленном канале).
- **`sparkline(values, width=24)`** — превращает последние `width` значений в строку типа `▂▄▆▇█▇▆▅`. Нормирует к `[min, max]` и мапит на 9 градаций юникод-блоков.
- **`gauge_bar(value, best, width=30)`** — горизонтальный бар `██████████░░░░░░░░`. Показывает `value / best`.
- **`rate_speed(bps)`** — `(label, стиль, dots_of_5)` по средней MB/s: `< 0.5` Very Slow, `< 2` Slow, `< 10` Decent, `< 50` Fast, иначе Blazing Fast.
- **`speed_dots(filled)`** — рендер точек `●●●○○`. Значение зажимается в `[0, 5]`.
- **`_clean_url_error(reason)`** — срезает префикс `[Errno N] ` и хвост после первой запятой, режет до `ERROR_MSG_MAX_LEN`. Превращает `[Errno 8] nodename nor servname provided, or not known` в лаконичное `nodename nor servname provided`.

### Секция 6. HTTP-слой (`speed_test.py:222-267`)

**`download_once(url, timeout=60.0)`** — низкоуровневый: делает один запрос, читает всё тело, возвращает `(elapsed, bytes, status)`. Пробрасывает исключения наружу.

Что здесь важно:
1. `ssl.create_default_context()` — системные CA (иначе на некоторых сборках Python падает на self-signed).
2. Заголовки: `User-Agent` (некоторые CDN режут пустой), `Accept: */*`, `Cache-Control: no-cache, no-store` (чтобы честно мерить сеть, а не прокси-кэш).
3. Таймер стартует **до** `urlopen` и останавливается **после** `close()` — в замер входят DNS, TCP-хендшейк, TLS-хендшейк, отправка, получение всего тела.
4. Читаем циклом по `CHUNK_SIZE` — считаем **фактически прошедшие** байты (могут отличаться от `Content-Length` при chunked-encoding/декомпрессии).
5. `resp.getcode()` — работает на 3.7+, в отличие от `.status`.

**`perform_measurement(url)`** — вся политика ошибок в одном месте:

```python
try:
    elapsed, downloaded, status = download_once(url)
except urllib.error.HTTPError as exc:
    return RequestResult(ok=False, error=f"HTTP {exc.code}")
except urllib.error.URLError as exc:
    return RequestResult(ok=False, error=_clean_url_error(...))
except OSError as exc:                 # socket.timeout, ConnectionResetError…
    ...
return RequestResult(ok=True, ...)
```

Никаких `except Exception` — только осмысленные категории.

### Секция 7. UI-компоненты (`speed_test.py:272-370`)

Четыре чистые функции — каждая берёт данные и возвращает `rich` renderable, **без побочных эффектов**:

- **`build_header(url, count)`** → большая двухлинейная панель с заголовком, целевым URL и планом.
- **`build_results_table(rows)`** → таблица `Request Log`. Колонки: `#`, `Status`, `Time`, `Size`, `Speed`, и хвостовая колонка с мини-гейджем (или красным текстом ошибки). Гейдж каждой строки нормируется к **лучшей на этот момент** скорости, поэтому картинка «оживает» по мере прогресса.
- **`build_stats_panel(rows)`** → компактная панель Live Statistics: сколько скачано, средняя скорость, лучшая, sparkline последних 24 значений.
- **`render_scene(url, rows, progress, count)`** → композит из четырёх renderable'ов через `rich.console.Group`. Именно это выражение передаётся в `Live.update()` при каждой итерации.

Ключевая идея: **каждый вызов `render_scene()` строит новую сцену с нуля** из текущего состояния `rows`. `Live` сам делает diff терминала — мигания не будет.

### Секция 8. Финальный отчёт (`speed_test.py:375-471`)

**`print_final_summary(url, rows, count)`** выводится **после** выхода из `Live`. Если ни одного успешного запроса нет — красная панель c сообщением о проверке сети и `return`. Иначе считает агрегаты:

```python
total_bytes = sum(r.n_bytes    for r in ok)
total_time  = sum(r.elapsed_sec for r in ok)
avg_bps     = total_bytes / total_time         # ← агрегированный throughput
avg_time    = mean(r.elapsed_sec   for r in ok)
med_time    = median(r.elapsed_sec for r in ok)
speeds      = [r.speed_bps for r in ok if r.speed_bps > 0]
best, worst = max(speeds, default=0.0), min(speeds, default=0.0)
```

> **Почему `sum / sum`, а не `mean(speeds)`?**
> Агрегированное деление даёт **правильный throughput** для набора неравных запросов — короткая быстрая попытка не перекашивает среднее. Для одинаковых запросов результат совпадёт с `mean`, но общий случай устойчивее.

Затем строится «геройская» панель (`AVERAGE INTERNET SPEED` + большие `X.XX MB/s` + `Connection: <label> ●●●○○`), три гейджа Avg/Best/Worst (все нормируются к `best`) и таблица фактов.

### Секция 9. Оркестрация (`speed_test.py:476-577`)

- **`resolve_url(cli_url)`** — валидированный URL или `None`. Если аргумент не передан, рисует приглашение и читает через `console.input(...)`. Валидация через `urllib.parse.urlparse`: схема `http`/`https`, непустой `netloc`.
- **`_make_progress()`** — фабрика `rich.progress.Progress` со всеми колонками (SpinnerColumn, TextColumn с описанием, BarColumn, счётчик, TimeElapsedColumn). Ключевой трюк: `Progress` подсовывается **как renderable внутрь `Live`**, а не запускается через `.start()`.
- **`run_measurements(url, count)`** — главный цикл. Обёрнут в `Live(...)` с `refresh_per_second=10`. Ловит `KeyboardInterrupt` — если пользователь нажал `Ctrl-C`, всё равно возвращает уже собранные `rows` для финального отчёта.
- **`parse_args(argv)`** — `argparse.Namespace` с `url` (позиционный, опциональный) и `-n/--count` (по умолчанию `DEFAULT_REQUEST_COUNT`).
- **`main(argv=None)`** — тонкая обёртка: parse → resolve → run → summary → return exit-code.

Тонкий `main` делает код удобным для тестирования: `st.main(["http://127.0.0.1:PORT/x.bin", "-n", "2"])` можно вызвать из pytest'а как обычную функцию и проверить `return code`.

---

## Как считается скорость

**На одну запись в таблице (`RequestResult.speed_bps`):**
```
speed_i = n_bytes_i / elapsed_sec_i     # байт/сек
```
затем `humanize_speed()` переводит в `MB/s` или `KB/s`.

**Итоговая средняя скорость (агрегированная, `print_final_summary`):**
```
avg_bps = sum(n_bytes_i) / sum(elapsed_sec_i)
```
Именно она печатается большим шрифтом в финальной панели.

**Best / Worst:**
```
best  = max(speed_bps_i)
worst = min(speed_bps_i)
```

**Время запроса** = `perf_counter` от `urlopen(...)` до полного дренажа тела и `close()`. Включает DNS-резолвинг, TCP + TLS-хендшейк, отправку запроса, ожидание ответа, скачивание всего тела. Именно поэтому первый запрос обычно медленнее последующих: DNS-кэш холодный, соединение свежее.

---

## Обработка ошибок

| Тип | Пример | Что видно в таблице |
|---|---|---|
| DNS не резолвит | `nodename nor servname provided` | `✗ err` + очищенное сообщение |
| Отказ соединения | `Connection refused` | То же |
| HTTP 4xx / 5xx | `HTTP 404`, `HTTP 500` | То же, статус в тексте |
| Timeout | `timed out` | То же (по умолчанию 60 сек) |
| SSL error | `certificate verify failed` | То же |
| Ctrl-C во время цикла | — | Жёлтое `Interrupted by user.`, финальный отчёт по собранному |
| Все N запросов упали | — | Красная панель `FINAL REPORT` без цифр |

---

## Возможные грабли

- **DNS не работает вообще** (`nodename nor servname provided`, все запросы падают за `0.00 s`) — проблема не в скрипте. Проверить `nslookup google.com`. Часто виноват активный **VPN** (Cisco AnyConnect, WireGuard, корпоративный) — либо отключить, либо поменять DNS-сервер на `1.1.1.1`/`8.8.8.8`.
- **CDN отдаёт из кэша** и время неадекватно мало — уже проставлен `Cache-Control: no-cache, no-store`, плюс URL типа `https://speed.cloudflare.com/__down?bytes=N` всегда генерирует свежие байты.
- **Файл слишком маленький** (< 100 KB) — замер шумный. Бери **≥ 5 MB**.
- **Файл слишком большой** и медленный канал — 10 × 100 MB может занять много минут. Ставь `-n 3` или бери файл поменьше.
- **PEP 668 «externally-managed environment»** при первом запуске — bootstrap автоматически попробует `--user` и `--break-system-packages`. Если всё равно не поставит — прогони вручную: `python3 -m pip install --user rich`.

---

## Публикация в git

Скрипт готов к пушу как есть. Внутри директории проекта:

```bash
git init -b main
git add .
git commit -m "Initial: speed_test.py + tests + docs"

# добавь свой remote
git remote add origin git@github.com:<user>/<repo>.git
git push -u origin main
```

Клонирующему достаточно:

```bash
git clone <url>
cd <repo>
python3 speed_test.py https://speed.cloudflare.com/__down?bytes=10000000
```

Rich доставится сам, никаких дополнительных шагов.
