"""The synthetic API Gateway event kv2_* Lambdas are invoked with."""

from __future__ import annotations

import json

import pytest

from src.kvasir_client import KvasirClient, KvasirError
from src.models import KvasirConfig


class FakeLambda:
    """Minimal stand-in for a boto3 Lambda client."""

    def __init__(self, status_code=200, body=None, function_error=None):
        self.invocations: list[dict] = []
        self.status_code = status_code
        self.body = body if body is not None else {"component_id": 4242}
        self.function_error = function_error

    def invoke(self, FunctionName, InvocationType, Payload):  # noqa: N803 - boto3 signature
        self.invocations.append(
            {
                "FunctionName": FunctionName,
                "InvocationType": InvocationType,
                "event": json.loads(Payload.decode("utf-8")),
            }
        )
        outer = {"statusCode": self.status_code, "body": json.dumps(self.body)}
        response = {
            "Payload": _Body(json.dumps(outer).encode("utf-8")),
            "ResponseMetadata": {"RequestId": "req-1"},
        }
        if self.function_error:
            response["FunctionError"] = self.function_error
        return response


class _Body:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


@pytest.fixture
def client(secrets):
    client = KvasirClient(secrets, KvasirConfig())
    client._lambda = FakeLambda()
    return client


def test_event_carries_the_authorizer_sub_and_a_json_body(client, secrets):
    client.component_update({"course_id": 500, "title": "t", "type": "echo",
                             "language": "en", "assets": {}, "details": {}})

    invocation = client._lambda.invocations[0]
    event = invocation["event"]

    assert invocation["FunctionName"] == "kv2_course"
    assert invocation["InvocationType"] == "RequestResponse"
    assert event["requestContext"]["authorizer"]["claims"]["sub"] == secrets.kvasir_user_sub
    assert isinstance(event["body"], str), "body must be a JSON string, as API Gateway sends it"

    body = json.loads(event["body"])
    assert body["action"] == "component_update"
    assert body["component_record"]["type"] == "echo"


def test_get_component_uses_the_editor_flag(client):
    client.get_component(9001)
    body = json.loads(client._lambda.invocations[0]["event"]["body"])
    assert body["action"] == "get_component"
    assert body["component_id"] == 9001
    assert body["editor"] is True, "reading as the editor must not count as a visit"


def test_unicode_survives_the_round_trip(client):
    client.component_update({"course_id": 500, "title": "Опрос дня", "type": "echo",
                             "language": "ru", "assets": {}, "details": {}})
    body = json.loads(client._lambda.invocations[0]["event"]["body"])
    assert body["component_record"]["title"] == "Опрос дня"


def test_error_status_raises_with_the_lambda_message(client):
    client._lambda = FakeLambda(status_code=404, body={"message": "COURSE_NOT_AUTHOR"})
    with pytest.raises(KvasirError, match="COURSE_NOT_AUTHOR"):
        client.get_component(9001)


def test_function_error_raises(client):
    client._lambda = FakeLambda(function_error="Unhandled")
    with pytest.raises(KvasirError, match="FunctionError"):
        client.get_component(9001)


def test_list_scrolls_targets_kv2_text(client):
    client._lambda = FakeLambda(body={"scrolls": [{"scroll_id": "abc"}], "has_more": False})
    scrolls = client.list_scrolls(4242)

    invocation = client._lambda.invocations[0]
    assert invocation["FunctionName"] == "kv2_text"
    body = json.loads(invocation["event"]["body"])
    assert body["action"] == "list_scrolls"
    assert body["component_id"] == 4242
    assert scrolls == [{"scroll_id": "abc"}]


def test_text_key_language_postfix(client):
    assert client.text_key(500, 4242, "en") == "500/text/4242.txt"
    assert client.text_key(501, 4243, "ru") == "501/text/4243.ru.txt"
    assert client.language_postfix("en") == ""
    assert client.language_postfix("ru") == ".ru"


def test_scroll_quiz_url_matches_the_frontend_convention(client):
    assert client.scroll_quiz_url(4242, "AbC123") == "https://quizly.pub/scroll-quiz?id=4242#AbC123"


def test_region_falls_back_to_env_default(secrets):
    client = KvasirClient(secrets, KvasirConfig(aws_region=""))
    assert client.region == "us-east-1"

    client = KvasirClient(secrets, KvasirConfig(aws_region="eu-west-1"))
    assert client.region == "eu-west-1"


def test_missing_kvasir_credentials_fail_fast(secrets):
    from src.secrets import SecretsError

    incomplete = secrets.model_copy(update={"kvasir_user_sub": ""})
    with pytest.raises(SecretsError, match="KVASIR_USER_SUB"):
        KvasirClient(incomplete, KvasirConfig())
