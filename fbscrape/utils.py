"""
Utility functions for Facebook scraping
"""
import base64
import dataclasses
import os
import platform
import shlex
import urllib.parse
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


# ============== cURL parser ==============

# Flags whose argument we keep / use.
_CURL_VALUE_FLAGS_METHOD = {"-X", "--request"}
_CURL_VALUE_FLAGS_HEADER = {"-H", "--header"}
_CURL_VALUE_FLAGS_DATA = {"-d", "--data", "--data-raw", "--data-binary",
                          "--data-urlencode", "--data-ascii"}
_CURL_VALUE_FLAGS_COOKIE = {"-b", "--cookie"}
_CURL_VALUE_FLAGS_USERAGENT = {"-A", "--user-agent"}
_CURL_VALUE_FLAGS_REFERER = {"-e", "--referer"}

# Flags that take a value but we don't surface (just skip the value).
_CURL_VALUE_FLAGS_OTHER = {
    "--url", "--output", "-o", "--max-time", "--connect-timeout",
    "--proxy", "-x", "--resolve", "--retry", "--cert", "--key",
    "--cacert", "--capath", "--ciphers", "--user", "-u",
}

# Boolean / no-value flags we silently ignore.
_CURL_BOOLEAN_FLAGS = {
    "--compressed", "--location", "-L", "--insecure", "-k", "--silent",
    "-s", "--verbose", "-v", "--include", "-i", "--head", "-I",
    "--fail", "-f", "--show-error", "-S", "--no-buffer", "-N",
    "--globoff", "-g", "--anyauth", "--basic", "--digest", "--ntlm",
    "--negotiate", "--http1.0", "--http1.1", "--http2", "--http2-prior-knowledge",
}


def parse_curl(curl_string: str) -> dict:
    """Parse a cURL command string into a structured dict.

    Supports the multi-line `curl ... \\\n  -H ...` form produced by browser
    "Copy as cURL" actions. URL-encoded form bodies (Content-Type
    application/x-www-form-urlencoded, or any body when Content-Type is unset)
    are decoded into a dict; fields whose values parse as JSON
    (notably `variables`) are recursively decoded.

    Returns:
        {
            "method": "POST",
            "url": "https://...",
            "headers": {"Header-Name": "value", ...},
            "body": {"field": "value" or decoded JSON, ...},  # empty dict if no body
            "raw_body": "av=...&__user=...",  # original --data-raw verbatim, or ""
        }
    """
    tokens = shlex.split(curl_string, posix=True)
    if not tokens:
        raise ValueError("Empty cURL string")

    # Drop the leading `curl` (some pastes drop it; tolerate both).
    if tokens[0].lower() == "curl":
        tokens = tokens[1:]

    method: str | None = None
    url: str | None = None
    headers: dict[str, str] = {}
    raw_body: str = ""

    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if tok in _CURL_VALUE_FLAGS_METHOD:
            i += 1
            method = tokens[i].upper()
        elif tok in _CURL_VALUE_FLAGS_HEADER:
            i += 1
            name, sep, value = tokens[i].partition(":")
            if sep:
                headers[name.strip()] = value.strip()
        elif tok in _CURL_VALUE_FLAGS_DATA:
            i += 1
            # Multiple -d on one command concatenate with &.
            raw_body = tokens[i] if not raw_body else f"{raw_body}&{tokens[i]}"
        elif tok in _CURL_VALUE_FLAGS_COOKIE:
            i += 1
            headers.setdefault("Cookie", tokens[i])
        elif tok in _CURL_VALUE_FLAGS_USERAGENT:
            i += 1
            headers.setdefault("User-Agent", tokens[i])
        elif tok in _CURL_VALUE_FLAGS_REFERER:
            i += 1
            headers.setdefault("Referer", tokens[i])
        elif tok in _CURL_VALUE_FLAGS_OTHER:
            i += 1  # consume value, drop it
        elif tok in _CURL_BOOLEAN_FLAGS:
            pass
        elif tok.startswith("-"):
            # Unknown long-form `--foo=bar` carries its value inline; bare
            # unknown flags are skipped without consuming a token.
            pass
        else:
            if url is None:
                url = tok
        i += 1

    if url is None:
        raise ValueError("Could not find URL in cURL string")

    if method is None:
        method = "POST" if raw_body else "GET"

    body: dict = {}
    if raw_body:
        content_type = ""
        for k, v in headers.items():
            if k.lower() == "content-type":
                content_type = v.lower()
                break
        if not content_type or "application/x-www-form-urlencoded" in content_type:
            parsed = urllib.parse.parse_qs(raw_body, keep_blank_values=True)
            for key, values in parsed.items():
                value = values[0] if len(values) == 1 else values
                # Only auto-decode JSON containers/strings — leave scalar form
                # fields (numeric IDs, flags, etc.) as strings so a numeric
                # `doc_id` doesn't become a Python int.
                if isinstance(value, str) and value.startswith(("{", "[", '"')):
                    try:
                        value = json.loads(value)
                    except (ValueError, json.JSONDecodeError):
                        pass
                body[key] = value

    return {
        "method": method,
        "url": url,
        "headers": headers,
        "body": body,
        "raw_body": raw_body,
    }


