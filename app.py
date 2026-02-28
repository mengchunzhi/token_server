import shutil
import requests
import re
import os
import json
import base64
import secrets
import uuid
from functools import wraps
from flask import Flask, jsonify, request, send_from_directory, render_template_string, session, redirect, url_for, abort
from flask_cors import CORS
import werkzeug.utils
from werkzeug.exceptions import HTTPException

# === 适配Render部署的基础配置 ===
app = Flask(__name__)
# 从环境变量获取secret_key，适配Render部署
app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(24))
# Render的端口通过环境变量PORT获取
PORT = int(os.environ.get('PORT', 5000))
# 配置CORS，适配Render的跨域
CORS(app, supports_credentials=True)
# 基础路径配置
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
BIN_DIR = os.path.join(BASE_DIR, 'bin')
# 文件元数据存储（记录固定ID、文件名、用户、更新时间、备注）
METADATA_FILE = os.path.join(BASE_DIR, 'file_metadata.json')

# 确保目录存在
for dir_path in [BIN_DIR, os.path.join(BIN_DIR, 'temp')]:
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)

# === 元数据管理（固定链接+备注核心） ===
def load_metadata():
    """加载文件元数据（固定ID+备注映射）"""
    default_metadata = {}
    if os.path.exists(METADATA_FILE):
        try:
            with open(METADATA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"加载元数据失败: {e}")
            return default_metadata
    return default_metadata

def save_metadata(metadata):
    """保存文件元数据"""
    try:
        with open(METADATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"保存元数据失败: {e}")
        raise

def get_file_fixed_id(username, filename):
    """获取文件的固定ID（不存在则创建）"""
    metadata = load_metadata()
    # 构建用户-文件唯一键
    user_file_key = f"{username}_{filename}"
    if user_file_key not in metadata:
        # 生成永久固定ID（UUID4）
        fixed_id = str(uuid.uuid4())
        metadata[user_file_key] = {
            "fixed_id": fixed_id,
            "username": username,
            "filename": filename,
            "note": "",  # 新增：备注字段，默认空
            "update_time": os.path.getmtime(get_safe_path(username, filename)) if os.path.exists(get_safe_path(username, filename)) else os.time()
        }
        save_metadata(metadata)
    return metadata[user_file_key]["fixed_id"]

def get_filename_by_fixed_id(fixed_id):
    """通过固定ID反向查找文件名和用户"""
    metadata = load_metadata()
    for key, info in metadata.items():
        if info["fixed_id"] == fixed_id:
            return info["username"], info["filename"]
    return None, None

def update_file_note(username, filename, new_note):
    """更新文件备注"""
    metadata = load_metadata()
    user_file_key = f"{username}_{filename}"
    if user_file_key in metadata:
        metadata[user_file_key]["note"] = new_note.strip()
        save_metadata(metadata)
        return True
    return False

def get_file_note(username, filename):
    """获取文件备注"""
    metadata = load_metadata()
    user_file_key = f"{username}_{filename}"
    return metadata.get(user_file_key, {}).get("note", "")

# === 配置管理 ===
def load_config():
    default_config = {
        "users": {
            "admin": {
                "password": os.environ.get('ADMIN_PASSWORD', 'admin123'),  # 优先环境变量
                "token": secrets.token_hex(16)
            }
        }
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if "username" in data:
                    token = secrets.token_hex(16)
                    new_config = {
                        "users": {
                            data["username"]: {
                                "password": data.get("password", "password"),
                                "token": token
                            }
                        }
                    }
                    save_config(new_config)
                    return new_config
                return data
        except Exception as e:
            print(f"加载配置失败: {e}")
            return default_config
    else:
        save_config(default_config)
    return default_config

def save_config(config_data):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"保存配置失败: {e}")
        raise

config = load_config()

# === 装饰器 & 工具函数 ===
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def get_user_bin_dir(username):
    """获取用户专属bin目录"""
    user_dir = os.path.join(BIN_DIR, username)
    if not os.path.exists(user_dir):
        os.makedirs(user_dir)
    return user_dir

