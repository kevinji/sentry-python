import weakref

import sentry_sdk
from sentry_sdk.consts import OP
from sentry_sdk.integrations import DidNotEnable, Integration
from sentry_sdk.integrations.logging import ignore_logger
from sentry_sdk.tracing import TRANSACTION_SOURCE_TASK
from sentry_sdk.utils import (
    _serialize_span_attribute,
    capture_internal_exceptions,
    ensure_integration_enabled,
    event_from_exception,
    format_timestamp,
    parse_version,
)

try:
    from rq.queue import Queue
    from rq.timeouts import JobTimeoutException
    from rq.version import VERSION as RQ_VERSION
    from rq.worker import Worker
    from rq.job import JobStatus
except ImportError:
    raise DidNotEnable("RQ not installed")

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any, Callable

    from sentry_sdk._types import Event, EventProcessor
    from sentry_sdk.utils import ExcInfo

    from rq.job import Job

DEFAULT_TRANSACTION_NAME = "unknown RQ task"


JOB_PROPERTY_TO_ATTRIBUTE = {
    "id": "messaging.message.id",
}

QUEUE_PROPERTY_TO_ATTRIBUTE = {
    "name": "messaging.destination.name",
}


class RqIntegration(Integration):
    identifier = "rq"
    origin = f"auto.queue.{identifier}"

    @staticmethod
    def setup_once():
        # type: () -> None

        version = parse_version(RQ_VERSION)

        if version is None:
            raise DidNotEnable("Unparsable RQ version: {}".format(RQ_VERSION))

        if version < (0, 6):
            raise DidNotEnable("RQ 0.6 or newer is required.")

        old_perform_job = Worker.perform_job

        @ensure_integration_enabled(RqIntegration, old_perform_job)
        def sentry_patched_perform_job(self, job, queue, *args, **kwargs):
            # type: (Any, Job, Queue, *Any, **Any) -> bool
            with sentry_sdk.new_scope() as scope:
                try:
                    transaction_name = job.func_name or DEFAULT_TRANSACTION_NAME
                except AttributeError:
                    transaction_name = DEFAULT_TRANSACTION_NAME

                scope.set_transaction_name(
                    transaction_name, source=TRANSACTION_SOURCE_TASK
                )
                scope.clear_breadcrumbs()
                scope.add_event_processor(_make_event_processor(weakref.ref(job)))

                with sentry_sdk.continue_trace(
                    job.meta.get("_sentry_trace_headers") or {}
                ):
                    with sentry_sdk.start_span(
                        op=OP.QUEUE_TASK_RQ,
                        name=transaction_name,
                        source=TRANSACTION_SOURCE_TASK,
                        origin=RqIntegration.origin,
                        attributes=_prepopulate_attributes(job, queue),
                    ):
                        rv = old_perform_job(self, job, queue, *args, **kwargs)

            if self.is_horse:
                # We're inside of a forked process and RQ is
                # about to call `os._exit`. Make sure that our
                # events get sent out.
                sentry_sdk.get_client().flush()

            return rv

        Worker.perform_job = sentry_patched_perform_job

        old_handle_exception = Worker.handle_exception

        def sentry_patched_handle_exception(self, job, *exc_info, **kwargs):
            # type: (Worker, Any, *Any, **Any) -> Any
            retry = (
                hasattr(job, "retries_left")
                and job.retries_left
                and job.retries_left > 0
            )
            failed = job._status == JobStatus.FAILED or job.is_failed
            if failed and not retry:
                _capture_exception(exc_info)

            return old_handle_exception(self, job, *exc_info, **kwargs)

        Worker.handle_exception = sentry_patched_handle_exception

        old_enqueue_job = Queue.enqueue_job

        @ensure_integration_enabled(RqIntegration, old_enqueue_job)
        def sentry_patched_enqueue_job(self, job, **kwargs):
            # type: (Queue, Any, **Any) -> Any
            job.meta["_sentry_trace_headers"] = dict(
                sentry_sdk.get_current_scope().iter_trace_propagation_headers()
            )

            return old_enqueue_job(self, job, **kwargs)

        Queue.enqueue_job = sentry_patched_enqueue_job

        ignore_logger("rq.worker")


def _make_event_processor(weak_job):
    # type: (Callable[[], Job]) -> EventProcessor
    def event_processor(event, hint):
        # type: (Event, dict[str, Any]) -> Event
        job = weak_job()
        if job is not None:
            with capture_internal_exceptions():
                extra = event.setdefault("extra", {})
                rq_job = {
                    "job_id": job.id,
                    "func": job.func_name,
                    "args": job.args,
                    "kwargs": job.kwargs,
                    "description": job.description,
                }

                if job.enqueued_at:
                    rq_job["enqueued_at"] = format_timestamp(job.enqueued_at)
                if job.started_at:
                    rq_job["started_at"] = format_timestamp(job.started_at)

                extra["rq-job"] = rq_job

        if "exc_info" in hint:
            with capture_internal_exceptions():
                if issubclass(hint["exc_info"][0], JobTimeoutException):
                    event["fingerprint"] = ["rq", "JobTimeoutException", job.func_name]

        return event

    return event_processor


def _capture_exception(exc_info, **kwargs):
    # type: (ExcInfo, **Any) -> None
    client = sentry_sdk.get_client()

    event, hint = event_from_exception(
        exc_info,
        client_options=client.options,
        mechanism={"type": "rq", "handled": False},
    )

    sentry_sdk.capture_event(event, hint=hint)


def _prepopulate_attributes(job, queue):
    # type: (Job, Queue) -> dict[str, Any]
    attributes = {
        "messaging.system": "rq",
    }

    for prop, attr in JOB_PROPERTY_TO_ATTRIBUTE.items():
        if getattr(job, prop, None) is not None:
            attributes[attr] = getattr(job, prop)

    for prop, attr in QUEUE_PROPERTY_TO_ATTRIBUTE.items():
        if getattr(queue, prop, None) is not None:
            attributes[attr] = getattr(queue, prop)

    for key in ("args", "kwargs"):
        if getattr(job, key, None):
            attributes[f"rq.job.{key}"] = _serialize_span_attribute(getattr(job, key))

    func = job.func
    if callable(func):
        func = func.__name__

    attributes["rq.job.func"] = _serialize_span_attribute(func)

    return attributes
