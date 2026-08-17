"""Kvasir access: kv2_course / kv2_text Lambda invocation and S3 prompt objects.

All Kvasir mutations go through the deployed Lambdas — never straight into
Kvasir's SQL. AWS clients are built from explicit .env credentials rather than
boto3's implicit credential chain, so this pipeline can never silently act as
whichever profile happens to be configured on the machine.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from src.models import KvasirConfig
from src.secrets import Secrets

logger = logging.getLogger(__name__)


class KvasirError(RuntimeError):
    """Raised when a Kvasir Lambda call fails or returns an error status."""


class KvasirClient:
    """Wrapper around the kv2_* Lambdas and the courses S3 bucket."""

    def __init__(self, secrets: Secrets, config: KvasirConfig):
        secrets.require_kvasir()
        self.secrets = secrets
        self.config = config
        self.region = config.aws_region or secrets.aws_default_region or "us-east-1"
        self._session = None
        self._lambda = None
        self._s3 = None

    # ── AWS clients ───────────────────────────────────────────────────────────

    @property
    def session(self):
        if self._session is None:
            import boto3

            self._session = boto3.Session(
                aws_access_key_id=self.secrets.aws_access_key_id,
                aws_secret_access_key=self.secrets.aws_secret_access_key,
                aws_session_token=self.secrets.aws_session_token or None,
                region_name=self.region,
            )
        return self._session

    @property
    def lambda_client(self):
        if self._lambda is None:
            self._lambda = self.session.client("lambda")
        return self._lambda

    @property
    def s3_client(self):
        if self._s3 is None:
            self._s3 = self.session.client("s3")
        return self._s3

    # ── Lambda invocation ─────────────────────────────────────────────────────

    def _invoke(self, function_name: str, payload: dict) -> dict:
        """Invoke a kv2_* Lambda with a synthetic API Gateway event.

        The Lambdas read the caller identity from
        ``requestContext.authorizer.claims.sub``; there is no HTTP layer here,
        so we construct that envelope ourselves.
        """
        event = {
            "requestContext": {
                "authorizer": {"claims": {"sub": self.secrets.kvasir_user_sub}}
            },
            "body": json.dumps(payload, ensure_ascii=False),
        }
        action = payload.get("action")

        try:
            response = self.lambda_client.invoke(
                FunctionName=function_name,
                InvocationType="RequestResponse",
                Payload=json.dumps(event, ensure_ascii=False).encode("utf-8"),
            )
        except Exception as exc:  # noqa: BLE001 - normalized into KvasirError
            raise KvasirError(f"{function_name}/{action}: invoke failed: {exc}") from exc

        request_id = response.get("ResponseMetadata", {}).get("RequestId")
        function_error = response.get("FunctionError")
        raw = response["Payload"].read()

        if function_error:
            raise KvasirError(
                f"{function_name}/{action}: FunctionError={function_error} "
                f"request_id={request_id} payload={raw[:500]!r}"
            )

        try:
            outer = json.loads(raw)
        except ValueError as exc:
            raise KvasirError(
                f"{function_name}/{action}: unparsable response: {raw[:300]!r}"
            ) from exc

        status = int(outer.get("statusCode", 500))
        body_raw = outer.get("body") or "{}"
        try:
            body = json.loads(body_raw) if isinstance(body_raw, str) else body_raw
        except ValueError:
            body = {"message": str(body_raw)[:300]}

        logger.info(
            "kvasir %s/%s status=%s request_id=%s component_id=%s",
            function_name,
            action,
            status,
            request_id,
            (body or {}).get("component_id") if isinstance(body, dict) else None,
        )

        if status >= 400:
            message = body.get("message") if isinstance(body, dict) else str(body)[:200]
            raise KvasirError(f"{function_name}/{action}: HTTP {status}: {message}")

        return body if isinstance(body, dict) else {"result": body}

    def invoke_kv2_course(self, payload: dict) -> dict:
        return self._invoke(self.config.kv2_course_lambda_name, payload)

    def invoke_kv2_text(self, payload: dict) -> dict:
        return self._invoke(self.config.kv2_text_lambda_name, payload)

    # ── Components ────────────────────────────────────────────────────────────

    def get_component(self, component_id: Any, siblings: bool = False) -> dict:
        """Fetch a component record. ``editor=True`` avoids counting a visit."""
        return self.invoke_kv2_course(
            {
                "action": "get_component",
                "component_id": int(component_id),
                "siblings": siblings,
                "editor": True,
            }
        )

    def get_course(self, course_id: Any) -> dict:
        """Fetch a course record. Readable for any course, not only ours."""
        body = self.invoke_kv2_course(
            {"action": "get_course", "course_id": int(course_id)}
        )
        course = body.get("course")
        return course if isinstance(course, dict) else body

    def component_update(self, component_record: dict) -> dict:
        """Create (no ``id``) or update (with ``id``) a component."""
        return self.invoke_kv2_course(
            {"action": "component_update", "component_record": component_record}
        )

    def list_scrolls(self, component_id: Any, limit: int = 50, offset: int = 0) -> list[dict]:
        """List the scrolls attached to a component (kv2_text ``list_scrolls``)."""
        body = self.invoke_kv2_text(
            {
                "action": "list_scrolls",
                "component_id": int(component_id),
                "limit": limit,
                "offset": offset,
            }
        )
        scrolls = body.get("scrolls")
        return scrolls if isinstance(scrolls, list) else []

    # ── S3 prompt objects ─────────────────────────────────────────────────────

    @staticmethod
    def language_postfix(language: str) -> str:
        """Kvasir stores English assets unsuffixed and others as ``.<lang>``."""
        lang = (language or "en").strip().lower()
        return "" if lang in ("", "en") else f".{lang}"

    def text_key(self, course_id: Any, name: Any, language: str, ext: str = "txt") -> str:
        """``{course_id}/text/{name}{.lang}.{ext}`` — the kv2_text convention."""
        return f"{course_id}/text/{name}{self.language_postfix(language)}.{ext}"

    def copy_object(self, source_key: str, destination_key: str) -> None:
        bucket = self.config.courses_bucket
        try:
            self.s3_client.copy_object(
                Bucket=bucket,
                CopySource={"Bucket": bucket, "Key": source_key},
                Key=destination_key,
            )
        except Exception as exc:  # noqa: BLE001
            raise KvasirError(f"S3 copy {source_key} -> {destination_key} failed: {exc}") from exc
        logger.info("S3 copied %s -> %s", source_key, destination_key)

    def get_text(self, key: str) -> str:
        try:
            obj = self.s3_client.get_object(Bucket=self.config.courses_bucket, Key=key)
            return obj["Body"].read().decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            raise KvasirError(f"S3 read {key} failed: {exc}") from exc

    def put_text(self, key: str, body: str) -> None:
        try:
            self.s3_client.put_object(
                Bucket=self.config.courses_bucket,
                Key=key,
                Body=body.encode("utf-8"),
                ContentType="text/plain; charset=utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            raise KvasirError(f"S3 write {key} failed: {exc}") from exc
        logger.info("S3 wrote %s (%d chars)", key, len(body))

    # ── URLs ──────────────────────────────────────────────────────────────────

    def editor_url(self, component_id: Any) -> str:
        return f"{self.config.echo_editor_base_url}{component_id}"

    def scroll_quiz_url(self, component_id: Any, scroll_id: str) -> str:
        """Public quiz URL, matching buildScrollQuizPageUrl() in scroll.js."""
        return f"{self.config.scroll_quiz_base_url}?id={component_id}#{scroll_id}"


def optional_client(secrets: Secrets, config: KvasirConfig) -> Optional[KvasirClient]:
    """Build a client, or return None when Kvasir credentials are absent."""
    try:
        return KvasirClient(secrets, config)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Kvasir client unavailable: %s", exc)
        return None
