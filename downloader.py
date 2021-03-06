#!/usr/bin/env python
import sqlite3
import threading
import logging
import requests
import time
import sys
from requests.adapters import HTTPAdapter
from datadog import statsd
from datadog.api.constants import CheckStatus
import random
import shelve
from collections import deque
from queue import FifoSQLiteQueue
from threading import Lock
from urlparse import urlparse
import argparse
import os
import csv

logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] [%(levelname)s] (%(threadName)-10s) %(message)s')

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/52.0.2743.116 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/53.0.2785.116 Safari/537.36',
    'Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/52.0.2743.116 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/52.0.2743.116 Safari/537.36',
    'Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/53.0.2785.116 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/53.0.2785.116 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; WOW64; rv:48.0) Gecko/20100101 Firefox/48.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/53.0.2785.116 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_6) AppleWebKit/601.7.8 (KHTML, like Gecko) Version/9.1.3 Safari/601.7.8',
    'Mozilla/5.0 (Windows NT 6.1; WOW64; rv:48.0) Gecko/20100101 Firefox/48.0',
    'Mozilla/5.0 (Windows NT 6.1; WOW64; Trident/7.0; rv:11.0) like Gecko',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/52.0.2743.116 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.11; rv:48.0) Gecko/20100101 Firefox/48.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_12) AppleWebKit/602.1.50 (KHTML, like Gecko) Version/10.0 Safari/602.1.50',
    'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:48.0) Gecko/20100101 Firefox/48.0',
    'Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/53.0.2785.116 Safari/537.36',
    'Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/52.0.2743.116 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_6) AppleWebKit/601.7.7 (KHTML, like Gecko) Version/9.1.2 Safari/601.7.7',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_10_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/52.0.2743.116 Safari/537.36',
    'Mozilla/5.0 (Windows NT 6.3; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/52.0.2743.116 Safari/537.36'
]


class SourceCursor(object):

    _fields = 'ImageID, OriginalURL'
    _sql_first = 'SELECT {} FROM urls ORDER BY ImageID ASC'.format(_fields)
    _sql_move_to = 'SELECT {} FROM urls WHERE ImageID>? ORDER BY ImageID ASC'.format(_fields)
    _sql_size = 'SELECT COUNT(*) FROM urls'

    def __init__(self, path, skip=0):
        self._path = os.path.abspath(path)
        # self._db = sqlite3.Connection(self._path, check_same_thread=False)
        # self.mutex = Lock()
        self.file = open(self._path, 'r')
        self.cursor = csv.reader(self.file)
        for _ in xrange(int(skip)):
            self.cursor.next()
        # self.moveTo(last_id)

    # def moveTo(self, last_id=None):
    #     with self._db as conn:
    #         if last_id is None:
    #             self.cursor = conn.execute(self._sql_first)
    #         else:
    #             self.cursor = conn.execute(self._sql_move_to, (last_id,))

    def close(self):
        self.file.close()

    def next(self):
        row = self.cursor.next()
        r = (row[0], row[2])
        logging.debug('source next {}'.format(r))
        return r

    def total(self):
        count = 0
        with open(self._path, 'r') as f:
            for _ in f:
                count += 1
        return count
        # with self._db as conn:
        #     return next(conn.execute(self._sql_size))[0]


class Config(object):

    CONFIG_LAST_ID = 'LAST_ID'
    CONFIG_PROCESSED_COUNT = 'PROCESSED_COUNT'
    CONFIG_TOTAL = 'TOTAL'

    def __init__(self, path):
        self._path = os.path.abspath(path)
        self.shelve = shelve.open(self._path)
        self.mutex = Lock()

    def __getitem__(self, key):
        return self.shelve.__getitem__(key)

    def __setitem__(self, key, value):
        self.shelve.__setitem__(key, value)
        self.shelve.sync()

    def __contains__(self, key):
        return self.shelve.__contains__(key)

    def inc(self, key):
        if key in self.shelve:
            self.shelve[key] += 1
        else:
            self.shelve[key] = 1.0
        self.shelve.sync()
        self.logProgress()

    def logProgress(self):
        progress = self.shelve[Config.CONFIG_PROCESSED_COUNT] / self.shelve[Config.CONFIG_TOTAL] * 100
        logging.debug('progress {}/{} {}%'.format(
            self.shelve[Config.CONFIG_PROCESSED_COUNT],
            self.shelve[Config.CONFIG_TOTAL],
            progress))
        statsd.gauge('url.downloader.progress', progress)

    def close():
        self.shelve.close()


