import json
import asyncio
import urllib.request
import struct
from astrbot.api import logger

# 全局信号量，强制 Ollama 请求单线程排队，保护 i3 CPU
_ollama_lock = asyncio.Semaphore(1)

class EmbeddingEngine:
    def __init__(self, config):
        cfg = config.get("embedding", {})
        self.enabled = cfg.get("enabled", True)
        self.api_url = cfg.get("api_url", "http://127.0.0.1:11434/api/embed")
        self.model_name = cfg.get("model_name", "qwen3-embedding:0.6b")
        self.timeout = cfg.get("timeout", 120)

    async def get_embedding(self, text: str) -> list[float]:
        if not self.enabled or not text.strip():
            return None
        
        payload = json.dumps({"model": self.model_name, "input": [text]}).encode('utf-8')
        req = urllib.request.Request(self.api_url, data=payload, headers={'Content-Type': 'application/json'})
        
        async with _ollama_lock:
            try:
                # 放到线程池中执行同步的 urllib 请求，避免阻塞主事件循环
                loop = asyncio.get_running_loop()
                response_data = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=self.timeout).read()),
                    timeout=self.timeout + 5
                )
                result = json.loads(response_data.decode('utf-8'))
                if "embeddings" in result and len(result["embeddings"]) > 0:
                    return result["embeddings"][0]
            except Exception as e:
                logger.warning(f"[Cyrene Memory] 向量化失败: {e}")
        return None

def vector_to_blob(vec: list[float]) -> bytes:
    if not vec: return b''
    return struct.pack(f'{len(vec)}f', *vec)

def blob_to_vector(blob: bytes) -> list[float]:
    if not blob: return []
    num_floats = len(blob) // 4
    return list(struct.unpack(f'{num_floats}f', blob))

def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = sum(a * a for a in v1) ** 0.5
    norm2 = sum(b * b for b in v2) ** 0.5
    if norm1 == 0 or norm2 == 0: return 0.0
    return dot / (norm1 * norm2)
