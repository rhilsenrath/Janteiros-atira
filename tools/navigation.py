"""Observation helper only. Future models choose actions from these observations."""
async def observe(page) -> str:
    return await page.locator("body").inner_text()
