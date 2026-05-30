import os
import uuid
import time
import threading
import requests
import random
import re 
import json

from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, jsonify, render_template, request

ai_bp = Blueprint("ai_aggregate", __name__, url_prefix="/ai")

executor = ThreadPoolExecutor(max_workers=16)
LOCK = threading.Lock()
SESSION_CACHE = {}

AI_MODELS = [
     {
       "name": "step-3.5-flash op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "stepfun/step-3.5-flash",
        "api_key": os.getenv("OP_API_KEY"),
    },
     {
       "name": "owl-alpha tempfr op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "openrouter/owl-alpha",
        "api_key": os.getenv("OP_API_KEY"),
    },
     {
       "name": "gpt-4o-mini op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "openai/gpt-4o-mini",
        "api_key": os.getenv("OP_API_KEY"),
    },
     {
       "name": "glm-5.1 al",
        "base_url": os.getenv("MODEL_AL_URL"),
        "model": "glm-5.1",
        "api_key": os.getenv("AL_API_KEY"),
    },
     {
       "name": "qwen3.7-max al", 
        "base_url": os.getenv("MODEL_AL_URL"),
        "model": "qwen3.7-max",
        "api_key": os.getenv("AL_API_KEY"),
    },
    {
       "name": "mimo2.5pr op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "xiaomi/mimo-v2.5-pro",
        "api_key": os.getenv("OP_API_KEY"),
    },
    {
       "name": "qwen3.6-plus op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "qwen/qwen3.6-plus",
        "api_key": os.getenv("OP_API_KEY"),
    },
    {
       "name": "DeepSeek-V4-Pro si",
        "base_url": os.getenv("MODEL_SI_URL"),
        "model": "deepseek-ai/DeepSeek-V4-Pro",
        "api_key": os.getenv("SI_API_KEY"),
        "is_arbiter": True  
    },
]

AI_MODELS_FAST = [
    {
        "name": "glm4.5 airfr op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "z-ai/glm-4.5-air:free",
        "api_key": os.getenv("OP_API_KEY"),
    },
     {
       "name": "Step-3.5-Flash si",
        "base_url": os.getenv("MODEL_SI_URL"),
        "model": "stepfun-ai/Step-3.5-Flash",
        "api_key": os.getenv("SI_API_KEY"),
    },
    
     {
         "name": "hy3-preview op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "tencent/hy3-preview",
        "api_key": os.getenv("OP_API_KEY"),
    },
    
     {
       "name": "DeepSeek-V4-Flash si",
        "base_url": os.getenv("MODEL_SI_URL"),
        "model": "deepseek-ai/DeepSeek-V4-Flash",
        "api_key": os.getenv("SI_API_KEY"),
        "is_arbiter": True  
    },
]

ARBITER_MODEL = {
    "name": "Arbiter dspro",
    "base_url": "https://api.deepseek.com/chat/completions",
    "model": "deepseek-v4-pro",
    "api_key": os.getenv("DS_API_KEY"),
}

@ai_bp.route("/")
def index():
    return render_template("ai_aggregate.html")

def estimate_tokens(text):
    if not text:
        return 0
    return max(1, int(len(text) / 4))

def request_ai(model_config, question, is_arbitration=False):
    start_time = time.time()

    headers = {
        "Authorization": f"Bearer {model_config['api_key']}",
        "Content-Type": "application/json"
    }

    if not is_arbitration:
        q_text = question + "\n\n---\n请完整回答上述问题。在回答的最后，必须单起一行严格输出『【核心观点摘要】』这七个字，然后输出200到400字的核心观点摘要。"
    else:
        q_text = question

    payload = {
        "model": model_config["model"],
        "messages": [{"role": "user", "content": q_text}],
        "temperature": 0.7
    }

    is_free_model = "free" in model_config["model"].lower()
    req_timeout = 180 if is_free_model else 300
    max_retries = 3
    data = None

    for attempt in range(max_retries):
        try:
            r = requests.post(
                model_config["base_url"],
                headers=headers,
                json=payload,
                timeout=req_timeout
            )
            
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == max_retries - 1:
                    r.raise_for_status()
                time.sleep((2 ** attempt) + random.uniform(0.1, 1.0))
                continue
            
            r.raise_for_status()
            data = r.json()
            break
        except requests.exceptions.Timeout as e:
            raise e
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                raise e
            time.sleep((2 ** attempt) + random.uniform(0.1, 1.0))

    elapsed = round(time.time() - start_time, 2)
    choice = data.get("choices", [{}])[0]
    answer = choice.get("message", {}).get("content", "")
    
    full_answer = answer
    summary = ""
    if not is_arbitration:
        if "【核心观点摘要】" in answer:
            parts = answer.rsplit("【核心观点摘要】", 1)
            full_answer = parts[0].strip()
            summary = parts[1].strip()
        else:
            summary = answer[:300] + "...\n(该模型未遵循摘要格式要求)"

    finish_reason = choice.get("finish_reason", "unknown")
    actual_model = data.get("model", model_config["model"])
    system_fingerprint = data.get("system_fingerprint", "none")

    usage = data.get("usage")
    cached_tokens = 0
    reasoning_tokens = 0

    if usage:
        token_source = "response"
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)
        
        prompt_details = usage.get("prompt_tokens_details", {})
        if isinstance(prompt_details, dict):
            cached_tokens = prompt_details.get("cached_tokens", 0)
        elif "prompt_cache_hit_tokens" in usage:
            cached_tokens = usage.get("prompt_cache_hit_tokens", 0)
            
        completion_details = usage.get("completion_tokens_details", {})
        if isinstance(completion_details, dict):
            reasoning_tokens = completion_details.get("reasoning_tokens", 0)
    else:
        token_source = "fallback"
        prompt_tokens = estimate_tokens(question)
        completion_tokens = estimate_tokens(answer)
        total_tokens = prompt_tokens + completion_tokens

    return {
        "success": True,
        "model": model_config["name"],
        "actual_model": actual_model,
        "system_fingerprint": system_fingerprint,
        "answer": answer,
        "full_answer": full_answer,
        "summary": summary,
        "time": elapsed,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "reasoning_tokens": reasoning_tokens,
        "finish_reason": finish_reason,
        "token_source": token_source
    }

