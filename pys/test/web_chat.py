
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
简单聊天应用 - 单文件整合版
使用端口: 18765 (不常用端口)
消息记录保存到: chat_log.txt
"""

from flask import Flask, request, jsonify, render_template_string
import json
import os
from datetime import datetime
import threading
import random
import string

app = Flask(__name__)

# 配置
PORT = 18765
LOG_FILE = "chat_log.txt"
HOST = "0.0.0.0"

# HTML模板
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>简单聊天室</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Microsoft YaHei', Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
        }
        
        .chat-container {
            background: white;
            border-radius: 15px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
            width: 100%;
            max-width: 600px;
            height: 700px;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }
        
        .chat-header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 20px;
            text-align: center;
        }
        
        .chat-header h1 {
            font-size: 24px;
            margin-bottom: 5px;
        }
        
        .chat-header p {
            font-size: 12px;
            opacity: 0.9;
        }
        
        .login-section {
            padding: 40px 30px;
            display: flex;
            flex-direction: column;
            gap: 20px;
        }
        
        .input-group {
            display: flex;
            flex-direction: column;
            gap: 8px;
        }
        
        .input-group label {
            font-weight: bold;
            color: #333;
        }
        
        .input-group input {
            padding: 12px;
            border: 2px solid #ddd;
            border-radius: 8px;
            font-size: 14px;
            transition: border-color 0.3s;
        }
        
        .input-group input:focus {
            outline: none;
            border-color: #667eea;
        }
        
        .btn {
            padding: 12px 24px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
        }
        
        .btn-random {
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
        }
        
        .chat-messages {
            flex: 1;
            padding: 20px;
            overflow-y: auto;
            background: #f8f9fa;
        }
        
        .message {
            margin-bottom: 15px;
            animation: fadeIn 0.3s ease-in;
        }
        
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }
        
        .message-header {
            display: flex;
            justify-content: space-between;
            margin-bottom: 5px;
            font-size: 12px;
            color: #666;
        }
        
        .message-username {
            font-weight: bold;
            color: #667eea;
        }
        
        .message-content {
            background: white;
            padding: 10px 15px;
            border-radius: 10px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.1);
            word-wrap: break-word;
        }
        
        .chat-input-area {
            padding: 20px;
            background: white;
            border-top: 1px solid #eee;
        }
        
        .input-row {
            display: flex;
            gap: 10px;
        }
        
        .input-row input {
            flex: 1;
            padding: 12px;
            border: 2px solid #ddd;
            border-radius: 8px;
            font-size: 14px;
        }
        
        .input-row input:focus {
            outline: none;
            border-color: #667eea;
        }
        
        .hidden {
            display: none !important;
        }
        
        .empty-message {
            text-align: center;
            color: #999;
            padding: 40px;
        }
    </style>
</head>
<body>
    <div class="chat-container">
        <div class="chat-header">
            <h1>💬 简单聊天室</h1>
            <p>端口: {{ port }} | 消息自动保存到日志</p>
        </div>
        
        <!-- 登录区域 -->
        <div id="loginSection" class="login-section">
            <div class="input-group">
                <label for="username">用户名:</label>
                <input type="text" id="username" placeholder="输入你的昵称" maxlength="20">
            </div>
            <button class="btn btn-random" onclick="generateRandomName()">🎲 随机生成名字</button>
            <button class="btn" onclick="joinChat()">加入聊天</button>
        </div>
        
        <!-- 聊天区域 -->
        <div id="chatSection" class="hidden" style="flex: 1; display: flex; flex-direction: column;">
            <div id="messages" class="chat-messages">
                <div class="empty-message">暂无消息，开始聊天吧！</div>
            </div>
            <div class="chat-input-area">
                <div class="input-row">
                    <input type="text" id="messageInput" placeholder="输入消息..." 
                           onkeypress="if(event.key==='Enter') sendMessage()">
                    <button class="btn" onclick="sendMessage()">发送</button>
                </div>
            </div>
        </div>
    </div>
    
    <script>
        let currentUser = '';
        
        // 生成随机名字
        function generateRandomName() {
            const adjectives = ['快乐', '聪明', '勇敢', '温柔', '活泼', '安静', '阳光', '神秘'];
            const nouns = ['小猫', '小狗', '星星', '月亮', '花朵', '小鸟', '鱼儿', '蝴蝶'];
            const randomAdj = adjectives[Math.floor(Math.random() * adjectives.length)];
            const randomNoun = nouns[Math.floor(Math.random() * nouns.length)];
            const randomNum = Math.floor(Math.random() * 1000);
            document.getElementById('username').value = randomAdj + randomNoun + randomNum;
        }
        
        // 加入聊天
        function joinChat() {
            const username = document.getElementById('username').value.trim();
            if (!username) {
                alert('请输入用户名！');
                return;
            }
            currentUser = username;
            document.getElementById('loginSection').classList.add('hidden');
            document.getElementById('chatSection').classList.remove('hidden');
            document.getElementById('chatSection').style.display = 'flex';
            loadMessages();
            // 每3秒刷新一次消息
            setInterval(loadMessages, 3000);
        }
        
        // 加载消息
        async function loadMessages() {
            try {
                const response = await fetch('/api/messages');
                const data = await response.json();
                displayMessages(data.messages);
            } catch (error) {
                console.error('加载消息失败:', error);
            }
        }
        
        // 显示消息
        function displayMessages(messages) {
            const messagesDiv = document.getElementById('messages');
            if (messages.length === 0) {
                messagesDiv.innerHTML = '<div class="empty-message">暂无消息，开始聊天吧！</div>';
                return;
            }
            
            messagesDiv.innerHTML = messages.map(msg => `
                <div class="message">
                    <div class="message-header">
                        <span class="message-username">${escapeHtml(msg.username)}</span>
                        <span>${msg.time}</span>
                    </div>
                    <div class="message-content">${escapeHtml(msg.content)}</div>
                </div>
            `).join('');
            
            // 滚动到底部
            messagesDiv.scrollTop = messagesDiv.scrollHeight;
        }
        
        // 发送消息
        async function sendMessage() {
            const input = document.getElementById('messageInput');
            const content = input.value.trim();
            
            if (!content) {
                return;
            }
            
            try {
                const response = await fetch('/api/send', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        username: currentUser,
                        content: content
                    })
                });
                
                const result = await response.json();
                if (result.success) {
                    input.value = '';
                    loadMessages();
                } else {
                    alert('发送失败: ' + result.error);
                }
            } catch (error) {
                console.error('发送消息失败:', error);
                alert('发送失败，请重试');
            }
        }
        
        // HTML转义
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
    </script>
</body>
</html>
"""

