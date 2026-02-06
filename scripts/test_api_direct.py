#!/usr/bin/env python3
"""
Пробуем вызвать API загрузки товаров напрямую.
"""
import asyncio
import json
import sys
sys.path.insert(0, '.')

from app.services.parser import OzonParser


async def main():
    print("=" * 60)
    print("Direct API Call Test")
    print("=" * 60)

    async with OzonParser() as parser:
        page = await parser._new_page(block_resources=False)

        # Go to search page first
        query = "брюки мужские"
        url = f"https://www.ozon.ru/search/?text={query}"
        print(f"\n🔍 Opening: {url}")

        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        final_url = page.url
        print(f"📍 Final URL: {final_url}")

        # Get initial products
        initial = await page.evaluate("""
            () => document.querySelectorAll('a[href*="/product/"]').length
        """)
        print(f"📦 Initial products: {initial}")

        # Try to manually trigger the API that loads more products
        print("\n🔄 Trying to manually call lazy load API...")

        # Method 1: Dispatch scroll event
        result1 = await page.evaluate("""
            () => {
                window.scrollTo(0, document.body.scrollHeight);
                window.dispatchEvent(new Event('scroll'));
                return 'scroll dispatched';
            }
        """)
        await page.wait_for_timeout(2000)

        count1 = await page.evaluate("() => document.querySelectorAll('a[href*=\"/product/\"]').length")
        print(f"  After scroll event: {count1} products")

        # Method 2: Find and trigger IntersectionObserver manually
        result2 = await page.evaluate("""
            () => {
                // Find all elements that might be observed for lazy loading
                const sentinels = document.querySelectorAll('[class*="sentinel"], [class*="Sentinel"], [class*="loader"], [class*="Loader"], [data-widget*="search"]');
                return sentinels.length;
            }
        """)
        print(f"  Sentinel/loader elements found: {result2}")

        # Method 3: Try clicking "show more" if exists
        show_more = await page.evaluate("""
            () => {
                const buttons = [...document.querySelectorAll('button, a, div[role="button"]')];
                for (const btn of buttons) {
                    const text = (btn.innerText || '').toLowerCase();
                    if (text.includes('показать ещё') || text.includes('загрузить ещё') || text.includes('показать больше')) {
                        btn.click();
                        return text;
                    }
                }
                return null;
            }
        """)
        if show_more:
            print(f"  Clicked button: {show_more}")
            await page.wait_for_timeout(2000)
            count = await page.evaluate("() => document.querySelectorAll('a[href*=\"/product/\"]').length")
            print(f"  After click: {count} products")

        # Method 4: Check what data-widget attributes exist (Ozon uses widgets)
        widgets = await page.evaluate("""
            () => {
                const widgets = document.querySelectorAll('[data-widget]');
                const names = new Set();
                widgets.forEach(w => names.add(w.getAttribute('data-widget')));
                return [...names];
            }
        """)
        print(f"\n📋 Data-widgets on page: {widgets[:20]}")

        # Method 5: Check if there's pagination in URL or page
        has_page_param = 'page=' in final_url
        print(f"\n  URL has page= param: {has_page_param}")

        # Try adding page parameter
        if not has_page_param:
            print("\n🔄 Trying page=2 parameter...")
            page2_url = final_url + ("&" if "?" in final_url else "?") + "page=2"
            print(f"  URL: {page2_url[:80]}...")

            await page.goto(page2_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            count_p2 = await page.evaluate("() => document.querySelectorAll('a[href*=\"/product/\"]').length")
            print(f"  Products on page 2: {count_p2}")

            # Check if these are different products
            ids_p2 = await page.evaluate("""
                () => {
                    const ids = [];
                    document.querySelectorAll('a[href*="/product/"]').forEach(a => {
                        const match = a.href.match(/product\\/[^?]*-(\\d+)/);
                        if (match) ids.push(match[1]);
                    });
                    return [...new Set(ids)].slice(0, 5);
                }
            """)
            print(f"  Product IDs on page 2: {ids_p2}")

        await page.close()


if __name__ == "__main__":
    asyncio.run(main())