def get_safe_path(username, filename):
    """生成安全的用户文件路径"""
    safe_filename = werkzeug.utils.secure_filename(filename)
    return os.path.join(get_user_bin_dir(username), safe_filename)

def extract_target_chars(text):
    pattern = r'[^a-zA-Z0-9+=?/]'
    return re.sub(pattern, '', text)

def extract_token_content(result_str):
    token_start = result_str.find('Token')
    if token_start == -1:
        return "未找到'Token'关键词"
    roleid_start = result_str.find('roleId')
    if roleid_start == -1:
        return "未找到'roleId'关键词"
    token_end = token_start + len('Token')
    if token_end >= roleid_start:
        return "token和roleId位置重叠或顺序不正确"
    return result_str[token_end:roleid_start]

def encode_to_base64(input_string):
    input_bytes = input_string.encode('utf-8')
    encoded_bytes = base64.b64encode(input_bytes)
    return encoded_bytes.decode('utf-8')

def decode_from_base64(encoded_string):
    try:
        encoded_bytes = encoded_string.encode('utf-8')
        decoded_bytes = base64.b64decode(encoded_bytes)
        return decoded_bytes.decode('utf-8')
    except Exception:
        return "Base64解码失败"

# === 核心路由 ===
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        
        current_config = load_config()
        users = current_config.get('users', {})
        
        if username in users and users[username]['password'] == password:
            session['logged_in'] = True
            session['username'] = username
            return redirect(url_for('index'))
        else:
            error = '无效的凭证'
    
    return render_template_string('''
<!DOCTYPE html>
<html>
<head>
    <title>登录</title>
    <style>
        body { font-family: Arial, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; background-color: #f4f4f4; margin: 0; }
        .login-container { background: white; padding: 30px; border-radius: 5px; box-shadow: 0 0 10px rgba(0,0,0,0.1); width: 300px; }
        input { display: block; margin: 10px 0; padding: 10px; width: 100%; border: 1px solid #ddd; border-radius: 3px; box-sizing: border-box; }
        button { width: 100%; padding: 10px; background-color: #007bff; color: white; border: none; border-radius: 3px; cursor: pointer; margin-top: 10px; }
        button.secondary { background-color: #6c757d; }
        button:hover { opacity: 0.9; }
        .error { color: red; margin-bottom: 10px; font-size: 0.9em; text-align: center; }
        h2 { text-align: center; margin-bottom: 20px; }
    </style>
</head>
<body>
    <div class="login-container">
        <h2>登录</h2>
        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}
        <form method="post">
            <input type="text" name="username" placeholder="用户名" required>
            <input type="password" name="password" placeholder="密码" required>
            <button type="submit">登录</button>
        </form>
        <button class="secondary" onclick="window.location.href='/register'">注册新账号</button>
    </div>
</body>
</html>
    ''', error=error)

@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        
        if not username or not password:
            error = '用户名和密码不能为空'
        else:
            current_config = load_config()
            users = current_config.get('users', {})
            
            if username in users:
                error = '用户名已存在'
            else:
                users[username] = {
                    "password": password,
                    "token": secrets.token_hex(16)
                }
                current_config['users'] = users
                save_config(current_config)
                get_user_bin_dir(username)
                return redirect(url_for('login'))
    
    return render_template_string('''
<!DOCTYPE html>
<html>
<head>
    <title>注册</title>
    <style>
        body { font-family: Arial, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; background-color: #f4f4f4; margin: 0; }
        .login-container { background: white; padding: 30px; border-radius: 5px; box-shadow: 0 0 10px rgba(0,0,0,0.1); width: 300px; }
        input { display: block; margin: 10px 0; padding: 10px; width: 100%; border: 1px solid #ddd; border-radius: 3px; box-sizing: border-box; }
        button { width: 100%; padding: 10px; background-color: #28a745; color: white; border: none; border-radius: 3px; cursor: pointer; margin-top: 10px; }
        button.secondary { background-color: #6c757d; }
        button:hover { opacity: 0.9; }
        .error { color: red; margin-bottom: 10px; font-size: 0.9em; text-align: center; }
        h2 { text-align: center; margin-bottom: 20px; }
    </style>
</head>
<body>
    <div class="login-container">
        <h2>注册新账号</h2>
        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}
        <form method="post">
            <input type="text" name="username" placeholder="用户名" required>
            <input type="password" name="password" placeholder="密码" required>
            <button type="submit">注册</button>
        </form>
        <button class="secondary" onclick="window.location.href='/login'">返回登录</button>
    </div>
</body>
</html>
    ''', error=error)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    session.pop('username', None)
    return redirect(url_for('login'))

