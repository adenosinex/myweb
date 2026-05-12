import os
import sqlite3
import streamlit as st
import pandas as pd
import requests
import time
import re
import shutil
import math
from datetime import datetime
import streamlit.components.v1 as components

# ================= 配置区 =================
DB_FILE = "texts_data.db"
OLLAMA_API_URL = "http://apple4.su7.dpdns.org:11434/api/generate"
MODEL_NAME = "huihui_ai/qwen2.5-abliterate:7b"

st.set_page_config(page_title="小说自动化审查系统 MAX", layout="wide")

# 初始化历史记录状态（用于撤回功能）
if 'history' not in st.session_state:
    st.session_state.history = []

# ================= 核心工具引擎 =================
def format_size(size_bytes):
    """字节转易读格式，辅助决策"""
    if size_bytes == 0: return "0B"
    units = ("B", "KB", "MB", "GB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    return f"{round(size_bytes / p, 2)} {units[i]}"

def extract_core_title(filename):
    """文件名洗澡提取核心书名：书名号《》绝对优先"""
    name = os.path.splitext(filename)[0]
    match = re.search(r'《(.*?)》', name)
    if match:
        core_name = match.group(1)
        return re.sub(r'[\[【\(（].*?[\]】\)）]', '', core_name).strip()
        
    name = name.replace('《', '').replace('》', '')
    name = re.sub(r'[\[【\(（].*?[\]】\)）]', '', name)
    dirty_suffixes = r'(_?作者[：:].*|_?整理版?|_?校对版?|_?完结|_?完本|_?连载中?|整理_校对.*|_?TXT下载.*|_?作品.*|全集|最新章节.*|加料版?.*|\d{1,}_\d{1,}.*)'
    name = re.sub(dirty_suffixes, '', name, flags=re.IGNORECASE)
    return name.strip('_- ') or os.path.splitext(filename)[0]

def read_text_preview(file_path, length=4000):
    """按需长度读取：同步缓存默认4000，AI请求可传入15000"""
    if not os.path.exists(file_path): return ""
    encodings = ['utf-8', 'gb18030', 'gbk', 'gb2312', 'big5', 'utf-16']
    for enc in encodings:
        try:
            with open(file_path, 'r', encoding=enc) as f:
                content = f.read(length * 2) 
                if '\ufffd' not in content[:500]: 
                    return content[:length]
        except UnicodeDecodeError: continue
        except Exception: pass
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        return f.read(length)

def archive_file(source_path, target_dir):
    """物理移动文件到指定目录"""
    if not os.path.exists(source_path): return False, "原文件已不存在"
    if not os.path.exists(target_dir):
        try: os.makedirs(target_dir, exist_ok=True)
        except Exception as e: return False, f"创建目录失败: {e}"
    try:
        target_path = os.path.join(target_dir, os.path.basename(source_path))
        if os.path.exists(target_path): os.remove(target_path)
        shutil.move(source_path, target_path)
        return True, target_path
    except Exception as e: return False, f"移动失败: {e}"

# ================= 数据库操作 =================
def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL;') 
    return conn

@st.cache_resource 
def init_db():
    with get_db_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS texts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT UNIQUE,
                file_path TEXT,
                tag TEXT DEFAULT 'Pending', 
                ai_summary TEXT DEFAULT '',
                is_whitelisted INTEGER DEFAULT 0,
                is_blacklisted INTEGER DEFAULT 0,
                is_archived INTEGER DEFAULT 0,
                preview_text TEXT DEFAULT '',
                ai_prompt TEXT DEFAULT '',
                ai_time_sec REAL DEFAULT 0.0,
                ai_timestamp TEXT DEFAULT '',
                core_title TEXT DEFAULT '',
                file_size INTEGER DEFAULT 0,
                char_count INTEGER DEFAULT 0
            )
        ''')
        conn.execute('CREATE TABLE IF NOT EXISTS rules (id INTEGER PRIMARY KEY AUTOINCREMENT, rule_type TEXT, keyword TEXT UNIQUE)')
        conn.commit()

init_db() 

def sync_files_to_db(folder_path):
    if not os.path.isdir(folder_path): return 0, 0
    best_local_files = {}
    for root, _, files in os.walk(folder_path):
        for file in files:
            if file.lower().endswith('.txt'):
                file_path = os.path.join(root, file)
                size = os.path.getsize(file_path)
                core_title = extract_core_title(file)
                if core_title not in best_local_files or size > best_local_files[core_title]['size']:
                    best_local_files[core_title] = {'path': file_path, 'size': size, 'filename': file}
    
    new_count, update_count = 0, 0
    total_files = len(best_local_files)
    
    if total_files > 0:
        pbar = st.sidebar.progress(0)
        stat = st.sidebar.empty()
        with get_db_connection() as conn:
            db_records = {row['core_title']: row for row in conn.execute("SELECT id, core_title, char_count, filename FROM texts").fetchall()}
            for i, (core_title, info) in enumerate(best_local_files.items()):
                stat.text(f"入库进度: {i+1}/{total_files} ({info['filename'][:12]}...)")
                est_char_count = info['size'] // 3 
                
                if core_title in db_records:
                    if est_char_count > db_records[core_title]['char_count'] + 5000:
                        # 数据库缓存依然只读 4000 字，维持极速响应
                        preview = read_text_preview(info['path'], 4000)
                        conn.execute("""
                            UPDATE texts SET filename=?, file_path=?, preview_text=?, file_size=?, char_count=?, is_archived=0 WHERE id=?
                        """, (info['filename'], info['path'], preview, info['size'], est_char_count, db_records[core_title]['id']))
                        update_count += 1
                else:
                    try:
                        preview = read_text_preview(info['path'], 4000)
                        conn.execute("""
                            INSERT INTO texts (filename, file_path, preview_text, core_title, file_size, char_count) 
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (info['filename'], info['path'], preview, core_title, info['size'], est_char_count))
                        new_count += 1
                    except sqlite3.IntegrityError: pass
                pbar.progress((i + 1) / total_files)
            conn.commit()
        stat.empty()
        pbar.empty()
    return new_count, update_count

def update_rules_in_db():
    with get_db_connection() as conn:
        conn.execute("UPDATE texts SET is_whitelisted = 0, is_blacklisted = 0")
        
        whites = [r['keyword'] for r in conn.execute("SELECT keyword FROM rules WHERE rule_type = 'white'").fetchall()]
        if whites:
            for i in range(0, len(whites), 100):
                chunk = whites[i:i+100]
                query = " OR ".join([f"preview_text LIKE '%{w}%'" for w in chunk])
                conn.execute(f"UPDATE texts SET is_whitelisted = 1 WHERE {query}")
                
        blacks = [r['keyword'] for r in conn.execute("SELECT keyword FROM rules WHERE rule_type = 'black'").fetchall()]
        if blacks:
            for i in range(0, len(blacks), 100):
                chunk = blacks[i:i+100]
                query = " OR ".join([f"preview_text LIKE '%{b}%'" for b in chunk])
                conn.execute(f"UPDATE texts SET is_blacklisted = 1 WHERE {query}")
                
        conn.commit()

def get_stats():
    with get_db_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM texts").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM texts WHERE tag = 'Pending' AND is_blacklisted = 0 AND is_archived = 0").fetchone()[0]
        blacklisted = conn.execute("SELECT COUNT(*) FROM texts WHERE is_blacklisted = 1 AND is_archived = 0").fetchone()[0]
        no_summary = conn.execute("SELECT COUNT(*) FROM texts WHERE tag = 'Pending' AND is_blacklisted = 0 AND ai_summary = '' AND is_archived = 0").fetchone()[0]
        return total, pending, blacklisted, no_summary

# ================= AI 接口 =================
def ask_ai_for_plot(text):
    # 增强版 Prompt：增加强制性指令，防止复读原文
    prompt = f"""你是一名专业的文学评论家和情节分析师。请对以下小说文本进行深度提炼。
【严禁规则】：
1. 绝对禁止复读原文，禁止续写故事。
2. 必须且只能输出 <人物>、<背景>、<情节> 三个标签的内容。
3. 即使信息不足，也要根据现有片段进行合理概括。

【输出格式】：
<人物>这里概括主要人物及其性格、核心关系（如：于途、李明，同学/好友关系）</人物>
<背景>这里概括故事发生的时空环境、社会背景（如：假期、现代都市小区）</背景>
<情节>这里用三句话概括本片段的核心事件脉络（如：李明与于途共同写作业，两家人互动互送礼物，展现纯真友谊）</情节>

【待分析文本】：
\n{text[:5000]}"""
    
    start_time = time.time()
    try:
        # 增加 timeout 到 180 秒，防止大文本处理超时
        response = requests.post(OLLAMA_API_URL, json={"model": MODEL_NAME, "prompt": prompt, "stream": False}, timeout=180)
        result_text = response.json().get("response", "AI 响应失败").strip()
    except Exception as e: result_text = f"请求失败: {e}"
    return prompt, result_text, time.time() - start_time, datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def parse_ai_summary(raw_text):
    if not raw_text: return {}
    text = re.sub(r'^```[\w]*\n|\n```$', '', raw_text.strip(), flags=re.MULTILINE)
    def extract_section(tag):
        pattern = rf'<{tag}>(.*?)(?:</{tag}>|<人物>|<背景>|<情节>|$)'
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match: return re.sub(r'</?(人物|背景|情节)>', '', match.group(1).strip(), flags=re.IGNORECASE).strip()
        return ""
    chars, bg, plot = extract_section("人物"), extract_section("背景"), extract_section("情节")
    if not chars and not bg and not plot: 
        return {"characters": "", "background": "", "plot": re.sub(r'</?(人物|背景|情节)>', '', text, flags=re.IGNORECASE).strip()}
    return {"characters": chars, "background": bg, "plot": plot}

# ================= UI 侧边栏 =================
st.sidebar.header("1. 数据源与跑批")
folder_path = st.sidebar.text_input("待审 TXT 文件夹 (支持子目录)", "./data")

if st.sidebar.button("同步文件并应用规则"):
    with st.spinner("极速提取前缀并构建索引中..."):
        new_c, up_c = sync_files_to_db(folder_path)
        update_rules_in_db()
        st.sidebar.success(f"完成！新增入库 {new_c} 篇，更新覆盖 {up_c} 篇")

st.sidebar.divider()
total, pending, blacklisted, no_summary = get_stats()

st.sidebar.subheader("无人值守跑批")
st.sidebar.write(f"待跑批: {no_summary} 篇")
if st.sidebar.button("🧹 重置所有待审摘要"):
    with get_db_connection() as conn: conn.execute("UPDATE texts SET ai_summary = '' WHERE tag = 'Pending'"); conn.commit()
    st.rerun()

batch_limit = st.sidebar.number_input("本次处理量", min_value=1, max_value=5000, value=100)
if st.sidebar.button("🚀 开始生成摘要"):
    if no_summary > 0:
        bar = st.sidebar.progress(0)
        stat = st.sidebar.empty()
        with get_db_connection() as conn:
            # 抓取 file_path，用于即时大段读取
            targets = conn.execute("SELECT id, filename, file_path FROM texts WHERE tag = 'Pending' AND is_blacklisted = 0 AND ai_summary = '' LIMIT ?", (batch_limit,)).fetchall()
            for i, row in enumerate(targets):
                stat.text(f"处理: {row['filename'][:15]}")
                # 【按需读取】跑到这本时，再从硬盘抓 15000 字喂给 AI
                ai_text = read_text_preview(row['file_path'], 5000)
                p_used, ans, dt, ts = ask_ai_for_plot(ai_text)
                
                conn.execute("UPDATE texts SET ai_summary=?, ai_prompt=?, ai_time_sec=?, ai_timestamp=? WHERE id=?", (ans, p_used, dt, ts, row['id']))
                conn.commit()
                bar.progress((i + 1) / len(targets))
        st.rerun()

st.sidebar.divider()
st.sidebar.header("2. 统一物理归档")
st.sidebar.caption("审完一批后，将文件批量移至对应目录")
keep_dir = st.sidebar.text_input("👍 留存目录", "./keep")
drop_dir = st.sidebar.text_input("👎 丢弃(含黑名单)", "./drop")
skip_dir = st.sidebar.text_input("⏭️ 跳过目录", "./skip")

if st.sidebar.button("📦 执行统一归档移动", type="primary", use_container_width=True):
    with st.spinner("正在批量移动文件..."):
        s_count, f_count = 0, 0
        with get_db_connection() as conn:
            targets = conn.execute("SELECT id, file_path, tag, is_blacklisted FROM texts WHERE is_archived = 0 AND (tag IN ('Keep', 'Drop', 'Skip') OR is_blacklisted = 1)").fetchall()
            for row in targets:
                if row['is_blacklisted'] == 1 or row['tag'] == 'Drop': tgt_folder = drop_dir
                elif row['tag'] == 'Keep': tgt_folder = keep_dir
                elif row['tag'] == 'Skip': tgt_folder = skip_dir
                else: continue
                
                success, new_path = archive_file(row['file_path'], tgt_folder)
                if success:
                    conn.execute("UPDATE texts SET file_path = ?, is_archived = 1 WHERE id = ?", (new_path, row['id']))
                    s_count += 1
                else:
                    if "不存在" in new_path: conn.execute("UPDATE texts SET is_archived = 1 WHERE id = ?", (row['id'],))
                    f_count += 1
            conn.commit()
        st.sidebar.success(f"✅ 归档完成！成功 {s_count} 篇，异常 {f_count} 篇。")
        time.sleep(1.5)
        st.rerun()

st.sidebar.divider()
st.sidebar.header("3. 黑白名单规则管理")
if st.sidebar.button("🔄 全局重刷黑白名单", use_container_width=True):
    with st.spinner("更新中..."): update_rules_in_db()
    st.rerun()

col_w1, col_w2 = st.sidebar.columns([2, 1])
with col_w1: new_white = st.text_input("白名单词汇", label_visibility="collapsed", placeholder="输入白名单...", key="input_white")
with col_w2:
    if st.button("加白", use_container_width=True):
        if new_white.strip():
            kw = new_white.strip()
            with get_db_connection() as conn: 
                conn.execute("INSERT OR IGNORE INTO rules (rule_type, keyword) VALUES ('white', ?)", (kw,))
                conn.execute("UPDATE texts SET is_whitelisted = 1 WHERE preview_text LIKE ?", (f"%{kw}%",))
                conn.commit()
            st.rerun()

col_b1, col_b2 = st.sidebar.columns([2, 1])
with col_b1: new_black = st.text_input("黑名单词汇", label_visibility="collapsed", placeholder="输入黑名单...", key="input_black")
with col_b2:
    if st.button("加黑", use_container_width=True):
        if new_black.strip():
            kw = new_black.strip()
            with get_db_connection() as conn: 
                conn.execute("INSERT OR IGNORE INTO rules (rule_type, keyword) VALUES ('black', ?)", (kw,))
                conn.execute("UPDATE texts SET is_blacklisted = 1 WHERE preview_text LIKE ?", (f"%{kw}%",))
                conn.commit()
            st.rerun()

st.sidebar.write("") 
if st.sidebar.button("📥 导出当前规则 (CSV)"):
    with get_db_connection() as conn: rdf = pd.read_sql("SELECT rule_type, keyword FROM rules", conn)
    st.sidebar.download_button("下载 rules.csv", rdf.to_csv(index=False).encode('utf-8-sig'), "rules.csv", mime="text/csv")

rule_csv_file = st.sidebar.file_uploader("📤 导入 CSV", type=['csv'])
if rule_csv_file:
    try:
        rdf = pd.read_csv(rule_csv_file)
        with get_db_connection() as conn:
            for _, row in rdf.iterrows(): conn.execute("INSERT OR IGNORE INTO rules (rule_type, keyword) VALUES (?, ?)", (row['rule_type'], str(row['keyword'])))
            conn.commit()
        with st.spinner("导入完毕，正在重建全库索引..."): update_rules_in_db()
        st.sidebar.success("✅ 导入成功并生效")
    except Exception as e: st.sidebar.error("导入失败")

# ================= 主界面逻辑 =================
st.write(f"### 数据库状态: 共 {total} 篇 | 待审 {pending} 篇 | 待归档黑名单 {blacklisted} 篇")

if pending > 0:
    with get_db_connection() as conn:
        current_row = conn.execute("SELECT * FROM texts WHERE tag = 'Pending' AND is_blacklisted = 0 AND is_archived = 0 ORDER BY (ai_summary != '') DESC, is_whitelisted DESC, id ASC LIMIT 1").fetchone()

    if current_row:
        if 'last_viewed_id' not in st.session_state: st.session_state.last_viewed_id = None
        if current_row['id'] != st.session_state.last_viewed_id:
            st.session_state.last_viewed_id = current_row['id']
            
        # 使用时间戳确保 Streamlit 在每次点击时都重新挂载这段脚本
        import time 
        refresh_id = time.time()
        
        js_code = f"""
        <script>
        // 强制刷新标识: {refresh_id}
        const doc = window.parent.document;
        
        // 滚动回顶部的函数
        function scrollToTop() {{
            const scrollContainers = [
                doc.querySelector('[data-testid="stAppViewContainer"]'),
                doc.querySelector('[data-testid="stMainBlockContainer"]'),
                doc.querySelector('section.main'),
                doc.documentElement,
                doc.body
            ];
            
            scrollContainers.forEach(el => {{
                // 瞬间定位，去除 smooth 防止动画滞后
                if (el) el.scrollTo({{ top: 0, left: 0, behavior: 'instant' }});
            }});
        }}
        
        // 1. 初次执行
        scrollToTop();

        // 2. 核心改进：设置 MutationObserver 监听主体区域的变化
        // 这样可以确保当 React (Streamlit 底层) 把新小说渲染出来后，立刻触发置顶
        if (!window.myScrollObserver) {{
            const mainContainer = doc.querySelector('[data-testid="stAppViewContainer"]') || doc.body;
            
            window.myScrollObserver = new MutationObserver(function(mutations) {{
                // 只要检测到节点增加或修改，就尝试置顶
                for (let mutation of mutations) {{
                    if (mutation.type === 'childList' || mutation.type === 'characterData') {{
                        scrollToTop();
                        break; // 触发一次就够了
                    }}
                }}
            }});
            
            // 监听子节点的增加/删除、文本变动，监听整棵子树
            window.myScrollObserver.observe(mainContainer, {{
                childList: true,
                subtree: true,
                characterData: true
            }});
        }}

        // 3. 快捷键监听 (只绑定一次)
        if (!doc.getElementById('kb-shortcut-script')) {{
            const script = doc.createElement('script');
            script.id = 'kb-shortcut-script';
            script.innerHTML = `
                document.addEventListener('keydown', function(e) {{
                    const activeTag = document.activeElement ? document.activeElement.tagName : '';
                    if (activeTag === 'INPUT' || activeTag === 'TEXTAREA') return;
                    
                    let btnText = '';
                    if (e.key === 'z' || e.key === 'Z') btnText = '👍 留存';
                    if (e.key === 'x' || e.key === 'X') btnText = '👎 丢弃';
                    if (e.key === 'q' || e.key === 'Q') btnText = '⏪ 撤回';
                    
                    if (btnText) {{
                        const buttons = Array.from(document.querySelectorAll('button'));
                        const targetBtn = buttons.find(b => b.textContent.includes(btnText));
                        if (targetBtn) targetBtn.click();
                    }}
                }});
            `;
            doc.head.appendChild(script);
        }}
        </script>
        """
        components.html(js_code, height=0)

        st.divider()
        col_text, col_action = st.columns([2, 1.5])
        with col_text:
            suffix = " ⭐(白名单)" if current_row['is_whitelisted'] else ""
            st.subheader(f"📄 {current_row['filename']}{suffix}")
            
            est_words = f"约 {round(current_row['char_count']/10000, 2)} 万" if current_row['char_count'] > 10000 else current_row['char_count']
            st.info(f"📊 物理体积: {format_size(current_row['file_size'])} ｜ 预估篇幅: {est_words} 字 ｜ 路径: {current_row['file_path']}")
            
            st.text_area("正文缓存预览 (前4000字)", current_row['preview_text'] + "\n\n...[截断]...", height=650, disabled=True)
            
        with col_action:
            st.subheader("🤖 AI 剧情解析")
            if current_row['ai_summary']:
                parsed_data = parse_ai_summary(current_row['ai_summary'])
                st.markdown("##### 👥 主要人物")
                st.info(parsed_data.get('characters') or "无")
                st.markdown("##### 🌍 故事背景")
                st.success(parsed_data.get('background') or "无")
                st.markdown("##### 🌊 核心情节")
                st.write(parsed_data.get('plot') or "无")
                
                # 新增单篇解析耗时显示
                if current_row['ai_time_sec'] > 0:
                    st.caption(f"⚡ 解析耗时: {current_row['ai_time_sec']:.2f} 秒")
                    
                with st.expander("⏱️ 原始返回数据"): st.text(current_row['ai_summary'])
            else:
                st.warning("暂无摘要。")
                if st.button("单次请求 AI", use_container_width=True):
                    with st.spinner("处理中..."):
                        # 【按需读取】点击单次解析时，抓取 5000 字
                        ai_text = read_text_preview(current_row['file_path'], 5000)
                        p_used, ans, dt, ts = ask_ai_for_plot(ai_text)
                        
                        with get_db_connection() as conn: 
                            conn.execute("UPDATE texts SET ai_summary = ?, ai_prompt = ?, ai_time_sec = ?, ai_timestamp = ? WHERE id = ?", (ans, p_used, dt, ts, current_row['id']))
                            conn.commit()
                            
                        # 【即时反馈】显示执行耗时
                        st.toast(f"✅ AI 解析完成，耗时 {dt:.2f} 秒！")
                        time.sleep(1)
                        st.rerun()
            
            st.subheader("🏷️ 判定 (快捷键: Z留存 | X丢弃 | Q撤回)")
            custom_tag = st.text_input("手动追加 Tag", key="custom_tag")
            c1, c2, c3, c4 = st.columns(4)
            
            with c1:
                if st.button("👍 留存", use_container_width=True):
                    final_tag = custom_tag if custom_tag else "Keep"
                    with get_db_connection() as conn: conn.execute("UPDATE texts SET tag = ? WHERE id = ?", (final_tag, current_row['id'])); conn.commit()
                    st.session_state.history.append(current_row['id']) 
                    st.rerun()
            with c2:
                if st.button("👎 丢弃", use_container_width=True):
                    with get_db_connection() as conn: conn.execute("UPDATE texts SET tag = 'Drop' WHERE id = ?", (current_row['id'],)); conn.commit()
                    st.session_state.history.append(current_row['id']) 
                    st.rerun()
            with c3:
                if st.button("⏭️ 跳过", use_container_width=True):
                    with get_db_connection() as conn: conn.execute("UPDATE texts SET tag = 'Skip' WHERE id = ?", (current_row['id'],)); conn.commit()
                    st.session_state.history.append(current_row['id']) 
                    st.rerun()
            with c4:
                if st.button("⏪ 撤回", use_container_width=True):
                    if st.session_state.history:
                        last_id = st.session_state.history.pop()
                        with get_db_connection() as conn: conn.execute("UPDATE texts SET tag = 'Pending' WHERE id = ?", (last_id,)); conn.commit()
                        st.rerun()
                    else: st.warning("无历史可撤回")
else:
    st.success("🎉 待审查队列已空！您可以点击左侧【执行统一物理归档】来清理已判定的文件。")