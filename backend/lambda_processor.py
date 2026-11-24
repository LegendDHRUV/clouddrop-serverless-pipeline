import hashlib
import io
import json
import os
import smtplib
import ssl
import traceback
import urllib.parse
import uuid
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import boto3

try:
    from PIL import Image, ImageOps  # comes from the Pillow Lambda layer
except Exception as e:
    print("PIL IMPORT FAILED:", repr(e))
    Image = None

s3 = boto3.client("s3")
table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
sns = boto3.client("sns")
ssm = boto3.client("ssm")
_password = None

PROCESSED_BUCKET = os.environ["PROCESSED_BUCKET"]
TOPIC_ARN = os.environ.get("TOPIC_ARN", "")
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "").strip().lower()  # Gmail sender; always receives a copy
MAX_BYTES = int(os.environ.get("MAX_BYTES", 5 * 1024 * 1024))

ALLOWED = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".txt": "text/plain",
}
MAGIC = {
    ".pdf": [b"%PDF"],
    ".png": [b"\x89PNG"],
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
}
IMAGE_EXT = (".png", ".jpg", ".jpeg")


def header_ok(ext, head):
    """Check the first bytes of the file really match its extension."""
    if ext == ".txt":
        return b"\x00" not in head
    return any(head.startswith(m) for m in MAGIC[ext])


def notify(item):
    if not TOPIC_ARN:
        return
    try:
        lines = [
            "CloudDrop has finished handling an uploaded file.",
            "",
            "File name : %s" % item["original_name"],
            "Status    : %s" % item["status"],
            "Type      : %s" % item.get("mime_type", "unknown"),
            "Original  : %.1f KB" % (int(item["size_bytes"]) / 1024),
        ]
        if item["status"] == "PROCESSED":
            lines.append("Processed : %.1f KB" % (int(item.get("processed_size_bytes", 0)) / 1024))
            lines.append("Thumbnail : %s" % ("created" if item.get("thumbnail_uri") else "not created"))
            if item.get("email_status"):
                lines.append("Email     : %s" % item["email_status"])
        else:
            lines.append("Reason    : %s" % item.get("reason", "unknown"))
        lines.append("Uploaded  : %s" % item["uploaded_at"])
        lines.append("File ID   : %s" % item["file_id"])
        sns.publish(
            TopicArn=TOPIC_ARN,
            Subject=("CloudDrop: %s - %s" % (item["status"], item["original_name"]))[:100],
            Message="\n".join(lines),
        )
    except Exception:
        traceback.print_exc()


def get_password():
    """Read the Gmail app password from SSM Parameter Store once per container."""
    global _password
    if _password is None:
        value = ssm.get_parameter(Name=os.environ["SMTP_PARAM"], WithDecryption=True)["Parameter"]["Value"]
        _password = value.replace(" ", "")
    return _password


def send_copies(item, data, recipients):
    """Email the exact uploaded file as an attachment through Gmail SMTP."""
    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=20)
        server.login(OWNER_EMAIL, get_password())
    except Exception:
        traceback.print_exc()
        return "FAILED: could not log in to Gmail"

    results = []
    for to_addr in recipients:
        try:
            msg = MIMEMultipart()
            msg["Subject"] = "CloudDrop: copy of %s" % item["original_name"]
            msg["From"] = OWNER_EMAIL
            msg["To"] = to_addr
            body = (
                "Attached is the exact file that was uploaded to CloudDrop.\n\n"
                "File name : %s\nType      : %s\nSize      : %.1f KB\nUploaded  : %s\n"
                % (item["original_name"], item["mime_type"], int(item["size_bytes"]) / 1024, item["uploaded_at"])
            )
            msg.attach(MIMEText(body, "plain"))

            maintype, subtype = item["mime_type"].split("/", 1)
            part = MIMEBase(maintype, subtype)
            part.set_payload(data)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=item["original_name"])
            msg.attach(part)

            server.sendmail(OWNER_EMAIL, [to_addr], msg.as_bytes())
            results.append("sent to %s" % to_addr)
        except Exception:
            traceback.print_exc()
            results.append("FAILED for %s" % to_addr)
    try:
        server.quit()
    except Exception:
        pass
    return "; ".join(results)


