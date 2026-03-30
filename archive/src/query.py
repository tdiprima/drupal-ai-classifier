import json
import os

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

load_dotenv()

def main():
    url = f"{os.getenv('DRUPAL_BASE_URL')}/jsonapi/node/{os.getenv('DRUPAL_CONTENT_TYPE')}"
    username = os.getenv("DRUPAL_USERNAME")
    password = os.getenv("DRUPAL_PASSWORD")

    # Headers for JSON:API
    headers = {
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json"
    }

    response = requests.get(
        url,
        headers=headers,
        auth=HTTPBasicAuth(username, password),
    )

    data = response.json()
    records = data.get("data", [])
    if records:
        print(json.dumps(records[0], indent=2))
        # If you only want the attributes (dropping links and relationships)
        # print(json.dumps(records[0]["attributes"], indent=2))


if __name__ == "__main__":
    main()
