#!/usr/bin/env python3
"""
Тест скроллинга и загрузки товаров.
Запусти на сервере чтобы увидеть что происходит.

Usage: uv run python scripts/test_scroll.py
"""
import asyncio
import sys
sys.path.insert(0, '.')

from app.services.parser import OzonParser


async def main():
    print("=" * 60)
    print("Scroll & Product Loading Test")
    print("=" * 60)

    async with OzonParser() as parser:
        page = await parser._new_page(block_resources=True)

        # Go to search
        query = "брюки мужские"
        url = f"https://www.ozon.ru/search/?text={query}"
        print(f"\n🔍 Opening: {url}")

        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # Check page status
        title = await page.title()
        print(f"📄 Title: {title}")
        print(f"📍 URL: {page.url}")

        # Check for blocks
        if "доступ ограничен" in title.lower():
            print("❌ BLOCKED!")
            content = await page.content()
            print(f"Page content (first 500 chars): {content[:500]}")
            await page.close()
            return

        # Count products
        products = await page.query_selector_all("a[href*='/product/']")
        print(f"\n📦 Initial products found: {len(products)}")

        # Get page height
        height = await page.evaluate("document.body.scrollHeight")
        print(f"📏 Page height: {height}px")

        # Try scrolling
        print("\n🔄 Starting scroll test...")
        for i in range(10):
            # Scroll
            await page.evaluate("""
                () => {
                    const viewportHeight = window.innerHeight;
                    const currentScroll = window.scrollY;
                    window.scrollTo({ top: currentScroll + viewportHeight * 0.8, behavior: 'instant' });
                }
            """)

            await page.wait_for_timeout(1500)

            # Count products again
            products = await page.query_selector_all("a[href*='/product/']")
            new_height = await page.evaluate("document.body.scrollHeight")
            scroll_pos = await page.evaluate("window.scrollY")

            print(f"  Scroll #{i+1}: products={len(products)}, height={new_height}px, scrollY={scroll_pos:.0f}")

            # Check if we're at the bottom
            at_bottom = await page.evaluate("""
                () => window.scrollY + window.innerHeight >= document.body.scrollHeight - 100
            """)
            if at_bottom and new_height == height:
                print("  ⚠️  At bottom, no new content loading")

            height = new_height

        # Final check
        print("\n" + "=" * 60)
        products = await page.query_selector_all("a[href*='/product/']")
        print(f"📦 Final product count: {len(products)}")

        # Check for any error messages on page
        error_el = await page.query_selector("[class*='error'], [class*='Error'], [class*='empty']")
        if error_el:
            error_text = await error_el.inner_text()
            print(f"⚠️  Found error/empty element: {error_text[:200]}")

        # Check network - are XHR requests being made?
        print("\n🔍 Checking if lazy load triggers are present...")
        has_observer = await page.evaluate("""
            () => {
                // Check if IntersectionObserver is being used
                return typeof IntersectionObserver !== 'undefined';
            }
        """)
        print(f"  IntersectionObserver available: {has_observer}")

        # Get all product IDs
        product_ids = await page.evaluate("""
            () => {
                const ids = [];
                const links = document.querySelectorAll('a[href*="/product/"]');
                for (const link of links) {
                    const href = link.getAttribute('href');
                    if (!href || href.includes('/reviews') || href.includes('/questions')) continue;
                    const match = href.match(/\\/product\\/[^?]*-(\\d+)/);
                    if (match && !ids.includes(match[1])) ids.push(match[1]);
                }
                return ids;
            }
        """)
        print(f"\n📋 Unique product IDs ({len(product_ids)}): {product_ids[:10]}...")

        await page.close()


if __name__ == "__main__":
    asyncio.run(main())
