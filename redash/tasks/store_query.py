import logging
import time
import signal
import redis
from celery.result import AsyncResult
from celery.utils.log import get_task_logger
from redash import redis_connection, models, utils, settings
from redash.utils import gen_query_hash
from redash.worker import celery
from .base import BaseTask
from .queries import QueryTaskTracker, QueryTask, signal_handler

logger = get_task_logger(__name__)

def _store_job_lock_id(query_hash, data_source_id, data_timestamp):
    return "store_job:%s:%s:%s" % (data_source_id, query_hash, data_timestamp)

def _unlock_store_job_lock(query_hash, data_source_id, data_timestamp):
    redis_connection.delete(_store_job_lock_id(query_hash, data_source_id, data_timestamp))

class StoreTaskTracker(QueryTaskTracker):
    DONE_LIST = 'store_task_trackers:done'
    WAITING_LIST = 'store_task_trackers:waiting'
    IN_PROGRESS_LIST = 'store_task_trackers:in_progress'

    def __init__(self, data):
        super(self.__class__, self).__init__(data)

    @classmethod
    def create(cls, task_id, state, query_hash, data_source_id, data_timestamp):
        data = dict(task_id=task_id, state=state,
                    query_hash=query_hash, data_source_id=data_source_id,
                    data_timestamp=data_timestamp,
                    created_at=time.time(),
                    started_at=None,
                    run_time=None)

        return cls(data)

    def save(self, connection=None):
        if connection is None:
            connection = redis_connection

        self.data['updated_at'] = time.time()
        key_name = self._key_name(self.data['task_id'])
        logging.info("key_name(store): %s", key_name)
        connection.set(key_name, utils.json_dumps(self.data))
        connection.zadd('store_task_trackers', time.time(), key_name)

        connection.zadd(self._get_list(), time.time(), key_name)

        for l in self.ALL_LISTS:
            if l != self._get_list():
                connection.zrem(l, key_name)

    @staticmethod
    def _key_name(task_id):
        return 'store_task_tracker:{}'.format(task_id)


class StoreTask(QueryTask):
    def __init__(self, job_id=None, async_result=None):
        super(self.__class__, self).__init__(job_id, async_result)

    def to_dict(self):
        if self._async_result.status == 'STARTED':
          updated_at = self._async_result.result.get('start_time', 0)
        else:
          updated_at = 0

        status = self.STATUSES[self._async_result.status]

        if isinstance(self._async_result.result, Exception):
            error = self._async_result.result.message
            status = 4
        elif self._async_result.status == 'REVOKED':
            error = 'Store exception cancelled.'
        else:
            error = ''

        if self._async_result.successful() and not error:
            store_result_id = self._async_result.result
        else:
            store_result_id = None

        return {
            'id': self._async_result.id,
            'updated_at': updated_at,
            'status': status,
            'error': error,
            'store_result_id': store_result_id,
        }

    

def enqueue_store_task(data_source, template_query_text, query_text, data_timestamp, query_task_id):
    query_hash = gen_query_hash(query_text)
    logging.info("Storing hisotrical query result for %s with data_timestamp=%s", query_hash, data_timestamp)
    job = None

    pipe = redis_connection.pipeline()
    try:
        pipe.watch(_store_job_lock_id(query_hash, data_source.id, data_timestamp))
        job_id = pipe.get(_store_job_lock_id(query_hash, data_source.id, data_timestamp))
        if job_id:
            logging.info("[%s] Found existing job: %s", query_hash, job_id)

            job = StoreTask(job_id=job_id)

            if job.ready():
                logging.info("[%s] job found is ready (%s), removing lock", query_hash, job.celery_status)
                redis_connection.delete(_store_job_lock_id(query_hash, data_source.id, data_timestamp))
                job = None

        if not job:
            pipe.multi()

            queue_name = data_source.queue_name

            result = store_historical_query_result.apply_async(args=(data_source, template_query_text, query_text, data_timestamp, query_task_id), queue=queue_name)
            job = StoreTask(async_result=result)
            tracker = StoreTaskTracker.create(result.id, 'created', query_hash, data_source.id, data_timestamp)
            tracker.save(connection=pipe)

            logging.info("[%s] Created new job: %s", query_hash, job.id)
            pipe.set(_store_job_lock_id(query_hash, data_source.id, data_timestamp), job.id, settings.JOB_EXPIRY_TIME)
            pipe.execute()

    except redis.WatchError:
        pass

    if not job:
        logging.error("[Manager][%s] Failed adding job for store.", template_query_hash)

    return job
        

class StoreExecutor(object):
    def __init__(self, task, data_source, template_query_text, query_text, data_timestamp, query_task_id):
        self.task = task
        self.data_source = data_source
        self.template_query_text = template_query_text
        self.query_text = query_text
        self.data_timestamp = data_timestamp
        self.template_query_hash = gen_query_hash(self.template_query_text)
        self.query_task_id = query_task_id
        self.tracker = StoreTaskTracker.get_by_task_id(task.request.id) or StoreTaskTracker.create(task.request.id,
                                                                                                   'create',
                                                                                                   gen_query_hash(query_text),
                                                                                                   self.data_source.id,
                                                                                                   self.data_timestamp)

    def run(self):
        signal.signal(signal.SIGINT, signal_handler)
        self.tracker.update(started_at=time.time(), state='started')

        while True:
            time.sleep(1)
            for task_tracker in QueryTaskTracker.all(QueryTaskTracker.IN_PROGRESS_LIST):
                if task_tracker.data['task_id'] == self.query_task_id:
                    logging.info("Waiting")
                    continue
            break

        
        latest_query_result_id = models.QueryResult.get_latest(self.data_source, self.query_text).id
        data = models.QueryResult.get_by_id(latest_query_result_id)

        store_result = models.HistoricalQueryResult.store_result(data.org,
                                                                 data.data_source,
                                                                 self.template_query_hash,
                                                                 self.template_query_text,
                                                                 data.data,
                                                                 data.runtime,
                                                                 data.retrieved_at,
                                                                 self.data_timestamp)
        _unlock_store_job_lock(self.query_text, self.data_source.id, self.data_timestamp)
        run_time = time.time() - self.tracker.started_at
        self.tracker.update(run_time=run_time, state='finished')
        
        return store_result.id
        

@celery.task(name="redash.tasks.store_historical_query_result", bind=True, base=BaseTask, track_started=True)
def store_historical_query_result(self, data_source, template_query_text, query_text, data_timestamp, query_task_id):
    return StoreExecutor(self, data_source, template_query_text, query_text, data_timestamp, query_task_id).run()
