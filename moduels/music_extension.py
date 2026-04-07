import sqlite3, json, csv, io, os, re, requests, threading, queue, time
from flask import Blueprint, request, jsonify, make_response

tags_bp = Blueprint('tags', __name__)
DB_PATH = 'db/universal_data.db'

GLOBAL_MODEL_MUSIC_API = os.environ.get('MODEL_MUSIC_API', '').strip()
GLOBAL_MODEL_MUSIC = os.environ.get('MODEL_MUSIC', '').strip()
GLOBAL_MODEL_MUSIC_URL = os.environ.get('MODEL_MUSIC_URL', '').strip()

TAG_CHOICES = ["流行","摇滚","民谣","电子","说唱","R&B","古典","爵士","古风","国风","乡村","蓝调","金属","朋克","雷鬼","放克","灵魂乐","快乐","伤感","浪漫","孤独","甜蜜","安静","放松","激昂","治愈","忧郁","温暖","紧张","神秘","快节奏","中速","慢节奏","舒缓","节奏感强","男声","女声","合唱","独唱","戏腔","纯音乐","钢琴","吉他","弦乐","电子合成器","鼓点重","人声为主"]
BATCH_QUEUE, MAX_BATCH_SIZE, BATCH_TIMEOUT = queue.Queue(), 10, 3.0

def _db_exec(query, params=(), fetchall=False):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        return cur.fetchall() if fetchall else None

def _init_db():
    _db_exec('CREATE TABLE IF NOT EXISTS song_tags (song_name TEXT PRIMARY KEY, tags TEXT, model_name TEXT, time_taken REAL)')
    try: _db_exec('ALTER TABLE song_tags ADD COLUMN model_name TEXT'); _db_exec('ALTER TABLE song_tags ADD COLUMN time_taken REAL')
    except: pass
    _db_exec('CREATE TABLE IF NOT EXISTS model_telemetry (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, model_name TEXT, batch_size INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER, pure_ai_time REAL, total_time REAL, status TEXT)')
    _db_exec('CREATE TABLE IF NOT EXISTS favorites (song_name TEXT PRIMARY KEY)')
    _db_exec('CREATE TABLE IF NOT EXISTS deleted_songs (song_name TEXT PRIMARY KEY)')
_init_db()

def log_telemetry(mod, b_size, tele, t_time, status):
    _db_exec('INSERT INTO model_telemetry (model_name, batch_size, prompt_tokens, completion_tokens, total_tokens, pure_ai_time, total_time, status) VALUES (?,?,?,?,?,?,?,?)',
             (mod, b_size, tele.get('prompt_tokens',0), tele.get('completion_tokens',0), tele.get('total_tokens',0), tele.get('pure_ai_time',0.0), round(t_time, 3), status))

@tags_bp.route('/api/tags', methods=['GET', 'POST', 'DELETE'])
def handle_tags():
    if request.method == 'GET':
        rows, tags_dict, all_cats = _db_exec('SELECT song_name, tags, model_name, time_taken FROM song_tags', fetchall=True), {}, set()
        for r in rows:
            try: t = json.loads(r[1])
            except: t = []
            tags_dict[r[0]] = {"tags": t, "model_name": r[2] or "", "time_taken": r[3] or 0.0}
            all_cats.update(t)
        return jsonify({"song_tags": tags_dict, "categories": list(all_cats)})
    elif request.method == 'POST':
        d = request.json
        _db_exec('INSERT OR REPLACE INTO song_tags VALUES (?, ?, ?, ?)', (d.get('song_name'), json.dumps(d.get('tags', []), ensure_ascii=False), d.get('model_name', ''), d.get('time_taken', 0.0)))
        return jsonify({"status": "success"})
    elif request.method == 'DELETE':
        _db_exec('DELETE FROM song_tags WHERE song_name=?', (request.json.get('song_name'),))
        return jsonify({"status": "success"})

