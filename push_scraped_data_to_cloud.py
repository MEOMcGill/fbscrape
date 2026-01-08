import os
import json
from tqdm import tqdm
import boto3
from facebook_scraper.post_scraper import load_data
from facebook_scraper.config import handles_queue, AWS_ACCESS_KEY, AWS_SECRET_KEY, USERS_DIR, PARTS_DIR, POSTS_DIR

def push_post_and_user_metadata_to_cloud(s3_parent_folder: str, s3_target_bucket: str):
    s3_folder = s3_parent_folder
    s3_client = boto3.client("s3",
                             aws_access_key_id=AWS_ACCESS_KEY,
                             aws_secret_access_key=AWS_SECRET_KEY,
                             )

    print(f"pushing user data to S3 bucket {s3_target_bucket}...")
    for user_file_name in tqdm(os.listdir(USERS_DIR)):
        my_user_object = s3_folder + "users/" + user_file_name
        print(f"uploading {user_file_name} as {my_user_object}...")
        response = s3_client.upload_file(os.path.join(USERS_DIR, user_file_name), s3_target_bucket, my_user_object)

    ################################################################################

    print(f"pushing post data to S3 bucket {s3_target_bucket}...")
    if len(os.listdir(PARTS_DIR)) > 0:
        raise Exception(f"directory {PARTS_DIR} is not empty")

    post_file_names = [x for x in os.listdir(POSTS_DIR)]
    for post_file_name in tqdm(post_file_names):
        post_file_path = os.path.join(POSTS_DIR, post_file_name)

        data = load_data(post_file_path)
        if len(data) == 0:
            continue

        # there's at least one record to upload
        part = 0
        step_size = 10000
        while part * step_size < len(data):
            data_part = data[part * step_size:(part + 1) * step_size]
            # save data_part to new file with name derived from original file
            new_file_name = post_file_name.rstrip('.jsonl') + f'_part{part}.jsonl'
            new_file_path = os.path.join(PARTS_DIR, new_file_name)
            print(f"saving part {part} of data to {new_file_name}")

            with open(new_file_path, "a") as my_file:
                for record in data_part:
                    my_file.write(json.dumps(record) + '\n')
            part += 1

    # push parts to S3:
    for new_file_name in tqdm(os.listdir(PARTS_DIR)):
        new_file_path = os.path.join(PARTS_DIR, new_file_name)
        my_post_object = s3_folder + "posts/" + new_file_name
        print(f"pushing {new_file_name} to {s3_target_bucket} as {my_post_object}")
        response = s3_client.upload_file(new_file_path, s3_target_bucket, my_post_object)
    print(f"successfully pushed data file to S3!")
    return

def push_folder(s3_parent_folder: str, s3_target_bucket: str, folder_name: str):
    if os.path.isdir(folder_name) == False:
        raise ValueError
    s3_folder = s3_parent_folder
    s3_client = boto3.client("s3",
                             aws_access_key_id=AWS_ACCESS_KEY,
                             aws_secret_access_key=AWS_SECRET_KEY,
                             )

    for i in os.listdir(folder_name):
        full_path: str = os.path.join(folder_name, i)
        my_post_object = s3_folder + "posts/" + i
        print(f"pushing {full_path} to {s3_target_bucket} as {my_post_object}")
        response = s3_client.upload_file(full_path, s3_target_bucket, my_post_object)

if __name__ == "__main__":
    s3_target_bucket = "meo-raw-data"
    s3_parent_folder = "facebook/scraper/"
    push_post_and_user_metadata_to_cloud(s3_parent_folder, s3_target_bucket)
    #push_folder(s3_parent_folder, s3_target_bucket, "/Users/mikad/MEOMcGill/instagram-scraper/instagram-scraper/data/old_parts")