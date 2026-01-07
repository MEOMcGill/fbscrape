"""
contains the functions for running an instagram post scraper
the principal function should be embeddable in a rabbitmq consumer
it should also be callable on its own
"""

from .config import (SCREEN_HEIGHT, MEOAPI_USERNAME, MEOAPI_PASSWORD, API_BASE_URL)
import random
import pandas as pd
import os

import functools
import time

from datetime import datetime, timedelta, UTC
from time import sleep

import json
import boto3
from tqdm import tqdm
from pika.adapters.blocking_connection import BlockingChannel

from playwright.sync_api import sync_playwright, expect, TimeoutError
# from playwright_stealth import stealth_sync
from .api_clients import get_seedlist_new, get_bearer_token, insert_crawler_history
from .api_clients import get_crawler_histories, validate_or_sanitize_date

from bs4 import BeautifulSoup

from .rabbit_mq_utilities import send_data_to_queue

import re
import requests
import geocoder

MAX_NUM_HANDLES_TO_HOVER_OVER = 2  # random.choice(range(0, 10))
AVG_NUM_SECONDS_TO_HOVER = 3
MAX_NUM_COMMENTS_TO_TRANSLATE = 2
TIMEOUT_MILLISECONDS = 3000
MAX_SCROLLS_PER_ITERATION = 100
MIN_SCROLLS_PER_ITERATION = 1
RETRY_MINUTES = 15
AVG_MINUTES_TO_WAIT_BETWEEN_HANDLES = 2
# self.batch_size = 30

BASE_URL = "https://www.instagram.com/"
POST_BASE_URL = BASE_URL + "p/"
REEL_BASE_URL = BASE_URL + 'reel/'

def internet_good():
    try:
        requests.get(f"https://8.8.8.8", timeout=10)
        return True
    except (ConnectionError, requests.exceptions.ConnectTimeout, requests.exceptions.Timeout):
        return False
    except Exception as e:
        print(f"Unexpected error checking internet connectivity: {e}")
        return False

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

def extract_posts_from_a_tags(page):

    def is_post(my_href):
        if my_href.startswith('/p/') or my_href.startswith('/reel/'):
            return True
        else:
            return False

    result = page.locator("a")
    a_tags_revealed_now = result.count()

    posts = []
    for i in range(0, result.count()):
        elt = result.nth(i)
        if is_post(elt.get_attribute("href")):
            posts.append(elt)

    return posts

