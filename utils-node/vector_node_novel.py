import os
import json
import time
import sqlite3
import threading
import requests
import numpy as np
from flask import Flask, request, jsonify, make_response
from sklearn.metrics.pairwise import cosine_similarity
import networkx as nx
from pyvis.network import Network
from urllib.parse import unquote, quote
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)

# ================= 配置区 =================
RESOURCE_NODE_URL = "http://one4.zin6.dpdns.org:8100"
OLLAMA_EMBEDDING_URL = "http://apple4.zin6.dpdns.org:11434/api/embeddings"
EMBEDDING_MODEL = "bge-m3:567m"
DB_PATH = r"C:\Users\xin-a\OneDrive\文档\sync sysnology\小工具\AI调用\vector_store_novel.db"
MAIN_SERVER_URL = "http://apple4.zin6.dpdns.org:8100" 

# ================= 数据库初始化 =================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS embeddings (
                filename TEXT PRIMARY KEY,
                vector BLOB NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
init_db()

# 内存缓存
vectors_db = {}
is_syncing = False

def load_vectors_to_memory():
    global vectors_db
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT filename, vector FROM embeddings")
            rows = cursor.fetchall()
            # 将存储的 bytes 转回 numpy 数组
            vectors_db = {row[0]: np.frombuffer(row[1], dtype=np.float32) for row in rows}
        print(f"[*] 数据库加载完毕，共 {len(vectors_db)} 条向量记录。")
    except Exception as e:
        print(f"[!] 数据库读取失败: {e}")

load_vectors_to_memory()

def save_vector_to_db(name, vector):
    """单条向量存入 SQLite"""
    vec_data = np.array(vector, dtype=np.float32).tobytes()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO embeddings (filename, vector) VALUES (?, ?)", (name, vec_data))

# ================= 核心工作单元 =================
def get_embedding(text):
    try:
        resp = requests.post(OLLAMA_EMBEDDING_URL, json={"model": EMBEDDING_MODEL, "prompt": text}, timeout=30)
        return resp.json().get("embedding", [])
    except: return []

def process_single_novel(name):
    text_feature = ""
    # 1. 获取简介
    try:
        safe_name = quote(name)
        detail_res = requests.get(f"{MAIN_SERVER_URL}/api/novel/skip/analysis/detail/{safe_name}", timeout=5)
        if detail_res.status_code == 200:
            data = detail_res.json()
            if not data.get("not_found"):
                text_feature = f"书名：{name}\n内容简介：{data.get('analysis_result', '')}"
    except: pass

    # 2. 降级拉取正文
    if not text_feature:
        try:
            content_res = requests.get(f"{RESOURCE_NODE_URL}/api/novel/content/{quote(name)}", timeout=10)
            if content_res.status_code == 200:
                text_feature = f"书名：{name}\n正文：{content_res.json().get('content', '')[:1000]}"
        except: return None

    # 3. 向量化
    if text_feature:
        vec = get_embedding(text_feature)
        if vec:
            save_vector_to_db(name, vec)
            return (name, np.array(vec, dtype=np.float32))
    return None

# ================= 同步任务 =================
def run_sync_task():
    global is_syncing, vectors_db
    if is_syncing: return
    is_syncing = True

    try:
        print("\n[开始] 请求资源节点全量列表...")
        novels = requests.get(f"{RESOURCE_NODE_URL}/api/novels/json", timeout=10).json()
        pending_novels = [name for name in novels if name not in vectors_db]
        
        if not pending_novels:
            print("[完毕] 数据库已是最新，无增量任务。")
            return

        print(f"[并行] 待处理: {len(pending_novels)} | 线程数: 8 | 模型: {EMBEDDING_MODEL}")

        with ThreadPoolExecutor(max_workers=8) as executor:
            future_to_novel = {executor.submit(process_single_novel, name): name for name in pending_novels}
            pbar = tqdm(as_completed(future_to_novel), total=len(pending_novels), desc="M4 加速处理中", unit="本")
            
            for future in pbar:
                result = future.result()
                if result:
                    name, vec_np = result
                    vectors_db[name] = vec_np  # 更新内存缓存以便实时查询
                    pbar.set_description(f"已存入: {name[:15]}")

        print("\n[完成] 增量向量数据已全部持久化至 SQLite。")
    except Exception as e:
        print(f"\n[异常] 同步中断: {e}")
    finally:
        is_syncing = False

# ================= API 接口 =================

@app.route('/api/vector/sync', methods=['POST'])
def trigger_sync():
    if is_syncing: return jsonify({"status": "running"})
    threading.Thread(target=run_sync_task, daemon=True).start()
    return jsonify({"status": "started"})

@app.route('/api/vector/similar', methods=['GET'])
def get_similar_novels():
    name = unquote(request.args.get('name', ''))
    top_k = int(request.args.get('k', 5))
    if name not in vectors_db:
        return jsonify({"error": "未收录", "filename": name}), 404
        
    target_vec = vectors_db[name].reshape(1, -1)
    all_names = list(vectors_db.keys())
    all_vecs = np.array([vectors_db[n] for n in all_names])
    
    sims = cosine_similarity(target_vec, all_vecs)[0]
    indices = np.argsort(sims)[::-1]
    
    recommendations = []
    for idx in indices:
        if all_names[idx] != name and len(recommendations) < top_k:
            recommendations.append({
                "filename": all_names[idx], 
                "similarity": round(float(sims[idx]), 4)
            })
    return jsonify({"target": name, "recommendations": recommendations})

@app.route('/api/vector/graph', methods=['GET'])
def render_graph():
    threshold = float(request.args.get('threshold', 0.6))
    if not vectors_db: return "无数据", 400
    all_names = list(vectors_db.keys())
    all_vecs = np.array(list(vectors_db.values()))
    sim_matrix = cosine_similarity(all_vecs)
    G = nx.Graph()
    for t in all_names: G.add_node(t, label=t.replace('.txt', ''))
    for i in range(len(all_names)):
        for j in range(i + 1, len(all_names)):
            sim = sim_matrix[i][j]
            if sim >= threshold:
                G.add_edge(all_names[i], all_names[j], value=float(sim), title=f"相似: {sim:.3f}")
    G.remove_nodes_from(list(nx.isolates(G)))
    net = Network(height="100vh", width="100%", bgcolor="#1a1a1a", font_color="#e0e0e0", select_menu=True)
    net.from_nx(G)
    return make_response(net.generate_html())
def load_vectors_to_memory():
    global vectors_db
    if not os.path.exists(DB_PATH):
        print("[!] 数据库文件尚不存在，将等待同步创建。")
        return

    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            # 检查表是否存在
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='embeddings'")
            if not cursor.fetchone():
                return

            cursor.execute("SELECT filename, vector FROM embeddings")
            rows = cursor.fetchall()
            
            # 核心：将 BLOB 还原为 NumPy 数组
            # BGE-M3 默认输出 1024 维的 float32 向量
            vectors_db = {row[0]: np.frombuffer(row[1], dtype=np.float32) for row in rows}
            
        print(f"[*] 缓存加载成功: 已载入 {len(vectors_db)} 本小说的向量数据。")
    except Exception as e:
        print(f"[!] 缓存加载异常: {e}")

# 在程序入口处调用
# load_vectors_to_memory()

if __name__ == '__main__':
    threading.Thread(target=run_sync_task, daemon=True).start()
    app.run(host='0.0.0.0', port=5001)