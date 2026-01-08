from .config import image_queue, AWS_ACCESS_KEY, AWS_SECRET_KEY
from .rabbit_mq_utilities import get_channel
import json
import os
from time import sleep
import requests
import boto3
from datetime import datetime
import pandas as pd

IMAGE_EXTENSIONS = ('.jpg', '.png', '.heic', '.webp')

HEADERS = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br, zstd',
    }


def push_image_to_s3(image_file, image_file_path, s3_target_bucket, s3_folder):
    s3_client = boto3.client("s3",
                             aws_access_key_id=AWS_ACCESS_KEY,
                             aws_secret_access_key=AWS_SECRET_KEY,
                             )

    print(f"pushing image to S3...")
    print(f"pushing {image_file_path} to S3 bucket {s3_target_bucket}...")
    my_object = s3_folder + image_file
    response = s3_client.upload_file(image_file_path, s3_target_bucket, my_object)

def get_image_name(post_id: str, image_url: str) -> str:
    image_name = image_url[image_url.rfind('/') + 1:]

    found_extension = False
    for extension in IMAGE_EXTENSIONS:
        if image_name.find(extension) != -1:
            found_extension = True
            image_name = image_name[:image_name.find(extension)]
            image_name = f"{post_id}_{image_name}.png"  # force every image type to be PNG

    if not found_extension:
        raise Exception
    return image_name


def fetch_image(image_url: str) -> bytes:
    total_permissible_retries = 1
    num_retries = 0
    sleep_time = 5
    # retry = False
    while True:
        try:
            resp = requests.get(image_url, headers=HEADERS)
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


def save_image_locally(image: bytes, image_full_path: str):
    if not isinstance(image, bytes):
        print(f"expected bytes of image but received {type(image)}")
        raise Exception

    if os.path.exists(image_full_path):
        os.remove(image_full_path)

    with open(image_full_path, "wb") as binary_file:
        binary_file.write(image)
    print(f"saved {image_full_path}")
    return


def record_outcome_locally(message: dict):
    image_dir = message["image-dir"]
    image_metadata_path = os.path.join(image_dir, "image_metadata.csv")

    df = pd.DataFrame.from_records([message])
    df['time_of_record'] = datetime.utcnow()

    if os.path.exists(image_metadata_path):
        df.to_csv(image_metadata_path, index=False, encoding='utf-8-sig', mode='a', header=None)
    else:
        df.to_csv(image_metadata_path, index=False, encoding='utf-8-sig')


def consumer():
    input_channel = get_channel()
    input_channel.queue_declare(queue=image_queue, durable=True)

    def callback(ch, method, properties, body):
        # Receive and parse message:
        message = json.loads(body.decode("utf-8"))
        if 'profile_pic_url' not in message.keys():
            post_id = message['post_id']
            image_url = message['image_url']
        else:
            post_id = message['handle']
            image_url = message['profile_pic_url']
        image_dir = message["image-dir"]
        s3_target_bucket = message["s3-bucket"]
        handle = message['aws-dir'][len('facebook/scraper/'):]
        handle = handle[:handle.find('/')]
        aws_dir = f"facebook/scraper/images/{handle}/"

        if not os.path.exists(image_dir):
            print(f"{image_dir} does not exist. skipping image")
        else:
            image_name = get_image_name(post_id, image_url)
            image_full_path = os.path.join(image_dir, image_name)

            if not os.path.exists(image_full_path):
                print(f"retrieving image destined for @{aws_dir}...")
                image_bytes = fetch_image(image_url)

                if not os.path.exists(image_full_path):
                    # Save data locally
                    if image_bytes is not None:
                        save_image_locally(image_bytes, image_full_path)

            # Check if file was suspiciously small:
            image_file_size_mb = os.path.getsize(image_full_path) / (1024.0 ** 2)
            print(f"file size was {image_file_size_mb} megabytes")

            # Push image to S3
            try:
                push_image_to_s3(image_name,
                                 image_full_path,
                                 s3_target_bucket,
                                 aws_dir
                                 )
            except Exception as e:
                print(e)


            # Delete image locally
            try:
                os.remove(image_full_path)
                print(f"successfully deleted file locally")
            except FileNotFoundError:
                print(f"no need to delete file -- no longer exists!")

        # Contact queue to acknowledge task completion
        ch.basic_ack(delivery_tag=method.delivery_tag)
        print("acknowledged task completion!")

    input_channel.basic_qos(prefetch_count=1)
    input_channel.basic_consume(on_message_callback=callback, queue=image_queue)
    print("listening for images...")
    input_channel.start_consuming()

if __name__ == "__main__":
    consumer()
