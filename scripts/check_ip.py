#!/usr/bin/env python3
"""
Проверка IP адреса и его репутации.
Usage: uv run python scripts/check_ip.py
"""
import asyncio
import sys
sys.path.insert(0, '.')

from app.services.parser import OzonParser


async def main():
    print("=" * 60)
    print("IP & Network Diagnostic")
    print("=" * 60)

    async with OzonParser() as parser:
        page = await parser._new_page(block_resources=False)

        # Check IP via multiple services
        ip_services = [
            ("https://api.ipify.org?format=json", "ip"),
            ("https://ipinfo.io/json", None),  # Returns full object
        ]

        for url, key in ip_services:
            try:
                await page.goto(url, wait_until="domcontentloaded")
                content = await page.content()
                print(f"\n📍 {url}:")
                # Extract JSON from page
                import re
                import json
                match = re.search(r'\{[^}]+\}', content)
                if match:
                    data = json.loads(match.group())
                    print(json.dumps(data, indent=2))
            except Exception as e:
                print(f"Error: {e}")

        # Check if we can reach Ozon at all
        print("\n" + "=" * 60)
        print("🔍 Testing Ozon access...")
        print("=" * 60)

        try:
            response = await page.goto("https://www.ozon.ru", wait_until="domcontentloaded", timeout=30000)
            print(f"Status: {response.status if response else 'No response'}")

            title = await page.title()
            print(f"Title: {title}")

            # Check for block indicators
            content = await page.content()
            if "доступ ограничен" in content.lower():
                print("❌ BLOCKED: 'Доступ ограничен' found in page")
            elif "captcha" in content.lower() or "challenge" in content.lower():
                print("⚠️  CAPTCHA/Challenge detected")
            else:
                print("✅ Page loaded without obvious blocks")

            # Check URL (might redirect to challenge)
            print(f"Final URL: {page.url}")

        except Exception as e:
            print(f"❌ Error accessing Ozon: {e}")

        await page.close()


if __name__ == "__main__":
    asyncio.run(main())
