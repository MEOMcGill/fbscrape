"""
Script to manually create a Facebook account and save cookies.

Usage:
    python tmp/create_account.py --output cookies.json

Set a breakpoint at the `breakpoint()` line, create your account in the browser,
then continue execution to save cookies.
"""

from fbscrape.utils import get_home_dir_path, get_device_os
from fbscrape.logger import logger

import argparse
import asyncio
import json
from camoufox.async_api import AsyncCamoufox
import os


async def main(output_path: str):

    # ensure the auth is there
    auth_path = os.path.join(get_home_dir_path(), "auth")
    if not os.path.exists(auth_path):
        os.makedirs(auth_path)
        logger.info(f"Created {auth_path} since didn't exist")

    # ensure output path is good
    full_output_path = os.path.join(get_home_dir_path(), "auth", output_path)
    if not output_path.endswith(".json"):
        full_output_path += ".json"
    if os.path.exists(full_output_path):
        raise FileExistsError(f"Output file {full_output_path} already exists.")
    logger.info(f"Saving storage state to {full_output_path}")

    current_os = get_device_os()
    logger.info(f"Detected OS: {current_os}")

    async with AsyncCamoufox(headless=False, os=current_os) as browser:
        context = await browser.new_context()
        page = await context.new_page()
        # To-do: Workaround for camoufox issue #473: br/zstd decompression broken
        await page.set_extra_http_headers({"Accept-Encoding": "gzip, deflate"})

        # Navigate to Facebook
        await page.goto("https://www.facebook.com")

        # Set breakpoint here - create your account manually, then continue
        logger.info("\n" + "="*60)
        logger.info("Browser is open. Create your account now.")
        logger.info("When done, press 'c' to save cookies and exit.")
        logger.info("="*60 + "\n")

        breakpoint()

        # Save cookies
        cookies = await context.storage_state(path=full_output_path)

        logger.info(f"Saved {len(cookies.get('cookies', []))} cookies to {full_output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create Facebook account and save cookies")
    parser.add_argument("--output", "-o", default="storage_state.json", help="Name of the output file to save cookies to. Default: storage_state.json")
    args = parser.parse_args()

    asyncio.run(main(args.output))