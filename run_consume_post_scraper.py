from instagram_scraper.config import instagram_logins
from instagram_scraper.consume_post_scraper import run as run_post_consumer
import argparse

parser = argparse.ArgumentParser(
    description="This script retrieves Instagram posts."
)
parser.add_argument(
    "--credentials",
    default="casey",
    help="Credentials header from the config file (ex. casey, alexei, etc).",
)

args = parser.parse_args()

print(f"using {args.credentials} for credentials...")
username = instagram_logins[args.credentials]['username']
password = instagram_logins[args.credentials]['password']
run_post_consumer(username, password)
