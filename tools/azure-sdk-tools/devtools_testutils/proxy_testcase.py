# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------
import os
import logging
import requests
import six
import sys
from typing import TYPE_CHECKING
import pdb

try:
    # py3
    import urllib.parse as url_parse
except:
    # py2
    import urlparse as url_parse

import pytest
import subprocess

from azure.core.exceptions import ResourceNotFoundError
from azure.core.pipeline.policies import ContentDecodePolicy
# the functions we patch
from azure.core.pipeline.transport import RequestsTransport

# the trimming function to clean up incoming arguments to the test function we are wrapping
from azure_devtools.scenario_tests.utilities import trim_kwargs_from_test_function
from .helpers import is_live, is_live_and_not_recording
from .config import PROXY_URL

if TYPE_CHECKING:
    from typing import Tuple

# To learn about how to migrate SDK tests to the test proxy, please refer to the migration guide at
# https://github.com/Azure/azure-sdk-for-python/blob/main/doc/dev/test_proxy_migration_guide.md


# defaults
RECORDING_START_URL = "{}/record/start".format(PROXY_URL)
RECORDING_STOP_URL = "{}/record/stop".format(PROXY_URL)
PLAYBACK_START_URL = "{}/playback/start".format(PROXY_URL)
PLAYBACK_STOP_URL = "{}/playback/stop".format(PROXY_URL)

# we store recording IDs in a module-level variable so that sanitizers can access them
# we map test IDs to recording IDs, rather than storing only the current test's recording ID, for parallelization
this = sys.modules[__name__]
this.recording_ids = {}


def get_recording_id():
    test_id = get_test_id()
    return this.recording_ids.get(test_id)


def get_test_id():
    # type: () -> str
    # pytest sets the current running test in an environment variable
    setting_value = os.getenv("PYTEST_CURRENT_TEST")

    path_to_test = os.path.normpath(setting_value.split(" ")[0])
    path_components = path_to_test.split(os.sep)

    for idx, val in enumerate(path_components):
        if val.startswith("test"):
            path_components.insert(idx + 1, "recordings")
            break

    return os.sep.join(path_components).replace("::", "").replace("\\", "/")


def start_record_or_playback(test_id):
    # type: (str) -> Tuple(str, dict)
    """Sends a request to begin recording or playing back the provided test.
    
    This returns a tuple, (a, b), where a is the recording ID of the test and b is the `variables` dictionary that maps
    test variables to values. If no variable dictionary was stored when the test was recorded, b is an empty dictionary.
    """
    head_commit = subprocess.check_output(["git", "rev-parse", "HEAD"])
    current_sha = head_commit.decode("utf-8").strip()
    variables = {}  # this stores a dictionary of test variable values that could have been stored with a recording

    if is_live():
        result = requests.post(
            RECORDING_START_URL,
            headers={"x-recording-file": test_id, "x-recording-sha": current_sha},
        )
        recording_id = result.headers["x-recording-id"]
    else:
        result = requests.post(
            PLAYBACK_START_URL,
            headers={"x-recording-file": test_id, "x-recording-sha": current_sha},
        )
        try:
            recording_id = result.headers["x-recording-id"]
        except KeyError:
            raise ValueError("No recording file found for {}".format(test_id))
        if result.text:
            try:
                variables = result.json()
            except ValueError as ex:  # would be a JSONDecodeError on Python 3, which subclasses ValueError
                six.raise_from(
                    ValueError("The response body returned from starting playback did not contain valid JSON"), ex
                )

    # set recording ID in a module-level variable so that sanitizers can access it
    this.recording_ids[test_id] = recording_id
    return (recording_id, variables)


def stop_record_or_playback(test_id, recording_id, test_output):
    # type: (str, str, dict) -> None
    if is_live():
        requests.post(
            RECORDING_STOP_URL,
            headers={
                "x-recording-file": test_id,
                "x-recording-id": recording_id,
                "x-recording-save": "true",
                "Content-Type": "application/json"
            },
            json=test_output
        )
    else:
        requests.post(
            PLAYBACK_STOP_URL,
            headers={"x-recording-file": test_id, "x-recording-id": recording_id},
        )


def get_proxy_netloc():
    parsed_result = url_parse.urlparse(PROXY_URL)
    return {"scheme": parsed_result.scheme, "netloc": parsed_result.netloc}


def transform_request(request, recording_id):
    """Redirect the request to the test proxy, and store the original request URI in a header"""
    headers = request.headers

    parsed_result = url_parse.urlparse(request.url)
    updated_target = parsed_result._replace(**get_proxy_netloc()).geturl()
    if headers.get("x-recording-upstream-base-uri", None) is None:
        headers["x-recording-upstream-base-uri"] = "{}://{}".format(parsed_result.scheme, parsed_result.netloc)
    headers["x-recording-id"] = recording_id
    headers["x-recording-mode"] = "record" if is_live() else "playback"
    request.url = updated_target


def recorded_by_proxy(test_func):
    """Decorator that redirects network requests to target the azure-sdk-tools test proxy. Use with recorded tests.

    For more details and usage examples, refer to
    https://github.com/Azure/azure-sdk-for-python/blob/main/doc/dev/test_proxy_migration_guide.md
    """

    def record_wrap(*args, **kwargs):
        if sys.version_info.major == 2 and not is_live():
            pytest.skip("Playback testing is incompatible with the azure-sdk-tools test proxy on Python 2")

        def transform_args(*args, **kwargs):
            copied_positional_args = list(args)
            request = copied_positional_args[1]

            transform_request(request, recording_id)

            return tuple(copied_positional_args), kwargs

        trimmed_kwargs = {k: v for k, v in kwargs.items()}
        trim_kwargs_from_test_function(test_func, trimmed_kwargs)

        if is_live_and_not_recording():
            return test_func(*args, **trimmed_kwargs)

        test_id = get_test_id()
        recording_id, variables = start_record_or_playback(test_id)
        original_transport_func = RequestsTransport.send

        def combined_call(*args, **kwargs):
            adjusted_args, adjusted_kwargs = transform_args(*args, **kwargs)
            return original_transport_func(*adjusted_args, **adjusted_kwargs)

        RequestsTransport.send = combined_call

        # call the modified function
        # we define test_output before invoking the test so the variable is defined in case of an exception
        test_output = None
        try:
            test_output = test_func(*args, variables=variables, **trimmed_kwargs)
        except TypeError:
            logger = logging.getLogger()
            logger.info(
                "This test can't accept variables as input. The test method should accept `**kwargs` and/or a "
                "`variables` parameter to make use of recorded test variables."
            )
        try:
            test_output = test_func(*args, **trimmed_kwargs)
        except ResourceNotFoundError as error:
            error_body = ContentDecodePolicy.deserialize_from_http_generics(error.response)
            error_with_message = ResourceNotFoundError(message=error_body["Message"], response=error.response)
            raise error_with_message
        finally:
            RequestsTransport.send = original_transport_func
            stop_record_or_playback(test_id, recording_id, test_output)

        return test_output

    return record_wrap
