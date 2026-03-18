import os

import requests
from dotenv import load_dotenv

load_dotenv()
url = f"{os.environ['DRUPAL_BASE_URL']}/jsonapi/node/new_product"

payload = {
  "data": {
    "type": "node--new_product",
    "attributes": {
      "title": "Test Product"
    }
  }
}

r = requests.post(
    url,
    auth=(os.environ["DRUPAL_USERNAME"], os.environ["DRUPAL_PASSWORD"]),
    headers={"Content-Type": "application/vnd.api+json"},
    json=payload
)

print(r.status_code)
print(r.text)
