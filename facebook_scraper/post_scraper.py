"""
Facebook post scraper - orchestration layer
Uses fbscrape module for core scraping functionality
"""

from .config import (SCREEN_HEIGHT, MEOAPI_USERNAME, MEOAPI_PASSWORD, API_BASE_URL)
import random
import pandas as pd
import os

import functools
import time

from datetime import datetime, timedelta, timezone
from time import sleep

import json
import boto3
from tqdm import tqdm
from pika.adapters.blocking_connection import BlockingChannel

from playwright.sync_api import expect
from .api_clients import get_seedlist_new, get_bearer_token, insert_crawler_history
from .api_clients import get_crawler_histories, validate_or_sanitize_date

from bs4 import BeautifulSoup

from .rabbit_mq_utilities import send_data_to_queue

import re
import requests
import geocoder

# Import fbscrape components
from fbscrape import (
    BrowserManager,
    PageController,
    FacebookAuth,
    ResponseInterceptor,
    FacebookScraper,
    ScrapingResult
)

MAX_NUM_HANDLES_TO_HOVER_OVER = 2  # random.choice(range(0, 10))
AVG_NUM_SECONDS_TO_HOVER = 3
MAX_NUM_COMMENTS_TO_TRANSLATE = 2
TIMEOUT_MILLISECONDS = 3000
MAX_SCROLLS_PER_ITERATION = 100
MIN_SCROLLS_PER_ITERATION = 1
RETRY_MINUTES = 15
AVG_MINUTES_TO_WAIT_BETWEEN_HANDLES = 2
# self.batch_size = 30

BASE_URL = "https://www.facebook.com/"
POST_BASE_URL = BASE_URL
REEL_BASE_URL = BASE_URL + 'reel/'

def sleep_before(seconds):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            print(f"sleeping {seconds} seconds before")
            time.sleep(seconds)
            return func(*args, **kwargs)

        return wrapper

    return decorator


def sleep_after(seconds):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            print(f"sleeping {seconds} seconds after")
            time.sleep(seconds)
            return result

        return wrapper

    return decorator

