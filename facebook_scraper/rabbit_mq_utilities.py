import pika
from pika.adapters.blocking_connection import BlockingChannel

from .config import pikaparams
import json

def get_channel():
    # Connect to queue:
    connection = pika.BlockingConnection(pikaparams)
    channel = connection.channel()
    return channel

def send_data_to_queue(data_jsons: list[dict], target_queue: str, channel: BlockingChannel = None):
    should_close = False

    if (channel is None) or (not channel.is_open) or (not channel.connection.is_open):
        # Only create new connection if no channel provided
        connection = pika.BlockingConnection(pikaparams)
        channel = connection.channel()
        should_close = True


    channel.queue_declare(queue=target_queue, durable=True)

    # Send message to queue:
    for data_json in data_jsons:
        channel.basic_publish(
            exchange="",
            routing_key=target_queue,
            body=json.dumps(data_json).encode('utf-8'),
            properties=pika.BasicProperties(
                delivery_mode=2,  # make message persistent
            ),
        )

    if should_close:
        channel.connection.close()