@app.route('/delete_account', methods=['POST'])
@login_required
def delete_account():
    username = session.get('username')
    if username == 'admin':
        return jsonify({"error": "无法删除管理员账号"}), 403
    
    # 删除用户配置
    current_config = load_config()
    if username in current_config['users']:
        del current_config['users'][username]
        save_config(current_config)
    
    # 删除用户文件和元数据
    user_dir = get_user_bin_dir(username)
    if os.path.exists(user_dir):
        shutil.rmtree(user_dir)
    metadata = load_metadata()
    to_delete = [k for k in metadata if metadata[k]['username'] == username]
    for k in to_delete:
        del metadata[k]
    save_metadata(metadata)
    
    # 登出
    session.pop('logged_in', None)
    session.pop('username', None)
    return jsonify({"message": "账号已删除"})

@app.route('/change_password', methods=['POST'])
@login_required
def change_password():
    username = session.get('username')
    data = request.get_json()
    new_password = data.get('new_password', '').strip()
    
    if not new_password:
        return jsonify({"error": "新密码不能为空"}), 400
        
    current_config = load_config()
    if username in current_config['users']:
        current_config['users'][username]['password'] = new_password
        save_config(current_config)
        return jsonify({"message": "密码修改成功"})
    return jsonify({"error": "用户不存在"}), 404

# === 新增：更新文件备注API ===
@app.route('/api/update_note/<filename>', methods=['POST'])
@login_required
def update_note(filename):
    """更新文件备注"""
    username = session.get('username')
    data = request.get_json()
    new_note = data.get('note', '').strip()
    
    success = update_file_note(username, filename, new_note)
    if success:
        return jsonify({"message": "备注更新成功"})
    else:
        return jsonify({"error": "文件不存在或备注更新失败"}), 404

# === 固定链接访问Token文件 ===
@app.route('/token/<fixed_id>')
def access_token_by_fixed_id(fixed_id):
    """通过固定ID访问Token文件（核心固定链接功能）"""
    username, filename = get_filename_by_fixed_id(fixed_id)
    if not username or not filename:
        abort(404, "文件不存在或已被删除")
    
    file_path = get_safe_path(username, filename)
    if not os.path.exists(file_path):
        abort(404, "文件不存在")
    
    # 返回文件内容（适配二进制/文本）
    try:
        with open(file_path, 'rb') as f:
            content = f.read()
        return content, 200, {
            'Content-Type': 'application/octet-stream',
            'Cache-Control': 'no-cache'  # 禁用缓存，确保获取最新内容
        }
    except Exception as e:
        abort(500, f"读取文件失败: {str(e)}")

# === 更新Token文件内容/替换文件 ===
@app.route('/api/update_file/<filename>', methods=['POST'])
@login_required
def update_file(filename):
    """更新Token文件（支持内容替换/文件替换）"""
    username = session.get('username')
    file_path = get_safe_path(username, filename)
    
    if not os.path.exists(file_path):
        return jsonify({"error": "文件不存在"}), 404
    
    # 两种更新方式：1. 上传新文件 2. 直接编辑文本内容
    if 'file' in request.files:
        # 文件替换
        new_file = request.files['file']
        if not new_file.filename.endswith('.bin'):
            return jsonify({"error": "仅支持.bin文件"}), 400
        
        # 覆盖原文件
        new_file.save(file_path)
        # 更新元数据时间
        metadata = load_metadata()
        user_file_key = f"{username}_{filename}"
        if user_file_key in metadata:
            metadata[user_file_key]['update_time'] = os.time()
            save_metadata(metadata)
        
        return jsonify({"message": "文件更新成功"})
    
    elif 'content' in request.json:
        # 文本内容更新（适用于文本型bin文件）
        content = request.json['content']
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            # 更新元数据时间
            metadata = load_metadata()
            user_file_key = f"{username}_{filename}"
            if user_file_key in metadata:
                metadata[user_file_key]['update_time'] = os.time()
                save_metadata(metadata)
            return jsonify({"message": "文件内容更新成功"})
        except Exception as e:
            return jsonify({"error": f"写入内容失败: {str(e)}"}), 500
    
    else:
        return jsonify({"error": "请提供文件或文本内容"}), 400

