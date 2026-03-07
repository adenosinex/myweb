import socket

print("正在进行最底层的 Socket 直连测试...")
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    # 纯 TCP 握手，绕过一切代理配置
    s.connect(('192.168.31.204', 8100))
    print("✅ Socket 握手成功！")
    print("结论：Python 完全有能力连通节点！之前的错误 100% 是代理环境变量导致的。")
    s.close()
except Exception as e:
    print(f"❌ Socket 握手失败：{e}")
    print("结论：代理不是背锅侠。macOS 系统层面（防火墙或网络策略）直接把 Python 的网络请求拦截了！")