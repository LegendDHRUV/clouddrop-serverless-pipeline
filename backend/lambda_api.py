import json
import os
import re
import uuid
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

S3_CONFIG = Config(signature_version="s3v4", s3={"addressing_style": "virtual"})
s3 = boto3.client("s3", region_name=REGION, endpoint_url="https://s3.%s.amazonaws.com" % REGION, config=S3_CONFIG)
table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])

RAW_BUCKET = os.environ["RAW_BUCKET"]
PROCESSED_BUCKET = os.environ["PROCESSED_BUCKET"]
PASSCODE = os.environ["PASSCODE"]
MAX_BYTES = int(os.environ.get("MAX_BYTES", 5 * 1024 * 1024))
ALLOWED_EXT = (".pdf", ".png", ".jpg", ".jpeg", ".txt")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def respond(status, body):
    
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=lambda o: int(o) if isinstance(o, Decimal) else str(o)),
    }


def handler(event, context):
    method = event["requestContext"]["http"]["method"]
    path = event["rawPath"]
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    params = event.get("queryStringParameters") or {}

    if method == "OPTIONS":
        return respond(204, {})
    if headers.get("x-passcode") != PASSCODE:
        return respond(401, {"error": "Wrong passcode"})

    try:
        body = json.loads(event.get("body") or "{}") if method == "POST" else {}
        if method == "POST" and path == "/upload-url":
            return upload_url(body)
        if method == "POST" and path == "/delete":
            return delete_file(body.get("file_id", ""))
        if method == "GET" and path == "/files":
            return list_files()
        if method == "GET" and path == "/download":
            return download(params.get("file_id", ""), params.get("kind", "processed"))
        return respond(404, {"error": "Not found"})
    except Exception as e:
        print("ERROR", repr(e))
        return respond(500, {"error": "Server error"})


def upload_url(body):
    filename = os.path.basename(body.get("filename", "file"))
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:100]
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return respond(400, {"error": "Allowed types: " + ", ".join(ALLOWED_EXT)})
    if int(body.get("size", 0)) > MAX_BYTES:
        return respond(400, {"error": "File larger than %d MB" % (MAX_BYTES // 1024 // 1024)})

    email = (body.get("notify_email") or "").strip().lower()
    if email and (len(email) > 120 or not email.isascii() or not EMAIL_RE.match(email)):
        return respond(400, {"error": "Invalid email address"})

    content_type = body.get("content_type") or "application/octet-stream"
    file_id = str(uuid.uuid4())
    key = "%s/%s" % (file_id, filename)

    fields = {"Content-Type": content_type}
    conditions = [{"Content-Type": content_type}, ["content-length-range", 1, MAX_BYTES]]
    if email:
   
        fields["x-amz-meta-notify-email"] = email
        conditions.append({"x-amz-meta-notify-email": email})

    post = s3.generate_presigned_post(
        Bucket=RAW_BUCKET, Key=key, Fields=fields, Conditions=conditions, ExpiresIn=300
    )
    return respond(200, {"file_id": file_id, "post": post})


def list_files():
    items, resp = [], table.scan()
    items += resp["Items"]
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items += resp["Items"]
   
    items = [i for i in items if not str(i.get("file_id", "")).startswith("HASH#")]
    items.sort(key=lambda i: i.get("uploaded_at", ""), reverse=True)
    return respond(200, {"files": items})


def download(file_id, kind):
    item = table.get_item(Key={"file_id": file_id}).get("Item")
    if not item or item.get("status") != "PROCESSED":
        return respond(404, {"error": "File not found or not processed"})
    uri = item.get("thumbnail_uri") if kind == "thumb" else item.get("processed_uri")
    if not uri:
        return respond(404, {"error": "No such version"})
    bucket, key = uri.replace("s3://", "").split("/", 1)
    url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=300)
    return respond(200, {"url": url})


def delete_file(file_id):
    if not file_id or file_id.startswith("HASH#"):
        return respond(400, {"error": "Bad file id"})
    item = table.get_item(Key={"file_id": file_id}).get("Item")
    if not item:
        return respond(404, {"error": "File not found"})


    for field in ("raw_uri", "processed_uri", "thumbnail_uri"):
        uri = item.get(field)
        if uri:
            bucket, key = uri.replace("s3://", "").split("/", 1)
            s3.delete_object(Bucket=bucket, Key=key)

   
    digest = item.get("sha256")
    if digest:
        try:
            table.delete_item(
                Key={"file_id": "HASH#" + digest},
                ConditionExpression=Attr("owner_id").eq(file_id),
            )
        except ClientError:
            pass  
    table.delete_item(Key={"file_id": file_id})
    return respond(200, {"deleted": file_id})