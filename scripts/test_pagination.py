#!/usr/bin/env python3
"""
Тест пагинации (page=N) для серверов где infinite scroll не работает.

Usage: uv run python scripts/test_pagination.py
"""
import asyncio
import sys
sys.path.insert(0, '.')

from app.services.parser import OzonParser


async def main():
    print("=" * 60)
    print("Pagination Test")
    print("=" * 60)

    async with OzonParser() as parser:
        page = await parser._new_page(block_resources=True)

        query = "брюки мужские"
        base_url = f"https://www.ozon.ru/search/?text={query}"

        total_products = 0

        for page_num in range(1, 6):  # Test 5 pages
            url = f"{base_url}&page={page_num}" if page_num > 1 else base_url
            print(f"\n📄 Page {page_num}: {url}")

            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)

            # Check for block
            title = await page.title()
            if "доступ ограничен" in title.lower():
                print("  ❌ BLOCKED!")
                break

            # Count products
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

            print(f"  📦 Products: {len(product_ids)}")
            total_products += len(product_ids)

            if len(product_ids) == 0:
                print("  ⚠️  No products - end of results or blocked")
                break

            # Show first few product IDs
            print(f"  IDs: {product_ids[:5]}...")

        print(f"\n{'='*60}")
        print(f"📊 Total products across {page_num} pages: {total_products}")

        await page.close()


if __name__ == "__main__":
    asyncio.run(main())
