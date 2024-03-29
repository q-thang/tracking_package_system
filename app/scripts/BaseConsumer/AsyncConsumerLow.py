import asyncio
import logging
from functools import reduce

import aiohttp
import os
import time

import redis
from confluent_kafka import Producer
from kafka import KafkaConsumer
from dotenv import load_dotenv
import ujson as json

import constants
from retry_webhook import RetryWebhook

load_dotenv()


# python worker.py BaseConsumer AsyncConsumerLow
class AsyncConsumerLow:
    __slots__ = ['list_msg', 'topic', 'brokers', 'group', 'limit_msg',
                 'package_data', 'timeout_msg', 'timeout_request', 'producer', 'cache']

    def __init__(self):
        self.brokers = os.getenv('BOOTSTRAP_SERVERS', 'kafka101:29092,kafka102:29092,kafka103:29092')
        self.topic = None
        self.group = None
        self.list_msg = []
        self.timeout_request = 3
        self.package_data = None
        self.producer = None
        self.cache = None

    def run(self):
        self.init_producer()
        self.init_redis()
        asyncio.run(self.listen_message())

    def init_redis(self):
        self.cache = redis.Redis(
            host=os.getenv('REDIS_HOST', 'redis'),
            port=os.getenv('REDIS_PORT', 6379)
        )

    def init_producer(self):
        producer_conf = {'bootstrap.servers': self.brokers}
        self.producer = Producer(**producer_conf)

    async def listen_message(self):
        print('CONSUMER TOPIC: ' + self.topic)
        print('CONSUMER GROUP: ' + self.group)
        print('CONSUMER BROKERS: ' + self.brokers)

        brokers = self.brokers.split(',')
        client = KafkaConsumer(
            self.topic,
            bootstrap_servers=brokers,
            auto_offset_reset='latest',
            enable_auto_commit=True,
            group_id=self.group,
            max_poll_records=constants.LIMIT_MSG
        )

        while True:
            try:
                messages = client.poll(constants.TIMEOUT_MSG)

                is_timeout = False
                if not messages:
                    is_timeout = True
                else:
                    for _, raw_message in messages.items():
                        for message in raw_message:
                            self.process_msg(message)
                            self.list_msg.append(self.package_data)

                if len(self.list_msg) >= constants.LIMIT_MSG \
                        or is_timeout and len(self.list_msg) > 0:
                    async with aiohttp.ClientSession() as session:
                        start_time = time.time()
                        tasks = [self.get_task(session, task) for task in self.list_msg]
                        await asyncio.gather(*tasks)

                        self.list_msg = []
                        time_metrics = time.time() - start_time
                        print(f"[METRIC] Metric time for processing messages to webhook: ", round(time_metrics, 2))
            except (TimeoutError, Exception):
                logging.error(f"Timeout because not getting any message after {constants.TIMEOUT_MSG}", exc_info=True)

    def process_msg(self, msg):
        try:
            self.package_data = json.loads(msg.value.decode('utf-8'))

            msg = f"[PROCESSING] Processing package of shop code: {self.package_data['shop_id']} with pkg_code: " \
                  f"{self.package_data['pkg_code']} and status: {self.package_data['package_status_id']}"

            self.produce_logstash(msg, self.package_data['pkg_code'])
        except (ValueError, Exception):
            logging.error("Cannot parse message because invalid format", exc_info=True)

    async def get_task(self, session, msg):
        base_url = os.getenv('WEBHOOK_URL')
        url = base_url + msg['webhook_url']
        shop_id = msg['shop_id']
        params = {
            'pkg_code': msg['pkg_code'],
            'package_status_id': msg['package_status_id'],
        }

        try:
            start_request = time.time()
            async with session.post(url, json=params, ssl=False, timeout=self.timeout_request) as response:
                response_status = response.status
                if response_status in constants.STATUS_ALLOW:
                    retry_webhook = RetryWebhook(topic=self.topic, brokers=self.brokers)
                    message = f'[RETRY]: Retry sending package {msg["pkg_code"]} because of getting error server ' \
                              f'response'
                    self.produce_logstash(message, msg['pkg_code'])
                    retry_webhook.retry(params['pkg_code'], response_status, msg)
                end_result = time.time()
                response_time = round(end_result - start_request, 2)
                response_log = f"[RESPONSE] Response {response}: Receive package {params['pkg_code']} " \
                               f"within {str(response_time)} seconds"
                self.produce_logstash(response_log, pkg_code=params['pkg_code'])
                self.calculate_avg_response(shop_id, response_time)

        except (TimeoutError, Exception) as e:
            self.switch_topic(msg)
            print(e)
            message = f"[TIMEOUT] Timeout for waiting for response, this request of package {msg['pkg_code']} of " \
                      f"shop {msg['shop_id']} will be switched to alternative topic"
            self.produce_logstash(message, msg['pkg_code'])

    def calculate_avg_response(self, shop_id, response_time):
        shop_cached = json.loads(self.cache.get(shop_id))
        time_responses = shop_cached['time_responses'] if 'time_responses' in shop_cached.keys() else []
        total_responses = shop_cached['total_responses'] if 'total_responses' in shop_cached.keys() else None

        if shop_cached:
            time_responses.append(response_time)
            if len(time_responses) < constants.LIMIT_REDIS_MSG:
                total_responses = reduce(lambda x, y: x + y, time_responses)
            else:
                first_response = time_responses.pop(0)
                total_responses = total_responses - first_response + response_time

            recalculated = total_responses / len(time_responses)
            shop_cached['time_responses'] = time_responses
            shop_cached['total_responses'] = total_responses
            shop_cached['avg_response'] = recalculated
            self.cache.set(shop_id, json.dumps(shop_cached))

    def switch_topic(self, message):
        rank_topic = constants.RANK_TOPIC
        for index, topic in enumerate(rank_topic):
            if topic == self.topic and index != len(rank_topic) - 1:
                self.producer_topic(rank_topic[index + 1], message)

    def producer_topic(self, topic, package_data):
        try:
            pkg_code = package_data['pkg_code']
            self.producer.produce(topic, json.dumps(package_data).encode('utf-8'), key=pkg_code)
            self.producer.poll(0)
            self.producer.flush()
        except Exception as e:
            logging.error('Has an error when producer to topic: ' + str(e))

    def produce_logstash(self, msg, pkg_code):
        try:
            topic = os.getenv("LOG_STASH_TOPIC", "logstash_topic")
            self.producer.produce(topic, json.dumps(msg).encode('utf-8'), key=pkg_code)
            self.producer.poll(10)
            self.producer.flush()
        except Exception as e:
            logging.error('Has an error when producer to topic log stash: ' + str(e))
