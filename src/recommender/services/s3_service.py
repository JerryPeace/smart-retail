"""S3Service — thin wrapper for reading and writing S3

Design principles:
  - Handles only I/O (read / write / list), no data transformation
  - Automatically handles LocalStack vs real AWS (via settings.aws_endpoint_url_s3)
  - async (aioboto3) to fit the FastAPI async stack
"""
from contextlib import asynccontextmanager
from typing import AsyncIterator

import aioboto3

from recommender.config import settings


class S3Service:
    def __init__(self) -> None:
        # Don't specify an access key — let boto3 read it automatically from the
        # AWS_PROFILE environment variable or ~/.aws/credentials (supports lab role
        # temporary credentials / IAM role / API key, from any source).
        # LocalStack doesn't check actual credentials, so a lab role can call LocalStack S3 too.
        self._session = aioboto3.Session(region_name=settings.aws_region)

    @asynccontextmanager
    async def _client(self) -> AsyncIterator:
        async with self._session.client(
            "s3",
            endpoint_url=settings.aws_endpoint_url_s3,  # None → use real AWS; if set, use LocalStack
        ) as client:
            yield client

    # === Read ===
    async def get_object(self, bucket: str, key: str) -> bytes:
        async with self._client() as s3:
            response = await s3.get_object(Bucket=bucket, Key=key)
            return await response["Body"].read()

    async def get_text(self, bucket: str, key: str, encoding: str = "utf-8") -> str:
        data = await self.get_object(bucket, key)
        return data.decode(encoding)

    # === Write ===
    async def put_object(
        self, bucket: str, key: str, body: bytes | str, content_type: str | None = None
    ) -> None:
        kwargs = {"Bucket": bucket, "Key": key, "Body": body}
        if content_type:
            kwargs["ContentType"] = content_type
        async with self._client() as s3:
            await s3.put_object(**kwargs)

    async def put_text(
        self, bucket: str, key: str, text: str, content_type: str = "text/plain"
    ) -> None:
        await self.put_object(bucket, key, text.encode("utf-8"), content_type)

    # === List ===
    async def list_objects(self, bucket: str, prefix: str = "") -> list[str]:
        keys: list[str] = []
        async with self._client() as s3:
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"])
        return keys

    async def exists(self, bucket: str, key: str) -> bool:
        async with self._client() as s3:
            try:
                await s3.head_object(Bucket=bucket, Key=key)
                return True
            except Exception:
                return False
