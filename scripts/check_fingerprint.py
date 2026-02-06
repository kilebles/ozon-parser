#!/usr/bin/env python3
"""
Диагностический скрипт для проверки browser fingerprint.
Запусти локально и на сервере, сравни результаты.

Usage: uv run python scripts/check_fingerprint.py
"""
import asyncio
import json
import sys
sys.path.insert(0, '.')

from app.services.parser import OzonParser


FINGERPRINT_SCRIPT = """
() => {
    const fp = {};

    // Navigator
    fp.userAgent = navigator.userAgent;
    fp.platform = navigator.platform;
    fp.languages = navigator.languages;
    fp.hardwareConcurrency = navigator.hardwareConcurrency;
    fp.deviceMemory = navigator.deviceMemory;
    fp.webdriver = navigator.webdriver;
    fp.plugins = Array.from(navigator.plugins || []).map(p => p.name);

    // Screen
    fp.screenWidth = screen.width;
    fp.screenHeight = screen.height;
    fp.screenAvailWidth = screen.availWidth;
    fp.screenAvailHeight = screen.availHeight;
    fp.colorDepth = screen.colorDepth;
    fp.pixelDepth = screen.pixelDepth;
    fp.devicePixelRatio = window.devicePixelRatio;

    // Timezone
    fp.timezoneOffset = new Date().getTimezoneOffset();
    fp.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;

    // WebGL
    try {
        const canvas = document.createElement('canvas');
        const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
        if (gl) {
            const debugInfo = gl.getExtension('WEBGL_debug_renderer_info');
            if (debugInfo) {
                fp.webglVendor = gl.getParameter(debugInfo.UNMASKED_VENDOR_WEBGL);
                fp.webglRenderer = gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL);
            }
            fp.webglVersion = gl.getParameter(gl.VERSION);
            fp.webglMaxTextureSize = gl.getParameter(gl.MAX_TEXTURE_SIZE);
        }
    } catch (e) {
        fp.webglError = e.message;
    }

    // Canvas fingerprint
    try {
        const canvas = document.createElement('canvas');
        canvas.width = 200;
        canvas.height = 50;
        const ctx = canvas.getContext('2d');
        ctx.textBaseline = 'top';
        ctx.font = '14px Arial';
        ctx.fillStyle = '#f60';
        ctx.fillRect(0, 0, 200, 50);
        ctx.fillStyle = '#069';
        ctx.fillText('Browser Fingerprint', 2, 15);
        fp.canvasHash = canvas.toDataURL().slice(-50);  // Last 50 chars as sample
    } catch (e) {
        fp.canvasError = e.message;
    }

    // AudioContext
    try {
        const AudioContext = window.AudioContext || window.webkitAudioContext;
        if (AudioContext) {
            const ctx = new AudioContext();
            fp.audioSampleRate = ctx.sampleRate;
            fp.audioState = ctx.state;
            ctx.close();
        }
    } catch (e) {
        fp.audioError = e.message;
    }

    // Fonts (check common fonts)
    try {
        const testFonts = ['Arial', 'Times New Roman', 'Courier New', 'Georgia', 'Verdana', 'Comic Sans MS'];
        const baseFonts = ['monospace', 'sans-serif', 'serif'];
        const testString = 'mmmmmmmmmmlli';
        const testSize = '72px';

        const span = document.createElement('span');
        span.style.position = 'absolute';
        span.style.left = '-9999px';
        span.style.fontSize = testSize;
        span.innerHTML = testString;
        document.body.appendChild(span);

        const baseWidths = {};
        baseFonts.forEach(font => {
            span.style.fontFamily = font;
            baseWidths[font] = span.offsetWidth;
        });

        const detectedFonts = [];
        testFonts.forEach(font => {
            let detected = false;
            baseFonts.forEach(baseFont => {
                span.style.fontFamily = `'${font}', ${baseFont}`;
                if (span.offsetWidth !== baseWidths[baseFont]) {
                    detected = true;
                }
            });
            if (detected) detectedFonts.push(font);
        });

        document.body.removeChild(span);
        fp.fonts = detectedFonts;
    } catch (e) {
        fp.fontsError = e.message;
    }

    // Connection
    if (navigator.connection) {
        fp.connectionEffectiveType = navigator.connection.effectiveType;
        fp.connectionDownlink = navigator.connection.downlink;
        fp.connectionRtt = navigator.connection.rtt;
    }

    // Battery
    fp.hasBattery = 'getBattery' in navigator;

    // Touch
    fp.maxTouchPoints = navigator.maxTouchPoints;
    fp.touchSupport = 'ontouchstart' in window;

    // Chrome-specific
    fp.hasChrome = !!window.chrome;
    fp.chromeRuntime = !!(window.chrome && window.chrome.runtime);

    // Headless indicators
    fp.webdriverPresent = 'webdriver' in navigator;
    fp.automationControlled = !!(navigator.userAgent.match(/HeadlessChrome/));
    fp.phantomPresent = !!window.callPhantom || !!window._phantom;
    fp.nightmarePresent = !!window.__nightmare;
    fp.seleniumPresent = !!window._selenium || !!window.callSelenium || !!document.__selenium_unwrapped;
    fp.playwrightPresent = !!window.__playwright;

    // Document
    fp.documentHidden = document.hidden;
    fp.documentVisibilityState = document.visibilityState;

    return fp;
}
"""


