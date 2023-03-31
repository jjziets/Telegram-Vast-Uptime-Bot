import requests
import os
import time

def telegram_request(path):
    while True:
        response = requests.get("https://api.telegram.org/bot" + os.getenv('TELEGRAM_TOKEN') + path)
        response_json = response.json()

        # Check if the response contains a rate limit error (error code 429)
        if response.status_code == 429:
            retry_after = response_json.get('parameters', {}).get('retry_after', 0)
            print(f"Rate limit hit. Retrying after {retry_after + 1}s...")
            time.sleep(retry_after + 1)
        else:
            return response_json
