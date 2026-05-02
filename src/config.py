import os
from openai import OpenAI
import boto3
# Initialize the client here
openai_client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))

# DynamoDB setup
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
customers_table     = dynamodb.Table('Customers')
conversations_table = dynamodb.Table('Conversations')