@tags_bp.route('/api/tags/csv', methods=['GET'])
def download_tags_csv():
    q = "SELECT st.song_name, st.tags, st.model_name, st.time_taken, mt.avg_pure_ai, mt.avg_total_tokens FROM song_tags st LEFT JOIN (SELECT model_name, ROUND(AVG(pure_ai_time), 3) AS avg_pure_ai, ROUND(AVG(total_tokens), 1) AS avg_total_tokens FROM model_telemetry WHERE status = 'success' GROUP BY model_name) mt ON st.model_name = mt.model_name"
    rows, si = _db_exec(q, fetchall=True), io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['歌曲名', '分类标签', '识别模型', '单首分摊耗时(s)', '模型批次均次推理耗时(s)', '模型批次均消耗Tokens'])
    for r in rows:
        try: t = " / ".join(json.loads(r[1])) if r[1] else "未分类"
        except: t = "数据损坏"
        cw.writerow([r[0], t, r[2] or "未知", r[3] or 0.0, r[4] if r[4] is not None else "N/A", r[5] if r[5] is not None else "N/A"])
    out = make_response(si.getvalue().encode('utf-8-sig'))
    out.headers.update({"Content-Disposition": "attachment; filename=song_tags_detailed_export.csv", "Content-type": "text/csv; charset=utf-8"})
    return out

@tags_bp.route('/api/tags/csv_import', methods=['POST'])
def import_and_reidentify_csv():
    if 'file' not in request.files: return jsonify({"error": "未上传文件"}), 400
    api_key, mod = request.form.get('api_key') or GLOBAL_MODEL_MUSIC_API, request.form.get('model_name') or GLOBAL_MODEL_MUSIC
    if not api_key: return jsonify({"error": "缺API Key"}), 400
    try:
        csv_in = csv.reader(io.StringIO(request.files['file'].stream.read().decode("utf-8-sig"), newline=None))
        headers = next(csv_in, None)
        idx = headers.index('歌曲名') if headers and '歌曲名' in headers else 0
        songs = [r[idx].strip() for r in csv_in if r and len(r) > idx and r[idx].strip()]
    except Exception as e: return jsonify({"error": f"CSV解析失败: {e}"}), 400

    succ, fail = 0, 0
    for i in range(0, len(songs), MAX_BATCH_SIZE):
        chunk, t0 = songs[i:i+MAX_BATCH_SIZE], time.time()
        try:
            res_dict, tele = call_silicon_batch(chunk, api_key, mod)
            avg_time = (time.time() - t0) / len(chunk)
            for s in chunk: _db_exec('INSERT OR REPLACE INTO song_tags VALUES (?, ?, ?, ?)', (s, json.dumps(res_dict.get(s, ["未分类"]), ensure_ascii=False), mod, round(avg_time, 3)))
            succ += len(chunk)
            log_telemetry(mod, len(chunk), tele, time.time() - t0, "success")
        except Exception as e:
            fail += len(chunk)
            log_telemetry(mod, len(chunk), {}, time.time() - t0, f"error: {str(e)[:50]}")
    return jsonify({"status": "completed", "success_count": succ, "fail_count": fail, "total": len(songs)})

def _handle_list_api(table_name):
    sn, method = request.json.get('song_name') if request.method != 'GET' else None, request.method
    if method != 'GET' and not sn: return jsonify({"error": "缺少 song_name"}), 400
    if method == 'GET': return jsonify([r[0] for r in _db_exec(f'SELECT song_name FROM {table_name}', fetchall=True)])
    elif method == 'POST': _db_exec(f'INSERT OR IGNORE INTO {table_name} VALUES (?)', (sn,))
    elif method == 'DELETE': _db_exec(f'DELETE FROM {table_name} WHERE song_name=?', (sn,))
    return jsonify({"status": "success"})

@tags_bp.route('/api/favorites', methods=['GET', 'POST', 'DELETE'])
def api_favorites(): return _handle_list_api('favorites')

