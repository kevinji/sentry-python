"""
# AWS Lambda System Tests

This testsuite uses boto3 to upload actual Lambda functions to AWS Lambda and invoke them.

For running test locally you need to set these env vars:
(You can find the values in the Sentry password manager by searching for "AWS Lambda for Python SDK Tests").

export SENTRY_PYTHON_TEST_AWS_ACCESS_KEY_ID="..."
export SENTRY_PYTHON_TEST_AWS_SECRET_ACCESS_KEY="..."


You can use `scripts/aws-cleanup.sh` to delete all files generated by this test suite.


If you need to debug a new runtime, use this REPL to run arbitrary Python or bash commands
in that runtime in a Lambda function: (see the bottom of client.py for more information.)

pip3 install click
python3 tests/integrations/aws_lambda/client.py --runtime=python4.0

IMPORTANT:

During running of this test suite temporary folders will be created for compiling the Lambda functions.
This temporary folders will not be cleaned up. This is because in CI generated files have to be shared
between tests and thus the folders can not be deleted right after use.

If you run your tests locally, you need to clean up the temporary folders manually. The location of
the temporary folders is printed when running a test.
"""

import base64
import json
import re
from textwrap import dedent

import pytest

RUNTIMES_TO_TEST = [
    "python3.8",
    "python3.10",
    "python3.12",
    "python3.13",
]

LAMBDA_PRELUDE = """
from sentry_sdk.integrations.aws_lambda import AwsLambdaIntegration, get_lambda_bootstrap
import sentry_sdk
import json
import time

from sentry_sdk.transport import Transport

def truncate_data(data):
    # AWS Lambda truncates the log output to 4kb, which is small enough to miss
    # parts of even a single error-event/transaction-envelope pair if considered
    # in full, so only grab the data we need.

    cleaned_data = {}

    if data.get("type") is not None:
        cleaned_data["type"] = data["type"]

    if data.get("contexts") is not None:
        cleaned_data["contexts"] = {}

        if data["contexts"].get("trace") is not None:
            cleaned_data["contexts"]["trace"] = data["contexts"].get("trace")

    if data.get("transaction") is not None:
        cleaned_data["transaction"] = data.get("transaction")

    if data.get("request") is not None:
        cleaned_data["request"] = data.get("request")

    if data.get("tags") is not None:
        cleaned_data["tags"] = data.get("tags")

    if data.get("exception") is not None:
        cleaned_data["exception"] = data.get("exception")

        for value in cleaned_data["exception"]["values"]:
            for frame in value.get("stacktrace", {}).get("frames", []):
                del frame["vars"]
                del frame["pre_context"]
                del frame["context_line"]
                del frame["post_context"]

    if data.get("extra") is not None:
        cleaned_data["extra"] = {}

        for key in data["extra"].keys():
            if key == "lambda":
                for lambda_key in data["extra"]["lambda"].keys():
                    if lambda_key in ["function_name"]:
                        cleaned_data["extra"].setdefault("lambda", {})[lambda_key] = data["extra"]["lambda"][lambda_key]
            elif key == "cloudwatch logs":
                for cloudwatch_key in data["extra"]["cloudwatch logs"].keys():
                    if cloudwatch_key in ["url", "log_group", "log_stream"]:
                        cleaned_data["extra"].setdefault("cloudwatch logs", {})[cloudwatch_key] = data["extra"]["cloudwatch logs"][cloudwatch_key].split("=")[0]

    if data.get("level") is not None:
        cleaned_data["level"] = data.get("level")

    if data.get("message") is not None:
        cleaned_data["message"] = data.get("message")

    if "contexts" not in cleaned_data:
        raise Exception(json.dumps(data))

    return cleaned_data

def event_processor(event):
    return truncate_data(event)

def envelope_processor(envelope):
    (item,) = envelope.items
    item_json = json.loads(item.get_bytes())

    return truncate_data(item_json)


class TestTransport(Transport):
    def capture_envelope(self, envelope):
        envelope_items = envelope_processor(envelope)
        print("\\nENVELOPE: {}\\n".format(json.dumps(envelope_items)))

def init_sdk(timeout_warning=False, **extra_init_args):
    sentry_sdk.init(
        dsn="https://123abc@example.com/123",
        transport=TestTransport,
        integrations=[AwsLambdaIntegration(timeout_warning=timeout_warning)],
        shutdown_timeout=10,
        **extra_init_args
    )
"""


@pytest.fixture
def lambda_client():
    from tests.integrations.aws_lambda.client import get_boto_client

    return get_boto_client()


@pytest.fixture(params=RUNTIMES_TO_TEST)
def lambda_runtime(request):
    return request.param