class ROB(object):

    def __init__(self, output_queue, config):
        self.q = deque()
        self.d = {}
        self.o = output_queue
        self.c = config
        # self.mutex = Lock()

    def pending(self, id_):
        # logging.debug('ROB pending')
        self.q.append(id_)

    def push(self, id_, obj):
        # logging.debug('ROB push')
        self.d[id_] = obj
        self.check()

    def check(self):
        logging.debug('ROB check')
        if len(self.q) == 0:
            return
        id_ = self.q[0]
        if id_ in self.d:
            success, data = self.d[id_]
            if success:
                self.o.push(data)
            logging.debug('ROB success & push')
            self.c.inc(Config.CONFIG_PROCESSED_COUNT)
            logging.debug('ROB inc config count')
            # self.c[Config.CONFIG_LAST_ID] = id_
            del self.d[id_]
            self.q.popleft()
            self.check()


def init_requests():
    s = requests.Session()
    s.mount('http://', HTTPAdapter(max_retries=1))
    s.mount('https://', HTTPAdapter(max_retries=1))


def fetch(url, retry=0):
    if retry == 2:
        statsd.increment('url.failed')
        logging.debug('fetch failed max retry')
        return (False, None)
    try:
        r = requests.get(url,
                         timeout=1,
                         headers={'User-Agent': random.choice(USER_AGENTS)})
        if r.status_code == 200:
            statsd.increment('url.success')
            logging.debug('fetch success')
            return (True, r.content)
        else:
            statsd.increment('url.failed')
            logging.debug('fetch failed {}'.format(r.status_code))
            return (False, None)
    except requests.exceptions.ConnectionError:
        time.sleep(60)
        process(url, retry + 1)
    except Exception as e:
        statsd.increment('url.failed')
        logging.debug('fetch {}'.format(str(e)))
        return (False, None)


def getFilename(url, id_):
    # get extension
    ext = os.path.splitext(urlparse(url).path)[1]
    # construct file name
    return "{}{}".format(id_, ext)


def worker(cur, cursor_lock, run_event, rob, queue):
    logging.debug('start')
    id_ = None
    url = None
    while run_event.is_set():
        logging.debug('new run, len(queue)={}'.format(len(queue)))
        if len(queue) >= 5000:
            time.sleep(5)
            continue
        try:
            with cursor_lock:
                logging.debug('lock acquire')
                id_, url = cur.next()
                rob.pending(id_)
                logging.debug('lock release')
        except StopIteration:
            logging.debug('StopIteration')
            break
        statsd.increment('url.process')
        logging.debug('start fetch {}'.format(id_))
        success, data = fetch(url)
        with cursor_lock:
            logging.debug('lock acquire')
            rob.push(id_, (success, (getFilename(url, id_), data)))
            logging.debug('lock release')
    logging.debug('stop with run_event={}'.format(run_event.is_set()))


def main():
    parser = argparse.ArgumentParser(description='urls downloader')
    parser.add_argument('config_path', help='The config path (will be created if not exists)')
    parser.add_argument('urls_path', help='The urls db path')
    parser.add_argument('queue_path', help='The buffer queue path')
    parser.add_argument('threads_count', help='threads count', type=int)
    args = parser.parse_args()

    init_requests()

    config = Config(args.config_path)
    queue = FifoSQLiteQueue(args.queue_path)
    rob = ROB(queue, config)
    cursor = None
    if Config.CONFIG_TOTAL not in config:
        cursor = SourceCursor(args.urls_path)
        config[Config.CONFIG_TOTAL] = cursor.total()
        config[Config.CONFIG_PROCESSED_COUNT] = 0.0
        logging.debug('new Job')
    else:
        cursor = SourceCursor(args.urls_path, config[Config.CONFIG_PROCESSED_COUNT])
        logging.debug('resume with {} done'.format(config[Config.CONFIG_PROCESSED_COUNT]))

    cursor_lock = threading.Lock()
    run_event = threading.Event()
    run_event.set()

    def genThread(i):
        return threading.Thread(name='worker_{}'.format(i),
                                target=worker,
                                args=(cursor,
                                      cursor_lock,
                                      run_event,
                                      rob,
                                      queue, ))

    threads = {}
    for i in range(args.threads_count):
        threads[i] = genThread(i)
        threads[i].start()
        time.sleep(0.5)

    try:
        while 1:
            time.sleep(5)
            statsd.service_check(check_name='url.downloader',
                                 status=CheckStatus.OK,
                                 message='heart beat ok')
            for k, t in threads.items():
                if not t.isAlive():
                    threads[k] = genThread(k)
                    threads[k].start()
    except KeyboardInterrupt:
        logging.debug('attempting to close threads')
        run_event.clear()
        for t in threads.values():
            t.join()
        logging.debug('threads successfully closed')

    cursor.close()
    queue.close()
    config.close()
    logging.debug('all done')


if __name__ == "__main__":
    main()
