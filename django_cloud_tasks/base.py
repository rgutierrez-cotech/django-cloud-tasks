import base64
import json
import logging
import time
import uuid
from uuid import UUID

from .apps import DCTConfig
from .connection import connection

logger = logging.getLogger(__name__)


class ComplexEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, UUID):
            return obj.hex
        return json.JSONEncoder.default(self, obj)


def retry(retry_limit, retry_interval):
    """
    Decorator for retrying task scheduling
    """

    def decorator(f):
        def wrapper():
            attempts_left = retry_limit
            error = None
            while attempts_left > 1:
                try:
                    return f()
                except Exception as e:
                    error = e
                    msg = 'Task scheduling failed. Reason: {0}. Retrying...'.format(str(e))
                    logger.warning(msg)
                    time.sleep(retry_interval)
                    attempts_left -= 1

            # Limit exhausted
            error.args = ('Task scheduling limit exhausted',) + error.args
            raise error

        return wrapper

    return decorator


def batch_callback_logger(id, message, exception):
    if exception:
        resp, _bytes = exception.args
        decoded = json.loads(_bytes.decode('utf-8'))
        raise Exception(decoded['error']['message'])


def batch_execute(tasks, retry_limit=30, retry_interval=3):
    """
    Executes tasks in batch
    :param tasks: list of CloudTaskWrapper objects
    :param retry_limit: How many times task scheduling will be attempted
    :param retry_interval: Interval between task scheduling attempts in seconds
    """
    if len(tasks) >= 1000:
        raise Exception('Maximum number of tasks in batch cannot exceed 1000')
    client = connection.client
    batch = client.new_batch_http_request()
    for t in tasks:
        batch.add(t.create_cloud_task(), callback=batch_callback_logger)

    if not retry_limit:
        return batch.execute()
    else:
        return retry(retry_limit=retry_limit, retry_interval=retry_interval)(batch.execute)()


class BaseTask(object):
    pass


class CloudTaskMockRequest(object):
    def __init__(self, request=None, task_id=None, request_headers=None):
        self.request = request
        self.task_id = task_id
        self.request_headers = request_headers
        self.setup()

    def setup(self):
        if not self.task_id:
            self.task_id = uuid.uuid4().hex
        if not self.request_headers:
            self.request_headers = dict()


class CloudTaskRequest(object):
    def __init__(self, request, task_id, request_headers):
        self.request = request
        self.task_id = task_id
        self.request_headers = request_headers

    @classmethod
    def from_cloud_request(cls, request):
        request_headers = request.META
        task_id = request_headers.get('HTTP_X_APPENGINE_TASKNAME')
        return cls(
            request=request,
            task_id=task_id,
            request_headers=request_headers
        )


class CloudTaskWrapper(object):
    def __init__(self, base_task, queue, data, internal_task_name=None, task_handler_url=None,
                 is_remote=False):
        self._base_task = base_task
        self._data = data
        self._queue = queue
        self._connection = None
        self._internal_task_name = internal_task_name or self._base_task.internal_task_name
        self._task_handler_url = task_handler_url or DCTConfig.task_handler_root_url()
        self._is_remote = is_remote
        self.setup()

    def setup(self):
        self._connection = connection
        if not self._internal_task_name:
            raise ValueError('Either `internal_task_name` or `base_task` should be provided')
        if not self._task_handler_url:
            raise ValueError('Could not identify task handler URL of the worker service')

    def execute(self, retry_limit=10, retry_interval=5):
        """
        Enqueue cloud task and send for execution
        :param retry_limit: How many times task scheduling will be attempted
        :param retry_interval: Interval between task scheduling attempts in seconds
        """
        if DCTConfig.execute_locally() and not self._is_remote:
            return self.run()

        if self._is_remote and DCTConfig.block_remote_tasks():
            logger.debug(
                'Remote task {0} was ignored. Task data:\n {1}'.format(self._internal_task_name, self._data)
            )
            return None

        if not retry_limit:
            return self.create_cloud_task().execute()
        else:
            return retry(retry_limit=retry_limit, retry_interval=retry_interval)(self.create_cloud_task().execute)()

    def run(self, mock_request=None):
        """
        Runs actual task function. Used for local execution of the task handler
        :param mock_request: Task instances accept request argument that holds various attributes of the request
        coming from Cloud Tasks service. You can pass a mock request here that emulates that request. If not provided,
        default mock request is created from `CloudTaskMockRequest`
        """
        request = mock_request or CloudTaskMockRequest()
        return self._base_task.run(request=request, **self._data) if self._data else self._base_task.run(request=request)

    def set_queue(self, queue):
        self._queue = queue

    @property
    def _cloud_task_queue_name(self):
        return '{}/queues/{}'.format(DCTConfig.project_location_name(), self._queue)

    def create_cloud_task(self):
        body = {
            'task': {
                'appEngineHttpRequest': {
                    'httpMethod': 'POST',
                    'relativeUrl': self._task_handler_url
                }
            }
        }

        payload = {
            'internal_task_name': self._internal_task_name,
            'data': self._data
        }
        payload = json.dumps(payload, cls=ComplexEncoder)

        base64_encoded_payload = base64.b64encode(payload.encode())
        converted_payload = base64_encoded_payload.decode()

        body['task']['appEngineHttpRequest']['payload'] = converted_payload

        task = self._connection.tasks_endpoint.create(parent=self._cloud_task_queue_name, body=body)

        return task


class RemoteCloudTask(object):
    def __init__(self, queue, handler, task_handler_url=None):
        self.queue = queue
        self.handler = handler
        self.task_handler_url = task_handler_url or DCTConfig.task_handler_root_url()

    def payload(self, payload):
        """
        Set payload and return task instance
        :param payload: Dict Payload
        :return: `CloudTaskWrapper` instance
        """
        task = CloudTaskWrapper(base_task=None, queue=self.queue, internal_task_name=self.handler,
                                task_handler_url=self.task_handler_url,
                                data=payload, is_remote=True)
        return task


def remote_task(queue, handler, task_handler_url=None):
    """
    Returns `RemoteCloudTask` instance. Can be used for scheduling tasks that are not available in the current scope
    :param queue: Queue name
    :param handler: Task handler function name
    :param task_handler_url: Entry point URL of the worker service for the task
    :return: `CloudTaskWrapper` instance
    """
    task = RemoteCloudTask(queue=queue, handler=handler, task_handler_url=task_handler_url)
    return task
