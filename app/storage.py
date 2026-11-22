"""S3-compatible storage (MinIO in compose). Falls back to a local folder when no endpoint is configured."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import get_settings


def _client():
    import boto3
    from botocore.config import Config
    s = get_settings()
    return boto3.client("s3", endpoint_url=s.s3_endpoint_url, aws_access_key_id=s.s3_access_key,
                        aws_secret_access_key=s.s3_secret_key, region_name="us-east-1",
                        config=Config(signature_version="s3v4", retries={"max_attempts": 2},
                                      connect_timeout=3, read_timeout=5))


def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Store and return a download link."""
    s = get_settings()
    if s.s3_endpoint_url:
        c = _client()
        try:
            c.head_bucket(Bucket=s.s3_bucket)
        except Exception:
            c.create_bucket(Bucket=s.s3_bucket)
        c.put_object(Bucket=s.s3_bucket, Key=key, Body=data, ContentType=content_type)
    else:
        path = Path(s.local_store_dir) / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return f"{s.public_base_url.rstrip('/')}/api/v1/files/{key}"


def put_json(key: str, obj: Any) -> str:
    return put_bytes(key, json.dumps(obj, indent=2, default=str).encode(), "application/json")


def get_bytes(key: str) -> tuple[bytes, str]:
    s = get_settings()
    if s.s3_endpoint_url:
        o = _client().get_object(Bucket=s.s3_bucket, Key=key)
        return o["Body"].read(), o.get("ContentType", "application/octet-stream")
    root = Path(s.local_store_dir).resolve()
    path = (root / key).resolve()
    if not path.is_relative_to(root) or not path.exists():
        raise FileNotFoundError(key)
    ct = "application/json" if key.endswith(".json") else "application/octet-stream"
    return path.read_bytes(), ct


def storage_ready() -> bool:
    s = get_settings()
    try:
        if s.s3_endpoint_url:
            _client().list_buckets()
        else:
            Path(s.local_store_dir).mkdir(parents=True, exist_ok=True)
        return True
    except Exception:
        return False