# === API：文件管理（包含备注） ===
@app.route('/api/files')
@login_required
def list_files():
    """列出用户所有文件（含固定链接+备注）"""
    username = session.get('username')
    user_dir = get_user_bin_dir(username)
    files = []
    
    if os.path.exists(user_dir):
        for filename in os.listdir(user_dir):
            if filename.endswith('.bin'):
                # 获取固定ID和固定链接
                fixed_id = get_file_fixed_id(username, filename)
                fixed_url = f"{request.host_url}token/{fixed_id}"
                # 获取文件大小、更新时间、备注
                file_path = get_safe_path(username, filename)
                file_size = os.path.getsize(file_path) / 1024  # KB
                update_time = os.path.getmtime(file_path)
                note = get_file_note(username, filename)
                
                files.append({
                    "filename": filename,
                    "note": note,
                    "fixed_id": fixed_id,
                    "fixed_url": fixed_url,
                    "file_size": round(file_size, 2),
                    "update_time": update_time,
                    "url": f"/token/{fixed_id}"
                })
    
    return jsonify(files)

@app.route('/api/upload', methods=['POST'])
@login_required
def upload_files():
    """批量上传bin文件"""
    username = session.get('username')
    if 'files' not in request.files:
        return jsonify({"error": "未选择文件"}), 400
    
    files = request.files.getlist('files')
    uploaded_count = 0
    
    for file in files:
        if file.filename == '':
            continue
        if file.filename.endswith('.bin'):
            filename = werkzeug.utils.secure_filename(file.filename)
            file_path = get_safe_path(username, filename)
            file.save(file_path)
            # 生成固定ID（自动写入元数据）
            get_file_fixed_id(username, filename)
            uploaded_count += 1
    
    return jsonify({"uploaded_count": uploaded_count, "message": "上传完成"})

@app.route('/api/files/<filename>', methods=['DELETE'])
@login_required
def delete_file(filename):
    """删除文件（含元数据）"""
    username = session.get('username')
    file_path = get_safe_path(username, filename)
    
    if not os.path.exists(file_path):
        return jsonify({"error": "文件不存在"}), 404
    
    try:
        os.remove(file_path)
        # 删除元数据
        metadata = load_metadata()
        user_file_key = f"{username}_{filename}"
        if user_file_key in metadata:
            del metadata[user_file_key]
            save_metadata(metadata)
        return jsonify({"message": "文件已删除"})
    except Exception as e:
        return jsonify({"error": f"删除失败: {str(e)}"}), 500