class FacebookPostScraper:
    def __init__(self,
                 headless,
                 mobile,
                 log_in_fresh,
                 auth_dir,
                 posts_dir,
                 users_dir,
                 parts_dir,
                 image_dir,
                 video_dir,
                 aws_access_key,
                 aws_secret_key,
                 s3_target_bucket,
                 s3_parent_folder,
                 retry_cases,
                 username,
                 password,
                 video_queue,
                 image_queue,
                 pika_parameters,
                 success_cases,
                 crash_cases):

        self.headless = headless
        self.mobile = mobile
        self.log_in_fresh = log_in_fresh
        self.posts_dir = posts_dir
        self.users_dir = users_dir
        self.parts_dir = parts_dir
        self.auth_dir = auth_dir
        self.image_dir = image_dir
        self.video_dir = video_dir
        self.aws_access_key = aws_access_key
        self.aws_secret_key = aws_secret_key
        self.s3_target_bucket = s3_target_bucket
        self.s3_parent_folder = s3_parent_folder
        self.retry_cases = retry_cases
        self.username = username
        self.password = password
        self.auth_json = os.path.join(self.auth_dir, f"{self.username.lower()}_login.json")
        self.seeds = []
        self.video_queue = video_queue
        self.image_queue = image_queue
        self.pika_parameters = pika_parameters
        self.ip = None
        self.ip_country = None
        self.scraped_so_far = 0
        self.success_cases = success_cases
        self.crash_cases = crash_cases
        self.batches = []

        # fbscrape components (initialized later)
        self.browser_manager = None
        self.page_controller = None
        self.facebook_auth = None
        self.response_interceptor = None
        self.context = None

        assert self.s3_parent_folder[-1] == "/"

    def initialize(self):
        """Initialize fbscrape components for scraping"""
        print("Initializing Facebook scraper components...")

        # Create browser manager and playwright instance
        self.browser_manager = BrowserManager()
        self.browser_manager.create_playwright_instance()

        # Create Facebook auth manager
        self.facebook_auth = FacebookAuth(
            username=self.username,
            password=self.password,
            auth_json_path=self.auth_json
        )

        # Create browser context with saved session
        self.context = self.browser_manager.create_browser_context(
            headless=self.headless,
            mobile=self.mobile,
            auth_storage_path=self.auth_json
        )

        # Create page controller
        page = self.context.new_page()
        self.page_controller = PageController(page)

        # Create response interceptor
        self.response_interceptor = ResponseInterceptor()
        self.response_interceptor.setup_interception(self.page_controller.page)

        # Navigate to Facebook and log in if necessary
        self.page_controller.goto(BASE_URL)
        time.sleep(10)

        if self.facebook_auth.need_to_log_in(self.page_controller.page):
            print("Login required")
            time.sleep(5)
            self.facebook_auth.manual_login(self.page_controller.page, self.mobile)
            time.sleep(10)
            self.facebook_auth.save_session_state(self.context)
            self.facebook_auth.clear_post_login_popups(self.page_controller.page, self.mobile)

        # Verify we are logged in
        try:
            expect(
                self.page_controller.page.get_by_label("Home")
                .or_(self.page_controller.page.get_by_role("link", name="Facebook", exact=True))
                .or_(self.page_controller.page.locator('[role="feed"]'))
                .first
            ).to_be_visible(timeout=30000)
            print("Login verification successful")
        except AssertionError:
            print("Login verification failed. Saving debug info...")
            print(f"Current URL: {self.page_controller.page.url}")
            self.page_controller.page.screenshot(path="login_failure.png")
            print("Saved screenshot to login_failure.png")
            raise

    def get_ip_address(self):
        resp = requests.get(f"https://ifconfig.me")
        self.ip = resp.content.decode('utf-8')
        return self.ip

    def identify_country_from_which_you_are_scraping(self):
        self.get_ip_address()
        geocoder_data = geocoder.ip(self.ip)
        self.ip_country = geocoder_data.country

    def scrape_seed(self,
            seed, ch: BlockingChannel | None = None) -> dict:

        print(f"now scraping @{seed['handle']}'s home page...")
        # Flush collected data from previous scrapes
        self.flush_data()

        # CORE SCRAPING (delegated to HomepageScraper)
        homepage_scraper = FacebookScraper(
            page_controller=self.page_controller,
            response_interceptor=self.response_interceptor
        )

        scraping_result = homepage_scraper.scrape_user_homepage(
            handle=seed['handle'],
            start_date=seed['start_date'],
            end_date=seed['end_date'],
            channel=ch
        )

        metadata = scraping_result.to_dict()

        # ORCHESTRATION (remains here)
        if metadata['result'] in self.retry_cases:
           return metadata

        elif metadata['result'] in self.crash_cases:
            self.scraper_crash_message(metadata, seed)

        elif metadata['result'] in self.success_cases:
            # POST & USER METADATA:
            if metadata['result'] != 'no posts' and metadata['result'] != 'account is private' and metadata['result'] != 'profile is not available':
                # Get collected posts and users from scraping result
                posts = scraping_result.posts
                users = scraping_result.users

                # Data transformation
                posts = post_flattener(posts)
                posts = post_date_filterer(posts, seed['start_date'], seed['end_date'])
                posts = post_authorship_filterer(seed['handle'], posts)

                # Save data locally
                file_name = self.save_data_locally_from_lists(seed, posts, users)

                # Extract asset URLs and send to queues for concurrent downloading:
                """if pursue_assets:
                    videos_metadata, images_metadata, profile_pics_metadata = extract_assets_from_posts(posts, users)
                    self.push_video_metadata_to_download_queue(videos_metadata, f"{self.s3_parent_folder}{seed['handle']}/{seed['date_range_phrase']}/post_videos/")
                    self.push_image_metadata_to_download_queue(images_metadata, f"{self.s3_parent_folder}{seed['handle']}/{seed['date_range_phrase']}/post_images/")
                    self.push_image_metadata_to_download_queue(profile_pics_metadata, f"{self.s3_parent_folder}{seed['handle']}/{seed['date_range_phrase']}/profile_pics/")"""

            self.scraped_so_far += 1
            return metadata
        else:
            raise Exception("unexpected scraping result")

    def tag_record_with_scraper_metadata(self, row, seed):
        seed_handle = seed['handle']
        seed_id = seed['SeedID']
        phh_id = seed['PlatformHandleID']

        row["scraper_metadata"] = {
            "collection": self.seed_list_name,
            "seed_id": seed_id,
            "phh_id": phh_id,
            "handle": seed_handle,
            "start_date": seed['start_date'],
            "headless": self.headless,
            "scraper_handle": self.username,
            "scraper_ip": self.ip,
            "crawled_date": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        }
        return row

    def save_data_locally_from_lists(self, seed: dict, posts: list, users: list) -> str:
        """
        Save posts and users data locally

        Args:
            seed: Seed information
            posts: List of post dictionaries
            users: List of user dictionaries

        Returns:
            Filename of saved data
        """
        print(f"Saving {len(posts)} posts and {len(users)} users...")
        crawled_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        filename = f"facebook_from_{seed['start_date']}_to_{seed['end_date']}.jsonl"

        for data_type, data in (("posts", posts), ("users", users)):
            if data_type == "posts":
                data_file_path = os.path.join(self.posts_dir, filename)
            elif data_type == "users":
                data_file_path = os.path.join(self.users_dir, filename)
            else:
                raise Exception

            # Create file and append the data line by line
            with open(data_file_path, "a") as my_file:
                for row in data:
                    # each 'row' is a dictionary of data from Facebook
                    # add a few extra fields tagging the data so its origin is identifiable
                    try:
                        assert isinstance(row, dict)
                        assert len(row.keys()) > 0
                    except:
                        raise
                    temp = {
                        "phh_id": str(seed['ID']),
                        "seed_id": str(seed['SeedID']),
                        "crawled_date": crawled_date,
                        "collection": seed['Collection'],
                        "data": row
                    }
                    my_file.write(json.dumps(temp) + '\n')

        return filename

    def parse_video_urls(self, video_xml: str) -> list:
        if video_xml is None:
            return []
        soup = BeautifulSoup(video_xml, "xml")
        video_urls = [video_url_tag.contents[0] for video_url_tag in soup.find_all('BaseURL')]
        video_urls = list(set(video_urls))  # TODO: test if this is really necessary to uniquify
        return video_urls


    def push_video_metadata_to_download_queue(self, videos_metadata, aws_dir):
        for video_metadata in videos_metadata:
            video_metadata['s3-bucket'] = self.s3_target_bucket
            video_metadata['video-dir'] = self.video_dir
            video_metadata['aws-dir'] = aws_dir

        send_data_to_queue(videos_metadata, self.video_queue)

    def push_image_metadata_to_download_queue(self, images_metadata, aws_dir):
        for image_metadata in images_metadata:
            image_metadata['s3-bucket'] = self.s3_target_bucket
            image_metadata['image-dir'] = self.image_dir
            image_metadata['aws-dir'] = aws_dir

        send_data_to_queue(images_metadata, self.image_queue)

    def delete_post_and_user_metadata_locally(self, post_file_name, user_file_name):
        for file_name in [post_file_name, user_file_name]:
            file_path = os.path.join(self.data_dir, file_name)
            os.remove(file_path)
        print(f"successfully deleted posts and users data locally")
        return

    def extract_image_url(self, each_item):
        image_urls = each_item['candidates']

        if not isinstance(image_urls, list):
            raise Exception

        if len(image_urls) == 0:
            raise Exception # if this is possible, return None and handle accordingly

        # take first image, it should be the one with the highest resolution
        image_url = image_urls[0]['url']
        return image_url

    def scraper_crash_message(self, metadata: dict, seed: dict):
        """crash the scraper, reporting some data to console"""
        print(f"scraper failed:")
        for key in metadata:
            print(f"{key}: {metadata[key]}")
        for key in seed:
            print(f"{key}: {seed[key]}")
        print(f"handles scraped since most recent restart: {self.scraped_so_far}")
        raise Exception("scraper crashed")

    def report_results_to_crawler_history(self, seed: dict):
        phh_id = seed['ID']
        start_date = seed['start_date']
        end_date = seed['end_date']
        # start_date = datetime.strptime(start_date, "%Y-%m-%d").strftime('%Y-%m-%dT%H:%M:%SZ')
        # end_date = datetime.strptime(self.end_date, "%Y-%m-%d").strftime('%Y-%m-%dT%H:%M:%SZ')
        token = get_bearer_token(MEOAPI_USERNAME, MEOAPI_PASSWORD)
        insert_crawler_history(token, API_BASE_URL, phh_id, start_date, end_date)

    def flush_data(self):
        """Flush collected data from response interceptor"""
        print("Flushing collected data...")
        if self.response_interceptor:
            self.response_interceptor.flush()


