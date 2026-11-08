# backend/test_embed.py（可复用之前的，改成走 dashscope）
import os, dashscope
from dotenv import load_dotenv
load_dotenv()
dashscope.api_key = os.environ["API_KEY"]
dashscope.base_http_api_url = os.environ["DASHSCOPE_EMB_URL"]
r = dashscope.TextEmbedding.call(model=os.environ["EMBED_MODEL"], input=["你好"])
# print("status:", r.status_code, "code:", getattr(r, "code", None), "msg:", getattr(r, "message", None))
#
# r = dashscope.TextEmbedding.call(model=os.environ["EMBED_MODEL"], input=["你好"])
e0 = r.output["embeddings"][0]
print(type(e0), e0)