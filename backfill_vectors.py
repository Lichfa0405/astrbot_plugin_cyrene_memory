import sqlite3
import urllib.request
import json
import struct
import time

db_path = "/root/astrbot-data/data/plugin_data/astrbot_plugin_cyrene_memory/cyrene_memory.db"
api_url = "http://127.0.0.1:11434/api/embed"
model = "qwen3-embedding:0.6b"

def get_vec(text):
    payload = json.dumps({"model": model, "input": [text]}).encode('utf-8')
    req = urllib.request.Request(api_url, data=payload, headers={'Content-Type': 'application/json'})
    resp = json.loads(urllib.request.urlopen(req, timeout=60).read().decode('utf-8'))
    return resp["embeddings"][0]

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("开始回填 L2...")
l2_rows = conn.execute("SELECT id, content FROM l2_events").fetchall()
for r in l2_rows:
    try:
        vec = get_vec(r["content"])
        blob = struct.pack(f'{len(vec)}f', *vec)
        conn.execute("INSERT OR REPLACE INTO embeddings (record_id, record_type, vector, updated_at) VALUES (?, 'l2', ?, ?)", (r["id"], blob, time.time()))
        print(f"L2 ID {r['id']} 向量化完成")
    except Exception as e:
        print(f"L2 ID {r['id']} 失败: {e}")

print("开始回填 L0...")
l0_rows = conn.execute("SELECT id, key, value FROM l0_profile").fetchall()
for r in l0_rows:
    try:
        text = f"{r['key']}: {r['value']}"
        vec = get_vec(text)
        blob = struct.pack(f'{len(vec)}f', *vec)
        conn.execute("INSERT OR REPLACE INTO embeddings (record_id, record_type, vector, updated_at) VALUES (?, 'l0', ?, ?)", (r["id"], blob, time.time()))
        print(f"L0 ID {r['id']} 向量化完成")
    except Exception as e:
        print(f"L0 ID {r['id']} 失败: {e}")

conn.commit()
conn.close()
print("全部回填完毕！")
