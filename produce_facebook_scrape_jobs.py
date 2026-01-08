from facebook_scraper.config import handles_queue, AWS_ACCESS_KEY, AWS_SECRET_KEY, USERS_DIR, PARTS_DIR, POSTS_DIR, MEOAPI_USERNAME, MEOAPI_PASSWORD
from facebook_scraper.config import *
from facebook_scraper.rabbit_mq_utilities import send_data_to_queue
from facebook_scraper.api_clients import get_gaps_api, get_bearer_token
import pandas as pd
from datetime import datetime, timedelta, timezone
import os
import shutil
import requests

# from facebook_scraper.rabbit_mq_utilities import get_channel
# def queue_backlogged(my_queue, backlog_threshold):
#     channel = get_channel()
#     status = channel.queue_declare(queue=my_queue, durable=True)
#     num_messages = status.method.message_count
#     return num_messages > backlog_threshold

def convert_to_date(my_date):
    try:
        return datetime.strptime(my_date, "%Y-%m-%d").date()
    except:
        return datetime.strptime(my_date, "%Y-%m-%dT%H:%M:%SZ").date()

def get_gaps():
    df = pd.DataFrame.from_records(get_gaps_api())

    # add conditions here
    #mask = ~df["MainType"].str.contains("foreign")
    #df = df[mask]
    ####

    seeds_df = df[['ID', 'SeedID', 'Handle', 'Collection']]
    seeds_df = seeds_df.drop_duplicates('ID')
    seeds_df = seeds_df.rename(columns={'Handle': 'handle'})
    #TODO REMOVE AFTER TEST
    seeds_df = seeds_df.head(3)
    seeds = seeds_df.to_dict('records')

    for seed in seeds:
        temp_df = df.loc[df['ID'] == seed['ID'], :]

        if temp_df.empty:
            raise Exception("/historical_seedlist has no record of this handle -- this should not happen")

        # among the gaps, find the earliest start date and latest end date, and scrape those
        start_dates = list(set(temp_df['missing_start_date']))
        start_dates = [convert_to_date(my_date) for my_date in start_dates]
        start_date = min(start_dates) - timedelta(days=3)

        end_dates = list(set(temp_df['missing_end_date']))
        end_dates = [convert_to_date(my_date) for my_date in end_dates]
        end_date = max(end_dates)
        end_date = min(end_date, datetime.now(timezone.utc).date())

        seed['start_date'] = start_date.strftime('%Y-%m-%d')
        seed['end_date'] = end_date.strftime('%Y-%m-%d')

    for seed in seeds:
        assert 'start_date' in seed.keys()
        assert 'end_date' in seed.keys()

    return seeds

if __name__ == "__main__":
    seed_list_name = "main"

    dir_to_compressed_dir = {
        USERS_DIR: COMPRESSED_USERS_DIR,
        POSTS_DIR: COMPRESSED_POSTS_DIR,
        PARTS_DIR: COMPRESSED_PARTS_DIR,
    }

    for my_dir in [USERS_DIR, POSTS_DIR, PARTS_DIR]:
        if len(os.listdir(my_dir)) > 0:
            today: str = datetime.now().strftime("%Y-%m-%d")
            # saved compressed version
            archive_name = shutil.make_archive(
                os.path.join(
                    f"{dir_to_compressed_dir[my_dir]}",
                    f"{my_dir.split(' / ')[-1]}_{today}"
                ),
                "zip",
                my_dir,
            )
            print(f'{my_dir} is nonempty.\nmoving compressed to {dir_to_compressed_dir[my_dir]}')
            if not os.path.exists(dir_to_compressed_dir[my_dir]):
                shutil.move(archive_name, dir_to_compressed_dir[my_dir])
            else:
                os.remove(
                    os.path.join(dir_to_compressed_dir[my_dir], archive_name)
                )

            for my_file in os.listdir(my_dir):
                os.remove(os.path.join(my_dir, my_file))

    scrape_ranges = get_gaps()
    for seed in scrape_ranges:
        seed["seed_list_name"] = seed_list_name

    for seed in scrape_ranges:
        print(seed)
        print(f"================================")
    send_data_to_queue(scrape_ranges, handles_queue)
    print(f"done pushing jobs to queue")