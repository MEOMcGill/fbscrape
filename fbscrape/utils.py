"""
Utility functions for Facebook scraping
"""
import base64
import dataclasses
import os
import platform
from pathlib import Path
import re
import requests
from datetime import datetime, timedelta, timezone
import json
import asyncio

from browserforge.fingerprints import Fingerprint, FingerprintGenerator
from browserforge.fingerprints.generator import ScreenFingerprint, NavigatorFingerprint, VideoCard


def get_device_os() -> str:
    """
    Detect the current OS and return the Camoufox os parameter value.

    Returns:
        "macos", "windows", or "linux"
    """
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    elif system == "windows":
        return "windows"
    else:
        return "linux"


def generate_fingerprint(os_name: str | None = None) -> Fingerprint:
    """Generate a Firefox-based browserforge Fingerprint for the given OS.

    Camoufox is Firefox-based and rejects non-Firefox fingerprints, so we
    constrain `browser` to firefox. Defaults to the current host OS.
    """
    os_name = os_name or get_device_os()
    return FingerprintGenerator(browser=("firefox",), os=(os_name,)).generate()


def serialize_fingerprint(fp: Fingerprint) -> str:
    """Serialize a Fingerprint to a JSON string (storable in SQLite TEXT)."""
    return json.dumps(dataclasses.asdict(fp))


def deserialize_fingerprint(s: str) -> Fingerprint:
    """Rehydrate a Fingerprint from its serialized JSON.

    Nested dataclass fields (`screen`, `navigator`, `videoCard`) must be
    reconstructed explicitly — camoufox does attribute access on them
    (e.g. `fingerprint.navigator.userAgent`), so leaving them as dicts
    produces an AttributeError at launch time.
    """
    data = json.loads(s)
    data["screen"] = ScreenFingerprint(**data["screen"])
    data["navigator"] = NavigatorFingerprint(**data["navigator"])
    if data.get("videoCard") is not None:
        data["videoCard"] = VideoCard(**data["videoCard"])
    return Fingerprint(**data)


def fingerprint_os(fp: Fingerprint) -> str | None:
    """Infer which OS a Fingerprint was generated for from `navigator.userAgent`.

    Returns 'macos' / 'windows' / 'linux' or None if the UA is unrecognized.
    """
    ua = (fp.navigator.userAgent or "").lower()
    if "macintosh" in ua or "mac os" in ua:
        return "macos"
    if "windows" in ua:
        return "windows"
    if "linux" in ua or "x11" in ua:
        return "linux"
    return None


class utc:
    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def from_iso(iso: str) -> datetime:
        return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)

    @staticmethod
    def ts() -> int:
        return int(utc.now().timestamp())


def parse_cookies(val: str) -> list[dict]:
    """
    Parse cookies from various formats into Playwright cookie format.

    Returns:
        List of cookie dicts in Playwright format (with domain, expires, httpOnly, etc.)
    """
    try:
        val = base64.b64decode(val).decode()
    except Exception:
        pass

    try:
        try:
            res = json.loads(val)
            if isinstance(res, dict) and "cookies" in res:
                res = res["cookies"]

            if isinstance(res, list):
                # Already in Playwright format (list of cookie dicts)
                return res
            if isinstance(res, dict):
                # Simple name-value dict, convert to Playwright format
                return [
                    {
                        "name": name,
                        "value": value,
                        "domain": ".facebook.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                        "sameSite": "None"
                    }
                    for name, value in res.items()
                ]
        except json.JSONDecodeError:
            # Cookie string format: "name1=value1; name2=value2"
            res = val.split("; ")
            res = [x.split("=", 1) for x in res]
            return [
                {
                    "name": x[0],
                    "value": x[1],
                    "domain": ".facebook.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "None"
                }
                for x in res
            ]
    except Exception:
        pass

    raise ValueError(f"Invalid cookie value: {val}")


def get_env_bool(key: str, default_val: bool = False) -> bool:
    val = os.getenv(key)
    if val is None:
        return default_val
    return val.lower() in ("1", "true", "yes")


def internet_good() -> bool:
    """
    Check if internet connection is working

    Returns:
        True if internet is working, False otherwise
    """
    try:
        requests.get("https://8.8.8.8", timeout=10)
        return True
    except (ConnectionError, requests.exceptions.ConnectTimeout, requests.exceptions.Timeout):
        return False
    except Exception as e:
        print(f"Unexpected error checking internet connectivity: {e}")
        return False


