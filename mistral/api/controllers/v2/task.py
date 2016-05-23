# Copyright 2013 - Mirantis, Inc.
# Copyright 2015 - StackStorm, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import json

from oslo_log import log as logging
import pecan
from pecan import rest
import wsme
from wsme import types as wtypes
import wsmeext.pecan as wsme_pecan

from mistral.api.controllers import resource
from mistral.api.controllers.v2 import action_execution
from mistral.api.controllers.v2 import types
from mistral.db.v2 import api as db_api
from mistral.engine import rpc
from mistral import exceptions as exc
from mistral.utils import rest_utils
from mistral.workbook import parser as spec_parser
from mistral.workflow import data_flow
from mistral.workflow import states


LOG = logging.getLogger(__name__)


class Task(resource.ResourceList):
    """Task resource."""

    id = wtypes.text
    name = wtypes.text

    workflow_name = wtypes.text
    workflow_id = wtypes.text
    workflow_execution_id = wtypes.text

    state = wtypes.text
    "state can take one of the following values: \
    IDLE, RUNNING, SUCCESS, ERROR, DELAYED"

    state_info = wtypes.text
    "an optional state information string"

    result = wtypes.text
    published = types.jsontype
    processed = bool

    created_at = wtypes.text
    updated_at = wtypes.text

    # Add this param to make Mistral API work with WSME 0.8.0 or higher version
    reset = wsme.wsattr(bool, mandatory=True)

    env = types.jsontype

    @classmethod
    def sample(cls):
        return cls(
            id='123e4567-e89b-12d3-a456-426655440000',
            workflow_name='flow',
            workflow_id='123e4567-e89b-12d3-a456-426655441111',
            workflow_execution_id='123e4567-e89b-12d3-a456-426655440000',
            name='task',
            state=states.SUCCESS,
            result='task result',
            published={'key': 'value'},
            processed=True,
            created_at='1970-01-01T00:00:00.000000',
            updated_at='1970-01-01T00:00:00.000000',
            reset=True
        )


class Tasks(resource.ResourceList):
    """A collection of tasks."""

    tasks = [Task]

    def __init__(self, **kwargs):
        self._type = 'tasks'

        super(Tasks, self).__init__(**kwargs)

    @classmethod
    def sample(cls):
        return cls(tasks=[Task.sample()])


def _get_task_resource_with_result(task_ex):
    task = Task.from_dict(task_ex.to_dict())
    task.result = json.dumps(data_flow.get_task_execution_result(task_ex))

    return task


def _get_task_resources_with_results(wf_ex_id=None):
    filters = {}

    if wf_ex_id:
        filters['workflow_execution_id'] = wf_ex_id

    task_exs = db_api.get_task_executions(**filters)
    tasks = [_get_task_resource_with_result(t_e) for t_e in task_exs]

    return Tasks(tasks=tasks)


class TasksController(rest.RestController):
    action_executions = action_execution.TasksActionExecutionController()

    @rest_utils.wrap_wsme_controller_exception
    @wsme_pecan.wsexpose(Task, wtypes.text)
    def get(self, id):
        """Return the specified task."""
        LOG.info("Fetch task [id=%s]" % id)

        task_ex = db_api.get_task_execution(id)

        return _get_task_resource_with_result(task_ex)

    @wsme_pecan.wsexpose(Tasks, types.uuid, int, types.uniquelist,
                         types.list, types.uniquelist)
    def get_all(self, marker=None, limit=None, sort_keys='created_at',
                sort_dirs='asc', fields=''):
        """Return all tasks within the execution.

         :param marker: Optional. Pagination marker for large data sets.
         :param limit: Optional. Maximum number of resources to return in a
                       single result. Default value is None for backward
                       compatibility.
         :param sort_keys: Optional. Columns to sort results by.
                           Default: created_at, which is backward compatible.
         :param sort_dirs: Optional. Directions to sort corresponding to
                           sort_keys, "asc" or "desc" can be chosen.
                           Default: desc. The length of sort_dirs can be equal
                           or less than that of sort_keys.
         :param fields: Optional. A specified list of fields of the resource to
                        be returned. 'id' will be included automatically in
                        fields if it's provided, since it will be used when
                        constructing 'next' link.
        """
        LOG.info(
            "Fetch tasks. marker=%s, limit=%s, sort_keys=%s, "
            "sort_dirs=%s, fields=%s", marker, limit, sort_keys, sort_dirs,
            fields
        )

        if fields and 'id' not in fields:
            fields.insert(0, 'id')

        rest_utils.validate_query_params(limit, sort_keys, sort_dirs)
        rest_utils.validate_fields(fields, Task.get_fields())

        marker_obj = None

        if marker:
            marker_obj = db_api.get_task_execution(marker)

        db_tasks = db_api.get_task_executions(
            limit=limit,
            marker=marker_obj,
            sort_keys=sort_keys,
            sort_dirs=sort_dirs,
            fields=fields
        )

        tasks_list = []

        for data in db_tasks:
            tasks_list_dict = (dict(zip(fields, data)) if fields else
                                data.to_dict())
            tasks_list.append(Task.from_dict(tasks_list_dict))

        return Tasks.convert_with_links(
            tasks_list,
            limit,
            pecan.request.host_url,
            sort_keys=','.join(sort_keys),
            sort_dirs=','.join(sort_dirs),
            fields=','.join(fields) if fields else ''
        )

    @rest_utils.wrap_wsme_controller_exception
    @wsme_pecan.wsexpose(Task, wtypes.text, body=Task)
    def put(self, id, task):
        """Update the specified task execution.

        :param id: Task execution ID.
        :param task: Task execution object.
        """
        LOG.info("Update task execution [id=%s, task=%s]" % (id, task))

        task_ex = db_api.get_task_execution(id)
        task_spec = spec_parser.get_task_spec(task_ex.spec)
        task_name = task.name or None
        reset = task.reset
        env = task.env or None

        if task_name and task_name != task_ex.name:
            raise exc.WorkflowException('Task name does not match.')

        wf_ex = db_api.get_workflow_execution(task_ex.workflow_execution_id)
        wf_name = task.workflow_name or None

        if wf_name and wf_name != wf_ex.name:
            raise exc.WorkflowException('Workflow name does not match.')

        if task.state != states.RUNNING:
            raise exc.WorkflowException(
                'Invalid task state. Only updating task to rerun is supported.'
            )

        if task_ex.state != states.ERROR:
            raise exc.WorkflowException(
                'The current task execution must be in ERROR for rerun.'
                ' Only updating task to rerun is supported.'
            )

        if not task_spec.get_with_items() and not reset:
            raise exc.WorkflowException(
                'Only with-items task has the option to not reset.'
            )

        rpc.get_engine_client().rerun_workflow(
            wf_ex.id,
            task_ex.id,
            reset=reset,
            env=env
        )

        task_ex = db_api.get_task_execution(id)

        return _get_task_resource_with_result(task_ex)


class ExecutionTasksController(rest.RestController):
    @wsme_pecan.wsexpose(Tasks, wtypes.text)
    def get_all(self, workflow_execution_id):
        """Return all tasks within the workflow execution."""
        LOG.info("Fetch tasks.")

        return _get_task_resources_with_results(workflow_execution_id)
