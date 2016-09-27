import logging
import time
import json
import signal
import redis
import pystache
from dateutil.relativedelta import relativedelta

from celery.result import AsyncResult
from celery.utils.log import get_task_logger

from redash import redis_connection, models, statsd_client, settings, utils
from redash.utils import gen_query_hash
from redash.worker import celery
from .base import BaseTask
from .queries import QueryTaskTracker, QueryTask, signal_handler, enqueue_query

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

    

def enqueue_store_task(data_source, template_query_text, query_text, data_timestamp, query_task_id, scheduled=False):
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

            if scheduled:
                queue_name = data_source.scheduled_queue_name
            else:
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

ONE_DAY = 24 * 60 * 60

def retrieve_next_date_timestamp(query):
    latest_history = models.HistoricalQueryResult.get_latest(query.data_source, query.query_hash)
    next_data_timestamp = None

    if latest_history is not None:
        latest_data_timestamp = latest_history.data_timestamp
        schedule = query.schedule
        if schedule.isdigit():
            ttl = int(schedule)
            if ttl == models.ONE_MONTH:
                next_data_timestamp = latest_data_timestamp + relativedelta(months=1)
            else:
                next_data_timestamp = latest_data_timestamp + relativedelta(seconds=ttl)
        else:
            if (utils.utcnow() - latest_data_timestamp).total_seconds() > ONE_DAY:
                next_data_timestamp = latest_data_timestamp + relativedelta(days=1)

    return next_data_timestamp
        

def create_parameter_values(parameters):
    parameter_values = {}

    for parameter in parameters:
        parameter_values[parameter['name']] = parameter['value']

    return parameter_values


@celery.task(name="redash.tasks.store_scheduled_query_results", base=BaseTask)
def store_scheduled_query_results():
    logger.info("Storing scheduled query results...")

    outdated_queries_count = 0
    query_ids = []

    with statsd_client.timer('manager.outdated_queries_lookup'):
        for query in models.Query.outdated_storing_queries():
            if settings.FEATURE_DISABLE_REFRESH_QUERIES: 
                logging.info("Disabled refresh queries, skip storing.")
            elif query.data_source.paused:
                logging.info("Skipping refresh of %s because datasource - %s is paused (%s).", query.id, query.data_source.name, query.data_source.pause_reason)
            else:
                parameters = query.options['parameters']
                parameter_values = create_parameter_values(parameters)
                if '__timestamp' in parameter_values:
                    refresh_interval = query.schedule
                    next_data_timestamp = retrieve_next_date_timestamp(query)
                    if next_data_timestamp is not None:
                        parameter_values['__timestamp'] = next_data_timestamp

                query_text = pystache.render(query.query, parameter_values)
                    
                job = enqueue_query(query_text, query.data_source,
                              scheduled=True,
                              metadata={'Query ID': query.id, 'Username': 'Scheduled'})
                enqueue_store_task(query.data_source, query.query, query_text, parameter_values['__timestamp'], job.to_dict()['id'], scheduled=True)

            query_ids.append(query.id)
            outdated_queries_count += 1

    statsd_client.gauge('manager.outdated_queries', outdated_queries_count)

    logger.info("Done refreshing queries. Found %d outdated queries: %s" % (outdated_queries_count, query_ids))

    status = redis_connection.hgetall('redash:status')
    now = time.time()

    redis_connection.hmset('redash:status', {
        'outdated_queries_count': outdated_queries_count,
        'last_refresh_at': now,
        'query_ids': json.dumps(query_ids)
    })

    statsd_client.gauge('manager.seconds_since_refresh', now - float(status.get('last_refresh_at', now)))


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

        
        latest_query_data_id = models.QueryResult.get_latest(self.data_source, self.query_text).id
        data = models.QueryResult.get_by_id(latest_query_data_id)

        store_result = models.HistoricalQueryResult.store_result(data.org,
                                                                 data.data_source,
                                                                 self.template_query_hash,
                                                                 self.template_query_text,
                                                                 data.data,
                                                                 data.runtime,
                                                                 data.retrieved_at,
                                                                 self.data_timestamp,
                                                                 self.template_query_hash,
                                                                 latest_query_data_id)
        _unlock_store_job_lock(self.query_text, self.data_source.id, self.data_timestamp)
        run_time = time.time() - self.tracker.started_at
        self.tracker.update(run_time=run_time, state='finished')
        
        return store_result.id
        

@celery.task(name="redash.tasks.store_historical_query_result", bind=True, base=BaseTask, track_started=True)
def store_historical_query_result(self, data_source, template_query_text, query_text, data_timestamp, query_task_id):
    return StoreExecutor(self, data_source, template_query_text, query_text, data_timestamp, query_task_id).run()
