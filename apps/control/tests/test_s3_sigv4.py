"""SigV4 逐字对拍 AWS 官方示例，防止自造实现「自己对自己」。

向量出处：AWS S3《Signature Calculations for the Authorization Header》里的
GET object / PUT object / GET bucket 三个示例（2013-05-24、us-east-1）。
"""

from __future__ import annotations

import hashlib

from app.services.s3_client import (
    EMPTY_SHA256,
    canonical_query,
    canonical_uri,
    sign_headers,
    signing_key,
)

AK = "AKIAIOSFODNN7EXAMPLE"
SK = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
AMZ_DATE = "20130524T000000Z"
HOST = "examplebucket.s3.amazonaws.com"


def _auth(**kwargs) -> str:
    return sign_headers(
        access_key=AK,
        secret_key=SK,
        region="us-east-1",
        amz_date=AMZ_DATE,
        **kwargs,
    )["authorization"]


def test_get_object_vector_matches_aws_example():
    assert _auth(
        method="GET",
        host=HOST,
        path="/test.txt",
        headers={"range": "bytes=0-9"},
        payload_hash=EMPTY_SHA256,
    ) == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request,"
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date,"
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_put_object_vector_matches_aws_example():
    # 这个向量同时证明 x-amz-content-sha256 用的是真摘要（不是 UNSIGNED-PAYLOAD）
    payload_hash = "44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072"
    assert payload_hash == hashlib.sha256(b"Welcome to Amazon S3.").hexdigest()
    assert _auth(
        method="PUT",
        host=HOST,
        path="/test$file.text",
        headers={
            "date": "Fri, 24 May 2013 00:00:00 GMT",
            "x-amz-storage-class": "REDUCED_REDUNDANCY",
        },
        payload_hash=payload_hash,
    ) == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request,"
        "SignedHeaders=date;host;x-amz-content-sha256;x-amz-date;x-amz-storage-class,"
        "Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"
    )


def test_list_objects_vector_matches_aws_example():
    assert _auth(
        method="GET",
        host=HOST,
        path="/",
        query={"max-keys": "2", "prefix": "J"},
        payload_hash=EMPTY_SHA256,
    ) == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request,"
        "SignedHeaders=host;x-amz-content-sha256;x-amz-date,"
        "Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"
    )


def test_canonical_uri_encodes_special_characters_once():
    assert canonical_uri("/test$file.text") == "/test%24file.text"
    assert canonical_uri("//bucket/key with space") == "//bucket/key%20with%20space"


def test_canonical_query_sorts_and_encodes():
    assert canonical_query({"prefix": "a b", "max-keys": "2"}) == "max-keys=2&prefix=a%20b"


def test_signing_key_derivation_matches_aws_documented_vector():
    # AWS 《Deriving the signing key》示例：20120215 / us-east-1 / iam
    assert signing_key(
        "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "20120215", "us-east-1", "iam"
    ).hex() == "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d"
