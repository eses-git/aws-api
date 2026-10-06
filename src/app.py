import base64
import json
import os
import urllib.parse
import uuid
from datetime import datetime
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

UPLOAD_BUCKET = os.environ.get(
    "UPLOAD_BUCKET_NAME",
    "estera-ai-doc-summarizer-storage"
)

DYNAMODB_TABLE = os.environ.get(
    "DYNAMODB_TABLE_NAME",
    "ai-document-summaries"
)

AWS_REGION = os.environ.get(
    "AWS_REGION",
    "eu-north-1"
)

s3_client = boto3.client(
    "s3",
    region_name=AWS_REGION,
    config=Config(
        signature_version="s3v4",
        s3={
            "addressing_style": "virtual"
        }
    )
)

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
table = dynamodb.Table(DYNAMODB_TABLE)

MIME_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
}

# Collection segment used to recognise document routes (e.g. "/documents/<id>").
DOCUMENTS_SEGMENT = "/documents"

# Accepted request keys for the target document id.
ID_KEYS = ("id", "documentId", "docId")


def _parse_json_body(event):
    """Safely decodes a JSON request body, always returning a dict."""
    raw = event.get("body") or "{}"

    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except Exception:
            return {}

    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}

    return parsed if isinstance(parsed, dict) else {}


def _extract_document_id(event, path):
    """
    Resolves the target document id from the request. Every shape the console
    (or a manual curl) may use is supported:

      1. `DELETE /documents/{id}`           -> explicit path parameter
      2. `DELETE /documents/{id}` (proxy)   -> trailing path segment
      3. `DELETE /documents?id={id}`        -> query string parameter
      4. `DELETE /documents` body {"id":..} -> JSON request body
    """
    # 1. Explicit path parameter (when an {id} route is configured).
    path_params = event.get("pathParameters") or {}
    for key in ID_KEYS:
        value = path_params.get(key)
        if value:
            return urllib.parse.unquote(str(value)).strip()

    # 2. Trailing segment of /documents/<id> (proxy routes land here).
    normalized = path.rstrip("/")
    marker = f"{DOCUMENTS_SEGMENT}/"
    if marker in normalized:
        candidate = normalized.split(marker, 1)[1].split("/")[0].strip()
        if candidate:
            return urllib.parse.unquote(candidate)

    # 3. Query string parameter.
    query = event.get("queryStringParameters") or {}
    for key in ID_KEYS:
        value = query.get(key)
        if value:
            return str(value).strip()

    # 4. JSON request body.
    if event.get("body"):
        body = _parse_json_body(event)
        for key in ID_KEYS:
            value = body.get(key)
            if value:
                return str(value).strip()

    return None


def lambda_handler(event, context):

    cors_headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": (
            "Content-Type,"
            "X-Amz-Date,"
            "Authorization,"
            "X-Api-Key,"
            "X-Amz-Security-Token"
        ),
        "Access-Control-Allow-Methods": (
            "GET,POST,OPTIONS,PUT,DELETE"
        )
    }

    if event.get("httpMethod") == "OPTIONS":
        return {
            "statusCode": 200,
            "headers": cors_headers,
            "body": ""
        }

    path = event.get("path", "")
    method = event.get("httpMethod", "GET")

    if (path.endswith("/documents") or path == "/") and method == "GET":
        try:
            response = table.scan()
            items = response.get("Items", [])
            return {
                "statusCode": 200,
                "headers": cors_headers,
                "body": json.dumps({
                    "message": "Success",
                    "documents": items
                })
            }
        except Exception as e:
            return {
                "statusCode": 500,
                "headers": cors_headers,
                "body": json.dumps({
                    "error": str(e)
                })
            }

    if path.endswith("/upload-url") and method == "POST":

        try:
            body = json.loads(event.get("body") or "{}")

            file_name = body.get(
                "fileName",
                "document.pdf"
            )

            _, ext = os.path.splitext(
                file_name.lower()
            )

            content_type = (
                body.get("fileType")
                or MIME_TYPES.get(
                    ext,
                    "application/octet-stream"
                )
            )

            key = f"uploads/{file_name}"
            doc_id = str(uuid.uuid4())

            presigned_url = s3_client.generate_presigned_url(
                ClientMethod="put_object",
                Params={
                    "Bucket": UPLOAD_BUCKET,
                    "Key": key,
                    "ContentType": content_type
                },
                ExpiresIn=300
            )

            # Ensure regional domain format
            global_domain = f"{UPLOAD_BUCKET}.s3.amazonaws.com"
            regional_domain = f"{UPLOAD_BUCKET}.s3.{AWS_REGION}.amazonaws.com"
            if global_domain in presigned_url:
                presigned_url = presigned_url.replace(global_domain, regional_domain)

            # Create pending entry in DynamoDB
            now = datetime.utcnow().isoformat()
            item = {
                "id": doc_id,
                "fileName": file_name,
                "s3Key": key,
                "status": "PROCESSING",
                "summary": "Document queued. Extracting text and generating AI summary...",
                "createdAt": now,
                "fileType": content_type
            }
            table.put_item(Item=item)

            return {
                "statusCode": 200,
                "headers": cors_headers,
                "body": json.dumps({
                    "uploadUrl": presigned_url,
                    "key": key,
                    "contentType": content_type,
                    "fileType": content_type,
                    "document": item
                })
            }

        except Exception as e:

            return {
                "statusCode": 500,
                "headers": cors_headers,
                "body": json.dumps({
                    "error": str(e)
                })
            }

    # DELETE /documents/{id}  or  DELETE /documents  with body {"id": "<DOC_ID>"}
    if method == "DELETE" and DOCUMENTS_SEGMENT in path:
        try:
            doc_id = _extract_document_id(event, path)

            if not doc_id:
                return {
                    "statusCode": 400,
                    "headers": cors_headers,
                    "body": json.dumps({
                        "error": (
                            "Missing document id. Use DELETE /documents/{id} "
                            'or DELETE /documents with body {"id": "<DOC_ID>"}.'
                        )
                    })
                }

            # 1. Retrieve the record so we know which S3 object to remove.
            existing = table.get_item(Key={"id": doc_id}).get("Item")
            s3_key = (existing or {}).get("s3Key")

            # 2. Remove the object from S3. delete_object is idempotent, and a
            #    failure here must not block removal of the DynamoDB record.
            s3_deleted = False
            if s3_key:
                try:
                    s3_client.delete_object(Bucket=UPLOAD_BUCKET, Key=s3_key)
                    s3_deleted = True
                except ClientError as s3_error:
                    print(f"S3 delete failed for key {s3_key}: {s3_error}")

            # 3. Remove the document record from DynamoDB (idempotent).
            table.delete_item(Key={"id": doc_id})

            return {
                "statusCode": 200,
                "headers": cors_headers,
                "body": json.dumps({
                    "message": (
                        f"Document {doc_id} deleted."
                        if existing
                        else f"Document {doc_id} was already deleted."
                    ),
                    "id": doc_id,
                    "found": bool(existing),
                    "s3Key": s3_key,
                    "s3Deleted": s3_deleted
                })
            }

        except Exception as e:

            return {
                "statusCode": 500,
                "headers": cors_headers,
                "body": json.dumps({
                    "error": str(e)
                })
            }

    return {
        "statusCode": 200,
        "headers": cors_headers,
        "body": json.dumps({
            "message": "API connected successfully",
            "documents": []
        })
    }