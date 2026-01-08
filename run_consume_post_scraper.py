from facebook_scraper.config import facebook_logins
from facebook_scraper.consume_post_scraper import run as run_post_consumer
import argparse

parser = argparse.ArgumentParser(
    description="This script retrieves Facebook posts."
)
parser.add_argument(
    "--credentials",
    default="casey",
    help="Credentials header from the config file (ex. casey, alexei, etc).",
)

args = parser.parse_args()

print(f"using {args.credentials} for credentials...")
username = facebook_logins[args.credentials]['username']
password = facebook_logins[args.credentials]['password']
run_post_consumer(username, password)
