import json
import requests

from flask import Blueprint, render_template, request, jsonify

import os

AI_MODELS = [
   {
        "name": "glm4.5 air silicom",
        "base_url": os.getenv("MODEL_SI_URL"),
        "model": "zai-org/GLM-4.5-Air",
        "api_key": os.getenv("SI_API_KEY"),
    },
    {
        "name": "qwen3.5-flash-02-23 silicom",
        "base_url": os.getenv("MODEL_SI_URL"),
        "model": "qwen/qwen3.5-flash-02-23",
        "api_key": os.getenv("SI_API_KEY"),
    },
    {
        "name": "mimo2.5pro",
        "base_url": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2.5-pro",
        "api_key": os.getenv("MI_API_KEY"),
    },
]

ARBITER_MODEL = {
    "name": "Arbiter dspro",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-v4-pro",
    "api_key": os.getenv("DS_API_KEY"),
}


  

ai_bp = Blueprint(
    "ai_aggregate",
    __name__,
    url_prefix="/ai"
)

# 保存本轮结果
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
        timeout=60
    )

    r.raise_for_status()

    data = r.json()

    return data["choices"][0]["message"]["content"]


@ai_bp.route("/ask", methods=["POST"])
def ask_single():

    data = request.json

    question = data.get("question")
    model_index = data.get("model_index")
    session_id = data.get("session_id")

    if session_id not in SESSION_CACHE:
        SESSION_CACHE[session_id] = []

    model_config = AI_MODELS[model_index]

    try:

        answer = request_ai(model_config, question)

        result = {
            "success": True,
            "model": model_config["name"],
            "answer": answer
        }

        SESSION_CACHE[session_id].append(result)

        return jsonify(result)

    except Exception as e:

        return jsonify({
            "success": False,
            "model": model_config["name"],
            "error": str(e)
        })


@ai_bp.route("/arbiter", methods=["POST"])
def arbiter():

    data = request.json

    session_id = data.get("session_id")
    question = data.get("question")

    answers = SESSION_CACHE.get(session_id, [])

    valid_answers = [
        x for x in answers if x["success"]
    ]

    if not valid_answers:
        return jsonify({
            "success": False,
            "error": "No valid answers"
        })

    merged_text = ""

    for item in valid_answers:

        merged_text += f"""
模型: {item['model']}

回答:
{item['answer']}

====================
"""

    final_prompt = f"""
用户问题：

{question}

下面是多个 AI 的回答：

{merged_text}

请完成：

1. 提炼共识
2. 指出分歧
3. 判断哪些观点更可信
4. 给出最终综合结论
5. 保留少数派但合理观点

输出结构化结果。
"""

    try:

        final_answer = request_ai(
            ARBITER_MODEL,
            final_prompt
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
