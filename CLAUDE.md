# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Ozon-parser is a Python-based web scraping tool that tracks product positions on Ozon (Russian e-commerce platform) and logs results to Google Sheets. Uses async Playwright for browser automation.

## Commands

```bash
# Install dependencies
uv sync

# Install Playwright browsers (required first time)
uv run playwright install

# Run the position tracker (original)
uv run python app/main.py

# Run with Playwright enhanced anti-bot (recommended for servers)
uv run python app/main_playwright.py

# Run with Selenium (Chrome/Edge) enhanced anti-bot
uv run python app/main_selenium.py

# Run with visible browser (debugging)
BROWSER_HEADLESS=false uv run python app/main.py

# Add/remove dependencies
uv add <package>
uv remove <package>
```

No test or lint commands are currently configured.

## Architecture

```
app/
├── main.py              # Entry point - runs position tracker (original)
├── main_playwright.py   # Entry point - Playwright with enhanced anti-bot
├── main_selenium.py     # Entry point - Selenium (Chrome/Edge) with enhanced anti-bot
├── settings.py          # Pydantic Settings config (.env support)
├── schemas/product.py   # Product Pydantic model
└── services/
    ├── parser.py             # OzonParser - Original Playwright automation
    ├── parser_playwright.py  # OzonParserPlaywright - Enhanced Playwright with stealth
    ├── parser_selenium.py    # OzonParserSelenium - Selenium with stealth
    ├── sheets.py             # GoogleSheetsService - Google Sheets API
    └── position_tracker.py   # Orchestrates tracking tasks
```

**Execution flow:** `main.py` → connects to Google Sheets → launches Playwright browser → reads search tasks from sheet → finds product positions on Ozon → writes results to daily columns.

## Key Patterns

- **Async/await throughout** - All browser operations use async Playwright
- **OzonParser as async context manager** - Use `async with OzonParser(...) as parser:`
- **Persistent browser context** - Session data stored in `browser_data/`
- **Captcha handling** - Parser detects captchas and waits 60s for manual solving
- **Anti-detection** - Custom user agent, viewport, timezone, Moscow geolocation

## Configuration

Environment variables via `.env`:
- `GOOGLE_SPREADSHEET_ID` - Target Google Sheets spreadsheet ID

Settings in `app/settings.py`:
- `browser_headless` (default: True)
- `browser_timeout` (default: 30000ms)
- `google_credentials_path` (default: credentials.json)

## Required Files (gitignored)

- `credentials.json` - Google Service Account credentials
- `.env` - Environment variables with spreadsheet ID
