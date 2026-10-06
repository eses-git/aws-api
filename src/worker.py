import io
import json
import os
import urllib.parse
import boto3
from pypdf import PdfReader

DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE_NAME", "ai-document-summaries")
AWS_REGION = os.environ.get("AWS_REGION", "eu-north-1")

# Bedrock output budget. Raised from the previous 300 tokens so Claude can
# return a complete summary (two full paragraphs) without being cut off.
MAX_SUMMARY_TOKENS = 1500

# Characters of source text handed to Bedrock. Raised from 4,000 so larger
# documents are summarized from their full body instead of a short excerpt.
MAX_INPUT_CHARS = 12000

s3_client = boto3.client("s3", region_name=AWS_REGION)
bedrock_runtime = boto3.client("bedrock-runtime", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
table = dynamodb.Table(DYNAMODB_TABLE)


def extract_text_from_file(file_bytes, file_key):
    """Extracts text depending on file extension (.pdf, .txt, .md)."""
    file_key_lower = file_key.lower()

    if file_key_lower.endswith(".pdf"):
        pdf_file = io.BytesIO(file_bytes)
        reader = PdfReader(pdf_file)
        extracted_pages = [page.extract_text() for page in reader.pages if page.extract_text()]
        return "\n".join(extracted_pages)

    elif file_key_lower.endswith((".txt", ".md")):
        return file_bytes.decode("utf-8", errors="ignore")

    return file_bytes.decode("utf-8", errors="ignore")


def generate_ai_summary(text_content, file_name):
    """Generates a concise summary via Bedrock Claude 3 Haiku."""
    if not text_content or not text_content.strip():
        return f"Document {file_name} was processed, but no readable text could be extracted."

    prompt = (
        f"Summarize the following document content in 2 concise paragraphs. "
        f"Highlight key background, skills, or findings:\n\n"
        f"{text_content[:MAX_INPUT_CHARS]}"
    )

    # max_tokens is no longer clamped at 300, so the full summary is returned
    # and stored instead of being truncated mid-sentence.
    payload = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": MAX_SUMMARY_TOKENS,
        "messages": [{"role": "user", "content": prompt}]
    }

    try:
        response = bedrock_runtime.invoke_model(
            modelId="anthropic.claude-3-haiku-20240307-v1:0",
            contentType="application/json",
            accept="application/json",
            body=json.dumps(payload)
        )
        response_body = json.loads(response["body"].read())
        return response_body["content"][0]["text"]
    except Exception as e:
        print(f"Bedrock invocation failed for {file_name}: {str(e)}")
        # Fallback summary if Bedrock model access is not yet granted in AWS Console
        text_preview = text_content.strip()[:250].replace("\n", " ")
        return f"Summary generated (fallback preview): {text_preview}..."


def lambda_handler(event, context):
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        file_name = os.path.basename(key)

        # Locate DynamoDB record by s3Key
        scan_res = table.scan()
        matching_items = [i for i in scan_res.get("Items", []) if i.get("s3Key") == key]
        doc_id = matching_items[0]["id"] if matching_items else None

        try:
            # 1. Download file bytes from S3
            s3_obj = s3_client.get_object(Bucket=bucket, Key=key)
            file_bytes = s3_obj["Body"].read()

            # 2. Extract text (PDF vs TXT vs MD)
            extracted_text = extract_text_from_file(file_bytes, key)

            # 3. Generate Summary
            ai_summary = generate_ai_summary(extracted_text, file_name)

            # 4. Update DynamoDB to PROCESSED
            if doc_id:
                table.update_item(
                    Key={"id": doc_id},
                    UpdateExpression="SET #st = :status, summary = :summary",
                    ExpressionAttributeNames={"#st": "status"},
                    ExpressionAttributeValues={
                        ":status": "PROCESSED",
                        ":summary": ai_summary
                    }
                )
                print(f"Successfully processed document {doc_id}")

        except Exception as e:
            error_msg = f"Failed to process {file_name}: {str(e)}"
            print(error_msg)
            if doc_id:
                table.update_item(
                    Key={"id": doc_id},
                    UpdateExpression="SET #st = :status, summary = :summary",
                    ExpressionAttributeNames={"#st": "status"},
                    ExpressionAttributeValues={
                        ":status": "FAILED",
                        ":summary": error_msg
                    }
                )

    return {"statusCode": 200, "body": "Processing complete"}