def is_post_url(href: str) -> bool:
    """
    Determine if a URL is a Facebook post URL

    Args:
        href: URL to check

    Returns:
        True if URL is a Facebook post, False otherwise
    """
    if href is None:
        return False

    # Facebook post patterns
    if "/posts/" in href:
        return True
    if "/reel/" in href:
        return True
    """if "/permalink.php" in href:
        return True
    if "/watch" in href:
        return True
    if "/photo" in href:
        return True"""

    return False


def extract_post_id(url: str) -> str | None:
    """
    Extract post ID from Facebook post URL

    Args:
        url: Facebook post URL

    Returns:
        Post ID if found, None otherwise
    """
    if not url:
        return None

    # Pattern: /posts/{post_id}
    match = re.search(r'/posts/([A-Za-z0-9_-]+)', url)
    if match:
        return match.group(1)

    # Pattern: /permalink.php?story_fbid={id}
    match = re.search(r'story_fbid=(\d+)', url)
    if match:
        return match.group(1)

    # Pattern: /watch/?v={id}
    match = re.search(r'[?&]v=(\d+)', url)
    if match:
        return match.group(1)

    return None


def parse_facebook_date(date_str: str) -> datetime | None:
    """
    Parse Facebook date strings into datetime objects (UTC).

    Examples:
        - "2h", "5m", "Just now"
        - "Yesterday at 5:00 PM"
        - "July 24 at 5:00 PM"
        - "July 24, 2023 at 5:00 PM"

    Args:
        date_str: Facebook date string

    Returns:
        datetime object in UTC, or None if parsing fails
    """
    if not date_str:
        return None

    date_str = date_str.strip()
    now = datetime.now(timezone.utc)

    try:
        # Relative time patterns
        if date_str.endswith('m'):  # minutes
            minutes = int(date_str[:-1])
            return now - timedelta(minutes=minutes)
        elif date_str.endswith('h'):  # hours
            hours = int(date_str[:-1])
            return now - timedelta(hours=hours)
        elif date_str.endswith('d'):  # days
            days = int(date_str[:-1])
            return now - timedelta(days=days)
        elif date_str.lower() in ["just now", "now"]:
            return now

        # "Yesterday" pattern
        if date_str.lower().startswith("yesterday"):
            return now - timedelta(days=1)

        # Absolute formats (not fully implemented)
        # For complex date strings like "July 24, 2023 at 5:00 PM"
        # we would need more sophisticated parsing or external library
        # For now, return None for unhandled patterns

        return None

    except Exception:
        return None

def unix_to_datetime(unix_timestamp: int) -> datetime:
    return datetime.fromtimestamp(unix_timestamp, tz=timezone.utc)

def get_home_dir_path():
    return os.path.dirname(Path(os.path.abspath(__file__)).parent)

def get_config_path():
    return os.path.join(get_home_dir_path(), "meo_facebook_scraper_config.cfg")

def parse_json_or_jsonl(body: str) -> list[dict]:
    # json.loads(body.decode('utf-8').strip().split('\n')[0])
    try:
        return [json.loads(body)]
    except json.decoder.JSONDecodeError:
        return [json.loads(i) for i in body.strip().split('\n')]

def flatten_dict(d: dict, parent_key='', sep='.') -> dict:
    items = []
    for k, v in d.items():
        new_key = parent_key + sep + k if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    items.extend(flatten_dict(item, f"{new_key}.{i}", sep=sep).items())
                else:
                    items.append((f"{new_key}.{i}", item))
        else:
            items.append((new_key, v))
    return dict(items)

def recursively_get_dict_value(dictionary: dict, key: str) -> dict | None:
    dictionary_flattened = flatten_dict(dictionary)
    matches = {k: v for k, v in dictionary_flattened.items() if k.endswith(key)}
    return matches

def save_jsonl(path: str, data: list[dict]) -> None:
    with open(path, "w") as f:
        for d in data:
            json.dump(d, f)
            f.write("\n")

async def gather(coros):
    for c in asyncio.as_completed(list(coros)):
        yield await c

if __name__ == "__main__":
    print(get_home_dir_path())