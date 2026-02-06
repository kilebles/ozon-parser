#!/usr/bin/env python3
"""
Бенчмарк для диагностики скорости.
Запусти локально и на сервере, сравни.

Usage: uv run python scripts/benchmark.py
"""
import asyncio
import time
import sys
sys.path.insert(0, '.')

from app.services.parser import OzonParser


async def main():
    print("=" * 60)
    print("Performance Benchmark")
    print("=" * 60)

    # 1. Browser launch time
    print("\n⏱️  Browser launch...")
    start = time.time()
    async with OzonParser() as parser:
        launch_time = time.time() - start
        print(f"   Browser launched in {launch_time:.2f}s")

        # 2. Page creation time
        print("\n⏱️  Page creation...")
        start = time.time()
        page = await parser._new_page(block_resources=True)
        page_time = time.time() - start
        print(f"   Page created in {page_time:.2f}s")

        # 3. Navigation time (to simple page)
        print("\n⏱️  Navigation to google.com...")
        start = time.time()
        await page.goto("https://www.google.com", wait_until="domcontentloaded")
        nav_time = time.time() - start
        print(f"   Loaded in {nav_time:.2f}s")

        # 4. Navigation to Ozon
        print("\n⏱️  Navigation to ozon.ru...")
        start = time.time()
        await page.goto("https://www.ozon.ru", wait_until="domcontentloaded")
        ozon_time = time.time() - start
        print(f"   Loaded in {ozon_time:.2f}s")

        # 5. Search page
        print("\n⏱️  Search page load...")
        start = time.time()
        await page.goto("https://www.ozon.ru/search/?text=test", wait_until="domcontentloaded")
        search_time = time.time() - start
        print(f"   Loaded in {search_time:.2f}s")

        # 6. JavaScript execution speed
        print("\n⏱️  JavaScript execution (1000 iterations)...")
        start = time.time()
        for _ in range(100):
            await page.evaluate("() => document.querySelectorAll('a').length")
        js_time = time.time() - start
        print(f"   100 JS calls in {js_time:.2f}s ({js_time*10:.0f}ms per call)")

        # 7. Product extraction (optimized)
        print("\n⏱️  Product extraction (optimized)...")
        await page.goto("https://www.ozon.ru/search/?text=брюки", wait_until="domcontentloaded")
        await page.wait_for_timeout(1000)

        start = time.time()
        for _ in range(10):
            await page.evaluate("""
                (seen) => {
                    const seenSet = new Set(seen);
                    const ids = [];
                    const links = document.getElementsByTagName('a');
                    for (let i = 0; i < links.length; i++) {
                        const href = links[i].href;
                        if (!href || !href.includes('/product/')) continue;
                        if (href.includes('/reviews') || href.includes('/questions')) continue;
                        const productIdx = href.indexOf('/product/');
                        if (productIdx === -1) continue;
                        const afterProduct = href.substring(productIdx + 9);
                        const queryIdx = afterProduct.indexOf('?');
                        const path = queryIdx > -1 ? afterProduct.substring(0, queryIdx) : afterProduct;
                        const lastDash = path.lastIndexOf('-');
                        if (lastDash === -1) continue;
                        const id = path.substring(lastDash + 1).replace(/\\/$/, '');
                        if (!/^\\d+$/.test(id)) continue;
                        if (!seenSet.has(id) && !ids.includes(id)) {
                            ids.push(id);
                        }
                    }
                    return ids;
                }
            """, [])
        extract_time = time.time() - start
        print(f"   10 extractions in {extract_time:.2f}s ({extract_time*100:.0f}ms per extraction)")

        await page.close()

    print("\n" + "=" * 60)
    print("Summary:")
    print(f"  Browser launch:  {launch_time:.2f}s")
    print(f"  Page creation:   {page_time:.2f}s")
    print(f"  Google nav:      {nav_time:.2f}s")
    print(f"  Ozon nav:        {ozon_time:.2f}s")
    print(f"  Search nav:      {search_time:.2f}s")
    print(f"  JS execution:    {js_time*10:.0f}ms/call")
    print(f"  Product extract: {extract_time*100:.0f}ms/call")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
