"""
S3 — canonical artifact store.
Owns: logs bundles, patches, diffs, screenshots, test outputs, reports.
"""

import json
from typing import Any

import boto3
from botocore.exceptions import ClientError

from src.common.config import settings
from src.common.logging import get_logger

logger = get_logger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        kwargs: dict[str, Any] = {"region_name": settings.AWS_REGION}
        if settings.AWS_ENDPOINT_URL:
            kwargs["endpoint_url"] = settings.AWS_ENDPOINT_URL
        _client = boto3.client("s3", **kwargs)
    return _client


# ── Write ─────────────────────────────────────────────────────────────────────

def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    _get_client().put_object(
        Bucket=settings.S3_BUCKET,
        Key=key,
        Body=data,
        ContentType=content_type,
        ServerSideEncryption="AES256",
    )
    logger.debug("s3.put", key=key, bytes=len(data))
    return key


def put_text(key: str, text: str) -> str:
    return put_bytes(key, text.encode("utf-8"), content_type="text/plain; charset=utf-8")


def put_json(key: str, data: Any) -> str:
    return put_bytes(
        key,
        json.dumps(data, indent=2, default=str).encode("utf-8"),
        content_type="application/json",
    )


# ── Read ──────────────────────────────────────────────────────────────────────

def get_bytes(key: str) -> bytes:
    response = _get_client().get_object(Bucket=settings.S3_BUCKET, Key=key)
    return response["Body"].read()


def get_text(key: str) -> str:
    return get_bytes(key).decode("utf-8")


def get_json(key: str) -> Any:
    return json.loads(get_text(key))


# ── Utilities ─────────────────────────────────────────────────────────────────

def exists(key: str) -> bool:
    try:
        _get_client().head_object(Bucket=settings.S3_BUCKET, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def presigned_url(key: str, expires_in: int = 3600) -> str:
    url = _get_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.S3_BUCKET, "Key": key},
        ExpiresIn=expires_in,
    )
    # In local mode the URL contains the internal Docker hostname — rewrite for browser access
    if settings.APP_ENV == "local":
        url = url.replace("http://localstack:4566", "http://localhost:4566")
    return url


def ensure_bucket_exists() -> None:
    """Create the S3 bucket if it doesn't exist (local dev only)."""
    try:
        _get_client().head_bucket(Bucket=settings.S3_BUCKET)
    except ClientError:
        if settings.AWS_REGION == "us-east-1":
            _get_client().create_bucket(Bucket=settings.S3_BUCKET)
        else:
            _get_client().create_bucket(
                Bucket=settings.S3_BUCKET,
                CreateBucketConfiguration={"LocationConstraint": settings.AWS_REGION},
            )
        logger.info("s3.bucket.created", bucket=settings.S3_BUCKET)
