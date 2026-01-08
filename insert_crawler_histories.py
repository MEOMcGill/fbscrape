from facebook_scraper.rabbit_mq_utilities import get_channel
from facebook_scraper.api_clients import get_bearer_token, insert_crawler_history
from facebook_scraper.config import MEOAPI_PASSWORD, MEOAPI_USERNAME, meo_api_queue
import json

def consumer():
    def callback(ch, method, properties, body):
        message = json.loads(body.decode("utf-8"))
        # insert crawler histories:
        api_response = insert_crawler_history(token, message['api_base'], message['message_id'], message['message_start_date'],
                                              message['message_end_date'])
        if api_response.json()['result'] != 'created':
            raise Exception(f"Failure writing to scraper history: API Response: {api_response}")

        # Contact queue to acknowledge task completion
        ch.basic_ack(delivery_tag=method.delivery_tag)
        print("task complete!")

    token = get_bearer_token(MEOAPI_USERNAME, MEOAPI_PASSWORD)
    input_channel = get_channel()
    input_channel.queue_declare(queue=meo_api_queue, durable=True)
    input_channel.basic_qos(prefetch_count=1)
    input_channel.basic_consume(on_message_callback=callback, queue=meo_api_queue)
    print("listening for handles to look up...")
    input_channel.start_consuming()

consumer()