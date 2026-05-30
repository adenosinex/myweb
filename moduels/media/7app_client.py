import os
import hashlib
from flask import Flask, request, send_from_directory, jsonify
import requests

STORAGE_DIR = 'storage'
SERVER_URL = 'http://apple4.su7.dpdns.org:5000/app'  # 更改为主程序实际的服务器地址
MY_URL = 'http://15x4.su7.dpdns.org:5001'          # 本地节点的访问地址

app = Flask(__name__)
os.makedirs(STORAGE_DIR, exist_ok=True)

def calculate_sha256(file_obj):
    sha256_hash = hashlib.sha256()
    file_obj.seek(0)
    for byte_block in iter(lambda: file_obj.read(4096), b""):
        sha256_hash.update(byte_block)
    file_obj.seek(0)
    return sha256_hash.hexdigest()

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "message": "No selected file"}), 400
        
    # 计算哈希值实现内容寻址存储 (CAS)
    file_hash = calculate_sha256(file)
    
    # 用哈希值前两位做二级子目录，防止单个目录下文件过多影响文件系统性能
    sub_dir = os.path.join(STORAGE_DIR, file_hash[:2])
    os.makedirs(sub_dir, exist_ok=True)
    
    file_path = os.path.join(sub_dir, file_hash[2:])
    
    # 如果文件已存在则不重复写入，达到物理去重目的
    if not os.path.exists(file_path):
        file.save(file_path)
        
    return jsonify({
        "status": "success",
        "sha256": file_hash,
        "filename": file.filename,
        "client_url": MY_URL
    })

@app.route('/file/<sha256>', methods=['GET'])
def get_file(sha256):
    sub_dir = os.path.join(STORAGE_DIR, sha256[:2])
    filename = sha256[2:]
    # send_from_directory 默认支持 HTTP Range 请求，完美支持大文件断点续传
    return send_from_directory(sub_dir, filename, as_attachment=True, download_name=sha256)

def register_to_server():
    try:
        requests.post(f"{SERVER_URL}/register_client", json={"client_url": MY_URL}, timeout=5)
        print(f"成功注册节点至主服务器: {SERVER_URL}")
    except Exception as e:
        print(f"注册节点失败，请检查主程序是否启动: {e}")

if __name__ == '__main__':
    # 启动时自动向主服务上报
    register_to_server()
    app.run(host='0.0.0.0', port=5001)