def save(item):
    table.put_item(Item=item)
    notify(item)
    print("SAVED", json.dumps(item, default=str))


def reject(base, reason):
    save(dict(base, mime_type="unknown", status="REJECTED", reason=reason))


def handler(event, context):
    for record in event["Records"]:
        process(record)
    return {"ok": True}


def process(record):
    bucket = record["s3"]["bucket"]["name"]
    key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
    size = record["s3"]["object"].get("size", 0)
    uploaded_at = record["eventTime"]

    # Keys look like "<file_id>/<name>". Manual console uploads have no slash.
    if "/" in key:
        file_id, name = key.split("/", 1)
    else:
        file_id, name = str(uuid.uuid4()), key

    base = {
        "file_id": file_id,
        "original_name": name,
        "size_bytes": size,
        "raw_uri": "s3://%s/%s" % (bucket, key),
        "uploaded_at": uploaded_at,
    }
    ext = os.path.splitext(name)[1].lower()
    stem = os.path.splitext(name)[0]

    try:
        if ext not in ALLOWED:
            return reject(base, "File type not allowed")
        if size > MAX_BYTES:
            return reject(base, "File too large")

        obj = s3.get_object(Bucket=bucket, Key=key)
        data = obj["Body"].read()
        meta = obj.get("Metadata", {})  # holds "notify-email" if the user typed one on the page
        if not header_ok(ext, data[:16]):
            return reject(base, "File content does not match its extension")

        # ---- duplicate detection (SHA-256 of the file content) ----
        digest = hashlib.sha256(data).hexdigest()
        base["sha256"] = digest
        existing = table.get_item(Key={"file_id": "HASH#" + digest}).get("Item")
        if existing:
            return save(dict(
                base,
                mime_type=ALLOWED[ext],
                status="DUPLICATE",
                duplicate_of=existing["owner_id"],
                reason="Same file as %s (already uploaded)" % existing["original_name"],
            ))

        item = dict(base, mime_type=ALLOWED[ext], status="PROCESSED")

        if ext in IMAGE_EXT and Image is not None:
            img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")

            main = img.copy()
            main.thumbnail((1600, 1600))
            buf = io.BytesIO()
            main.save(buf, "JPEG", quality=80, optimize=True)
            out_key = "%s/%s.jpg" % (file_id, stem)
            s3.put_object(Bucket=PROCESSED_BUCKET, Key=out_key, Body=buf.getvalue(), ContentType="image/jpeg")

            thumb = img.copy()
            thumb.thumbnail((256, 256))
            tbuf = io.BytesIO()
            thumb.save(tbuf, "JPEG", quality=75)
            thumb_key = "%s/thumb_%s.jpg" % (file_id, stem)
            s3.put_object(Bucket=PROCESSED_BUCKET, Key=thumb_key, Body=tbuf.getvalue(), ContentType="image/jpeg")

            item["processed_uri"] = "s3://%s/%s" % (PROCESSED_BUCKET, out_key)
            item["thumbnail_uri"] = "s3://%s/%s" % (PROCESSED_BUCKET, thumb_key)
            item["processed_size_bytes"] = len(buf.getvalue())
        else:
            out_key = "%s/%s" % (file_id, name)
            s3.put_object(Bucket=PROCESSED_BUCKET, Key=out_key, Body=data, ContentType=ALLOWED[ext])
            item["processed_uri"] = "s3://%s/%s" % (PROCESSED_BUCKET, out_key)
            item["processed_size_bytes"] = len(data)

        # ---- email the exact uploaded file (owner + optional address from the page) ----
        if OWNER_EMAIL:
            recipients = [OWNER_EMAIL]
            extra = (meta.get("notify-email") or "").strip().lower()
            if extra and extra not in recipients:
                recipients.append(extra)
            item["email_status"] = send_copies(item, data, recipients)

        # ---- remember this file's hash so the next identical upload is flagged ----
        try:
            table.put_item(
                Item={
                    "file_id": "HASH#" + digest,
                    "owner_id": file_id,
                    "original_name": name,
                    "uploaded_at": uploaded_at,
                    "status": "HASH",
                },
                ConditionExpression="attribute_not_exists(file_id)",
            )
        except Exception:
            traceback.print_exc()

        save(item)
    except Exception:
        traceback.print_exc()
        reject(base, "Processing error")