class InstaPostScraper:
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
        self.insta_session = None
        self.video_queue = video_queue
        self.image_queue = image_queue
        self.pika_parameters = pika_parameters
        self.ip = None
        self.ip_country = None
        self.scraped_so_far = 0
        self.success_cases = success_cases
        self.crash_cases = crash_cases
        self.batches = []

        assert self.s3_parent_folder[-1] == "/"

    def create_playwright_instance(self):
        self.playwright = sync_playwright().start()

    def get_ip_address(self):
        resp = requests.get(f"https://ifconfig.me")
        self.ip = resp.content.decode('utf-8')
        return self.ip

    def identify_country_from_which_you_are_scraping(self):
        self.get_ip_address()
        geocoder_data = geocoder.ip(self.ip)
        self.ip_country = geocoder_data.country

    def failed_to_load_retry_button_appears(self):
        """
        Sometimes, instead of forcibly logging you out, Instagram will prevent you from
        scrolling down a target home page by presenting a temporary pop-up window saying
        'Failed to Load. [Retry]' with a retry button. If this happens, the scraping
        accounts needs to be switched out, so the scraper needs to crash gracefully.
        """
        retry_button = self.insta_session.page.get_by_role("button", name="Retry")
        if retry_button.count() > 0:
            failed_to_load_message = self.insta_session.page.get_by_text("Failed to Load")
            if failed_to_load_message.count() > 0:
                return True
            else:
                raise Exception
        return False

    def check_if_user_and_password_are_acceptable(self):
        print(f"scraping {self.seed_list_name} with @{self.username} ({self.ip})")
        # input('ok?')

    def check_if_local_dir_is_clear(self):
        for my_dir in (self.image_dir, self.video_dir, self.data_dir):
            if len(os.listdir(my_dir)) > 0:
                raise Exception(f"{my_dir} is non-empty")

    def initialize_insta_session(self):
        if self.mobile:
            my_phone = self.playwright.devices['iPhone 13']
            browser = self.playwright.webkit.launch(headless=False)
            context = browser.new_context(
                **my_phone,
                storage_state=self.auth_json if os.path.exists(self.auth_json) else None
            )
        else:
            browser = self.playwright.chromium.launch(headless=self.headless)
            # browser = self.playwright.webkit.launch(headless=self.headless)
            context = browser.new_context(
                storage_state=self.auth_json if os.path.exists(self.auth_json) else None
            )
        self.insta_session = InstagramSession(self.username, self.password, False, context)

    def cookies_expired(self) -> bool:
        if not os.path.exists(self.auth_json):
            return True

        with open(self.auth_json, "r") as f:
            auth_dict = json.load(f)

        for cookie in auth_dict["cookies"]:
            if datetime.fromtimestamp(cookie["expires"]) < datetime.now():
                return True

        return False

    def need_to_log_in(self) -> bool:
        # if self.cookies_expired():
        #     # Cookies are expired. Need to log in.
        #     print('cookies expired; need to log in')
        #     return True

        # Check if the login layout is showing:
        if (
                self.insta_session.page.get_by_label("Phone number, username, or email").is_visible()
                and self.insta_session.page.get_by_label("Password").is_visible()
                and self.insta_session.page.get_by_role("button", name="Log in", exact=True).is_visible()
        ):
            # Login layout is showing. Need to log in.
            print(f"login layout is showing! need to log in")
            return True

        # Cookies are not expired and the login layout is not showing. No need to log in.
        return False


    @sleep_before(5)
    @sleep_after(10)
    def log_in_if_necessary(self):
        self.insta_session.page.goto(BASE_URL)
        time.sleep(10)

        if self.need_to_log_in():
            self.insta_session.log_in_to_instagram(self.mobile)
            self.insta_session.browser.storage_state(path=self.auth_json)

        # expect to arrive on the home page
        expect( self.insta_session.page.get_by_label("Home")).to_be_visible(timeout=10000)
        return

    def scrape_seed(self,
            seed, ch: BlockingChannel | None = None) -> dict:

        print(f"now scraping @{seed['handle']}'s home page...")
        # flush API data from scraper
        self.flush_data()

        # scrape the data from instagram
        metadata = self.scraper_user_home_page(seed, ch)

        if metadata['result'] in self.retry_cases:
           return metadata

        elif metadata['result'] in self.crash_cases:
            self.scraper_crash_message(metadata, seed)

        elif metadata['result'] in self.success_cases:
            # POST & USER METADATA:
            if metadata['result'] != 'no posts' and metadata['result'] != 'account is private' and metadata['result'] != 'profile is not available':
                # save the post and user data locally
                self.insta_session.post_metadata_list = post_flattener(self.insta_session.post_metadata_list)
                self.insta_session.post_metadata_list = post_date_filterer(self.insta_session.post_metadata_list, seed['start_date'], seed['end_date'])
                self.insta_session.post_metadata_list = post_authorship_filterer(seed['handle'], self.insta_session.post_metadata_list)
                file_name = self.save_data_locally(seed)
                # push the post and user data to cloud
                # self.push_post_and_user_metadata_to_cloud(file_name)
                # # delete the post and user data locally
                # self.delete_post_and_user_metadata_locally(post_file_name, user_file_name)

                # extract asset URLs and send to queues for concurrent downloading:
                """if pursue_assets:
                    videos_metadata, images_metadata, profile_pics_metadata = extract_assets_from_posts(self.insta_session.post_metadata_list, self.insta_session.user_metadata_list)
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

    def save_data_locally(self, seed: dict) -> str:
        posts = self.insta_session.post_metadata_list
        users = self.insta_session.user_metadata_list
        print(f"saving posts...")
        crawled_date = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        filename = f"instagram_from_{seed['start_date']}_to_{seed['end_date']}.jsonl"

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
                    # each 'row' is a dictionary of data from Instagram
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
        print(f"flushing data...")
        self.insta_session.post_metadata_list = []
        self.insta_session.user_metadata_list = []


    def find_lowest_post(self):
        lowest_post = None
        def is_post(my_href: str):
            post_pattern = "/[A-Za-z0-9-_.]+/p/[A-Za-z0-9-_.]+/?"
            reel_pattern = "/[A-Za-z0-9-_.]+/reel/[A-Za-z0-9-_.]+/?"

            if my_href.startswith('/p/'):
                return True
            if my_href.startswith('/reel/'):
                return True
            if re.match(post_pattern, my_href) is not None:
                return True
            if re.match(reel_pattern, my_href) is not None:
                return True
            return False

        result = self.insta_session.page.locator("a")

        for i in range(0, result.count()):
            j = result.count() - 1 - i # last index first
            elt = result.nth(j)
            print(elt.get_attribute('href'))
            if is_post(elt.get_attribute("href")):
                lowest_post = elt
                break
        return lowest_post

    def get_lowest_post_datetime_utc(self):
        # calculate date/time of lowest revealed post, and decide if it's time to stop:
        if not self.insta_session.post_metadata_list:
            return None
        try:
            lowest_post_datetime_utc = datetime.fromtimestamp(
                self.insta_session.post_metadata_list[-1]['caption']['created_at'])
        except Exception as e:
            try:
                lowest_post_datetime_utc = datetime.fromtimestamp(
                    self.insta_session.post_metadata_list[-1]['taken_at'])
            except:
                lowest_post_datetime_utc = datetime.fromtimestamp(
                    self.insta_session.post_metadata_list[-1]['media']['taken_at']) # To-do: sometimes self.insta_session.post_metadata_list == []
        return lowest_post_datetime_utc

    def scraper_user_home_page(self, seed, ch: BlockingChannel | None):
        handle = seed['handle']
        start_date = seed['start_date']
        total_scrolls = 0
        prev_post_url = None
        scrape_start_time = datetime.now(UTC)
        repeated_post_count = 0
        num_retries_lowest_post = 0
        internet_bad_count = 0
        while True:
            if self.failed_to_load_retry_button_appears() and self.insta_session.page.url == f"{BASE_URL}{handle}/": # this is catching old errors from previous homepages so make sure that we're in fact on the page to scrape
                return {'result': 'failed to load',
                        'time-started': str(scrape_start_time),
                        'time-taken': str(datetime.now(UTC) - scrape_start_time)
                        }

            try:
                if not self.insta_session.page.url == f"{BASE_URL}{handle}/":
                    try:
                        self.go_to_target_home_page(handle)
                    except Exception as e:
                        raise # TODO: add logic here!

                if not self.insta_session.page.url == f"{BASE_URL}{handle}/":
                    print("Logged out :/")
                    return {'result': 'logged out while scraping',
                            'time-started': str(scrape_start_time),
                            'time-taken': str(datetime.now(UTC) - scrape_start_time)
                            }

                # scroll:
                while True:
                    if (ch is not None) and (total_scrolls % 20 == 0):
                        if ch.is_open:
                            ch.connection.process_data_events(time_limit=0)
                        else:
                            print("WARNING: channel is closed !!")
                    retry_button = self.insta_session.page.get_by_role("button", name="Retry")
                    if retry_button.count() > 0:
                        print(f"oh no! provoked the 'Failed to Load / Retry' sequence")
                        # case 1: the account is private
                        account_is_private = self.insta_session.page.get_by_text("account is private")
                        if account_is_private.count() > 0:
                            print("account is private")
                            return {'result': 'account is private',
                                    'time-started': str(scrape_start_time),
                                    'time-taken': str(datetime.now(UTC) - scrape_start_time)}
                        else:
                            print("account is is not private")

                        # case 2: failed to load
                        failed_to_load_message = self.insta_session.page.get_by_text("Failed to Load")
                        print(f"failed to load message") if failed_to_load_message.count()>0 else print(f"no failed to load message")
                        return {'result': 'failed to load',
                                'time-started': str(scrape_start_time),
                                'time-taken': str(datetime.now(UTC) - scrape_start_time)
                                }

                    reload_button = self.insta_session.page.get_by_role('button', name='Reload page')
                    if reload_button.count() > 0:
                        print(f"oh no! provoked the 'Something went wrong / Reload' sequence")
                        something_went_wrong_message = self.insta_session.page.get_by_text("Something went wrong")
                        print(f"something went wrong message") if something_went_wrong_message.count() > 0 else print(f"no something went wrong message")
                        return {'result': 'something went wrong - reload',
                                'time-started': str(scrape_start_time),
                                'time-taken': str(datetime.now(UTC) - scrape_start_time)
                                }

                    # check if profile is available
                    profile_not_available = self.insta_session.page.get_by_text("Profile isn't available")
                    if profile_not_available.count() > 0:
                        print("Profile isn't available")
                        return {'result': 'profile is not available',
                                'time-started': str(scrape_start_time),
                                'time-taken': str(datetime.now(UTC) - scrape_start_time)
                                }

                    try:
                        lowest_post = self.find_lowest_post()
                        num_retries_lowest_post = 0
                        break
                    except Exception as e:
                        print(e)
                        if str(e) == "Target crashed":
                            # Instagram has crashed
                            return {'result': 'target crashed',
                                    'time-started': str(scrape_start_time),
                                    'time-taken': str(datetime.now(UTC) - scrape_start_time)
                                    }

                        else:
                            print(f"trying again...")
                            num_retries_lowest_post += 1
                            if num_retries_lowest_post > 5:
                                raise
                            sleep(5)


                if lowest_post is None:
                    if self.insta_session.page.get_by_text("Sorry, this page isn't available").count() > 0:
                        print(f"no posts found! :/")
                        return {'result': 'no posts',
                                'time-started': str(scrape_start_time),
                                'time-taken': str(datetime.now(UTC) - scrape_start_time)
                                }
                    elif self.insta_session.page.get_by_text("No Posts Yet").count() > 0:
                        print(f"no posts found! :/")
                        return {'result': 'no posts',
                                'time-started': str(scrape_start_time),
                                'time-taken': str(datetime.now(UTC) - scrape_start_time)
                                }
                    elif self.insta_session.page.get_by_text("This account is private").count() > 0:
                        print(f"no posts found! :/")
                        return {'result': 'no posts',
                                'time-started': str(scrape_start_time),
                                'time-taken': str(datetime.now(UTC) - scrape_start_time)
                                }
                    else:
                        return {'result': 'timeout error',
                                'time-started': str(scrape_start_time),
                                'time-taken': str(datetime.now(UTC) - scrape_start_time)
                                }

                try:
                    lowest_post_url = lowest_post.get_attribute('href')
                except Exception as e:
                    print(e)
                    raise

                lowest_post.scroll_into_view_if_needed()
                total_scrolls += 1

                lowest_post_datetime_utc = self.get_lowest_post_datetime_utc()
                if lowest_post_datetime_utc is None: # deals with the case where self.insta_session.post_metadata_list == []
                    return {'result': 'timeout error',
                            'time-started': str(scrape_start_time),
                            'time-taken': str(datetime.now(UTC) - scrape_start_time)}

                print(f"post date is {lowest_post_datetime_utc}")
                print(f"start date is {start_date}")

                if lowest_post_datetime_utc < datetime.strptime(
                        start_date, "%Y-%m-%d"):
                    print(f"post pre-dates the target start date! you have scrolled down far enough :)")
                    # exit post page:
                    return {'result': 'scraped until user-specified starting date was reached',
                            'time-started': str(scrape_start_time),
                            'time-taken': str(datetime.now(UTC)-scrape_start_time)
                            }

                if lowest_post_url == prev_post_url:
                    print(f"uh-oh! this is the same post we scrolled to on the last iteration!")
                    repeated_post_count += 1

                    if repeated_post_count > 15: # unsure why modular division...
                        if not internet_good():
                            internet_bad_count += 1
                            repeated_post_count = 0

                            if internet_bad_count > 10:
                                return {'result': 'bad internet',
                                        'time-started': str(scrape_start_time),
                                        'time-taken': str(datetime.now(UTC) - scrape_start_time)
                                        }

                    if repeated_post_count > 20:
                        return {'result': 'scraped until first ever post was reached',
                                'time-started': str(scrape_start_time),
                                'time-taken': str(datetime.now(UTC) - scrape_start_time)
                                }
                else:
                    repeated_post_count = 0

                prev_post_url = lowest_post_url

                print(f"scrolled {total_scrolls} times so far!")
                print(f"uncovered {len(self.insta_session.post_metadata_list)} posts!")
                print(f"uncovered {len(self.insta_session.user_metadata_list)} users!")
                print(f"scraper has been running for this long: {datetime.now(UTC)-scrape_start_time}")
                
                
                sleep(1)
            except TimeoutError as f:
                return {'result': 'timeout error',
                                    'time-started': str(scrape_start_time),
                                    'time-taken': str(datetime.now(UTC) - scrape_start_time)
                        }
            except Exception as f:
                print(f)
                raise f

    @sleep_before(2)
    def scrape_datetime(self):
        time_elts = self.insta_session.page.locator('time')
        if time_elts.count() == 0:
            print(f"no datetime found :/")
            return

        last_time_elt = time_elts.nth(time_elts.count()-1)
        utc_datetime_of_post = last_time_elt.get_attribute("datetime")
        return utc_datetime_of_post


    @sleep_after(5)
    def go_to_target_home_page(self, seed):
        target_home_page = f"https://www.instagram.com/{seed}/"
        self.insta_session.page.goto(target_home_page)


class InstagramSession:
    def __init__(self, username, password, want_headless, browser):
        self.username = username
        self.password = password
        self.want_headless = want_headless
        self.browser = browser
        self.page = self.open_page()
        self.responses = []
        self.xhr_bodies = []
        self.images = []
        self.videos = []
        self.user_metadata_list = []
        self.post_metadata_list = []

        self.page.on("response", self.intercept_response)

    def clear_popup_after_login_if_necessary(self, mobile):
        if mobile:
            label = 'Not now'
        else:
            label = "Not Now"

        try:
            self.page.get_by_role('button', name=label).nth(0).click(timeout=5000)
        except Exception as e:
            print(e)
            print('no pop-up window! continuing...')

    def flush_response_lists(self):
        self.xhr_bodies = []
        self.images = []
        self.videos = []
        self.responses = []

    def open_page(self):
        page = self.browser.new_page()
        # stealth_sync(page)  # stealthify
        return page

    def extract_media_name_from_url(self, media_url):
        for file_ext in ('.jpg', '.png', '.heic', '.mp4', '.gif', '.webp'):
            if media_url.find(file_ext) != -1:
                media_name = media_url[0:media_url.find(file_ext)] + file_ext
                media_name = media_name[media_name.rfind('/') + 1:]
                return media_name
        raise Exception

    def parse_data_from_feed(self, data):
        edges = data['edges']
        count = 0
        for edge in edges:
            node = edge['node']
            print(node['__typename'])
            if node['__typename'] == 'XDTFeedItem':
                if node['media'] is not None:
                    self.post_metadata_list.append(node)
                    count += 1
                else:
                    print(node['media'])
                    print(f"(ignoring)")
            elif node['__typename'] == 'XDTMediaDict':
                self.post_metadata_list.append(node)
                count += 1
            else:
                print(f"what is this??")
                # TODO: handle
                raise Exception
        print(f"appended {count} new data items!")
        return

    def handle_bulk_route_definition(self, body):
        body_decoded = body.decode('utf-8')
        if body_decoded.startswith('for (;;);'):
            body_decoded = body_decoded[len('for (;;);'):]
        my_dict = json.loads(body_decoded)
        data = my_dict['payload']
        data = data['payloads']
        print(data.keys())
        # self.responses.append((url, body))

    def intercept_response(self, response):
        # sift through web browser traffic, identifying assets (images and videos) and API responses
        if response.request.resource_type == 'xhr':
            while True:
                explore = False
                body = response.body()
                url = response.url

                # if the url starts with any of the following API directories, set explore=True:
                for api_directory in (
                        "https://www.instagram.com/api/graphql/",
                        "https://www.instagram.com/api/v1",
                        "https://www.instagram.com/graphql/"
                ):
                    if url.startswith(api_directory):
                        explore = True
                        break

                if explore:
                    my_dict = json.loads(body.decode('utf-8'))
                    if 'data' not in my_dict.keys():
                        if 'status' in my_dict.keys():
                            print(my_dict)
                            print(f'(ignoring)')
                            break
                        else:
                            print(f"unexpected!")
                            break
                    if 'xdt_notification_badge' in my_dict['data'].keys():
                        # print(my_dict['data'])
                        print(f"(ignoring)")
                        break
                    elif 'lightspeed_web_request_for_igd' in my_dict['data'].keys():
                        # print(my_dict['data'])
                        print(f"(ignoring)")
                        break
                    elif 'xdt_api__v1__feed__timeline__connection' in my_dict['data'].keys():
                        # TODO: these are timeline posts, you need to save them and extract images & videos
                        print(f"saving!")
                        self.parse_data_from_feed(my_dict['data']['xdt_api__v1__feed__timeline__connection'])
                    elif 'user' in my_dict['data'].keys():
                        print(f"saving user data!")
                        self.user_metadata_list.append(my_dict['data']['user'])
                    elif 'highlights' in my_dict['data'].keys():
                        print(f"(ignoring highlights)")
                        break
                    elif 'xdt_api__v1__feed__user_timeline_graphql_connection' in my_dict['data'].keys():
                        self.parse_data_from_feed(my_dict['data']['xdt_api__v1__feed__user_timeline_graphql_connection'])
                    elif 'xdt_api__v1__discover__chaining' in my_dict['data'].keys():
                        data = my_dict['data']['xdt_api__v1__discover__chaining']
                        users = data['users']
                        self.user_metadata_list += users
                    elif 'xdt_api__v1__media__shortcode__web_info' in my_dict['data'].keys():
                        data = my_dict['data']['xdt_api__v1__media__shortcode__web_info']
                        for item in data['items']:
                            self.post_metadata_list.append(item)
                    else:
                        print('unhandled data!')
                        # TODO: handle dict_keys(['fetch__XDTMediaDict'])
                        raise Exception
                else:
                    # explore = False
                    if url.find('.js')==-1: # not javascript
                        if url.startswith("https://www.instagram.com/ajax/bulk-route-definitions/"):
                            self.handle_bulk_route_definition(body)
                        else:
                            print(f"funky url: {url}")
                            print(body)
                print(f"=================================")
                break
        else: # all non-XHR requests are dropped
            pass
        return response

    def check_if_logged_in(self):
        return False  # TODO: figure out how to know if we're logged in / not

    def close_browser(self):
        self.page.close()

    @sleep_before(10)
    @sleep_after(10)
    def log_in_to_instagram(self, mobile):
        if mobile:
            self.page.get_by_role('button', name='Log in').click()
            time.sleep(5)
        self.page.get_by_label('Phone number, username, or email').fill(self.username)
        time.sleep(1)
        self.page.get_by_label('Password').fill(self.password)
        time.sleep(1)
        if mobile:
            self.page.get_by_role('button', name='Log in').click() # TODO: doesn't seem to work when mobile=False
        else:
            self.page.get_by_role('button', name='Log in').nth(0).click()

    def click_prev_post(self):
        try:
            # find somewhere neutral to position the mouse
            comment_button = self.page.get_by_label("Add a comment") # TODO: not foolproof, as some posts have comments disabled
            if comment_button.count() == 0:
                comment_button = self.page.get_by_text("Comments on this post have been limited.")
                comment_button.count()
            comment_button.hover()

            go_back_button = self.page.get_by_role('button').locator("css=svg[aria-label='Go back']")
            assert go_back_button.count() == 1
            go_back_button.click(timeout=10000)

            return True

        except Exception as e:
            try:
                self.page.keyboard.press("ArrowLeft")
                return True
            except:
                print(e)
                raise

    def click_next_post(self):
        try:
            svgs = self.page.locator('svg')
            svgs = [svgs.nth(i) for i in range(0, svgs.count())]
            nexts = [svg for svg in svgs if svg.get_attribute('aria-label') == "Next"]

            if len(nexts) == 0:
                return False

            elif len(nexts) > 1:
                self.page.keyboard.press("ArrowRight")
                return True

            else: # len(nexts) == 1
                next = nexts[0]
                try:
                    next.click()
                    return True
                except Exception as e:
                    print(e)
                    raise
        except Exception as e:
            print(e)
            raise

    @sleep_after(10)
    def get_page(self, url):
        self.page.goto(url)


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
        return datetime.strptime(my_date, "%Y-%m-%dT%H:%M:%SZ").date()