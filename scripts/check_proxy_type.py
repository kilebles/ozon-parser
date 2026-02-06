#!/usr/bin/env python3
"""
Проверяет тип IP (датацентр/резидентный) и скорость через прокси.
"""
import asyncio
import time
import sys
sys.path.insert(0, '.')

from app.services.parser import OzonParser


async def main():
    print("=" * 60)
    print("Proxy IP Type & Speed Check")
    print("=" * 60)

    async with OzonParser() as parser:
        page = await parser._new_page(block_resources=False)

        # Check IP info
        start = time.time()
        await page.goto("https://ipinfo.io/json", wait_until="domcontentloaded")
        ipinfo_time = time.time() - start
        content = await page.content()

        import re
        import json
        match = re.search(r'\{[^}]+\}', content)
        if match:
            data = json.loads(match.group())
            print(f"\n📍 IP: {data.get('ip')}")
            print(f"🏢 Org: {data.get('org')}")
            print(f"🌍 Country: {data.get('country')}")
            print(f"🏙️  City: {data.get('city')}")

            org = data.get('org', '').lower()
            country = data.get('country', '').upper()

            # Detect datacenter keywords
            datacenter_keywords = ['hosting', 'vps', 'server', 'cloud', 'data center',
                                   'datacenter', 'hetzner', 'ovh', 'digitalocean',
                                   'amazon', 'google', 'microsoft', 'linode', 'vultr']

            is_datacenter = any(kw in org for kw in datacenter_keywords)
            is_russia = country == 'RU'

            print(f"\n{'❌ DATACENTER IP' if is_datacenter else '✅ Possibly residential'}")
            print(f"{'✅ Russian IP' if is_russia else '❌ NOT Russian IP - Ozon will limit functionality!'}")

            if not is_russia:
                print("\n⚠️  Для полного функционала Ozon нужен российский IP!")

        # Speed tests
        print("\n" + "=" * 60)
        print("⏱️  Speed Tests")
        print("=" * 60)

        # Test 1: ipinfo (already done)
        print(f"\n  ipinfo.io:     {ipinfo_time*1000:.0f}ms")

        # Test 2: Google
        start = time.time()
        await page.goto("https://www.google.com", wait_until="domcontentloaded")
        google_time = time.time() - start
        print(f"  google.com:    {google_time*1000:.0f}ms")

        # Test 3: Ozon homepage
        start = time.time()
        await page.goto("https://www.ozon.ru", wait_until="domcontentloaded")
        ozon_time = time.time() - start
        print(f"  ozon.ru:       {ozon_time*1000:.0f}ms")

        # Test 4: Ozon search
        start = time.time()
        await page.goto("https://www.ozon.ru/search/?text=test", wait_until="domcontentloaded")
        search_time = time.time() - start
        print(f"  ozon search:   {search_time*1000:.0f}ms")

        # Test 5: Multiple requests (latency consistency)
        print("\n  Latency test (5 requests to google):")
        latencies = []
        for i in range(5):
            start = time.time()
            await page.goto("https://www.google.com/search?q=" + str(i), wait_until="domcontentloaded")
            lat = (time.time() - start) * 1000
            latencies.append(lat)
            print(f"    #{i+1}: {lat:.0f}ms")

        avg_latency = sum(latencies) / len(latencies)
        print(f"\n  Average latency: {avg_latency:.0f}ms")

        # Summary
        print("\n" + "=" * 60)
        print("📊 Summary")
        print("=" * 60)
        print(f"  Country: {data.get('country')} {'✅' if is_russia else '❌ (need RU)'}")
        print(f"  Type: {'Datacenter ❌' if is_datacenter else 'Residential ✅'}")
        print(f"  Avg latency: {avg_latency:.0f}ms {'✅' if avg_latency < 500 else '⚠️ slow' if avg_latency < 1000 else '❌ very slow'}")

        if not is_russia:
            print("\n💡 Рекомендация: используй российские резидентные/мобильные прокси")

        await page.close()


if __name__ == "__main__":
    asyncio.run(main())