# ============================================================================
# Utility functions for data processing
# ============================================================================

def post_flattener(posts: list[dict]) -> list[dict]:
    """
    Flatten posts from nested structure

    For Facebook posts, this may need to be updated based on actual response structure.
    Currently keeps posts as-is since Facebook GraphQL structure is TBD.
    """
    # TODO: Update based on actual Facebook GraphQL response structure
    # For now, just return posts as-is
    return posts


def extract_assets_from_post(post: dict) -> (list[dict], list[dict], list[dict]):
    # videos:
    video_metadata = extract_videos_from_post(post)

    # images:
    image_metadata = extract_images_from_post(post)

    # profile pics:
    profile_pic_metadata = extract_profile_pics_from_post(post)

    print(f"extracted {len(video_metadata)} videos")
    print(f"extracted {len(image_metadata)} images")
    print(f"extracted {len(profile_pic_metadata)} profile pics")
    return video_metadata, image_metadata, profile_pic_metadata


def extract_assets_from_posts(posts: list[dict], users: list[dict]) -> (list[dict], list[dict], list[dict]):
    image_metadata = []
    video_metadata = []
    profile_pic_metadata = []
    for post in posts:
        (video_metadata_for_one_post,
         image_metadata_for_one_post,
         profile_pic_metadata_for_one_post) = extract_assets_from_post(post)

        # accumulate
        video_metadata += video_metadata_for_one_post
        image_metadata += image_metadata_for_one_post
        profile_pic_metadata += profile_pic_metadata_for_one_post

    # extract profile pics from user metadata:
    profile_pic_metadata += extract_profile_pics_from_users(users)
    # de-duplicate:
    df = pd.DataFrame.from_records(profile_pic_metadata)
    df = df.drop_duplicates('handle')
    profile_pic_metadata = df.to_dict('records')

    print(f"extracted {len(image_metadata)} images")
    print(f"extracted {len(profile_pic_metadata)} profile pics")
    print(f"extracted {len(video_metadata)} videos")
    return video_metadata, image_metadata, profile_pic_metadata