# === 主页面（新增备注功能） ===
@app.route('/')
@login_required
def index():
    return render_template_string('''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Token URL & Bin 文件管理</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 1000px; margin: 0 auto; padding: 20px; }
        h1 { text-align: center; }
        .header-controls { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 10px; }
        .upload-section { margin-bottom: 20px; padding: 15px; border: 1px solid #ddd; border-radius: 5px; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { border: 1px solid #ddd; padding: 12px; text-align: left; }
        th { background-color: #f2f2f2; }
        button { cursor: pointer; padding: 6px 12px; background-color: #007bff; color: white; border: none; border-radius: 3px; }
        button.delete { background-color: #dc3545; }
        button.update { background-color: #ffc107; color: black; }
        button.save-note { background-color: #28a745; font-size: 0.8em; margin-left: 5px; }
        button.copy { background-color: #28a745; font-size: 0.8em; margin-left: 5px; }
        button.logout { background-color: #6c757d; }
        button.delete-account { background-color: #dc3545; margin-right: 10px; }
        button.change-password { background-color: #ffc107; color: black; margin-right: 10px; }
        button:hover { opacity: 0.8; }
        .url-cell { word-break: break-all; font-family: monospace; font-size: 0.9em; }
        .fixed-url { color: #28a745; font-weight: 600; }
        .note-input { padding: 6px; width: 200px; border: 1px solid #ddd; border-radius: 3px; }
        
        /* Modal Styles */
        .modal { display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; overflow: auto; background-color: rgba(0,0,0,0.4); }
        .modal-content { background-color: #fefefe; margin: 8% auto; padding: 20px; border: 1px solid #888; width: 90%; max-width: 500px; border-radius: 5px; }
        .close { color: #aaa; float: right; font-size: 28px; font-weight: bold; cursor: pointer; }
        .close:hover { color: black; }
        .modal input, .modal textarea { width: 100%; padding: 10px; margin: 10px 0; display: inline-block; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; }
        .modal textarea { min-height: 200px; resize: vertical; }
        .modal-buttons { display: flex; gap: 10px; margin-top: 15px; }
        .modal-buttons button { flex: 1; }
        .file-size { font-size: 0.85em; color: #666; }
    </style>
</head>
<body>
    <div class="header-controls">
        <div>
            <span>欢迎, {{ session['username'] }}</span>
        </div>
        <div>
            <button class="change-password" onclick="openPasswordModal()">修改密码</button>
            {% if session['username'] != 'admin' %}
            <button class="delete-account" onclick="deleteAccount()">注销账号</button>
            {% endif %}
            <button class="logout" onclick="window.location.href='/logout'">退出登录</button>
        </div>
    </div>
    <h1>Token URL & Bin 文件管理</h1>
    
    <div class="upload-section">
        <h3>批量上传 bin 文件</h3>
        <input type="file" id="fileInput" accept=".bin" multiple>
        <button onclick="uploadFile()">上传</button>
        <p style="margin-top: 10px; color: #666;">上传后的文件将生成<b>永久固定链接</b>，更新文件内容/备注链接不变</p>
    </div>

    <table id="fileTable">
        <thead>
            <tr>
                <th>文件名</th>
                <th>备注名</th>
                <th>文件大小(KB)</th>
                <th>固定Token链接</th>
                <th>操作</th>
            </tr>
        </thead>
        <tbody>
            <!-- 文件列表将在这里生成 -->
        </tbody>
    </table>

    <!-- 密码修改弹窗 -->
    <div id="passwordModal" class="modal">
        <div class="modal-content">
            <span class="close" onclick="closePasswordModal()">&times;</span>
            <h3>修改密码</h3>
            <input type="password" id="newPassword" placeholder="输入新密码">
            <button onclick="changePassword()">确认修改</button>
        </div>
    </div>

    <!-- 文件更新弹窗 -->
    <div id="updateFileModal" class="modal">
        <div class="modal-content">
            <span class="close" onclick="closeUpdateFileModal()">&times;</span>
            <h3>更新文件：<span id="updateFilename"></span></h3>
            <div style="margin: 15px 0;">
                <p>方式1：上传新的.bin文件覆盖（推荐）</p>
                <input type="file" id="updateFileInput" accept=".bin">
            </div>
            <div style="margin: 15px 0;">
                <p>方式2：直接编辑文件内容（文本格式）</p>
                <textarea id="updateFileContent" placeholder="输入文件内容..."></textarea>
            </div>
            <div class="modal-buttons">
                <button onclick="submitFileUpdate()">确认更新</button>
                <button onclick="closeUpdateFileModal()" style="background-color: #6c757d;">取消</button>
            </div>
        </div>
    </div>

    <script>
        const API_BASE = '';
        let currentUpdateFilename = '';

        // 密码弹窗控制
        function openPasswordModal() {
            document.getElementById('passwordModal').style.display = "block";
        }
        function closePasswordModal() {
            document.getElementById('passwordModal').style.display = "none";
            document.getElementById('newPassword').value = "";
        }

        // 文件更新弹窗控制
        function openUpdateFileModal(filename) {
            currentUpdateFilename = filename;
            document.getElementById('updateFilename').textContent = filename;
            document.getElementById('updateFileInput').value = "";
            document.getElementById('updateFileContent').value = "";
            // 预加载文件内容（可选）
            fetch(`/token/${getFixedIdByFilename(filename)}`)
                .then(res => res.text())
                .then(content => {
                    document.getElementById('updateFileContent').value = content;
                })
                .catch(err => console.log('预加载内容失败:', err));
            document.getElementById('updateFileModal').style.display = "block";
        }
        function closeUpdateFileModal() {
            document.getElementById('updateFileModal').style.display = "none";
            currentUpdateFilename = '';
        }

        // 点击空白处关闭弹窗
        window.onclick = function(event) {
            if (event.target.classList.contains('modal')) {
                event.target.style.display = "none";
            }
        }

        // 修改密码
        async function changePassword() {
            const newPassword = document.getElementById('newPassword').value.trim();
            if (!newPassword) {
                alert("请输入新密码");
                return;
            }
            try {
                const response = await fetch(`${API_BASE}/change_password`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ new_password: newPassword })
                });
                const result = await response.json();
                if (response.ok) {
                    alert(result.message);
                    closePasswordModal();
                } else {
                    alert('修改失败: ' + (result.error || '未知错误'));
                }
            } catch (error) {
                console.error('修改失败:', error);
                alert('修改出错');
            }
        }

        // 注销账号
        async function deleteAccount() {
            if (!confirm('确定要注销账号吗？此操作将永久删除您的账号及所有上传的文件，且不可恢复！')) return;
            try {
                const response = await fetch(`${API_BASE}/delete_account`, { method: 'POST' });
                if (response.ok) {
                    alert('账号已注销');
                    window.location.href = '/login';
                } else {
                    const result = await response.json();
                    alert('注销失败: ' + (result.error || '未知错误'));
                }
            } catch (error) {
                console.error('注销失败:', error);
                alert('注销出错');
            }
        }

        // 保存文件备注
        async function saveFileNote(filename) {
            const noteInput = document.getElementById(`note-${filename}`);
            const newNote = noteInput.value.trim();
            
            try {
                const response = await fetch(`${API_BASE}/api/update_note/${filename}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ note: newNote })
                });
                const result = await response.json();
                if (response.ok) {
                    alert(result.message);
                    loadFiles(); // 刷新列表
                } else {
                    alert('备注保存失败: ' + (result.error || '未知错误'));
                }
            } catch (error) {
                console.error('保存备注失败:', error);
                alert('保存备注出错');
            }
        }

        // 加载文件列表
        async function loadFiles() {
            try {
                const response = await fetch(`${API_BASE}/api/files`);
                if (response.status === 401 || response.url.includes('/login')) {
                    window.location.href = '/login';
                    return;
                }
                const files = await response.json();
                const tbody = document.querySelector('#fileTable tbody');
                tbody.innerHTML = '';
                
                files.forEach(file => {
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td>${file.filename}</td>
                        <td>
                            <input type="text" id="note-${file.filename}" class="note-input" 
                                   placeholder="输入备注名（选填）" value="${file.note || ''}">
                            <button class="save-note" onclick="saveFileNote('${file.filename}')">保存</button>
                        </td>
                        <td>${file.file_size} <span class="file-size">(更新于: ${new Date(file.update_time * 1000).toLocaleString()})</span></td>
                        <td class="url-cell">
                            <a href="${file.fixed_url}" target="_blank" class="fixed-url">${file.fixed_url}</a>
                            <button class="copy" onclick="copyToClipboard('${file.fixed_url}')">复制</button>
                        </td>
                        <td>
                            <button class="update" onclick="openUpdateFileModal('${file.filename}')">更新</button>
                            <button class="delete" onclick="deleteFile('${file.filename}')">删除</button>
                        </td>
                    `;
                    tbody.appendChild(tr);
                });
            } catch (error) {
                console.error('加载文件失败:', error);
            }
        }

        // 上传文件
        async function uploadFile() {
            const fileInput = document.getElementById('fileInput');
            const files = fileInput.files;
            if (files.length === 0) {
                alert('请选择至少一个文件');
                return;
            }
            const formData = new FormData();
            let validCount = 0;
            for (let i = 0; i < files.length; i++) {
                if (files[i].name.endsWith('.bin')) {
                    formData.append('files', files[i]);
                    validCount++;
                }
            }
            if (validCount === 0) {
                alert('只能上传 .bin 文件');
                return;
            }
            try {
                const response = await fetch(`${API_BASE}/api/upload`, {
                    method: 'POST',
                    body: formData
                });
                const result = await response.json();
                if (response.ok) {
                    alert(`成功上传 ${result.uploaded_count} 个文件`);
                    fileInput.value = '';
                    loadFiles();
                } else {
                    if (response.status === 401) window.location.href = '/login';
                    else alert('上传失败: ' + (result.error || '未知错误'));
                }
            } catch (error) {
                console.error('上传失败:', error);
                alert('上传出错');
            }
        }

        // 删除文件
        async function deleteFile(filename) {
            if (!confirm(`确定要删除 ${filename} 吗？`)) return;
            try {
                const response = await fetch(`${API_BASE}/api/files/${filename}`, { method: 'DELETE' });
                if (response.ok) {
                    alert('文件已删除');
                    loadFiles();
                } else {
                    const result = await response.json();
                    alert('删除失败: ' + (result.error || '未知错误'));
                }
            } catch (error) {
                console.error('删除失败:', error);
                alert('删除出错');
            }
        }

        // 更新文件
        async function submitFileUpdate() {
            if (!currentUpdateFilename) return;
            const fileInput = document.getElementById('updateFileInput');
            const fileContent = document.getElementById('updateFileContent').value.trim();
            
            // 验证：必须选文件或填内容
            if (!fileInput.files.length && !fileContent) {
                alert('请上传新文件或输入文件内容');
                return;
            }

            try {
                let response;
                // 方式1：文件上传
                if (fileInput.files.length) {
                    const formData = new FormData();
                    formData.append('file', fileInput.files[0]);
                    response = await fetch(`${API_BASE}/api/update_file/${currentUpdateFilename}`, {
                        method: 'POST',
                        body: formData
                    });
                } 
                // 方式2：内容更新
                else {
                    response = await fetch(`${API_BASE}/api/update_file/${currentUpdateFilename}`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ content: fileContent })
                    });
                }

                const result = await response.json();
                if (response.ok) {
                    alert(result.message);
                    closeUpdateFileModal();
                    loadFiles();
                } else {
                    alert('更新失败: ' + (result.error || '未知错误'));
                }
            } catch (error) {
                console.error('更新失败:', error);
                alert('更新出错');
            }
        }

        // 复制到剪贴板
        function copyToClipboard(text) {
            navigator.clipboard.writeText(text)
                .then(() => alert('链接已复制到剪贴板'))
                .catch(() => {
                    // 降级方案
                    const textArea = document.createElement('textarea');
                    textArea.value = text;
                    document.body.appendChild(textArea);
                    textArea.select();
                    document.execCommand('copy');
                    document.body.removeChild(textArea);
                    alert('链接已复制到剪贴板');
                });
        }

        // 辅助：通过文件名找固定ID（用于预加载内容）
        function getFixedIdByFilename(filename) {
            const rows = document.querySelectorAll('#fileTable tbody tr');
            for (let row of rows) {
                if (row.cells[0].textContent === filename) {
                    const fixedUrl = row.cells[3].querySelector('a').href;
                    return fixedUrl.split('/').pop();
                }
            }
            return '';
        }

        // 页面加载时加载文件列表
        window.onload = loadFiles;
    </script>
</body>
</html>
    ''', session=session)

# === 错误处理 & 启动配置 ===
@app.errorhandler(HTTPException)
def handle_http_exception(e):
    """统一HTTP错误处理"""
    response = jsonify({"error": e.description})
    response.status_code = e.code
    return response

@app.errorhandler(Exception)
def handle_generic_exception(e):
    """统一异常处理（适配Render日志）"""
    print(f"服务器错误: {str(e)}")  # Render日志会捕获print输出
    return jsonify({"error": "服务器内部错误"}), 500

if __name__ == '__main__':
    # 适配Render部署：监听0.0.0.0，端口用环境变量，关闭调试模式
    app.run(
        host='0.0.0.0',
        port=PORT,
        debug=False  # 生产环境必须关闭debug
    )
