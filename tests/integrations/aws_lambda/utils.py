import gzip
import json
import os
import requests
import sys
import time
import threading

from aws_cdk import (
    CfnResource,
    Stack,
)
from constructs import Construct
from fastapi import FastAPI, Request
import uvicorn

from scripts.build_aws_lambda_layer import build_packaged_zip, DIST_PATH


LAMBDA_FUNCTION_DIR = "./tests/integrations/aws_lambda/lambda_functions/"
LAMBDA_FUNCTION_TIMEOUT = 10
SAM_PORT = 3001

PYTHON_VERSION = f"python{sys.version_info.major}.{sys.version_info.minor}"


class LocalLambdaStack(Stack):
    """
    Uses the AWS CDK to create a local SAM stack containing Lambda functions.
    """
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        print("[LocalLambdaStack] Creating local SAM Lambda Stack: %s" % self)

        # Override the template synthesis
        self.template_options.template_format_version = "2010-09-09"
        self.template_options.transforms = ["AWS::Serverless-2016-10-31"]

        print("[LocalLambdaStack] Create Sentry Lambda layer package")
        filename = "sentry-sdk-lambda-layer.zip"
        build_packaged_zip(
            make_dist=True,
            out_zip_filename=filename,
        )

        print(
            "[LocalLambdaStack] Add Sentry Lambda layer containing the Sentry SDK to the SAM stack"
        )
        self.sentry_layer = CfnResource(
            self,
            "SentryPythonServerlessSDK",
            type="AWS::Serverless::LayerVersion",
            properties={
                "ContentUri": os.path.join(DIST_PATH, filename),
                "CompatibleRuntimes": [
                    PYTHON_VERSION,
                ],
            },
        )

        print(
            "[LocalLambdaStack] Add all Lambda functions defined in "
            "/tests/integrations/aws_lambda/lambda_functions/ to the SAM stack"
        )
        lambda_dirs = [
            d
            for d in os.listdir(LAMBDA_FUNCTION_DIR)
            if os.path.isdir(os.path.join(LAMBDA_FUNCTION_DIR, d))
        ]
        for lambda_dir in lambda_dirs:
            CfnResource(
                self,
                lambda_dir,
                type="AWS::Serverless::Function",
                properties={
                    "CodeUri": os.path.join(LAMBDA_FUNCTION_DIR, lambda_dir),
                    "Handler": "sentry_sdk.integrations.init_serverless_sdk.sentry_lambda_handler",
                    "Runtime": PYTHON_VERSION,
                    "Timeout": LAMBDA_FUNCTION_TIMEOUT,
                    "Layers": [
                        {"Ref": self.sentry_layer.logical_id}
                    ],  # Add layer containing the Sentry SDK to function.
                    "Environment": {
                        "Variables": {
                            "SENTRY_DSN": "http://123@host.docker.internal:9999/0",
                            "SENTRY_INITIAL_HANDLER": "index.handler",
                            "SENTRY_TRACES_SAMPLE_RATE": "1.0",
                        }
                    },
                },
            )
            print(
                "[LocalLambdaStack] - Created Lambda function: %s (%s)"
                % (
                    lambda_dir,
                    os.path.join(LAMBDA_FUNCTION_DIR, lambda_dir),
                )
            )

    @classmethod
    def wait_for_stack(cls, timeout=30, port=SAM_PORT):
        """
        Wait for SAM to be ready, with timeout.
        """
        start_time = time.time()
        while True:
            if time.time() - start_time > timeout:
                raise TimeoutError(
                    "SAM failed to start within %s seconds. (Maybe Docker is not running?)"
                    % timeout
                )

            try:
                # Try to connect to SAM
                response = requests.get(f"http://127.0.0.1:{port}/")
                if response.status_code == 200 or response.status_code == 404:
                    return

            except requests.exceptions.ConnectionError:
                time.sleep(1)
                continue


class SentryServerForTesting:
    """
    A simple Sentry.io style server that accepts envelopes and stores them in a list.
    """

    def __init__(self, port=9999, log_level="warning"):
        self.envelopes = []
        self.port = port
        self.log_level = log_level
        self.app = FastAPI()

        @self.app.post("/api/0/envelope/")
        async def envelope(request: Request):
            print("[SentryServerForTesting] Received envelope")
            try:
                raw_body = await request.body()
            except:
                return {"status": "no body received"}

            try:
                body = gzip.decompress(raw_body).decode("utf-8")
            except:
                # If decompression fails, assume it's plain text
                body = raw_body.decode("utf-8")

            lines = body.split("\n")

            current_line = 1  # line 0 is envelope header
            while current_line < len(lines):
                # skip empty lines
                if not lines[current_line].strip():
                    current_line += 1
                    continue

                # skip envelope item header
                current_line += 1

                # add envelope item to store
                envelope_item = lines[current_line]
                if envelope_item.strip():
                    self.envelopes.append(json.loads(envelope_item))

            return {"status": "ok"}

    def run_server(self):
        uvicorn.run(self.app, host="0.0.0.0", port=self.port, log_level=self.log_level)

    def start(self):
        print("[SentryServerForTesting] Starting server")
        server_thread = threading.Thread(target=self.run_server, daemon=True)
        server_thread.start()

    def clear_envelopes(self):
        print("[SentryServerForTesting] Clearing envelopes")
        self.envelopes = []
