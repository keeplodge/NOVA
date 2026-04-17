import os
import requests
import certifi
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("MAILERLITE_API_KEY")
if not API_KEY:
    raise ValueError("MAILERLITE_API_KEY not found in .env")

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

def fetch_groups():
    url = "https://connect.mailerlite.com/api/groups"
    params = {"limit": 25, "page": 1}
    groups = []

    while True:
        response = requests.get(url, headers=headers, params=params, verify=False)
        response.raise_for_status()
        data = response.json()

        groups.extend(data["data"])

        meta = data.get("meta", {})
        if meta.get("current_page", 1) >= meta.get("last_page", 1):
            break
        params["page"] += 1

    return groups

groups = fetch_groups()
print(f"Found {len(groups)} group(s):\n")
for group in groups:
    print(f"  {group['name']:<40} ID: {group['id']}")