# 线程锁，确保文件写入安全
file_lock = threading.Lock()


def log_message(username, content):
    """将消息追加到日志文件"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {username}: {content}\n"
    
    with file_lock:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_entry)


def read_messages(limit=50):
    """读取最近的消息"""
    if not os.path.exists(LOG_FILE):
        return []
    
    messages = []
    with file_lock:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # 只取最后limit条
        recent_lines = lines[-limit:] if len(lines) > limit else lines
        
        for line in recent_lines:
            line = line.strip()
            if not line:
                continue
            
            # 解析格式: [2026-09-16 10:30:00] 用户名: 消息内容
            try:
                time_end = line.index(']')
                time_str = line[1:time_end]
                rest = line[time_end+2:]  # 跳过 "] "
                colon_idx = rest.index(': ')
                username = rest[:colon_idx]
                content = rest[colon_idx+2:]
                
                messages.append({
                    'time': time_str,
                    'username': username,
                    'content': content
                })
            except (ValueError, IndexError):
                continue
    
    return messages


@app.route('/')
def index():
    """主页"""
    return render_template_string(HTML_TEMPLATE, port=PORT)


@app.route('/api/messages', methods=['GET'])
def get_messages():
    """获取消息列表"""
    messages = read_messages()
    return jsonify({
        'success': True,
        'messages': messages
    })


@app.route('/api/send', methods=['POST'])
def send_message():
    """发送消息"""
    data = request.get_json()
    
    if not data:
        return jsonify({
            'success': False,
            'error': '无效请求'
        }), 400
    
    username = data.get('username', '').strip()
    content = data.get('content', '').strip()
    
    if not username:
        return jsonify({
            'success': False,
            'error': '用户名不能为空'
        }), 400
    
    if not content:
        return jsonify({
            'success': False,
            'error': '消息内容不能为空'
        }), 400
    
    # 记录消息
    log_message(username, content)
    
    return jsonify({
        'success': True,
        'message': '发送成功'
    })


if __name__ == '__main__':
    print(f"🚀 聊天服务器启动中...")
    print(f"📍 访问地址: http://localhost:{PORT}")
    print(f"📍 局域网访问: http://你的IP:{PORT}")
    print(f"📝 消息日志: {os.path.abspath(LOG_FILE)}")
    print(f"{'='*50}")
    
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
 
