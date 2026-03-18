import os
import json
import time
import sqlite3
import threading
import requests
import re
import numpy as np
from flask import Flask, request, jsonify
from sklearn.metrics.pairwise import cosine_similarity
from urllib.parse import unquote, quote
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)

# ================= 配置区 =================
RESOURCE_NODE_URL = "http://one4.zin6.dpdns.org:8100"
OLLAMA_EMBEDDING_URL = "http://apple4.zin6.dpdns.org:11434/api/embeddings"
EMBEDDING_MODEL = "bge-m3:567m"
DB_PATH = r'C:\Users\xin-a\OneDrive\文档\sync sysnology\小工具\AI调用\universal_data.db'
VEC_TABLE = "video_embeddings"

# ================= 全局缓存变量 =================
video_vectors = {}      # 原始向量字典 {filename: np.array}
vector_matrix = None    # 预计算矩阵 (NumPy Array)
vector_keys = []        # 矩阵对应的文件名列表
idx_map = {}            # 文件名到矩阵索引的映射 {filename: int}
is_syncing = False

# ================= 核心工具 =================

def refresh_matrix_cache():
    """重建矩阵缓存，确保计算逻辑与内存数据同步，解决 KeyError"""
    global video_vectors, vector_matrix, vector_keys, idx_map
    if not video_vectors:
        print("[!] 缓存刷新跳过：video_vectors 为空")
        return
    
    vector_keys = list(video_vectors.keys())
    vector_matrix = np.array([video_vectors[k] for k in vector_keys])
    idx_map = {name: i for i, name in enumerate(vector_keys)}
    print(f"[*] 矩阵缓存已刷新: {vector_matrix.shape}, 索引条数: {len(idx_map)}")

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f'''
            CREATE TABLE IF NOT EXISTS {VEC_TABLE} (
                filename TEXT PRIMARY KEY,
                vector BLOB NOT NULL,
                feature_text TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

def load_vectors_to_memory():
    global video_vectors
    try:
        if not os.path.exists(DB_PATH): return
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{VEC_TABLE}'")
            if not cursor.fetchone(): return

            cursor.execute(f"SELECT filename, vector FROM {VEC_TABLE}")
            rows = cursor.fetchall()
            video_vectors = {row[0]: np.frombuffer(row[1], dtype=np.float32) for row in rows}
        print(f"[*] 缓存就绪：已从 SQLite 加载 {len(video_vectors)} 条视频向量")
        refresh_matrix_cache()
    except Exception as e:
        print(f"[!] 缓存加载失败: {e}")

def normalize_name(name):
    """移除所有后缀、前缀和特殊符号，只保留核心文字进行匹配，解决 404"""
    if not name: return ""
    name = unquote(name)
    name = re.sub(r'^\[NEW\]_', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\.(mp4|mkv|avi|wmv|mov|webm)$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'[^\w\u4e00-\u9fa5]', '', name)
    return name.lower()

def get_embedding(text):
    try:
        resp = requests.post(OLLAMA_EMBEDDING_URL, json={"model": EMBEDDING_MODEL, "prompt": text}, timeout=30)
        return resp.json().get("embedding", [])
    except Exception as e:
        return []

def save_vector_to_db(name, vector, feature_text):
    vec_data = np.array(vector, dtype=np.float32).tobytes()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"INSERT OR REPLACE INTO {VEC_TABLE} (filename, vector, feature_text, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)", 
                    (name, vec_data, feature_text))

# ================= 并行工作单元 =================

def process_single_video(name, ai_info=None):
    clean_name = name.replace('.mp4', '').replace('.mkv', '').replace('[NEW]_', '')
    if ai_info and ai_info.get('category') != '未分类':
        category = ai_info.get('category')
        tags = "，".join(ai_info.get('tags', []))
        text_feature = f"视频标题: {clean_name}; 分类: {category}; 标签: {tags}"
    else:
        text_feature = f"视频标题: {clean_name}"

    vec = get_embedding(text_feature)
    if vec:
        save_vector_to_db(name, vec, text_feature)
        return (name, np.array(vec, dtype=np.float32))
    return None

def run_sync_task():
    global is_syncing, video_vectors
    if is_syncing: return
    is_syncing = True

    try:
        print("\n[开始] 视频向量增量同步...")
        resp = requests.get(f"{RESOURCE_NODE_URL}/api/videos/json", timeout=10)
        all_videos = resp.json() if resp.status_code == 200 else []
        
        ai_data_map = {}
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='video_store'")
            if cursor.fetchone():
                cursor.execute("SELECT filename, category, tags FROM video_store")
                for row in cursor.fetchall():
                    ai_data_map[row['filename']] = {
                        "category": row['category'], 
                        "tags": json.loads(row['tags']) if row['tags'] else []
                    }

        pending = [n for n in all_videos if n not in video_vectors]
        if not pending:
            print("[完毕] 向量库已是最新状态。")
            return

        with ThreadPoolExecutor(max_workers=8) as executor:
            future_to_video = {executor.submit(process_single_video, n, ai_data_map.get(n)): n for n in pending}
            pbar = tqdm(as_completed(future_to_video), total=len(pending), desc="同步进度")
            
            for future in pbar:
                res = future.result()
                if res:
                    name, vec_np = res
                    video_vectors[name] = vec_np

        print("\n[完成] 同步结束，正在刷新矩阵缓存...")
        refresh_matrix_cache()
    except Exception as e:
        print(f"\n[异常] 同步失败: {e}")
    finally:
        is_syncing = False

# ================= API 路由 =================

@app.route('/api/video/vector/sync', methods=['POST'])
def trigger_sync():
    if is_syncing: return jsonify({"status": "running"})
    threading.Thread(target=run_sync_task, daemon=True).start()
    return jsonify({"status": "started"})

@app.route('/api/video/similar', methods=['GET'])
def get_similar_videos():
    global vector_matrix, vector_keys, idx_map
    
    raw_name = request.args.get('name', '')
    top_k = int(request.args.get('k', 50))      # 期望获取的总数
    min_count = int(request.args.get('min', 5))  # 🌟 新增：最少保底返回个数
    threshold = float(request.args.get('threshold', 0.7)) # 🌟 默认高阈值
    
    query_name = unquote(raw_name).strip()
    
    # 1. 匹配 target_key 逻辑 (保持你的强力匹配逻辑不变)
    target_key = None
    if query_name in video_vectors:
        target_key = query_name
    else:
        query_norm = normalize_name(query_name)
        for k in vector_keys:
            if query_norm == normalize_name(k) or query_norm in normalize_name(k):
                target_key = k
                break

    if not target_key:
        return jsonify({"error": "Video not found", "received": query_name}), 404

    # 2. 检查索引完整性
    if target_key not in idx_map:
        refresh_matrix_cache()
        if target_key not in idx_map:
            return jsonify({"error": "Index mismatch"}), 500

    # 3. 极速矩阵运算
    try:
        target_idx = idx_map[target_key]
        target_vec = vector_matrix[target_idx].reshape(1, -1)
        sims = cosine_similarity(target_vec, vector_matrix)[0]
        
        # 获取降序索引（排除掉自己，所以取 top_k + 1）
        actual_k = min(top_k + 1, len(sims))
        top_indices = np.argpartition(sims, -actual_k)[-actual_k:]
        top_indices = top_indices[np.argsort(sims[top_indices])][::-1]

        recommendations = []
        
        for idx in top_indices:
            curr_name = vector_keys[idx]
            if curr_name == target_key: continue
            
            sim_val = round(float(sims[idx]), 4)
            
            # 🌟 核心逻辑判断：
            # 如果已满足 high_threshold，直接加入
            # 如果不满足阈值，但当前结果数还没达到 min_count，也强制加入 (保底)
            if sim_val >= threshold or len(recommendations) < min_count:
                if sim_val < 0.01: continue # 极端情况：完全不相关则不返回
                
                recommendations.append({
                    "filename": curr_name,
                    "displayTitle": curr_name.replace('.mp4','').replace('.mkv','').replace('[NEW]_',''),
                    "url": f"{RESOURCE_NODE_URL}/videos/{quote(curr_name)}",
                    "similarity": sim_val
                })
            
            # 达到 top_k 上限则停止
            if len(recommendations) >= top_k: 
                break

        return jsonify({
            "target": target_key,
            "recommendations": recommendations,
            "count": len(recommendations),
            "info": f"Threshold used: {threshold}, Min count: {min_count}"
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Calculation error: {str(e)}"}), 500
if __name__ == '__main__':
    init_db()
    load_vectors_to_memory()
    # 启动自动检查
    threading.Thread(target=run_sync_task, daemon=True).start()
    # 使用 5003 端口
    app.run(host='0.0.0.0', port=5003)