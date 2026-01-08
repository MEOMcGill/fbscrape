# Facebook Scraper Conversion Walkthrough

We have successfully converted the `instagram_scraper` codebase into a `facebook_scraper` package.

## Changes Made

### 1. Directory Structure and Configuration
- **Renamed** `instagram_scraper/` directory to `facebook_scraper/`.
- **Renamed** `meo_instagram_scraper_config.cfg` to `meo_facebook_scraper_config.cfg`.
- **Updated** config file headers to `[facebook-login]`.

### 2. Code Refactoring

#### `facebook_scraper/config.py`
- Updated to read `meo_facebook_scraper_config.cfg`.
- Replaced `instagram_logins` with `facebook_logins`.

#### `facebook_scraper/api_clients.py`
- Updated API queries to filter by `Platform:facebook`.

#### `facebook_scraper/consume_post_scraper.py`
- Updated imports to use `FacebookPostScraper`.
- Updated S3 paths to `facebook/scraper/`.

#### `facebook_scraper/post_scraper.py`
- **Class Implementation**: Renamed `InstaPostScraper` to `FacebookPostScraper` and `InstagramSession` to `FacebookSession`.
- **Base URLs**: Set to `https://www.facebook.com`.
- **Logic Adaptation**:
    - Updated `go_to_target_home_page` to use Facebook URLs.
    - Updated `is_post` to detect Facebook post patterns (`/posts/`, `/permalink.php`, etc.).
    - **Neutralized** Instagram-specific network interception logic (GraphQL parsing) to prevent crashes on Facebook pages.
    - Updated `get_lowest_post_datetime_utc` to handle cases where no metadata is captured (safe fallback).
    - Replaced `UTC` with `timezone.utc` for Python 3.10 compatibility.
- **Date Extraction**: Implemented DOM-based date parsing strategies:
    - **`aria-label`**: Extracts date strings from link attributes (e.g., "July 24 at 5:00 PM").
    - **`abbr`**: checks for abbreviation tags used for timestamps.
    - **`parse_facebook_date`**: A helper function to converting Facebook's relative and absolute date strings into UTC datetimes.

### 3. Run Scripts
- **`produce_facebook_scrape_jobs.py`**: Created (renamed from Instagram version) to fetch Facebook seed lists and push jobs to RabbitMQ.
- **`run_consume_post_scraper.py`**: Updated to use `facebook_scraper` and `facebook_logins`.
- **`run_image_consumer.py` & `run_video_consumer.py`**: Updated imports to `facebook_scraper`.
- **`push_scraped_data_to_cloud.py`**: Updated config and S3 paths.

## Prerequisites
- **RabbitMQ Server**: You must have a RabbitMQ server running locally.
    - **Docker**: `docker run -d --hostname my-rabbit --name some-rabbit -p 15672:15672 -p 5672:5672 rabbitmq:3-management`
    - **Linux**: `sudo systemctl start rabbitmq-server`

## Usage Instructions

1.  **Configure Credentials**:
    Edit `meo_facebook_scraper_config.cfg` and add your Facebook credentials under `[facebook-login]`.

2.  **Generate Scrape Jobs**:
    Fetch the seed list and populate the RabbitMQ queue:
    ```bash
    python produce_facebook_scrape_jobs.py
    ```

3.  **Run the Scraper**:
    Start the scraper consumer:
    ```bash
    python run_consume_post_scraper.py --credentials casey
    ```
    (Replace `casey` with the credential key defined in your config if different).

4.  **Run Asset Consumers (Optional)**:
    To download images/videos found by the scraper:
    ```bash
    python run_image_consumer.py
    python run_video_consumer.py
    ```
