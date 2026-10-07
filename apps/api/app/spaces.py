"""DigitalOcean Spaces, through its S3-compatible API.

The same store as `LocalStorage`, one level removed. Two differences matter and
neither is cosmetic:

* **There are no directories.** `list_keys` is a prefix listing rather than a
  walk, and `local_dir` returns None -- which is the signal
  `SourceStore.materialize` reads when it has to stage objects onto disk for the
  segmenter. That is the one caller that cannot pretend the store is a
  dictionary, and it is why swapping this in is not only a constructor change.

* **There is no filesystem, so failures arrive in a different vocabulary.**
  botocore raises `ClientError`, not `OSError`. Every call here is translated:
  a missing key becomes a 404, and anything else becomes `OSError`, because that
  is what a failed file operation raises and what callers already guard against.
  A store whose failures were spelled differently would be a store whose guards
  only worked on one backend -- and the guard that matters runs during startup,
  where the symptom is an API that will not boot.

Nothing here is viewer-aware. Scoping sits above this in `app/tenancy.py`, so a
key arriving here is already namespaced.
"""

from __future__ import annotations

import uuid
from typing import Any

import boto3
from fastapi import HTTPException, status

from .storage import SCHEME, check_key, check_prefix, key_from_ref

#: S3 deletes at most a thousand objects per call.
_DELETE_BATCH = 1000

#: The ways the service says "there is nothing there". The status code is checked
#: as well, because a proxy or a misconfiguration can answer with a code this
#: list has never heard of.
_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound", "NoSuchBucket"})


def _response_of(exc: Exception) -> dict[str, Any]:
    response = getattr(exc, "response", None)
    return response if isinstance(response, dict) else {}


def _error_code(exc: Exception) -> str:
    return str(_response_of(exc).get("Error", {}).get("Code", ""))


def _status_code(exc: Exception) -> int | None:
    value = _response_of(exc).get("ResponseMetadata", {}).get("HTTPStatusCode")
    return value if isinstance(value, int) else None


class SpacesStorage:
    """A bucket. Objects are addressed by key, exactly as files were by path."""

    def __init__(
        self,
        bucket: str,
        region: str = "nyc3",
        access_key: str = "",
        secret_key: str = "",
        endpoint: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.bucket = bucket
        self.region = region
        # Injected by tests, so the store can be exercised without a network or
        # a credential. Nothing in the application passes one.
        self._client = client or boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint or f"https://{region}.digitaloceanspaces.com",
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
        )

    # -- the error vocabulary -----------------------------------------------

    def _request(self, operation: str, **kwargs: Any) -> Any:
        try:
            return getattr(self._client, operation)(**kwargs)
        except Exception as exc:
            code = _error_code(exc)
            if code in _MISSING_CODES or _status_code(exc) == 404:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND, detail="missing artefact"
                ) from exc
            # Deliberately broad: a credential problem, a timeout and a DNS
            # failure all mean the same thing to a caller, which is that the
            # store is not answering.
            raise OSError(f"spaces {operation} failed: {code or exc}") from exc

    # -- refs ---------------------------------------------------------------

    def ref(self, key: str) -> str:
        return f"{SCHEME}{check_key(key)}"

    def _key(self, ref: str) -> str:
        return check_key(key_from_ref(ref))

    # -- io -----------------------------------------------------------------

    def put(self, key: str, data: bytes) -> str:
        key = check_key(key)
        self._request("put_object", Bucket=self.bucket, Key=key, Body=data)
        return self.ref(key)

    def get(self, ref: str) -> bytes:
        body = self._request("get_object", Bucket=self.bucket, Key=self._key(ref))["Body"]
        return body.read()

    def exists(self, ref: str) -> bool:
        try:
            key = self._key(ref)
        except HTTPException:
            return False
        try:
            self._request("head_object", Bucket=self.bucket, Key=key)
        except HTTPException:
            return False
        return True

    def remove_tree(self, key: str) -> int:
        """Delete an object, or everything under a prefix. Returns how many went.

        A bucket has no directories, so "a file" and "a directory" are the same
        question asked of different keys. Both cases are handled because both are
        asked: deleting a job removes `jobs/<id>.json`, and deleting the artifacts
        it produced removes the `artifacts/<id>/` prefix.
        """
        key = check_key(key)
        doomed = [key] if self.exists(self.ref(key)) else []
        doomed += self.list_keys(f"{key}/")
        if not doomed:
            return 0

        for start in range(0, len(doomed), _DELETE_BATCH):
            batch = doomed[start:start + _DELETE_BATCH]
            self._request(
                "delete_objects",
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": item} for item in batch], "Quiet": True},
            )
        return len(doomed)

    def list_keys(self, prefix: str) -> list[str]:
        prefix = check_prefix(prefix)
        found: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = self._request("list_objects_v2", **kwargs)
            found += [entry["Key"] for entry in page.get("Contents") or []]
            token = page.get("NextContinuationToken") if page.get("IsTruncated") else None
            if not token:
                break
        return sorted(found)

    def local_dir(self, prefix: str) -> None:
        """There is no directory to hand over, and that is the answer.

        `SourceStore.materialize` reads this None and stages the objects onto
        disk for as long as the segmenter needs them.
        """
        return None

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())

    def reset(self) -> None:
        """Refused, unlike the local store.

        `LocalStorage.reset` deletes a development directory that is trivially
        recreated. Emptying a bucket is the same call shape and nothing like the
        same consequence, so the two do not share a verb here.
        """
        raise RuntimeError(
            "refusing to empty a bucket. Delete the bucket deliberately, or the "
            "prefix you mean, rather than through a call written for a dev directory."
        )
