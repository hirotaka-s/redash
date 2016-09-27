import json
import time

import pystache
from flask import make_response, request, after_this_request
from flask_login import current_user
from flask_restful import abort
from redash import models, settings, utils
from redash.tasks import record_event
from redash.permissions import require_permission, not_view_only, has_access, require_access, view_only
from redash.handlers.base import BaseResource, get_object_or_404
from redash.utils import collect_query_parameters, collect_parameters_from_request
from redash.tasks.store_query import enqueue_store_task, StoreTask
from .query_results import QueryResultResource
from wsgiref.handlers import format_date_time
import datetime


def error_response(message):
    return {'job': {'status': 4, 'error': message}}, 400


def store_historical_query_result(data_source, query_id, query_text, data_timestamp, query_task_id, max_age=0):
    template_query_text = models.Query.get_by_id(query_id).query
    if max_age == 0:
        query_result = None
    else:
        store_job = enqueue_store_task(data_source, template_query_text, query_text,  data_timestamp, query_task_id)
        return {'job': store_job.to_dict()}


class HistoricalQueryResultListResource(BaseResource):
    def post(self):
        params = request.get_json(force=True)

        max_age = int(params.get('max_age', -1))
        query_id = params.get('query_id', 'adhoc')
        query_text = params.get('query_text', None)
        data_timestamp = params.get('data_timestamp', None)
        query_task_id = params.get('task_id', None)

        data_source = models.DataSource.get_by_id_and_org(params.get('data_source_id'), self.current_org)


        if not has_access(data_source.groups, self.current_user, not_view_only):
            return {'job': {'status': 4, 'error': 'You do not have permission to store historical query results with this data source.'}}, 403

        self.record_event({
            'action': 'store_historical_query_result',
            'timestamp': int(time.time()),
            'object_id': data_source.id,
            'object_type': 'data_source',
            'query_id': query_id
        })

        return store_historical_query_result(data_source, query_id, query_text, data_timestamp, query_task_id, max_age)


class HistoricalQueryResultResource(QueryResultResource):
    @require_permission('view_query')
    def get(self, query_id=None, store_result_id=None, filetype='json'):
        template_query_hash = None
       
        if store_result_id is not None and query_id is not None:
            query = get_object_or_404(models.Query.get_by_id_and_org, query_id, self.current_org)

        if store_result_id:
            query = get_object_or_404(models.HistoricalQueryResult.get_by_id, store_result_id)
        
        if query:
            template_query_hash = query._data['query_hash']


        if template_query_hash:
            query_result = get_object_or_404(models.HistoricalQueryResult.get_historical_results_by_hash_and_org, template_query_hash, self.current_org)
        else:
            query_result = None

        if query_result:
            require_access(query_result[0].data_source.groups, self.current_user, view_only)

            if isinstance(self.current_user, models.ApiUser):
                event = {
                    'user_id': None,
                    'org_id': self.current_org.id,
                    'action': 'api_get',
                    'timestamp': int(time.time()),
                    'api_key': self.current_user.name,
                    'file_type': filetype,
                    'user_agent': request.user_agent.string,
                    'ip': request.remote_addr
                }

                event['object_type'] = 'historical_query_result'
                event['object_id'] = query_id

                record_event.delay(event)

            if filetype == 'json':
                response = self.make_json_response(query_result)
            elif filetype == 'xlsx':
                response = self.make_excel_response(query_result)
            else:
                response = self.make_csv_response(query_result)

            if len(settings.ACCESS_CONTROL_ALLOW_ORIGIN) > 0:
                self.add_cors_headers(response.headers)

            @after_this_request
            def d_header(response):
                stamp = time.mktime(datetime.datetime.now().timetuple())
                response.headers['Last-Modified'] = format_date_time(stamp)
                return response

            return response

        else:
            abort(404, message='No cached result found for this query.')


    def make_json_response(self, historical_query_results):
        result = historical_query_results[0].to_dict()
        del result['data_timestamp']
        result['data']['columns'].append({'fiendly_name': 'data_timestamp', 'name': 'data_timestamp', 'type': 'datetime'})
        del result['data']['rows'][:]

        for query_result in historical_query_results:
            query_result_dict = query_result.to_dict()
            for row in query_result_dict['data']['rows']:
                row['data_timestamp'] = query_result_dict['data_timestamp']
                result['data']['rows'].append(row)

        data = json.dumps({'historical_query_results': result}, cls=utils.JSONEncoder)
        return make_response(data, 200, {})


        

        


class StoreJobResource(BaseResource):
    def get(self, job_id):
        job = StoreTask(job_id=job_id)
        return {'job': job.to_dict()}

    def delete(self, job_id):
        job = StoreTask(job_id=job_id)
        job.cancel()

