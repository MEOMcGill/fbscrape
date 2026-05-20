"""Unit tests for `fbscrape.utils.parse_curl` and `format_parsed_curl`.

Exercises:
  - Token-walking parser against synthetic + DevTools-style multi-line cURLs.
  - URL-encoded body decoding + nested JSON pickup on `variables`.
  - Structured (default) vs full output, telemetry filtering, redaction toggle.
  - CLI smoke test through `CliRunner` for the `utils parse-curl` command.
"""

from __future__ import annotations

import textwrap

import pytest
from click.testing import CliRunner

from fbscrape.cli import cli
from fbscrape.utils import format_parsed_curl, parse_curl


SIMPLE_CURL = (
    "curl 'https://example.com/api/graphql/' "
    "-X POST "
    "-H 'Content-Type: application/x-www-form-urlencoded' "
    "-H 'X-FB-Friendly-Name: SampleQuery' "
    "--data-raw 'av=1&doc_id=12345&fb_api_req_friendly_name=SampleQuery"
    "&variables=%7B%22count%22%3A3%2C%22id%22%3A%22abc%22%7D"
    "&__dyn=blob&__csr=blob2&fb_dtsg=secret-token&lsd=secret-lsd&jazoest=2"
    "&server_timestamps=true'"
)

# A faithful (truncated) DevTools-style multi-line cURL with line continuations.
MULTILINE_CURL = textwrap.dedent(r"""
    curl 'https://www.facebook.com/api/graphql/' \
      --compressed \
      -X POST \
      -H 'User-Agent: Mozilla/5.0' \
      -H 'Accept: */*' \
      -H 'Content-Type: application/x-www-form-urlencoded' \
      -H 'X-FB-Friendly-Name: GroupsCometFeedRegularStoriesPaginationQuery' \
      -H 'Sec-Fetch-Site: same-origin' \
      -H 'Cookie: datr=abc; c_user=42; xs=stuff' \
      --data-raw 'av=42&__user=42&fb_dtsg=DTSG&lsd=LSD&jazoest=99&fb_api_req_friendly_name=GroupsCometFeedRegularStoriesPaginationQuery&doc_id=26804327385904923&variables=%7B%22count%22%3A3%2C%22sortingSetting%22%3A%22TOP_POSTS%22%7D'
""").strip()


def test_parse_curl_basic_post():
    parsed = parse_curl(SIMPLE_CURL)
    assert parsed["method"] == "POST"
    assert parsed["url"] == "https://example.com/api/graphql/"
    assert parsed["headers"]["X-FB-Friendly-Name"] == "SampleQuery"
    assert parsed["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert parsed["body"]["doc_id"] == "12345"
    assert parsed["body"]["fb_api_req_friendly_name"] == "SampleQuery"


def test_parse_curl_decodes_variables():
    parsed = parse_curl(SIMPLE_CURL)
    # `variables` should be JSON-decoded into a dict, not left as a string.
    assert parsed["body"]["variables"] == {"count": 3, "id": "abc"}


def test_parse_curl_handles_line_continuations():
    parsed = parse_curl(MULTILINE_CURL)
    assert parsed["method"] == "POST"
    assert parsed["url"] == "https://www.facebook.com/api/graphql/"
    assert parsed["body"]["doc_id"] == "26804327385904923"
    assert parsed["body"]["variables"]["sortingSetting"] == "TOP_POSTS"
    assert parsed["headers"]["Cookie"].startswith("datr=abc")


def test_parse_curl_method_inferred_from_body():
    no_method = (
        "curl 'https://example.com/api/' "
        "-H 'X-Test: 1' "
        "--data-raw 'k=v'"
    )
    parsed = parse_curl(no_method)
    assert parsed["method"] == "POST"


def test_parse_curl_get_when_no_body_no_method():
    parsed = parse_curl("curl 'https://example.com/'")
    assert parsed["method"] == "GET"
    assert parsed["body"] == {}
    assert parsed["raw_body"] == ""


def test_parse_curl_cookie_flag():
    parsed = parse_curl("curl 'https://example.com/' --cookie 'a=b; c=d'")
    assert parsed["headers"]["Cookie"] == "a=b; c=d"


def test_parse_curl_compressed_does_not_swallow_url():
    # --compressed is a boolean flag; should not consume the next token.
    parsed = parse_curl("curl --compressed 'https://example.com/'")
    assert parsed["url"] == "https://example.com/"


def test_format_structured_redacts_by_default():
    parsed = parse_curl(MULTILINE_CURL)
    out = format_parsed_curl(parsed)  # full=False, redact=True (defaults)
    assert "<redacted>" in out
    # Specific secrets should NOT appear verbatim.
    assert "DTSG" not in out
    assert "LSD" not in out
    assert "datr=abc" not in out


def test_format_structured_drops_telemetry():
    parsed = parse_curl(MULTILINE_CURL)
    out = format_parsed_curl(parsed)
    # Telemetry headers gone in structured mode.
    assert "User-Agent" not in out
    assert "Sec-Fetch-Site" not in out
    # Headlined fields ARE present.
    assert "friendly_name: GroupsCometFeedRegularStoriesPaginationQuery" in out
    assert "doc_id: 26804327385904923" in out
    # Decoded variables visible as JSON.
    assert '"sortingSetting": "TOP_POSTS"' in out


def test_format_structured_drops_telemetry_body_fields():
    parsed = parse_curl(SIMPLE_CURL)
    out = format_parsed_curl(parsed)
    assert "__dyn" not in out
    assert "__csr" not in out
    assert "server_timestamps" not in out


def test_format_full_includes_everything():
    parsed = parse_curl(MULTILINE_CURL)
    out = format_parsed_curl(parsed, full=True)
    # Every header and body field present.
    assert "User-Agent" in out
    assert "Sec-Fetch-Site" in out
    # Cookie still redacted (redact defaults to True even under --full).
    assert "<redacted>" in out
    assert "datr=abc" not in out


def test_format_raw_shows_secrets():
    parsed = parse_curl(MULTILINE_CURL)
    out = format_parsed_curl(parsed, redact=False)
    assert "<redacted>" not in out
    assert "DTSG" in out
    assert "datr=abc" in out


def test_format_first_line_is_method_and_url():
    parsed = parse_curl(SIMPLE_CURL)
    out = format_parsed_curl(parsed)
    assert out.splitlines()[0] == "POST https://example.com/api/graphql/"


def test_cli_parse_curl_smoke():
    runner = CliRunner()
    result = runner.invoke(cli, ["utils", "parse-curl", SIMPLE_CURL])
    assert result.exit_code == 0, result.output
    assert "friendly_name: SampleQuery" in result.output
    assert "doc_id: 12345" in result.output
    # Secrets redacted by default.
    assert "secret-token" not in result.output
    assert "<redacted>" in result.output


def test_cli_parse_curl_full_and_raw_flags():
    runner = CliRunner()
    result = runner.invoke(cli, ["utils", "parse-curl", SIMPLE_CURL, "--full", "--raw"])
    assert result.exit_code == 0, result.output
    # --full surfaces telemetry body fields.
    assert "__dyn" in result.output
    # --raw stops redaction.
    assert "secret-token" in result.output
    assert "<redacted>" not in result.output
