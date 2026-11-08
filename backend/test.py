# 临时执行一次，删掉旧的 kb 集合
import os
from pathlib import Path
from dotenv import load_dotenv
from qdrant_client import QdrantClient

# ★ 锚定到本文件所在目录，无论从哪里运行都能找到 backend/.env
load_dotenv(Path(__file__).resolve().parent / ".env")

# Deleted:load_dotenv(Path("backend/.env"))
c = QdrantClient(path=os.environ["QDRANT_PATH"])
c.delete_collection("kb")
print("kb 集合已删除，下次上传会按 1024 维重建")