# Internet Speed Tester

Скрипт для замера пропускной способности интернет-канала. Выполняет N последовательных HTTP GET-запросов к указанному URL, замеряет время и объём каждого ответа, выводит агрегированную скорость.

## Требования

Python 3.7 или новее. Работает на macOS, Linux, Windows. Зависимость `rich` устанавливается автоматически при первом запуске скрипта; при необходимости её можно поставить заранее.

## Установка

```bash
git clone <url>
cd <каталог проекта>
```

Опционально:

```bash
python3 -m pip install -r requirements.txt
```

## Запуск

```bash
python3 speed_test.py https://speed.cloudflare.com/__down?bytes=10000000
```

Опции:

```
-n, --count N   количество запросов (по умолчанию 10)
-h, --help      справка
```

При запуске без URL скрипт запросит его интерактивно.

## Тесты

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
```

Если в окружении присутствуют несовместимые pytest-плагины от других пакетов, отключите автозагрузку:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest
```

## Структура проекта

```
speed_test.py            основной модуль
tests/                   pytest-набор
requirements.txt         runtime-зависимости
requirements-dev.txt     зависимости для разработки и тестов
pytest.ini               конфигурация pytest
```

## Устройство кода

Модуль `speed_test.py` разделён на девять секций.

### 1. Bootstrap

Функция `_bootstrap` проверяет наличие модуля `rich`. При его отсутствии последовательно выполняются попытки установки:

1. `pip install rich`
2. `pip install --user rich`
3. `pip install --break-system-packages rich`

Вывод `pip` захватывается через `capture_output`. При успехе вызывается `_refresh_import_paths`, добавляющая пользовательский `site-packages` в `sys.path` и сбрасывающая кэш импортов. При неудаче всех трёх попыток выводится журнал `pip` и `sys.exit(1)`.

Bootstrap выполняется до импорта модулей `rich`.

### 2. Импорты

Стандартная библиотека: `urllib.request`, `urllib.error`, `urllib.parse`, `ssl`, `time`, `statistics`, `dataclasses`, `argparse`, `re`. Из `rich` импортируются `box`, `Align`, `Console`, `Group`, `Live`, `Panel`, `Progress`, `Table`, `Text` и колонки прогресс-виджета.

### 3. Константы

Параметры вынесены в константы уровня модуля:

```
DEFAULT_REQUEST_COUNT   количество запросов по умолчанию
REQUEST_TIMEOUT_SEC     таймаут одного запроса
CHUNK_SIZE              размер порции чтения из сокета
UI_REFRESH_HZ           частота обновления UI
TABLE_GAUGE_WIDTH       ширина мини-гейджа в таблице
FINAL_GAUGE_WIDTH       ширина гейджа в финальной панели
SPARKLINE_WIDTH         ширина sparkline
ERROR_MSG_MAX_LEN       максимальная длина сообщения об ошибке
SPARK_CHARS             набор юникод-символов для sparkline
USER_AGENT              значение заголовка User-Agent
```

### 4. RequestResult

Frozen-датакласс, представляющий результат одного запроса. Поля: `ok`, `elapsed_sec`, `n_bytes`, `status`, `error`. Свойство `speed_bps` вычисляет байты в секунду; возвращает `0.0` при `ok=False` либо `elapsed_sec <= 0`.

### 5. Форматирующие хелперы

```
humanize_bytes(n)           байты в строку с единицей измерения
humanize_speed(bps)         скорость в MB/s либо KB/s
sparkline(values, width)    последовательность юникод-блоков
gauge_bar(value, best, ...) горизонтальный бар value/best
rate_speed(bps)             классификация скорости и число точек 1..5
speed_dots(filled)          отрисовка N/5 заполненных точек
_clean_url_error(reason)    нормализация сообщения URLError
```

Все функции чистые, без побочных эффектов.

### 6. HTTP-слой

`download_once(url, timeout)` выполняет один HTTP-запрос:

* `ssl.create_default_context` использует системные корневые сертификаты;
* заголовки: `User-Agent`, `Accept: */*`, `Cache-Control: no-cache, no-store`;
* замер `time.perf_counter` охватывает интервал от `urlopen` до полного дренажа тела и закрытия соединения;
* чтение по `CHUNK_SIZE` с подсчётом принятых байт;
* `resp.getcode()` для получения HTTP-статуса.

Функция возвращает `(elapsed_seconds, bytes_downloaded, http_status)`. Исключения не перехватываются.

`perform_measurement(url)` оборачивает результат в `RequestResult`. Различаются три категории ошибок:

