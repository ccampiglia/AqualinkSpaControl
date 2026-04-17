import httpx
import os
import sys
import json
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000")

GET_COMMANDS = {"devices", "onetouch"}

def run_test(action):
    print(f"--- Sending {action} command ---")
    try:
        if action in GET_COMMANDS:
            r = httpx.get(f"{BASE_URL}/{action}")
        else:
            r = httpx.post(f"{BASE_URL}/{action}")
        print(f"Status Code: {r.status_code}")
        data = r.json()
        if isinstance(data, dict) and "status" in data:
            print(f"Response: {data.get('status')} - {data.get('reason', 'Action executed')}")
        else:
            print(json.dumps(data, indent=2))
    except Exception as e:
        print(f"Failed to connect to local server: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_tool.py [spa-on | spa-off | all-off | spa-mode | devices | onetouch]")
        sys.exit(1)

    cmd = sys.argv[1]
    run_test(cmd)
