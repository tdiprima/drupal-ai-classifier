import os

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

load_dotenv()

# Drupal site URL and endpoint
endpoint = f"{os.getenv('DRUPAL_BASE_URL')}/jsonapi/node/article"
username = os.getenv("DRUPAL_USERNAME")
password = os.getenv("DRUPAL_PASSWORD")

# Headers for JSON:API
headers = {
    "Accept": "application/vnd.api+json",
    "Content-Type": "application/vnd.api+json"
}

# Payload for creating a new article
payload = {
    "data": {
        "type": "node--article",
        "attributes": {
            "title": "New Article from Python",
            "body": {
                "value": "This content was created via Python.",
                "format": "plain_text"
            }
        }
    }
}

# Make the POST request
response = requests.post(
    endpoint,
    headers=headers,
    auth=HTTPBasicAuth(username, password),
    json=payload
)

# Check response
print("Status Code:", response.status_code)
print("Response:", response.json())