@tags_bp.route('/api/deleted_songs', methods=['GET', 'POST', 'DELETE'])
def api_deleted_songs(): return _handle_list_api('deleted_songs')

def call_silicon_batch(songs, api_key, model):
    if not GLOBAL_MODEL_MUSIC_URL: raise Exception("缺少 MODEL_MUSIC_URL 环境变量")
    t0, prompt = time.time(), f"请为以下歌曲从库中选1-3个标签：[{'，'.join(TAG_CHOICES)}]\n列表：{json.dumps(songs, ensure_ascii=False)}\n必须只返回纯JSON(无代码块)，键为歌名，值为标签数组。"
    resp = requests.post(GLOBAL_MODEL_MUSIC_URL, json={"model": model, "messages": [{"role": "system", "content": "你严格输出JSON。"}, {"role": "user", "content": prompt}], "temperature": 0.1}, headers={"Authorization": f"Bearer {api_key}"}, timeout=120)
    p_time = time.time() - t0
    if resp.status_code != 200: raise Exception(f"HTTP {resp.status_code}: {resp.text[:100]}")
    res_j = resp.json()
    if 'choices' not in res_j: raise Exception(str(res_j.get('error', res_j)))
    tele = {'prompt_tokens': res_j.get('usage',{}).get('prompt_tokens',0), 'completion_tokens': res_j.get('usage',{}).get('completion_tokens',0), 'total_tokens': res_j.get('usage',{}).get('total_tokens',0), 'pure_ai_time': p_time}
    if not (m := re.search(r'\{.*\}', res_j['choices'][0]['message']['content'], re.DOTALL)): raise Exception("非合法 JSON 格式")
    clean = {s: [t for t in tags if t in TAG_CHOICES][:3] or ["未分类"] if isinstance(tags, list) else ["未分类"] for s, tags in json.loads(m.group(0)).items()}
    return clean, tele

def ai_batch_worker():
    while True:
        batch, t0 = [BATCH_QUEUE.get()], time.time()
        while len(batch) < MAX_BATCH_SIZE and (time.time() - t0) < BATCH_TIMEOUT:
            try: batch.append(BATCH_QUEUE.get(timeout=BATCH_TIMEOUT - (time.time() - t0)))
            except queue.Empty: break
        songs, api, mod = [b['song_name'] for b in batch], batch[0]['api_key'], batch[0]['model_name']
        rt0 = time.time()
        try:
            res, tele = call_silicon_batch(songs, api, mod)
            t_time = time.time() - rt0
            log_telemetry(mod, len(batch), tele, t_time, "success")
            for b in batch: b.update({'result': res.get(b['song_name'], ["未分类"]), 'time_taken': t_time/len(batch)}); b['event'].set()
        except Exception as e:
            t_time = time.time() - rt0
            log_telemetry(mod, len(batch), {}, t_time, f"error: {str(e)[:50]}")
            for b in batch: b['error'] = str(e); b['event'].set()

for _ in range(2): threading.Thread(target=ai_batch_worker, daemon=True).start()

@tags_bp.route('/api/ai/tag', methods=['POST'])
def tag_song_api():
    d = request.json
    api_key, mod = d.get('api_key') or GLOBAL_MODEL_MUSIC_API, d.get('model_name') or GLOBAL_MODEL_MUSIC
    if not api_key: return jsonify({"error": "缺少 API Key"}), 400
    ctx = {'song_name': d.get('song_name'), 'api_key': api_key, 'model_name': mod, 'event': threading.Event(), 'error': None}
    BATCH_QUEUE.put(ctx)
    if not ctx['event'].wait(125.0): return jsonify({"error": "等待超时"}), 504
    if ctx['error']: return jsonify({"error": ctx['error']}), 500
    return jsonify({"tags": ctx['result'], "model_name": ctx['model_name'], "time_taken": round(ctx['time_taken'], 3)})