def run_model(session_id, model_config, delay_seconds=0):
    if delay_seconds > 0:
        time.sleep(delay_seconds)

    session = SESSION_CACHE.get(session_id)
    if not session:
        return

    model_name = model_config["name"]
    if session.get("model_status", {}).get(model_name) != "pending":
        return

    try:
        result = request_ai(model_config, session["question"])
    except Exception as e:
        result = {
            "success": False,
            "model": model_name,
            "error": str(e),
            "time": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
            "finish_reason": "error",
            "token_source": "unknown"
        }

    with LOCK:
        if session["model_status"].get(model_name) == "pending":
            session["model_status"][model_name] = "completed"
            session["answers"].append(result)
            session["completed"] += 1

@ai_bp.route("/start", methods=["POST"])
def start():
    data = request.json
    question = data.get("question", "").strip()
    fast_mode = data.get("fast_mode", False)
    cached_answers = data.get("cached_answers", [])

    if not question:
        return jsonify({"success": False, "error": "question empty"})

    models = AI_MODELS_FAST if fast_mode else AI_MODELS
    session_id = str(uuid.uuid4())

    cached_map = {c["model"]: c for c in cached_answers if c.get("success")}

    SESSION_CACHE[session_id] = {
        "question": question,
        "fast_mode": fast_mode,
        "answers": [],
        "completed": 0,
        "returned": 0,
        "total": len(models),
        "model_status": {m["name"]: "pending" for m in models},
        "created_at": time.time()
    }

    base_url_delays = {}

    for model in models:
        model_name = model["name"]
        url = model["base_url"]
        
        if model_name in cached_map:
            SESSION_CACHE[session_id]["model_status"][model_name] = "completed"
            SESSION_CACHE[session_id]["answers"].append(cached_map[model_name])
            SESSION_CACHE[session_id]["completed"] += 1
            continue

        current_delay = base_url_delays.get(url, 0)
        jitter = random.uniform(0.1, 0.5)
        
        executor.submit(run_model, session_id, model, current_delay + jitter)
        base_url_delays[url] = current_delay + random.uniform(0.5, 1.5)

    return jsonify({
        "success": True,
        "session_id": session_id,
        "models": [m["name"] for m in models]
    })

@ai_bp.route("/abort", methods=["POST"])
def abort():
    data = request.json
    session_id = data.get("session_id")
    model_name = data.get("model")

    session = SESSION_CACHE.get(session_id)
    if not session:
        return jsonify({"success": False, "error": "invalid session"})

    with LOCK:
        if session["model_status"].get(model_name) == "pending":
            session["model_status"][model_name] = "aborted"
            session["answers"].append({
                "success": False,
                "model": model_name,
                "error": "已由用户手动终止",
                "time": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cached_tokens": 0,
                "reasoning_tokens": 0,
                "finish_reason": "abort",
                "token_source": "unknown"
            })
            session["completed"] += 1

    return jsonify({"success": True})

@ai_bp.route("/poll", methods=["POST"])
def poll():
    data = request.json
    session_id = data.get("session_id")
    session = SESSION_CACHE.get(session_id)

    if not session:
        return jsonify({"success": False, "error": "invalid session"})

    returned = session["returned"]
    answers = session["answers"]
    new_answers = answers[returned:]
    session["returned"] = len(answers)

    return jsonify({
        "success": True,
        "answers": new_answers,
        "completed": session["completed"],
        "total": session["total"],
        "finished": session["completed"] >= session["total"]
    })

