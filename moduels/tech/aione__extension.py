
import uuid
import requests

from flask import (
    Blueprint,
    jsonify,
    render_template,
    request
)

import os

ai_bp = Blueprint(
    "ai_aggregate",
    __name__,
    url_prefix="/ai"
)

AI_MODELS = [
    {
        "name": "glm4.5 air silicom",
        "base_url": os.getenv("MODEL_SI_URL"),
        "model": "zai-org/GLM-4.5-Air",
        "api_key": os.getenv("SI_API_KEY"),
    },
    {
        "name": "qwen3.5-flash-02-23 silicom",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "qwen/qwen3.5-flash-02-23",
        "api_key": os.getenv("OP_API_KEY"),
    },
    {
        "name": "mimo2.5pro",
        "base_url": "https://api.xiaomimimo.com/v1/chat/completions",
        "model": "mimo-v2.5-pro",
        "api_key": os.getenv("MI_API_KEY"),
    },
]

AI_MODELS_fast = [
     
    {
        "name": "glm4.5 air op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "z-ai/glm-4.5-air:free",
        "api_key": os.getenv("OP_API_KEY"),
    },
    {
        "name": "qwen3-235b-a22b-2507 op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "qwen/qwen3-235b-a22b-2507",
        "api_key": os.getenv("OP_API_KEY"),
    },
    {
        "name": "mimo2.5",
        "base_url": "https://api.xiaomimimo.com/v1/chat/completions",
        "model": "mimo-v2.5",
        "api_key": os.getenv("MI_API_KEY"),
    },
]

ARBITER_MODEL = {
    "name": "Arbiter dspro",
    "base_url": "https://api.deepseek.com/chat/completions",
    "model": "deepseek-v4-pro",
    "api_key": os.getenv("DS_API_KEY"),
}

SESSION_CACHE = {}


@ai_bp.route("/")
def index():
    return render_template("ai_aggregate.html")


def request_ai(model_config, question):

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
        timeout=90
    )

    r.raise_for_status()

    data = r.json()

    return data["choices"][0]["message"]["content"]


@ai_bp.route("/start", methods=["POST"])
def start():

    data = request.json

    question = data.get("question", "").strip()

    if not question:
        return jsonify({
            "success": False,
            "error": "question empty"
        })

    session_id = str(uuid.uuid4())

    SESSION_CACHE[session_id] = {
        "question": question,
        "index": 0,
        "answers": []
    }

    return jsonify({
        "success": True,
        "session_id": session_id,
        "total": len(AI_MODELS)
    })


@ai_bp.route("/next", methods=["POST"])
def next_answer():

    data = request.json

    session_id = data.get("session_id")

    session = SESSION_CACHE.get(session_id)

    if not session:
        return jsonify({
            "success": False,
            "error": "invalid session"
        })

    index = session["index"]

    if index >= len(AI_MODELS):

        return jsonify({
            "success": True,
            "finished": True
        })

    model_config = AI_MODELS[index]

    session["index"] += 1

    try:

        answer = request_ai(
            model_config,
            session["question"]
        )

        result = {
            "success": True,
            "model": model_config["name"],
            "answer": answer
        }

        session["answers"].append(result)

        return jsonify(result)

    except Exception as e:

        result = {
            "success": False,
            "model": model_config["name"],
            "error": str(e)
        }

        session["answers"].append(result)

        return jsonify(result)


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

    merged_text = ""

    for item in valid_answers:

        merged_text += f"""

模型:
{item['model']}

回答:
{item['answer']}

====================

"""

    prompt = f"""
用户问题:

{session['question']}

以下是多个 AI 的回答:

{merged_text}

请:

1. 提炼共识
2. 分析冲突
3. 判断可信度
4. 给出最终综合结论
5. 保留合理少数派观点

输出清晰结构化结果。
"""

    try:

        final_answer = request_ai(
            ARBITER_MODEL,
            prompt
        )

        return jsonify({
            "success": True,
            "answer": final_answer
        })

    except Exception as e:

        return jsonify({
            "success": False,
            "error": str(e)
        })

