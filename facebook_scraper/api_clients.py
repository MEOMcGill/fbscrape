from datetime import datetime
from .config import MEOAPI_PASSWORD, MEOAPI_USERNAME
import requests
from .config import API_BASE_URL
from urllib.parse import urljoin


def get_bearer_token(username: str, password: str) -> str:
    with requests.session() as session:
        resp = session.post(urljoin(API_BASE_URL, "/meologin"), params={"username": username, "password": password})
    if resp.status_code == 200:
        access_token = resp.json()["access_token"]
        return access_token
    else:
        return None


def get_seedlist_new():
    TOKEN = get_bearer_token(MEOAPI_USERNAME, MEOAPI_PASSWORD)

    headers = {
        "Authorization": f"Bearer {TOKEN}"
    }

    target_url = urljoin(API_BASE_URL, "/phh/seedlist?query=Platform:facebook")
    with requests.session() as session:
        response = session.get(target_url, headers=headers, verify=True)

    data = response.json()
    return data

def validate_or_sanitize_date(my_date):
    try:
        datetime.strptime(my_date, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        try:
            # suppose it's in %Y-%m-%d format:
            my_date = datetime.strptime(my_date, '%Y-%m-%d')
            my_date = my_date.strftime('%Y-%m-%dT%H:%M:%SZ')
        except:
            raise("convert date to %Y-%m-%d or %Y-%m-%dT%H:%M:%SZ formats")
    return my_date

def insert_crawler_history(token: str, url: str, phh_id: str, start_date: str, end_date: str):
    start_date = validate_or_sanitize_date(start_date)
    end_date = validate_or_sanitize_date(end_date)

    params = {
        "phh_id": str(phh_id),
        "start_date": start_date,
        "end_date": end_date
    }

    headers = {
        "Authorization": f"Bearer {token}"  # Add Bearer token to the headers
    }
    response = requests.get(urljoin(url,"/phh/insert/crawler_history"), params=params, headers=headers,
                            verify=True)
    return response


def get_crawler_histories() -> list[dict]:
    token = get_bearer_token(MEOAPI_USERNAME, MEOAPI_PASSWORD)

    headers = {
        "Authorization": f"Bearer {token}"  # Add Bearer token to the headers
    }
    response = requests.get(urljoin(API_BASE_URL, "/phh/get/crawler_history?query=Platform:facebook"), headers=headers,
                            verify=True)

    if not response.status_code == 200:
        raise Exception(response.text)
    data = response.json()
    return data

def get_gaps_api() -> list[dict]:
    token = get_bearer_token(MEOAPI_USERNAME, MEOAPI_PASSWORD)

    headers = {
        "Authorization": f"Bearer {token}"  # Add Bearer token to the headers
    }
    response = requests.get(urljoin(API_BASE_URL, "/phh/historical_seedlist?query=Platform:facebook"), headers=headers,
                            verify=True)

    if not response.status_code == 200:
        raise Exception(response.text)
    data = response.json()
    return data

