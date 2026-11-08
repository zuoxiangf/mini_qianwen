# check_env.py
import os
from openai import OpenAI

KEY = "sk-ws-H.YLHHMIP.sbvu.MEUCIQDdP3x1ddPEgYL7wdUBceD79Gu8wrQHWghXqzU0g2ObhQIgVSknmOa9BZUadX6bQ4yliCfQAKPR_-xbGdkTxTCpj4I"
BASE = os.environ.get("BASE_URL", "https://maas.qianwenaiapi.com/compatible-mode/v1")



client = OpenAI(api_key=KEY, base_url=BASE, timeout=30)
r = client.chat.completions.create(
    model="qwen3.6-flash",
    messages=[{"role":"user","content":"只回两个字：收到"}],
    temperature=0,
)

print("model:", r.model)
print("reply:", r.choices[0].message.content)
