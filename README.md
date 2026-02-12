# Ozon Position Tracker

Отслеживает позиции товаров в поиске Ozon и записывает в Google Sheets.

## Установка

```bash
# Зависимости
uv sync

# Браузер (первый раз)
uv run playwright install chromium
```

## Настройка

1. **Google Sheets** — положить `credentials.json` (Service Account) в корень проекта

2. **`.env`** — создать файл:
```env
GOOGLE_SPREADSHEET_IDS=id1,id2,id3
LOG_LEVEL=INFO
```

ID таблицы — из URL: `docs.google.com/spreadsheets/d/[ЭТО_ID]/edit`

## Формат таблицы

Лист должен называться "Позиции":

| A (артикул) | B (название) | C (запрос) | D+ (результаты) |
|-------------|--------------|------------|-----------------|
| 123456789   | Товар 1      |            |                 |
|             |              | запрос 1   | 15              |
|             |              | запрос 2   | 1000+           |
| 987654321   | Товар 2      |            |                 |
|             |              | запрос 3   | 42              |

## Запуск

```bash
# Один раз
uv run python app/main.py --once

# По расписанию (каждые 2 часа)
uv run python app/main.py

# Дневной итог вручную
uv run python app/main.py --summary
```

## Логи

Пишутся в `logs/YYYY-MM-DD_HH-MM-SS.log`

## Опции .env

```env
GOOGLE_SPREADSHEET_IDS=id1,id2,id3  # таблицы через запятую
LOG_LEVEL=INFO                       # DEBUG для подробностей
BROWSER_HEADLESS=true                # false для отладки
BOT_TOKEN=...                        # Telegram уведомления (опционально)
```
