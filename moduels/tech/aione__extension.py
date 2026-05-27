
import os
import uuid
import time
import threading
import requests

from concurrent.futures import ThreadPoolExecutor

from flask import (
    Blueprint,
    jsonify,
    render_template,
    request
)

ai_bp = Blueprint(
    "ai_aggregate",
    __name__,
    url_prefix="/ai"
)

executor = ThreadPoolExecutor(max_workers=16)

LOCK = threading.Lock()

SESSION_CACHE = {}

AI_MODELS = [
    {
        "name": "glm4.5 air si",
        "base_url": os.getenv("MODEL_SI_URL"),
        "model": "zai-org/GLM-4.5-Air",
        "api_key": os.getenv("SI_API_KEY"),
    },
    {
        "name": "qwen3.5-flash-02-23 op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "qwen/qwen3.5-flash-02-23",
        "api_key": os.getenv("OP_API_KEY"),
    },
    {
       "name": "mimo2.5pr op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "xiaomi/mimo-v2.5-pro",
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
       "name": "step-3.5-flash op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "stepfun/step-3.5-flash",
        "api_key": os.getenv("OP_API_KEY"),
    },
    {
       "name": "deepseek-v3.2 si",
        "base_url": os.getenv("MODEL_SI_URL"),
        "model": "Pro/deepseek-ai/DeepSeek-V3.2",
        "api_key": os.getenv("SI_API_KEY"),
    },
    {
       "name": "MiniMax-M2.5 si",
        "base_url": os.getenv("MODEL_SI_URL"),
        "model": "Pro/MiniMaxAI/MiniMax-M2.5",
        "api_key": os.getenv("SI_API_KEY"),
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
        "name": "minimax-m2.5fr op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "minimax/minimax-m2.5:free",
        "api_key": os.getenv("OP_API_KEY"),
    },
    {
        "name": "qwen3-235b-a22b-2507 op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "qwen/qwen3-235b-a22b-2507",
        "api_key": os.getenv("OP_API_KEY"),
    },
    {
         "name": "mimo2.5 op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "xiaomi/mimo-v2.5",
        "api_key": os.getenv("OP_API_KEY"),
    },
    {
         "name": "gpt-oss-120b:fr op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "openai/gpt-oss-120b:free",
        "api_key": os.getenv("OP_API_KEY"),
    },
    {
         "name": "laguna-m.1:fr op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "poolside/laguna-m.1:free",
        "api_key": os.getenv("OP_API_KEY"),
    },
    {
         "name": "nemotronfr op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "nvidia/nemotron-3-super-120b-a12b:free",
        "api_key": os.getenv("OP_API_KEY"),
    },
    {
         "name": "hy3-preview op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "tencent/hy3-preview",
        "api_key": os.getenv("OP_API_KEY"),
    },
    {
         "name": "deepseek-v4-flash ds",
        "base_url": os.getenv("MODEL_DS_URL"),
        "model": "deepseek-v4-flash",
        "api_key": os.getenv("DS_API_KEY"), 
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


def request_ai(model_config, question):

    start_time = time.time()

    headers = {
        "Authorization": f"Bearer {model_config['api_key']}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": model_config["model"],
        "messages": [
            {
                "role": "user",
                "content": question
            }
        ],
        "temperature": 0.7
    }

    r = requests.post(
        model_config["base_url"],
        headers=headers,
        json=payload,
        timeout=180
    )

    elapsed = round(
        time.time() - start_time,
        2
    )

    r.raise_for_status()

    data = r.json()

    answer = (
        data
        .get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )

    usage = data.get("usage", {})

    prompt_tokens = usage.get(
        "prompt_tokens",
        estimate_tokens(question)
    )

    completion_tokens = usage.get(
        "completion_tokens",
        estimate_tokens(answer)
    )

    total_tokens = usage.get(
        "total_tokens",
        prompt_tokens + completion_tokens
    )

    return {
        "success": True,
        "model": model_config["name"],
        "answer": answer,
        "time": elapsed,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens
    }


def run_model(
    session_id,
    model_config
):

    session = SESSION_CACHE.get(session_id)

    if not session:
        return

    try:

        result = request_ai(
            model_config,
            session["question"]
        )

    except Exception as e:

        result = {
            "success": False,
            "model": model_config["name"],
            "error": str(e),
            "time": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0
        }

    with LOCK:

        session["answers"].append(result)

        session["completed"] += 1


@ai_bp.route("/start", methods=["POST"])
def start():

    data = request.json

    question = (
        data.get("question", "")
        .strip()
    )

    fast_mode = data.get(
        "fast_mode",
        False
    )

    if not question:

        return jsonify({
            "success": False,
            "error": "question empty"
        })

    models = (
        AI_MODELS_FAST
        if fast_mode
        else AI_MODELS
    )

    session_id = str(uuid.uuid4())

    SESSION_CACHE[session_id] = {
        "question": question,
        "answers": [],
        "completed": 0,
        "returned": 0,
        "total": len(models),
        "created_at": time.time()
    }

    for model in models:

        executor.submit(
            run_model,
            session_id,
            model
        )

    return jsonify({
        "success": True,
        "session_id": session_id
    })


@ai_bp.route("/poll", methods=["POST"])
def poll():

    data = request.json

    session_id = data.get("session_id")

    session = SESSION_CACHE.get(session_id)

    if not session:

        return jsonify({
            "success": False,
            "error": "invalid session"
        })

    returned = session["returned"]

    answers = session["answers"]

    new_answers = answers[returned:]

    session["returned"] = len(answers)

    return jsonify({
        "success": True,
        "answers": new_answers,
        "completed": session["completed"],
        "total": session["total"],
        "finished":
            session["completed"]
            >= session["total"]
    })


@ai_bp.route("/final", methods=["POST"])
def final():

    data = request.json

    session_id = data.get("session_id")

    session = SESSION_CACHE.get(session_id)

    if not session:

        return jsonify({
            "success": False,
            "error": "invalid session"
        })

    valid_answers = [
        x for x in session["answers"]
        if x["success"]
    ]

    if not valid_answers:

        return jsonify({
            "success": False,
            "error": "all models failed"
        })

    merged = ""

    total_time = 0
    total_tokens = 0

    for item in valid_answers:

        total_time += item["time"]
        total_tokens += item["total_tokens"]

        merged += f"""

模型:
{item['model']}

耗时:
{item['time']} 秒

tokens:
{item['total_tokens']}

回答:
{item['answer']}

=====================

"""

    prompt = f"""
用户问题:

{session['question']}

以下是多个 AI 的回答:

{merged}

请完成:

1. 提炼共识
2. 分析分歧
3. 判断可信度
4. 给出最终综合结论
5. 保留合理少数派观点

输出结构化结果。
"""

    try:

        result = request_ai(
            ARBITER_MODEL,
            prompt
        )

        return jsonify({
            "success": True,
            "answer": result["answer"],
            "time": result["time"],
            "tokens": result["total_tokens"],
            "source_total_tokens": total_tokens,
            "source_total_time": round(total_time, 2)
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "error": str(e)
        })