def extract_videos_from_post(post: dict) -> list[dict]:
    post_id = post['id']
    video_urls = []

    if post['video_dash_manifest'] is not None:
        # there are video URLs listed in video_dash_manifest
        # extract them for scraping
        video_urls += parse_video_urls(post['video_dash_manifest'])

    if post['carousel_media'] is not None:
        # there are video URLs listed in carousel_media
        # extract them for scraping
        for each_item in post['carousel_media']:
            video_urls += parse_video_urls(each_item['video_dash_manifest'])

    this_post_video_metadata = [
        {
            'post_id': post_id,
            'video_url': video_url
        } for video_url in video_urls
    ]

    return this_post_video_metadata

def extract_images_from_post(post: dict) -> list[dict]:
    post_id = post['id']
    image_urls = []

    if post['image_versions2'] is not None:
        print(f"there is a single image associated with this post")
        image_url = extract_image_url(post['image_versions2'])
        image_urls.append(image_url)
    if post['carousel_media'] is not None:
        print(f"there are multiple images... or videos associated with this post")
        for each_item in post['carousel_media']:
            image_url = extract_image_url(each_item['image_versions2'])
            image_urls.append(image_url)

    this_post_image_metadata = [
        {
            'post_id': post_id,
            'image_url': image_url
        } for image_url in image_urls
    ]

    return this_post_image_metadata

def extract_profile_pics_from_post(post: dict) -> list[dict]:
    profile_pic_metadata = []

    # user:
    profile_pic_metadata.append(
        {
            'handle': post['user']['username'],
            'profile_pic_url': post['user']['hd_profile_pic_url_info']['url']
        }
    )

    # facepile:
    for each_item in post['facepile_top_likers']:
        profile_pic_metadata.append(
            {
                'handle': each_item['username'] if 'username' in each_item.keys() else each_item['id'],
                'profile_pic_url': each_item['profile_pic_url']
            }
        )

    # owner
    if 'owner' in post.keys():
        profile_pic_metadata.append(
            {
                'handle': post['owner']['username'],
                'profile_pic_url': post['owner']['profile_pic_url']
            }
        )

    df = pd.DataFrame(profile_pic_metadata, columns=('handle', 'profile_pic_url'))
    df = df.drop_duplicates('handle')

    return df.to_dict('records')




def extract_profile_pics_from_users(user_metadatas: list[dict]) -> list[dict]:
    profile_pics_metadata = []
    for user_metadata in user_metadatas:
        profile_pic_url = user_metadata['profile_pic_url']
        if profile_pic_url is not None:
            profile_pic_metadata = {
                'handle': user_metadata['username'],
                'profile_pic_url': profile_pic_url,
            }
            profile_pics_metadata.append(profile_pic_metadata)

    if len(profile_pics_metadata) == 0:
        return []

    return profile_pics_metadata


def post_flattener(posts: list[dict]) -> list[dict]:
    new_posts = []
    for post in posts:
        if post['__typename'] == 'XDTMediaDict':
            new_post = post
        elif post['__typename'] == 'XDTFeedItem':
            new_post = post['media']
        else:
            print("unhandled post type")
            raise Exception

        new_posts.append(new_post)
    return new_posts


