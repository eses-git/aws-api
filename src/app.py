import json
import os
import uuid
from datetime import datetime
import boto3
from botocore.config import Config

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

    return {
        "statusCode": 200,
        "headers": cors_headers,
        "body": json.dumps({
            "message": "API connected successfully",
            "documents": []
        })
    }