"""Storage abstraction: LocalStorage is a no-op (CLI parity); S3Storage round-trips a
working dir to/from a key prefix. The S3 client is faked (injected via the ``client=``
arg) so these run without boto3 installed."""

from __future__ import annotations

import hashlib

import pytest

from yapper.storage import LocalStorage, S3Storage


def test_local_storage_materialize_persist_are_noops(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("hi")
    store = LocalStorage(tmp_path)
    # No durable backend -> nothing fetched, nothing pushed; the dir is untouched.
    assert store.materialize("anything", work) == 0
    assert store.persist(work, "anything") == 0
    assert (work / "a.txt").read_text() == "hi"


class _FakeS3:
    """Minimal in-memory stand-in for the boto3 S3 client surface S3Storage uses."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def upload_file(self, local, bucket, key):
        with open(local, "rb") as f:
            self.objects[key] = f.read()

    def download_file(self, bucket, key, local):
        import os

        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "wb") as f:
            f.write(self.objects[key])

    def get_paginator(self, _op):
        objects = self.objects

        class _P:
            def paginate(self, Bucket, Prefix):  # noqa: N803
                contents = [
                    {"Key": k, "Size": len(v), "ETag": '"' + hashlib.md5(v).hexdigest() + '"'}
                    for k, v in objects.items() if k.startswith(Prefix)
                ]
                yield {"Contents": contents}

        return _P()

    def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803
        return f"https://fake/{Params['Key']}?op={op}&ttl={ExpiresIn}"


def test_s3_storage_round_trips_a_working_dir(tmp_path):
    work = tmp_path / "src"
    (work / "sub").mkdir(parents=True)
    (work / "top.json").write_text('{"k": 1}')
    (work / "sub" / "frame.bin").write_bytes(b"\x00\x01\x02")

    fake = _FakeS3()
    store = S3Storage("bucket", client=fake)
    prefix = "sess/movie"

    uploaded = store.persist(work, prefix)
    assert uploaded == 2
    # second persist is a no-op (size+md5 match) -> resumability stays cheap
    assert store.persist(work, prefix) == 0

    dest = tmp_path / "dst"
    fetched = store.materialize(prefix, dest)
    assert fetched == 2
    assert (dest / "top.json").read_text() == '{"k": 1}'
    assert (dest / "sub" / "frame.bin").read_bytes() == b"\x00\x01\x02"
    # warm cache: re-materialize fetches nothing
    assert store.materialize(prefix, dest) == 0


def test_s3_presign_urls():
    store = S3Storage("bucket", client=_FakeS3())
    assert "op=put_object" in store.presign_put("sess/movie/source.mp4")
    assert "op=get_object" in store.presign_get("sess/movie/zh/recap_final.mp4")


def test_presign_uses_public_endpoint_client(monkeypatch):
    """With a public endpoint set, presigned URLs must be signed against a client bound
    to that endpoint (browser-reachable), while server-side ops use the internal one."""
    pytest.importorskip("boto3")
    made = []

    class _Client:
        def __init__(self, endpoint):
            self.endpoint = endpoint

        def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803
            return f"{self.endpoint}/{Params['Key']}"

    def fake_boto3_client(_svc, *, endpoint_url=None, region_name=None, config=None):
        c = _Client(endpoint_url)
        made.append(endpoint_url)
        return c

    import boto3

    monkeypatch.setattr(boto3, "client", fake_boto3_client)
    store = S3Storage(
        "bucket", endpoint_url="http://minio:9000",
        public_endpoint_url="http://localhost:9000", region="us-east-1",
    )
    # two clients built: internal + public
    assert "http://minio:9000" in made and "http://localhost:9000" in made
    # presigned URL carries the browser-reachable host, not the internal one
    assert store.presign_get("k").startswith("http://localhost:9000/")
