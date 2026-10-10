import asyncio
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from playwright.async_api import async_playwright
from tools.navigation import TABLE_SCRIPT

async def main():
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True)
        try:
            page=await browser.new_page()
            await page.set_content('''<table><tr><th>Reference</th><th>Bore</th><th>Stroke</th><th>Pressure</th></tr>
              <tr><td>Fixture</td><td><svg aria-label="Bore: 42"><title>42 mm</title></svg></td><td>123</td><td>456</td></tr>
              <tr hidden><td>Obsolete</td><td>9</td><td>9</td><td>9</td></tr></table>''')
            text=await page.locator('table').evaluate_all(TABLE_SCRIPT)
            assert 'Stroke: 123 | Pressure: 456' in text, text
            assert 'Bore: Bore: 42 42 mm' in text, text
            assert 'Obsolete' not in text, text
            print('PASS: table columns and visible SVG labels preserved; hidden rows excluded')
        finally:
            await browser.close()

asyncio.run(main())
