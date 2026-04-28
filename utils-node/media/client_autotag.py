import os
import csv
import json
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from tqdm import tqdm
except ImportError:
    print("错误: 缺少 tqdm 库。请先执行 'pip install tqdm'")
    exit(1)

# ================= 配置参数 =================
API_KEY = os.environ.get("OPENROUTER_API_KEY")
MODEL = "qwen/qwen3-235b-a22b-2507"  # 可替换为 OpenRouter 支持的任意模型
INPUT_CSV = input('csv:')
OUTPUT_CSV = "tagged_videos_import.csv"
PROMPT_FILE = "prompt.txt"
MAX_WORKERS = 5  # 并发线程数
BATCH_SIZE = 5   # 每次合并处理的文件名数量
# ============================================

def read_prompt(file_path):
    """读取独立文本 Prompt，若不存在则创建默认高兼容性批量模板"""
    if not os.path.exists(file_path):
        with open(file_path, 'w', encoding='utf-8') as f:
           f.write(
                "你是一个专业的视频特征提取助手。我将提供一个JSON对象，键为【视频ID】，值为【视频文件名】。\n"
                "请提取每个文件名的核心特征标签（如题材、场景、人物特征等，过滤掉无意义的符号、日期、和集数）。\n"
                "【强制要求】：必须返回一个纯合法的JSON对象，保持键(视频ID)完全不变，值为对应的标签字符串数组。\n"
                "【示例输出】：\n"
                "{\n"
                "  \"1024\": [\"少女\", \"健身\"],\n"
                "  \"1025\": [\"风景\", \"航拍\"]\n"
                "}"
            )
    with open(file_path, 'r', encoding='utf-8') as f:
        return f.read().strip()

def get_signature(filename):
    """
    预处理：去除扩展名，并剔除所有的数字，作为判断是否重复的【指纹】
    """
    name = os.path.splitext(filename)[0]
    # 剔除所有数字
    sig = re.sub(r'\d+', '', name)
    # 剔除首尾的特殊符号和空格，增强匹配率
    sig = re.sub(r'^[\s\-_【】\[\]]+|[\s\-_【】\[\]]+$', '', sig)
    return sig.strip()

def parse_batch_tags(text, expected_ids):
    """解析模型返回的批量 JSON 结果，最大程度兼容不同的返回结构"""
    text = text.strip()
    text = re.sub(r'^```(json)?\s*', '', text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r'```$', '', text, flags=re.MULTILINE).strip()
    
    result_map = {vid: "" for vid in expected_ids}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            if "results" in data and isinstance(data["results"], dict):
                data = data["results"]
            elif "tags" in data and isinstance(data["tags"], dict):
                data = data["tags"]

        if isinstance(data, dict):
            for vid in expected_ids:
                val = data.get(vid) or data.get(int(vid)) or data.get(str(vid))
                if val:
                    if isinstance(val, list):
                        result_map[vid] = " ".join([str(item).strip() for item in val if str(item).strip()])
                    elif isinstance(val, str):
                        words = re.findall(r'[\u4e00-\u9fa5a-zA-Z0-9_]+', val)
                        result_map[vid] = " ".join(words)
    except json.JSONDecodeError:
        pass
        
    return result_map

def process_chunk(chunk, prompt_template, is_test=False):
    """处理一批数据，返回字典 {vid: tags_string}"""
    input_dict = {row[0]: row[1] for row in chunk}
    tags_map = {vid: "" for vid in input_dict.keys()}
    
    if not API_KEY:
        return tags_map
        
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": prompt_template},
            {"role": "user", "content": json.dumps(input_dict, ensure_ascii=False)}
        ]
    }
    
    try:
        resp = requests.post(
            url="https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=40 
        )
        resp.raise_for_status()
        result_text = resp.json()['choices'][0]['message']['content']
        tags_map = parse_batch_tags(result_text, list(input_dict.keys()))
        
        # 测试模式：强力打印监控
        if is_test:
            print("\n" + "="*50)
            print("👇 【模型原始返回 JSON 字符串】 👇")
            print("-" * 50)
            print(result_text)
            print("-" * 50)
            print("\n👇 【程序解析提取出的最终 Tag】 👇")
            print("-" * 50)
            for vid, name in input_dict.items():
                print(f"📄 文件: {name}")
                print(f"🏷️  提取: {tags_map.get(vid, '（无）')}")
                print("-")
            print("="*50 + "\n")
            
    except Exception as e:
        if is_test:
            print(f"\n[!] 测试请求失败: {e}")
            if 'resp' in locals():
                print(f"API 响应: {resp.text}")
                
    return tags_map