def extract_arbiter_json(text):
    block_match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
    if block_match:
        text_to_parse = block_match.group(1)
    else:
        text_to_parse = text
    match = re.search(r'\{.*\}', text_to_parse, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except:
            pass
    return None

@ai_bp.route("/final", methods=["POST"])
def final():
    data = request.json or {}
    session_id = data.get("session_id")
    session = SESSION_CACHE.get(session_id)

    if not session:
        return jsonify({"success": False, "error": "invalid session"}), 404

    valid_answers = [x for x in session.get("answers", []) if x.get("success")]
    if not valid_answers:
        return jsonify({"success": False, "error": "all models failed"}), 500

    models_list = AI_MODELS_FAST if session.get("fast_mode") else AI_MODELS
    arbiter_configs = [m for m in models_list if m.get("is_arbiter")]
    arbiter_config = arbiter_configs[0] if arbiter_configs else ARBITER_MODEL

    GREEKS = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta", "Theta", "Iota", "Kappa", "Lambda", "Mu"]
    alias_to_model = {}
    model_to_alias = {}
    
    total_time = 0
    total_tokens = 0
    summary_text = ""

    for i, item in enumerate(valid_answers):
        alias = GREEKS[i % len(GREEKS)]
        alias_to_model[alias] = item["model"]
        model_to_alias[item["model"]] = alias
        item["alias"] = alias
        
        total_time += item.get("time", 0)
        total_tokens += item.get("total_tokens", 0)
        summary_text += f"模型代号: {alias}\n摘要内容:\n{item.get('summary', '无摘要')}\n\n"

    phase1_prompt = f"""你是一个匿名仲裁系统。以下是多个AI模型(代号Alpha, Beta等)对用户问题的核心观点摘要。

用户原始问题: {session['question']}

各模型摘要列表:
{summary_text}

你的任务是：基于以上摘要，决定是否需要查看某些模型的全文才能做出最终仲裁。
如果你认为基于摘要已经能够给出全面且深度的结论，请直接在 final_answer 字段输出完整的仲裁报告。

请严格输出一个 JSON 对象，不要包含其他无关内容和解释：
{{
    "need_details": true 或 false,
    "required_models": ["Alpha", "Gamma"], 
    "reason": "简述需要看全文的原因，或不需要的原因",
    "final_answer": "如果 need_details 为 false，请在这里直接输出最终完整的仲裁报告（必须包含：共识结论、关键分歧、优缺点点评、发现的错误与幻觉、最终综合结论，支持换行符 \\n）。如果为 true，此字段留空。"
}}"""

    try:
        res1 = request_ai(arbiter_config, phase1_prompt, is_arbitration=True)
        parsed_json = extract_arbiter_json(res1["answer"])
        
        final_text = ""
        details_fetched_alias = []
        
        if parsed_json and isinstance(parsed_json, dict):
            need_details = parsed_json.get("need_details", True)
            
            # 第一轮直接给出了最终仲裁结论
            if not need_details and parsed_json.get("final_answer"):
                final_text = parsed_json.get("final_answer")
            else:
                # 第一轮认为摘要不够，发起第二轮长文本请求
                req_models = parsed_json.get("required_models", [])
                details_fetched_alias = req_models
                detail_text = ""
                for ans in valid_answers:
                    if ans["alias"] in req_models:
                        detail_text += f"模型代号: {ans['alias']}\n全文内容:\n{ans.get('full_answer', '')}\n\n"
                        
                phase2_prompt = f"""你请求了以下模型的全文:
{detail_text}

请结合之前你看到的摘要，给出最终的综合仲裁结果。
用户原始问题: {session['question']}

请按以下格式输出最终仲裁报告：
# 共识结论
# 关键分歧
# 模型点评 (使用代号，如Alpha认为...)
# 发现的错误与幻觉
# 最终综合结论 (明确表达判断，不要简单折中)
# 保留的少数派观点 (若无则不写)

直接输出最终的 Markdown 文本结果即可。"""
                res2 = request_ai(arbiter_config, phase2_prompt, is_arbitration=True)
                final_text = res2["answer"]
        else:
            # 降级：如果模型没有输出标准 JSON，直接要求聚合
            phase2_prompt = f"请综合以下各个模型的摘要回答给出最终仲裁结论。保留少数派、识别幻觉，使用代号指代模型:\n{summary_text}"
            res2 = request_ai(arbiter_config, phase2_prompt, is_arbitration=True)
            final_text = res2["answer"]

        # 将匿名化后的代号替换回前端使用的真实模型名
        for alias, real_name in alias_to_model.items():
            final_text = final_text.replace(alias, real_name)
            
        details_fetched_real = [alias_to_model.get(a, a) for a in details_fetched_alias]

        return jsonify({
            "success": True,
            "answer": final_text,
            "time": res1["time"] + (res2["time"] if 'res2' in locals() else 0),
            "tokens": res1["total_tokens"] + (res2["total_tokens"] if 'res2' in locals() else 0),
            "token_source": "arbitration",
            "source_model_count": len(valid_answers),
            "source_total_tokens": total_tokens,
            "source_total_time": round(total_time, 2),
            "participating_models": list(model_to_alias.keys()),
            "details_fetched": details_fetched_real
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500