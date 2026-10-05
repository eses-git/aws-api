import json

def lambda_handler(event, context):
    """
    AWS Lambda handler function.
    """
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*"
        },
        "body": json.dumps({
            "message": "Hello from AWS SAM!",
            "status": "success"
        })
    }