def main():
    if not API_KEY:
        print("错误: 未检测到环境变量 OPENROUTER_API_KEY")
        return

    if not os.path.exists(INPUT_CSV):
        print(f"错误: 找不到输入文件 {INPUT_CSV}，请先在网页端导出。")
        return

    prompt_template = read_prompt(PROMPT_FILE)
    
    with open(INPUT_CSV, 'r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        rows = list(reader)
        
    if len(rows) < 2:
        print("CSV 文件数据为空或只有表头。")
        return
        
    header = rows[0]
    data_rows = rows[1:]
    
    # ================= 核心优化：全量预处理指纹去重 =================
    sig_to_rep_row = {}  # { "指纹" : 代表行数据 }
    vid_to_sig = {}      # { "视频ID" : "指纹" }
    
    for row in data_rows:
        vid, filename = row[0], row[1]
        sig = get_signature(filename)
        vid_to_sig[vid] = sig
        
        # 只保留第一个拥有该指纹的视频作为“代表”去请求 API
        if sig not in sig_to_rep_row:
            sig_to_rep_row[sig] = row
            
    unique_tasks = list(sig_to_rep_row.values())
    saved_count = len(data_rows) - len(unique_tasks)
    
    print(f"📊 总数据量: {len(data_rows)} 条")
    print(f"✂️  剔除数字后相似视频合并，实际需请求大模型: {len(unique_tasks)} 条 (为您节省了 {saved_count} 次处理!)\n")
    
    chunks = [unique_tasks[i:i + BATCH_SIZE] for i in range(0, len(unique_tasks), BATCH_SIZE)]
    
    global_tags_map = {} # 保存所有请求回来的 {代表vid : tags_str}
    
    # ================= 1. 首批数据预检测试 =================
    print("🛠️ 正在发送【第一批】代表数据进行测试验证，请稍候...")
    first_chunk = chunks[0]
    first_tags_map = process_chunk(first_chunk, prompt_template, is_test=True)
    global_tags_map.update(first_tags_map)
    
    if len(chunks) > 1:
        confirm = input("❓ 请确认上述解析结果是否符合预期？\n👉 按【回车】或输入【y】继续处理剩余数据，输入【n】退出程序: ").strip().lower()
        if confirm not in ['', 'y', 'yes']:
            print("🛑 用户已取消，程序安全退出。你可以修改 prompt.txt 后再次重试。")
            return
            
        # ================= 2. 多线程处理剩余数据 =================
        print(f"\n🚀 启动 {MAX_WORKERS} 个并发线程请求处理剩余 {len(chunks)-1} 批代表数据...\n")
        remaining_chunks = chunks[1:]
        
        with tqdm(total=len(remaining_chunks), desc="打标进度", unit="批") as pbar:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                future_to_chunk = {
                    executor.submit(process_chunk, chunk, prompt_template, False): chunk 
                    for chunk in remaining_chunks
                }
                
                for future in as_completed(future_to_chunk):
                    try:
                        chunk_tags_map = future.result()
                        global_tags_map.update(chunk_tags_map)
                    except Exception as exc:
                        tqdm.write(f"[!] 某批次执行异常: {exc}")
                    finally:
                        pbar.update(1)
                        
    # ================= 3. 数据还原与分发 =================
    # 将大模型的返回结果，根据指纹映射给所有的原始视频
    results = []
    for row in data_rows:
        vid = row[0]
        sig = vid_to_sig[vid]
        
        # 找到这组相似视频的“代表者ID”
        rep_vid = sig_to_rep_row[sig][0] 
        # 获取该代表者的打标结果
        final_tags = global_tags_map.get(rep_vid, "")
        
        new_row = row.copy()
        if len(new_row) >= 3:
            new_row[2] = final_tags
        else:
            new_row.append(final_tags)
        results.append(new_row)

    # 保证按原始 ID 排序回写
    try:
        results.sort(key=lambda x: int(x[0]))
    except ValueError:
        pass 
        
    with open(OUTPUT_CSV, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(results)
        
    print(f"\n🎉 处理完成！同系列视频已自动应用相同标签。")
    print(f"📁 新文件已生成: {OUTPUT_CSV}，可直接在网页端导入。")

if __name__ == "__main__":
    main()