```
urllib.error.HTTPError   → RequestResult(ok=False, error="HTTP <code>")
urllib.error.URLError    → error проходит через _clean_url_error
OSError                  → сохраняется первая строка сообщения (socket.timeout,
                           ConnectionResetError и др.)
```

### 7. UI-компоненты

Все функции чистые: принимают состояние, возвращают renderable-объект `rich`.

```
build_header(url, count)              заголовочная панель
build_results_table(rows)             таблица запросов с мини-гейджами
build_stats_panel(rows)               панель live-статистики со sparkline
render_scene(url, rows, progress, count)  композит через rich.console.Group
```

При каждом обновлении `render_scene` пересобирает сцену; `rich.Live` выполняет diff терминала.

### 8. Финальный отчёт

`print_final_summary(url, rows, count)` вычисляет:

```
total_bytes = sum(r.n_bytes    for r in ok)
total_time  = sum(r.elapsed_sec for r in ok)
avg_bps     = total_bytes / total_time
avg_time    = mean(r.elapsed_sec)
med_time    = median(r.elapsed_sec)
best_bps    = max(speed_bps)
worst_bps   = min(speed_bps)
```

Используется агрегированное деление, а не арифметическое среднее по скоростям. При равных по размеру запросах результат совпадает с `mean(speeds)`, но остаётся корректным при разбросе размеров ответов.

При отсутствии успешных запросов выводится красная панель с сообщением о проверке URL и сети.

### 9. Оркестрация

```
resolve_url(cli_url)     возвращает валидированный URL либо None
_make_progress()         фабрика rich.progress.Progress
run_measurements(url, n) цикл замеров внутри rich.Live
parse_args(argv)         argparse.Namespace
main(argv)               parse_args → resolve_url → run_measurements → summary
```

Валидация URL: схема `http` или `https`, непустой `netloc` (`urllib.parse.urlparse`). `KeyboardInterrupt` внутри цикла перехватывается; уже собранные результаты передаются в финальный отчёт.

## Формулы

Скорость одного запроса:

```
speed_i = n_bytes_i / elapsed_sec_i
```

Агрегированная средняя скорость:

```
avg_bps = sum(n_bytes_i) / sum(elapsed_sec_i)
```

Минимум и максимум:

```
best_bps  = max(speed_i)
worst_bps = min(speed_i)
```

Замер `elapsed_sec_i` включает разрешение DNS, установку TCP-соединения, TLS-хендшейк, отправку запроса, приём ответа и закрытие соединения.

## Обработка ошибок

| Ситуация | Отображение |
|---|---|
| Ошибка разрешения DNS | `✗ err`, очищенное сообщение |
| Отказ соединения | `✗ err`, очищенное сообщение |
| HTTP 4xx или 5xx | `✗ err`, `HTTP <code>` |
| Таймаут | `✗ err`, `timed out` |
| Ошибка TLS | `✗ err`, `certificate verify failed` и подобные |
| `Ctrl-C` | Прерывание цикла, финальный отчёт по уже собранным данным |
| Все запросы неуспешны | Красная финальная панель без числовых показателей |

## Тестирование

Набор из 57 тестов в `tests/test_speed_test.py`. HTTP-интеграция использует фикстуру `http_server`, запускающую `http.server.ThreadingHTTPServer` на `127.0.0.1` на случайном свободном порту. Внешняя сеть не требуется.

Покрытие:

* форматирующие хелперы: границы, пороги, длины, диапазоны символов;
* `RequestResult`: значения по умолчанию, вычисление `speed_bps`, frozen-инвариант;
* HTTP-слой: успешный запрос, `HTTPError`, отказ соединения;
* CLI: валидные и невалидные URL, интерактивный prompt через `monkeypatch`;
* оркестрация: `run_measurements` и `main`, коды завершения.

## Публикация в git

```bash
git init -b main
git add .
git commit -m "Initial commit"
git remote add origin <url>
git push -u origin main
```

## Возможные проблемы

DNS-запросы возвращают ошибку за нулевое время: выполните `nslookup google.com`. Частая причина — активный VPN, перехватывающий разрешение имён. Решение: отключить VPN либо указать публичные DNS-серверы (`1.1.1.1`, `8.8.8.8`).

Файл менее 100 KB: замер зашумлён накладными расходами хендшейка, буферизацией сокета и TCP slow-start. Для устойчивого результата используйте файл от 5 MB.

Файл слишком велик для канала: длительное выполнение. Уменьшите `-n` либо выберите файл меньшего размера.

PEP 668 `externally-managed environment`: bootstrap последовательно пробует `--user` и `--break-system-packages`. При окончательной неудаче требуется ручная установка:

```bash
python3 -m pip install --user rich
```
