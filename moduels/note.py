AI_MODELS = [
     {
       "name": "step-3.5-flash op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "stepfun/step-3.5-flash",
        # $0.30 /M output tokens
        "api_key": os.getenv("OP_API_KEY"),
    },
    # {
    #     "name": "glm4.5 air si",
    #     "base_url": os.getenv("MODEL_SI_URL"),
    #     "model": "zai-org/GLM-4.5-Air",
    #     # 6.000/ M Tokens 
    #     # glm4.5 air si：备用降级/轻度雷同。该模型最大的亮点是纠正了常识性错误（明确指出 CVT 踏板无链条需检查），且流程清晰。但其在数据深度和龙骨专项分析上，分别被 step-3.5-flash op 和 qwen3.5-flash-02-23 op 压制，只能作为边缘备用。
    #     "api_key": os.getenv("SI_API_KEY"),
    # },
    {
        "name": "qwen3.5-flash-02-23 op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "qwen/qwen3.5-flash-02-23",
        # $0.26 /M output tokens
        "api_key": os.getenv("OP_API_KEY"),
    },
    {
       "name": "mimo2.5pr op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "xiaomi/mimo-v2.5-pro",
        "api_key": os.getenv("OP_API_KEY"),
    },
   
    # {
    #    "name": "gpt-4o-mini op",
    #     "base_url": os.getenv("MODEL_OP_URL"),
    #     "model": "openai/gpt-4o-mini",
    # vpn
    #     "api_key": os.getenv("OP_API_KEY"),
    # },
   
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
       "name": "owl-alpha tempfr op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "openrouter/owl-alpha",
        "api_key": os.getenv("OP_API_KEY"),
    },
    {
        "name": "minimax-m2.5fr op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "minimax/minimax-m2.5:free",
        "api_key": os.getenv("OP_API_KEY"),
    },
    # {
    #     "name": "qwen3-235b-a22b-2507 op",
    #     "base_url": os.getenv("MODEL_OP_URL"),
    #     "model": "qwen/qwen3-235b-a22b-2507",
    # 被 deepseek-v4-flash ds 和 glm4.5 airfr op 完全覆盖
    #     "api_key": os.getenv("OP_API_KEY"),
    # },
    # {
    #      "name": "mimo2.5 op",
    #     "base_url": os.getenv("MODEL_OP_URL"),
    #     "model": "xiaomi/mimo-v2.5",
    #     # $0.28 /M output tokens "hy3-preview op备用
    #     "api_key": os.getenv("OP_API_KEY"),
    # },
      {
         "name": "hy3-preview op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "tencent/hy3-preview",
        # $0.26 /M output tokens
        "api_key": os.getenv("OP_API_KEY"),
    },

    {
         "name": "gpt-oss-120b:fr op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "openai/gpt-oss-120b:free",
        "api_key": os.getenv("OP_API_KEY"),
    },
    
    {
         "name": "nemotronfr op",
        "base_url": os.getenv("MODEL_OP_URL"),
        "model": "nvidia/nemotron-3-super-120b-a12b:free",
        "api_key": os.getenv("OP_API_KEY"),
    },
  
    {
         "name": "deepseek-v4-flash ds",
        "base_url": os.getenv("MODEL_DS_URL"),
        "model": "deepseek-v4-flash",
        "api_key": os.getenv("DS_API_KEY"), 
    },
]