# ============== cURL formatter ==============

_REDACT_HEADERS = {"cookie"}
_REDACT_BODY_FIELDS = {"fb_dtsg", "lsd", "jazoest"}
_REDACTED = "<redacted>"

_TELEMETRY_HEADERS = {
    "user-agent", "accept", "accept-language", "accept-encoding",
    "origin", "alt-used", "connection", "sec-fetch-dest", "sec-fetch-mode",
    "sec-fetch-site", "dnt", "sec-gpc", "x-asbd-id", "pragma", "cache-control",
    "te", "upgrade-insecure-requests",
}
_TELEMETRY_BODY_FIELDS = {
    "__dyn", "__csr", "__hsdp", "__hblp", "__sjsp", "__s", "__hsi", "__ccg",
    "__a", "__aaid", "__req", "__rev", "__hs", "__spin_r", "__spin_b", "__spin_t",
    "__comet_req", "__crn", "dpr", "qpl_active_flow_ids", "fb_api_caller_class",
    "fb_api_analytics_tags", "server_timestamps",
}
_BODY_HEADLINE_FIELDS = ("fb_api_req_friendly_name", "doc_id")


def _render_value(v) -> str:
    """Render a body-field value for printing (JSON-pretty if dict/list)."""
    if isinstance(v, (dict, list)):
        return json.dumps(v, indent=2, ensure_ascii=False)
    return str(v)


def _format_kv_block(items: list[tuple[str, str]], indent: str = "  ") -> str:
    lines = []
    for k, v in items:
        if "\n" in v:
            # Multiline value (pretty JSON): print key on its own line, value indented.
            lines.append(f"{indent}{k}:")
            for vline in v.splitlines():
                lines.append(f"{indent}{indent}{vline}")
        else:
            lines.append(f"{indent}{k}: {v}")
    return "\n".join(lines)


def format_parsed_curl(parsed: dict, *, full: bool = False, redact: bool = True) -> str:
    """Render `parse_curl()` output as human-readable text.

    full=False (default) → structured summary: METHOD/URL, friendly_name,
    doc_id, decoded `variables` JSON, interesting headers, and non-telemetry
    body fields. Drops fingerprint-only headers (Sec-Fetch-*, User-Agent, ...)
    and telemetry body blobs (__csr, __dyn, __hsdp, ...).

    full=True → every header and every body field. `variables` is still
    JSON-pretty-printed since it's the whole point of inspecting these.

    redact=True (default) → Cookie header value and fb_dtsg/lsd/jazoest body
    values are replaced with <redacted>.
    """
    method = parsed.get("method", "GET")
    url = parsed.get("url", "")
    headers: dict = parsed.get("headers") or {}
    body: dict = parsed.get("body") or {}

    def maybe_redact_header(name: str, value: str) -> str:
        if redact and name.lower() in _REDACT_HEADERS:
            return _REDACTED
        return value

    def maybe_redact_body(name: str, value) -> str:
        if redact and name in _REDACT_BODY_FIELDS:
            return _REDACTED
        return _render_value(value)

    out: list[str] = [f"{method} {url}"]

    if full:
        if headers:
            out.append("")
            out.append("headers:")
            out.append(_format_kv_block(
                [(k, maybe_redact_header(k, v)) for k, v in headers.items()]
            ))
        if body:
            out.append("")
            out.append("body:")
            out.append(_format_kv_block(
                [(k, maybe_redact_body(k, v)) for k, v in body.items()]
            ))
        elif parsed.get("raw_body"):
            out.append("")
            out.append("raw_body:")
            out.append(f"  {parsed['raw_body']}")
        return "\n".join(out)

    # Structured mode.
    friendly = body.get("fb_api_req_friendly_name")
    doc_id = body.get("doc_id")
    if friendly or doc_id:
        out.append("")
        if friendly:
            out.append(f"friendly_name: {friendly}")
        if doc_id:
            out.append(f"doc_id: {doc_id}")

    if "variables" in body:
        out.append("")
        out.append("variables:")
        out.append(_render_value(body["variables"]))

    interesting_headers = [
        (k, maybe_redact_header(k, v))
        for k, v in headers.items()
        if k.lower() not in _TELEMETRY_HEADERS
    ]
    if interesting_headers:
        out.append("")
        out.append("headers:")
        out.append(_format_kv_block(interesting_headers))

    other_body_items = [
        (k, maybe_redact_body(k, v))
        for k, v in body.items()
        if k not in _BODY_HEADLINE_FIELDS
        and k != "variables"
        and k not in _TELEMETRY_BODY_FIELDS
    ]
    if other_body_items:
        out.append("")
        out.append("body (other fields):")
        out.append(_format_kv_block(other_body_items))

    return "\n".join(out)


if __name__ == "__main__":
    print(get_home_dir_path())