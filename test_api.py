import os
import sys
from dotenv import load_dotenv
load_dotenv()
from openai import OpenAI

print("Starting API test...", flush=True)
api_key = os.getenv('OPENAI_API_KEY')
base_url = os.getenv('OPENAI_API_BASE')
print(f"API Key: {api_key[:8]}...", flush=True)
print(f"Base URL: {base_url}", flush=True)

try:
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=120)
    print("Calling API...", flush=True)
    resp = client.chat.completions.create(
        model='qwen3-coder-plus',
        messages=[{'role':'user','content':'hi'}],
        max_tokens=20
    )
    print(f"Response received", flush=True)
    c = resp.choices[0]
    print(f'Finish reason: {c.finish_reason}', flush=True)
    content = c.message.content
    if content:
        print(f'Content: [{content}]', flush=True)
    else:
        print('Content: [EMPTY]', flush=True)
    print(f'Model: {resp.model}', flush=True)
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}", flush=True)
