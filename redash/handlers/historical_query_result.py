import json
import csv
import cStringIO
import time
import dateutil.parser
from dateutil.relativedelta import relativedelta

import pystache
from flask import make_response, request, after_this_request
from flask_login import current_user
from flask_restful import abort
import xlsxwriter
from redash import models, settings, utils
from redash.tasks import record_event
from redash.permissions import require_permission, not_view_only, has_access, require_access, view_only
from redash.handlers.base import BaseResource, get_object_or_404
from redash.utils import collect_query_parameters, collect_parameters_from_request
from redash.tasks.store_query import enqueue_store_task, StoreTask
from .query_results import QueryResultResource, enqueue_query
from wsgiref.handlers import format_date_time
import datetime


def error_response(message):
    return {'job': {'status': 4, 'error': message}}, 400


def store_historical_query_result(data_source, query_id, query_text, data_timestamp, query_task_id, max_age=0):
    template_query_text = models.Query.get_by_id(query_id).query
    historical_query_result = None

    if max_age == 0:
        historical_query_result = None
    elif query_task_id is None:
        historical_query_result = models.HistoricalQueryResult.get_historical_results_by_query_and_data_source(data_source, template_query_text)

    if historical_query_result is not None:
        return {'historical_query_result': join_historical_query_result(historical_query_result)}
    else:
        store_job = enqueue_store_task(data_source, template_query_text, query_text,  data_timestamp, query_task_id)
        return {'job': store_job.to_dict()}


def query_execute_and_store_result(data_source, query_id, time_range):
    template_query_text = models.Query.get_by_id(query_id).query
    timestamp_param = {}

    timestamp_param['__timestamp'] = dateutil.parser.parse(time_range['execute_from'])
    execute_to = dateutil.parser.parse(time_range['execute_to'])
    execution_interval_hours = relativedelta(hours=time_range['execution_interval_hours'])

    while timestamp_param['__timestamp'] <= execute_to:
        query_text = pystache.render(template_query_text, timestamp_param)
        query_job = enqueue_query(query_text, data_source, metadata={"Username": current_user.name, "Query ID": query_id})
        store_job = enqueue_store_task(data_source, template_query_text, query_text, timestamp_param['__timestamp'], query_job.to_dict()['id'])
        timestamp_param['__timestamp'] += execution_interval_hours

    return {'job': store_job.to_dict()}
    

class HistoricalQueryResultListResource(BaseResource):
    def post(self):
        params = request.get_json(force=True)

        max_age = int(params.get('max_age', -1))
        query_id = params.get('query_id', 'adhoc')
        query_text = params.get('query_text', None)
        data_timestamp = params.get('data_timestamp', None)
        query_task_id = params.get('task_id', None)
        time_range = params.get('time_range', None)

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

        if time_range is not None and query_task_id is None:
            return query_execute_and_store_result(data_source, query_id, time_range)

        return store_historical_query_result(data_source, query_id, query_text, data_timestamp, query_task_id, max_age)


class HistoricalQueryResultResource(QueryResultResource):
    @require_permission('view_query')
    def get(self, query_id=None, store_result_id=None, filetype='json'):
        template_query_hash = None
       
        if store_result_id is None and query_id is not None:
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


    def make_json_response(self, historical_query_result):
        data = json.dumps({'historical_query_result': join_historical_query_result(historical_query_result)}, cls=utils.JSONEncoder)
        return make_response(data, 200, {})

    @staticmethod
    def make_csv_response(query_result):
        s = cStringIO.StringIO()

        query_data = join_historical_query_result(query_result)['data']
        writer = csv.DictWriter(s, fieldnames=[col['name'] for col in query_data['columns']])
        writer.writer = utils.UnicodeWriter(s)
        writer.writeheader()
        for row in query_data['rows']:
            writer.writerow(row)

        headers = {'Content-Type': "text/csv; charset=UTF-8"}
        return make_response(s.getvalue(), 200, headers)

    @staticmethod
    def make_excel_response(query_result):
        s = cStringIO.StringIO()

        query_data = join_historical_query_result(query_result)['data']
        query_data = json.loads(json.dumps(query_data, cls=utils.JSONEncoder))
        book = xlsxwriter.Workbook(s)
        sheet = book.add_worksheet("result")

        column_names = []
        for (c, col) in enumerate(query_data['columns']):
            sheet.write(0, c, col['name'])
            column_names.append(col['name'])

        for (r, row) in enumerate(query_data['rows']):
            for (c, name) in enumerate(column_names):
                sheet.write(r + 1, c, row.get(name))

        book.close()

        headers = {'Content-Type': "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
        return make_response(s.getvalue(), 200, headers)



def join_historical_query_result(historical_query_result):
    result = historical_query_result[0].to_dict()
    del result['data_timestamp']
    result['data']['columns'].append({'fiendly_name': 'data_timestamp', 'name': 'data_timestamp', 'type': 'datetime'})
    del result['data']['rows'][:]

    for query_result in historical_query_result:
        query_result_dict = query_result.to_dict()
        for row in query_result_dict['data']['rows']:
            row['data_timestamp'] = query_result_dict['data_timestamp']
            result['data']['rows'].append(row)

    return result



class StoreJobResource(BaseResource):
    def get(self, job_id):
        job = StoreTask(job_id=job_id)
        return {'job': job.to_dict()}

    def delete(self, job_id):
        job = StoreTask(job_id=job_id)
        job.cancel()