async def main():
    print("=" * 60)
    print("Browser Fingerprint Diagnostic")
    print("=" * 60)

    async with OzonParser() as parser:
        page = await parser._new_page(block_resources=False)

        # Navigate to a simple page
        await page.goto("https://www.google.com", wait_until="domcontentloaded")
        await page.wait_for_timeout(1000)

        # Get fingerprint
        fingerprint = await page.evaluate(FINGERPRINT_SCRIPT)

        print("\n📋 FINGERPRINT RESULTS:\n")
        print(json.dumps(fingerprint, indent=2, ensure_ascii=False))

        # Highlight potential issues
        print("\n" + "=" * 60)
        print("⚠️  POTENTIAL ISSUES:")
        print("=" * 60)

        issues = []

        if fingerprint.get('webdriver') is True:
            issues.append("❌ navigator.webdriver = true (DETECTED!)")

        if fingerprint.get('playwrightPresent'):
            issues.append("❌ Playwright detected in window")

        if fingerprint.get('seleniumPresent'):
            issues.append("❌ Selenium traces detected")

        if 'HeadlessChrome' in str(fingerprint.get('userAgent', '')):
            issues.append("❌ HeadlessChrome in User-Agent")

        if fingerprint.get('automationControlled'):
            issues.append("❌ AutomationControlled detected")

        webgl_renderer = fingerprint.get('webglRenderer', '')
        if 'SwiftShader' in webgl_renderer or 'llvmpipe' in webgl_renderer:
            issues.append(f"⚠️  WebGL renderer looks like server: {webgl_renderer}")

        if not fingerprint.get('fonts') or len(fingerprint.get('fonts', [])) < 3:
            issues.append(f"⚠️  Few fonts detected: {fingerprint.get('fonts')} (server usually has limited fonts)")

        if fingerprint.get('hardwareConcurrency', 0) > 16:
            issues.append(f"⚠️  High CPU cores: {fingerprint.get('hardwareConcurrency')} (servers often have many)")

        platform = fingerprint.get('platform', '')
        ua = fingerprint.get('userAgent', '')
        if 'Linux' in platform and 'Windows' in ua:
            issues.append(f"⚠️  Platform/UA mismatch: platform={platform}, but UA says Windows")
        if 'Win32' in platform and 'Linux' in ua:
            issues.append(f"⚠️  Platform/UA mismatch: platform={platform}, but UA says Linux")

        if not fingerprint.get('hasChrome'):
            issues.append("⚠️  window.chrome missing (should be present in Chrome)")

        if fingerprint.get('documentHidden'):
            issues.append("⚠️  Document is hidden (headless indicator)")

        if issues:
            for issue in issues:
                print(issue)
        else:
            print("✅ No obvious issues detected!")

        await page.close()


if __name__ == "__main__":
    asyncio.run(main())
