from .config import video_queue, AWS_ACCESS_KEY, AWS_SECRET_KEY
from .rabbit_mq_utilities import get_channel
import json
import os
from time import sleep
import requests
import boto3
from datetime import datetime
import pandas as pd

HEADERS = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br, zstd',
    }


def push_video_to_s3(video_file, video_file_path, s3_target_bucket, s3_folder):
    s3_client = boto3.client("s3",
                             aws_access_key_id=AWS_ACCESS_KEY,
                             aws_secret_access_key=AWS_SECRET_KEY,
                             )

    print(f"pushing video to S3...")
    print(f"pushing {video_file_path} to S3 bucket {s3_target_bucket}...")
    my_object = s3_folder + video_file
    response = s3_client.upload_file(video_file_path, s3_target_bucket, my_object)

def get_video_name(post_id: str, video_url: str) -> str:
    video_name = video_url[video_url.rfind('/') + 1:]
    video_name = video_name[:video_name.find('mp4')+len('.mp4')-1]
    video_name = f"{post_id}_{video_name}"
    return video_name


def fetch_video(video_url: str) -> bytes:
    total_permissible_retries = 1
    num_retries = 0
    sleep_time = 5
    # retry = False
    while True:
        try:
            resp = requests.get(video_url, headers=HEADERS)
            content = resp.content
            break
        except Exception as e:
            print(e)
            if isinstance(e, requests.exceptions.ChunkedEncodingError):
                print(e)
            elif isinstance(e, requests.exceptions.ConnectionError):
                print(e)
            else:
                raise

        num_retries += 1
        if num_retries > total_permissible_retries:
            print(f"pausing for {sleep_time} seconds before retrying...")
            sleep(sleep_time)
            sleep_time = sleep_time * 2  # exponential backoff
        else:
            # return None
            content = None
            break
    return content


def save_video_locally(video: bytes, video_full_path: str):
    if not isinstance(video, bytes):
        print(f"expected bytes of mp4 video but received {type(video)}")
        raise Exception

    if os.path.exists(video_full_path):
        os.remove(video_full_path)

    with open(video_full_path, "wb") as binary_file:
        binary_file.write(video)
    print(f"saved {video_full_path}")
    return


def record_outcome_locally(message: dict):
    video_dir = message["video-dir"]
    video_metadata_path = os.path.join(video_dir, "video_metadata.csv")

    df = pd.DataFrame.from_records([message])
    df['time_of_record'] = datetime.utcnow()

    if os.path.exists(video_metadata_path):
        df.to_csv(video_metadata_path, index=False, encoding='utf-8-sig', mode='a', header=None)
    else:
        df.to_csv(video_metadata_path, index=False, encoding='utf-8-sig')


def consumer():
    input_channel = get_channel()
    input_channel.queue_declare(queue=video_queue, durable=True)

    def callback(ch, method, properties, body):
        # Receive and parse message:
        message = json.loads(body.decode("utf-8"))
        # seed_id = message['seed_id']
        post_id = message['post_id']
        video_url = message['video_url']
        # handle = message["handle"]
        video_dir = message["video-dir"]
        s3_target_bucket = message["s3-bucket"]
        # s3_parent_folder = message["s3-parent-folder"]
        # seed_list_name = message["seed-list-name"]
        # start_date = message["start-date"]
        handle = message['aws-dir'][len('facebook/scraper/'):]
        handle = handle[:handle.find('/')]
        aws_dir = f"facebook/scraper/videos/{handle}/"

        if not os.path.exists(video_dir):
            print(f"{video_dir} does not exist. skipping video")
        else:
            print(f"retrieving video destined for @{aws_dir}...")

            # video name:
            video_name = get_video_name(post_id, video_url)
            video_full_path = os.path.join(video_dir, video_name)
            video_bytes = fetch_video(video_url)

            # Save data locally
            if video_bytes is not None:
                save_video_locally(video_bytes, video_full_path)

                # check if file was suspiciously small:
                video_file_size_mb = os.path.getsize(video_full_path) / (1024.0 ** 2)
                print(f"file size was {video_file_size_mb} megabytes")
                if video_file_size_mb < 0.25:
                    if video_name.endswith('video_dashint.mp4'):
                        print(f"file is suspiciously small!")

                # Push video to S3
                push_video_to_s3(video_name,
                                 video_full_path,
                                 s3_target_bucket,
                                 aws_dir,
                                 )
                # Delete video locally
                os.remove(video_full_path)
                print(f"successfully deleted file locally")

            else:
                record_outcome_locally(message)

        # Contact queue to acknowledge task completion
        ch.basic_ack(delivery_tag=method.delivery_tag)
        print("acknowledged task completion!")

    input_channel.basic_qos(prefetch_count=1)
    input_channel.basic_consume(on_message_callback=callback, queue=video_queue)
    print("listening for videos...")
    input_channel.start_consuming()

if __name__ == "__main__":
    consumer()
