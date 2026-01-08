"""
subscribe a consumer to a Rabbit queue and launch the instagram post scraper
"""

from .config import (handles_queue, AWS_ACCESS_KEY as aws_access_key, \
                     AWS_SECRET_KEY as aws_secret_key, video_queue, image_queue, pikaparams as pika_parameters,
                     API_BASE_URL as api_base, MEOAPI_USERNAME, MEOAPI_PASSWORD,
                     AUTH_DIR, PARTS_DIR, POSTS_DIR, USERS_DIR, IMAGES_DIR, VIDEOS_DIR, meo_api_queue)
from .rabbit_mq_utilities import get_channel, send_data_to_queue
import json
from .post_scraper import FacebookPostScraper
from .api_clients import insert_crawler_history, get_bearer_token
import time
from pika.adapters.blocking_connection import BlockingChannel

def consumer(username, password):
    def callback(ch: BlockingChannel, method, properties, body):
        if facebook_scraper.scraped_so_far > batch_size:
            print(f"{batch_size} accounts scraped consecutively. Resting for {rest_time} seconds")
            ch.basic_nack(delivery_tag=method.delivery_tag)
            # facebook_scraper.facebook_session.page.goto("https://nytimes.com")
            time.sleep(rest_time)
            facebook_scraper.scraped_so_far = 0
        else:
            # Receive and parse message
            message = json.loads(body.decode("utf-8"))

            """TODO: decide if it's time for the scraper to rest/not, based on a count of how much
            work it's done so far. to rest: give a nack acknowledgment, close the playwright browser and
             then sleep for a number of seconds
            """

            metadata = facebook_scraper.scrape_seed(
                message, ch
            )

            if metadata['result'] not in facebook_scraper.retry_cases:
                # record progress to API
                send_data_to_queue([
                    {"api_base": api_base,
                     "message_id": message['ID'],
                     "message_start_date": message['start_date'],
                     "message_end_date": message['end_date'],}
                ], target_queue=meo_api_queue, channel=ch)

                # insert to crawler history
                token = get_bearer_token(MEOAPI_USERNAME, MEOAPI_PASSWORD)
                api_response = insert_crawler_history(token, api_base, message['ID'], message['start_date'], message['end_date'])
                if api_response.json()['result'] != 'created':
                     raise Exception(f"Failure writing to scraper history: API Response: {api_response}")

                # Contact queue to acknowledge task completion
                print(f"pausing before moving on to next seed...")
                time.sleep(5)
                ch.basic_ack(delivery_tag=method.delivery_tag)
                print("task complete!")
            else:
                print("task failed - retry")
                ch.basic_nack(delivery_tag=method.delivery_tag)


    facebook_scraper = FacebookPostScraper(
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
                 crash_cases)

    facebook_scraper.identify_country_from_which_you_are_scraping()

    if facebook_scraper.ip_country is None:
        input('VPN ok?')
    elif facebook_scraper.ip_country == 'CA':
        print("WARNING: You are scraping from Canada -> C-18 Meta news ban may mask certain accounts")
    else:
        print(f"scraping from {facebook_scraper.ip_country}")

    facebook_scraper.create_playwright_instance()
    facebook_scraper.initialize_facebook_session()
    facebook_scraper.log_in_if_necessary()

    input_channel = get_channel()
    input_channel.queue_declare(queue=handles_queue, durable=True)
    input_channel.basic_qos(prefetch_count=1)
    input_channel.basic_consume(on_message_callback=callback, queue=handles_queue)
    print("listening for handles to look up...")
    input_channel.start_consuming()


def run(username, password):
    consumer(
        username,
        password
    )

image_backlog_rest_time = 100
video_backlog_rest_time = 300
rest_time = 300 # 1000
batch_size = 100 # 100
seed_list_type = "handle"
headless = False
mobile = False
want_cloud_storage = True
log_in_fresh = False
auth_dir = AUTH_DIR
posts_dir = POSTS_DIR
users_dir = USERS_DIR
image_dir = IMAGES_DIR
video_dir = VIDEOS_DIR
parts_dir = PARTS_DIR
s3_target_bucket = "meo-raw-data"
s3_parent_folder = "facebook/scraper/"
retry_cases = ['bad internet', 'timeout error']
success_cases = (
    'scraped until user-specified starting date was reached',
    'scraped until first ever post was reached',
    'no posts',
    'account is private',
    'profile is not available'
)
crash_cases = (
    'bad internet',
    'failed to load',
    'target crashed',
    'something went wrong - reload',
    'logged out while scraping'
)