@pytest.fixture
def run_lambda_function(request, lambda_client, lambda_runtime):
    def inner(
        code, payload, timeout=30, syntax_check=True, layer=None, initial_handler=None
    ):
        from tests.integrations.aws_lambda.client import run_lambda_function

        response = run_lambda_function(
            client=lambda_client,
            runtime=lambda_runtime,
            code=code,
            payload=payload,
            add_finalizer=request.addfinalizer,
            timeout=timeout,
            syntax_check=syntax_check,
            layer=layer,
            initial_handler=initial_handler,
        )

        # Make sure the "ENVELOPE:" and "EVENT:" log entries are always starting a new line. (Sometimes they don't.)
        response["LogResult"] = (
            base64.b64decode(response["LogResult"])
            .replace(b"EVENT:", b"\nEVENT:")
            .replace(b"ENVELOPE:", b"\nENVELOPE:")
            .splitlines()
        )
        response["Payload"] = json.loads(response["Payload"].read().decode("utf-8"))
        del response["ResponseMetadata"]

        envelope_items = []

        for line in response["LogResult"]:
            print("AWS:", line)
            if line.startswith(b"ENVELOPE: "):
                line = line[len(b"ENVELOPE: ") :]
                envelope_items.append(json.loads(line.decode("utf-8")))
            else:
                continue

        return envelope_items, response

    return inner


def test_initialization_order(run_lambda_function):
    """Zappa lazily imports our code, so by the time we monkeypatch the handler
    as seen by AWS already runs. At this point at least draining the queue
    should work."""

    envelope_items, _ = run_lambda_function(
        LAMBDA_PRELUDE
        + dedent(
            """
            def test_handler(event, context):
                init_sdk()
                sentry_sdk.capture_exception(Exception("Oh!"))
        """
        ),
        b'{"foo": "bar"}',
    )

    (event,) = envelope_items

    assert event["level"] == "error"
    (exception,) = event["exception"]["values"]
    assert exception["type"] == "Exception"
    assert exception["value"] == "Oh!"



def test_traces_sampler_gets_correct_values_in_sampling_context(
    run_lambda_function,
    DictionaryContaining,  # noqa: N803
    ObjectDescribedBy,  # noqa: N803
    StringContaining,  # noqa: N803
):
    # TODO: This whole thing is a little hacky, specifically around the need to
    # get `conftest.py` code into the AWS runtime, which is why there's both
    # `inspect.getsource` and a copy of `_safe_is_equal` included directly in
    # the code below. Ideas which have been discussed to fix this:

    # - Include the test suite as a module installed in the package which is
    #   shot up to AWS
    # - In client.py, copy `conftest.py` (or wherever the necessary code lives)
    #   from the test suite into the main SDK directory so it gets included as
    #   "part of the SDK"

    # It's also worth noting why it's necessary to run the assertions in the AWS
    # runtime rather than asserting on side effects the way we do with events
    # and envelopes. The reasons are two-fold:

    # - We're testing against the `LambdaContext` class, which only exists in
    #   the AWS runtime
    # - If we were to transmit call args data they way we transmit event and
    #   envelope data (through JSON), we'd quickly run into the problem that all
    #   sorts of stuff isn't serializable by `json.dumps` out of the box, up to
    #   and including `datetime` objects (so anything with a timestamp is
    #   automatically out)

    # Perhaps these challenges can be solved in a cleaner and more systematic
    # way if we ever decide to refactor the entire AWS testing apparatus.

    import inspect

    _, response = run_lambda_function(
        LAMBDA_PRELUDE
        + dedent(inspect.getsource(StringContaining))
        + dedent(inspect.getsource(DictionaryContaining))
        + dedent(inspect.getsource(ObjectDescribedBy))
        + dedent(
            """
            from unittest import mock

            def _safe_is_equal(x, y):
                # copied from conftest.py - see docstring and comments there
                try:
                    is_equal = x.__eq__(y)
                except AttributeError:
                    is_equal = NotImplemented

                if is_equal == NotImplemented:
                    # using == smoothes out weird variations exposed by raw __eq__
                    return x == y

                return is_equal

            def test_handler(event, context):
                # this runs after the transaction has started, which means we
                # can make assertions about traces_sampler
                try:
                    traces_sampler.assert_any_call(
                        DictionaryContaining(
                            {
                                "aws_event": DictionaryContaining({
                                    "httpMethod": "GET",
                                    "path": "/sit/stay/rollover",
                                    "headers": {"Host": "x.io", "X-Forwarded-Proto": "http"},
                                }),
                                "aws_context": ObjectDescribedBy(
                                    type=get_lambda_bootstrap().LambdaContext,
                                    attrs={
                                        'function_name': StringContaining("test_"),
                                        'function_version': '$LATEST',
                                    }
                                )
                            }
                        )
                    )
                except AssertionError:
                    # catch the error and return it because the error itself will
                    # get swallowed by the SDK as an "internal exception"
                    return {"AssertionError raised": True,}

                return {"AssertionError raised": False,}


            traces_sampler = mock.Mock(return_value=True)

            init_sdk(
                traces_sampler=traces_sampler,
            )
        """
        ),
        b'{"httpMethod": "GET", "path": "/sit/stay/rollover", "headers": {"Host": "x.io", "X-Forwarded-Proto": "http"}}',
    )

    assert response["Payload"]["AssertionError raised"] is False