def post_date_filterer(posts: list[dict], start_date: str, end_date: str) -> list[dict]:
    start_datetime = datetime.strptime(start_date + " 00:00:00", "%Y-%m-%d %H:%M:%S")
    end_datetime = datetime.strptime(end_date + " 23:59:59", "%Y-%m-%d %H:%M:%S")
    new_posts = [x for x in posts if
                 datetime.fromtimestamp(x['taken_at']) >= start_datetime
                 and
                 datetime.fromtimestamp(x['taken_at']) <= end_datetime]
    print(f"{len(posts)} posts before filtering by date")
    print(f"{len(new_posts)} posts after filtering by date -- "
          f"start date: {start_date} "
          f"end_date: {end_date}")
    return new_posts


def post_authorship_filterer(handle: str, records: list[dict]):
    print(f"{len(records)} records before filtering")
    records = [x for x in records if keep_record(x, handle)]
    print(f"{len(records)} records after filtering")
    return records


def extract_image_url(image_versions2):
    image_urls = image_versions2['candidates']

    if not isinstance(image_urls, list):
        raise Exception

    if len(image_urls) == 0:
        raise Exception # if this is possible, return None and handle accordingly

    # take first image, it should be the one with the highest resolution
    image_url = image_urls[0]['url']
    return image_url


def parse_video_urls(video_xml: str) -> list:
    if video_xml is None:
        return []
    soup = BeautifulSoup(video_xml, "xml")
    video_urls = [video_url_tag.contents[0] for video_url_tag in soup.find_all('BaseURL')]
    video_urls = list(set(video_urls))  # TODO: test if this is really necessary to uniquify
    return video_urls


def load_data(file_path):
    """loads JSON into memory"""
    data = []
    with open(file_path, "r") as f:
        for line in f:
            data.append(json.loads(line))
    return data


def keep_record(record: dict, handle: str):
    author = record["user"]["username"]

    if handle.lower() != author.lower():
        # the author of the post is not the same as the target handle
        if len(record['coauthor_producers']) > 0:
            # perhaps the target handle was coauthoring with this other author!
            for coauthor_dict in record['coauthor_producers']:
                if coauthor_dict['username'].lower() == handle.lower():
                    # indeed, one of the coauthors of the post was the target handle - yay
                    return True
            # none of the coauthors was the target handle
            return False
        else:
            # the post was not coauthored
            return False
    else:
        # the author is the target handle - yay
        return True


def keep_record_ex_post(record: dict, handle: str):
    author = record["data"]["user"]["username"]

    if handle.lower() != author.lower():
        # the author of the post is not the same as the target handle
        if len(record['data']['coauthor_producers']) > 0:
            # perhaps the target handle was coauthoring with this other author!
            for coauthor_dict in record['data']['coauthor_producers']:
                if coauthor_dict['username'].lower() == handle.lower():
                    # indeed, one of the coauthors of the post was the target handle - yay
                    return True
            # none of the coauthors was the target handle
            return False
        else:
            # the post was not coauthored
            return False
    else:
        # the author is the target handle - yay
        return True


def presentify_future_date(my_date):
    today = datetime.utcnow().date()
    if my_date > today:
        return today
    else:
        return my_date


def remove_subset_date_tuples(date_tuples: list[tuple]) -> list[tuple]:
    filtered_date_tuples = []
    for i in range(0, len(date_tuples)):
        candidate_tuple = date_tuples[i]
        add = True
        for j in range(0, len(date_tuples)):
            if i != j:
                a_tuple = date_tuples[j]
                if candidate_tuple[0] >= a_tuple[0] and candidate_tuple[1] <= a_tuple[1]:
                    # print(f"candidate {candidate_tuple} is a subset of {a_tuple}")
                    add = False
        if add:
            filtered_date_tuples.append(candidate_tuple)
    return filtered_date_tuples


def detect_gaps_between_consecutive_scrapes(date_tuples: list[tuple]) -> list[tuple]:
    gaps_for_this_seed = []
    for i in range(0, len(date_tuples)):
        if i == len(date_tuples) - 1:
            continue
        first_tuple = date_tuples[i]
        second_tuple = date_tuples[i + 1]
        assert first_tuple[0] < second_tuple[0]  # this should be true by construction
        assert second_tuple[1] > first_tuple[1]  # this should be true by construction
        # if the start date of the second tuple is less than three days prior to the end date of the first tuple, we have a gap:
        if first_tuple[1] - timedelta(days=3) >= second_tuple[0]:
            # no gap
            continue
        gaps_for_this_seed.append((first_tuple[1] - timedelta(days=3), second_tuple[0]))
    return gaps_for_this_seed


def convert_to_date(my_date):
    try:
        return datetime.strptime(my_date, "%Y-%m-%d").date()
    except:
        return None