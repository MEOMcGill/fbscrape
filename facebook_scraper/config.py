import configparser, os, platform, pika, json

HOME_DIR = (
    os.environ["USERPROFILE"] if platform.system() == "Windows" else os.environ["HOME"]
)

HOME_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

my_config_file = os.path.join(HOME_DIR, "meo_facebook_scraper_config.cfg")
config = configparser.ConfigParser()
config.read(my_config_file)

AWS_REGION = config["aws"]["region"]
AWS_ACCESS_KEY = config["aws"]["access-key-1"]
AWS_SECRET_KEY = config["aws"]["secret-key-1"]
PROJECT_DIR = os.path.join(HOME_DIR, config["paths"]["project-dir"])

AUTH_DIR = os.path.join(HOME_DIR, config["paths"]["auth-dir"])
POSTS_DIR = os.path.join(HOME_DIR, config["paths"]["posts-dir"])
USERS_DIR = os.path.join(HOME_DIR, config["paths"]["users-dir"])
PARTS_DIR = os.path.join(HOME_DIR, config["paths"]["parts-dir"])
IMAGES_DIR = os.path.join(HOME_DIR, config["paths"]["images-dir"])
VIDEOS_DIR = os.path.join(HOME_DIR, config["paths"]["videos-dir"])

# old compressed folder
COMPRESSED = os.path.join(HOME_DIR, "old")
COMPRESSED_AUTH_DIR = os.path.join(HOME_DIR, "data", "compressed", config["paths"]["auth-dir"])
COMPRESSED_POSTS_DIR = os.path.join(HOME_DIR, "data", "compressed", config["paths"]["posts-dir"])
COMPRESSED_USERS_DIR = os.path.join(HOME_DIR, "data", "compressed", config["paths"]["users-dir"])
COMPRESSED_PARTS_DIR = os.path.join(HOME_DIR, "data", "compressed", config["paths"]["parts-dir"])
COMPRESSED_IMAGES_DIR = os.path.join(HOME_DIR, "data", "compressed", config["paths"]["images-dir"])
COMPRESSED_VIDEOS_DIR = os.path.join(HOME_DIR, "data", "compressed", config["paths"]["videos-dir"])

for my_dir in AUTH_DIR, POSTS_DIR, USERS_DIR, PARTS_DIR, IMAGES_DIR, VIDEOS_DIR:
    if not os.path.exists(my_dir):
        try:
            os.mkdir(my_dir)
        except:
            raise Exception(my_dir + " does not exist")


USERNAME_1 = config["facebook-login"]["username-1"]
PASSWORD_1 = config["facebook-login"]["password-1"]

facebook_logins = {
    "casey": {"username": USERNAME_1, "password": PASSWORD_1},
}

if not os.path.exists(
    os.path.join(AUTH_DIR, f"{USERNAME_1.lower()}_login.json")
):
    with open(os.path.join(AUTH_DIR, f"{USERNAME_1.lower()}_login.json"), "w") as f:
        json.dump(facebook_logins, f)

SCREEN_WIDTH = int(config["browser-specs"]["screen-width"])
SCREEN_HEIGHT = int(config["browser-specs"]["screen-height"])

MEOAPI_USERNAME = config["meo-api-credentials"]["username"]
MEOAPI_PASSWORD = config["meo-api-credentials"]["password"]
API_BASE_URL = config["meo-api-credentials"]["domain"]

pikaparams = pika.ConnectionParameters(
    config["rabbit-mq"]["host"],
    credentials=pika.PlainCredentials(
        config["rabbit-mq"]["user"], config["rabbit-mq"]["password"]
    ),
    heartbeat=int(config["rabbit-mq"]["heartbeat"]),
    blocked_connection_timeout=int(config["rabbit-mq"]["blocked-connection-timeout"]),
)

video_queue = config["rabbit-mq"]["videos-to-retrieve-from-insta-cdn-queue"]
image_queue = config["rabbit-mq"]["images-to-retrieve-from-insta-cdn-queue"]
handles_queue = config["rabbit-mq"]["handles-to-scrape-queue"]
meo_api_queue = config["rabbit-mq"]["meo-api-messages-queue"]