#!/usr/bin/env python3
"""
Перехватывает сетевые запросы при скролле чтобы понять
почему infinite scroll не работает через прокси.

Usage: uv run python scripts/debug_network.py
"""
import asyncio
import sys
sys.path.insert(0, '.')

from app.services.parser import OzonParser


async def main():
    print("=" * 60)
    print("Network Debug - Infinite Scroll Analysis")
    print("=" * 60)

    async with OzonParser() as parser:
        # Don't block resources for this test
        page = await parser._new_page(block_resources=False)

        # Collect XHR/Fetch requests
        api_requests = []
        failed_requests = []

        def on_request(request):
            if request.resource_type in ('xhr', 'fetch'):
                url = request.url
                # Filter interesting requests
                if any(kw in url for kw in ['search', 'product', 'graphql', 'api', 'catalog']):
                    api_requests.append({
                        'url': url[:150],
                        'method': request.method,
                        'type': request.resource_type
                    })

        def on_response(response):
            if response.request.resource_type in ('xhr', 'fetch'):
                url = response.url
                status = response.status
                if status >= 400 or any(kw in url for kw in ['search', 'product', 'graphql', 'api', 'catalog']):
                    if status >= 400:
                        failed_requests.append({
                            'url': url[:100],
                            'status': status
                        })

        page.on('request', on_request)
        page.on('response', on_response)

        # Go to search page
        query = "брюки мужские"
        url = f"https://www.ozon.ru/search/?text={query}"
        print(f"\n🔍 Opening: {url}")

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)  # Wait for JS to load
        print(f"📄 Title: {await page.title()}")
        print(f"📍 Final URL: {page.url}")

        # Initial products
        initial_count = await page.evaluate("""
            () => document.querySelectorAll('a[href*="/product/"]').length
        """)
        print(f"\n📦 Initial products: {initial_count}")

        print(f"\n📡 API requests on page load: {len(api_requests)}")
        for req in api_requests[-5:]:  # Last 5
            print(f"   {req['method']} {req['url'][:80]}...")

        # Clear and scroll
        api_requests.clear()
        failed_requests.clear()

        print("\n" + "=" * 60)
        print("🔄 Scrolling to trigger lazy load...")
        print("=" * 60)

        for i in range(5):
            # Scroll down
            await page.evaluate("""
                () => window.scrollTo(0, document.body.scrollHeight)
            """)

            # Wait for potential network activity
            await page.wait_for_timeout(2000)

            # Check products
            count = await page.evaluate("""
                () => document.querySelectorAll('a[href*="/product/"]').length
            """)

            print(f"\n  Scroll #{i+1}: products={count}")
            print(f"  API requests since last scroll: {len(api_requests)}")

            if api_requests:
                for req in api_requests:
                    print(f"    → {req['method']} {req['url'][:70]}...")
                api_requests.clear()

            if failed_requests:
                print(f"  ❌ Failed requests:")
                for req in failed_requests:
                    print(f"    → {req['status']} {req['url']}")
                failed_requests.clear()

        # Check console errors
        print("\n" + "=" * 60)
        print("🔍 Checking page state...")
        print("=" * 60)

        # Check if there's a "load more" button or pagination
        has_pagination = await page.evaluate("""
            () => {
                const pagination = document.querySelector('[class*="pagination"], [class*="Pagination"], a[href*="page="]');
                return !!pagination;
            }
        """)
        print(f"  Has pagination element: {has_pagination}")

        # Check if infinite scroll container exists
        scroll_container = await page.evaluate("""
            () => {
                // Look for common infinite scroll patterns
                const possibleContainers = document.querySelectorAll('[class*="infinite"], [class*="Infinite"], [data-widget="searchResultsV2"]');
                return possibleContainers.length;
            }
        """)
        print(f"  Infinite scroll containers found: {scroll_container}")

        # Check for any "show more" type buttons
        show_more = await page.evaluate("""
            () => {
                const buttons = document.querySelectorAll('button, a');
                for (const btn of buttons) {
                    const text = btn.innerText.toLowerCase();
                    if (text.includes('показать ещё') || text.includes('загрузить') || text.includes('больше')) {
                        return btn.innerText;
                    }
                }
                return null;
            }
        """)
        if show_more:
            print(f"  Found 'show more' button: {show_more}")

        await page.close()


if __name__ == "__main__":
    asyncio.run(main())
