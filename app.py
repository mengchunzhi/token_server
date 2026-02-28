import shutil
import requests
import re
import os
import json
import base64
import secrets
from functools import wraps
from flask import Flask, jsonify, request, send_from_directory, render_template_string, session, redirect, url_for
from flask_cors import CORS
import werkzeug.utils
from uuid import uuid4

app = Flask(__name__)
# Render 部署适配：使用环境变量设置 secret_key，避免重启后失效
app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(24))
# Render 部署适配：允许跨域（Render 域名可能不同）
CORS(app, supports_credentials=True)

# ========== 核心配置（适配 Render 部署） ==========
# Render 上的目录使用绝对路径，且兼容本地开发
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
BIN_DIR = os.path.join(BASE_DIR, 'bin')
# 新增：文件元数据存储（用于固定链接、备注）
METADATA_FILE = os.path.join(BASE_DIR, 'file_metadata.json')

# 创建必要目录（Render 为只读文件系统？NO，/tmp 可写，但持久化需用 Render Disk，这里兼容）
for dir_path in [BIN_DIR]:
    if not os.path.exists(dir_path):
        os.makedirs(dir_path, exist_ok=True)

# ========== 配置加载/保存 ==========
def load_config():
    default_config = {
        "users": {
            "admin": {
                "password": "admin123",
                "token": secrets.token_hex(16)
            }
        }
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                data = json.load(f)
                # 迁移旧配置格式
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
        # 如果文件不存在，保存默认配置
        save_config(default_config)
    return default_config

def save_config(config_data):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config_data, f, indent=4)

# ========== 文件元数据管理（固定链接/备注核心） ==========
def load_file_metadata():
    """加载文件元数据（固定ID、备注、原文件名、用户）"""
    default_metadata = {"files": []}
    if os.path.exists(METADATA_FILE):
        try:
            with open(METADATA_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"加载元数据失败: {e}")
            return default_metadata
    else:
        save_file_metadata(default_metadata)
        return default_metadata

def save_file_metadata(metadata_data):
    """保存文件元数据"""
    with open(METADATA_FILE, 'w') as f:
        json.dump(metadata_data, f, indent=4)

def get_file_meta_by_id(file_id, username):
    """根据固定ID和用户获取文件元数据"""
    metadata = load_file_metadata()
    for file_meta in metadata["files"]:
        if file_meta["file_id"] == file_id and file_meta["username"] == username:
            return file_meta
    return None

def get_file_meta_by_filename(filename, username):
    """根据原文件名和用户获取文件元数据"""
    metadata = load_file_metadata()
    for file_meta in metadata["files"]:
        if file_meta["original_filename"] == filename and file_meta["username"] == username:
            return file_meta
    return None

def create_file_meta(username, original_filename):
    """创建文件元数据（生成固定ID）"""
    file_id = str(uuid4())[:8]  # 短UUID作为固定ID
    metadata = load_file_metadata()
    # 确保ID唯一
    while any(f["file_id"] == file_id for f in metadata["files"]):
        file_id = str(uuid4())[:8]
    # 新增元数据
    new_meta = {
        "file_id": file_id,
        "username": username,
        "original_filename": original_filename,
        "remark": "",  # 初始备注为空
        "update_time": os.path.getmtime(get_user_bin_path(username, original_filename))
    }
    metadata["files"].append(new_meta)
    save_file_metadata(metadata)
    return file_id

def update_file_meta(file_id, username, update_data):
    """更新文件元数据（备注/更新时间）"""
    metadata = load_file_metadata()
    for idx, file_meta in enumerate(metadata["files"]):
        if file_meta["file_id"] == file_id and file_meta["username"] == username:
            if "remark" in update_data:
                metadata["files"][idx]["remark"] = update_data["remark"]
            if "update_time" in update_data:
                metadata["files"][idx]["update_time"] = update_data["update_time"]
            save_file_metadata(metadata)
            return True
    return False

def delete_file_meta(file_id, username):
    """删除文件元数据"""
    metadata = load_file_metadata()
    new_files = [f for f in metadata["files"] if not (f["file_id"] == file_id and f["username"] == username)]
    metadata["files"] = new_files
    save_file_metadata(metadata)

# ========== 工具函数 ==========
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

def get_user_bin_path(username, filename):
    """获取用户文件的安全路径"""
    safe_filename = werkzeug.utils.secure_filename(filename)
    return os.path.join(get_user_bin_dir(username), safe_filename)

def extract_target_chars(text):
    # 保留字母(a-zA-Z)、数字(0-9)及特定符号(+=?/)
    pattern = r'[^a-zA-Z0-9+=?/]'
    return re.sub(pattern, '', text)

def extract_token_content(result_str):
    # 查找'token'的位置（注意大小写）
    token_start = result_str.find('Token')
    if token_start == -1:
        return "未找到'Token'关键词"
   
    # 查找'roleId'的位置（注意大小写）
    roleid_start = result_str.find('roleId')
    if roleid_start == -1:
        return "未找到'roleId'关键词"
   
    # 计算token结束位置（跳过'token'本身）
    token_end = token_start + len('Token')
   
    # 提取token到roleId之间的内容
    if token_end >= roleid_start:
        return "token和roleId位置重叠或顺序不正确"
    extracted_content = result_str[token_end:roleid_start]
    return extracted_content

def encode_to_base64(input_string):
    # 将字符串转换为bytes
    input_bytes = input_string.encode('utf-8')
    # 进行Base64编码
    encoded_bytes = base64.b64encode(input_bytes)
    # 将编码后的bytes转换回字符串
    encoded_string = encoded_bytes.decode('utf-8')
    return encoded_string

def decode_from_base64(encoded_string):
    encoded_bytes = encoded_string.encode('utf-8')
    decoded_bytes = base64.b64decode(encoded_bytes)
    decoded_string = decoded_bytes.decode('utf-8')
    return decoded_string

# ========== 路由 - 认证 ==========
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        # 重新加载配置
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
        username = request.form['username']
        password = request.form['password']
        
        if not username or not password:
            error = '用户名和密码不能为空'
        else:
            current_config = load_config()
            users = current_config.get('users', {})
            
            if username in users:
                error = '用户名已存在'
            else:
                # 创建新用户
                users[username] = {
                    "password": password,
                    "token": secrets.token_hex(16)
                }
                current_config['users'] = users
                save_config(current_config)
                
                # 创建用户目录
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
    
    # 防止删除管理员账号
    if username == 'admin':
        return jsonify({"error": "无法删除管理员账号"}), 403
    
    # 删除用户数据
    current_config = load_config()
    if username in current_config['users']:
        del current_config['users'][username]
        save_config(current_config)
    
    # 删除用户目录
    user_dir = get_user_bin_dir(username)
    if os.path.exists(user_dir):
        try:
            shutil.rmtree(user_dir)
        except Exception as e:
            return jsonify({"error": f"删除文件失败: {str(e)}"}), 500
    
    # 删除用户元数据
    metadata = load_file_metadata()
    metadata["files"] = [f for f in metadata["files"] if f["username"] != username]
    save_file_metadata(metadata)
            
    # 登出
    session.pop('logged_in', None)
    session.pop('username', None)
    
    return jsonify({"message": "账号已删除"})

@app.route('/change_password', methods=['POST'])
@login_required
def change_password():
    username = session.get('username')
    data = request.get_json()
    
    if not data or 'new_password' not in data:
        return jsonify({"error": "新密码不能为空"}), 400
        
    new_password = data['new_password']
    
    current_config = load_config()
    if username in current_config['users']:
        current_config['users'][username]['password'] = new_password
        save_config(current_config)
        return jsonify({"message": "密码修改成功"})
    
    return jsonify({"error": "用户不存在"}), 404

# ========== 路由 - 核心功能（文件管理） ==========
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
        th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
        th { background-color: #f2f2f2; }
        button { cursor: pointer; padding: 5px 10px; background-color: #007bff; color: white; border: none; border-radius: 3px; }
        button.delete { background-color: #dc3545; }
        button.update { background-color: #ffc107; color: black; }
        button.remark { background-color: #28a745; }
        button:hover { opacity: 0.8; }
        .url-cell { word-break: break-all; font-family: monospace; font-size: 0.9em; }
        .copy-btn { margin-left: 5px; background-color: #6c757d; font-size: 0.8em; }
        .logout-btn { background-color: #6c757d; }
        .delete-account-btn { background-color: #dc3545; margin-right: 10px; }
        .change-password-btn { background-color: #ffc107; color: black; margin-right: 10px; }
        
        /* Modal Styles */
        .modal { display: none; position: fixed; z-index: 1; left: 0; top: 0; width: 100%; height: 100%; overflow: auto; background-color: rgba(0,0,0,0.4); }
        .modal-content { background-color: #fefefe; margin: 15% auto; padding: 20px; border: 1px solid #888; width: 300px; border-radius: 5px; }
        .close { color: #aaa; float: right; font-size: 28px; font-weight: bold; cursor: pointer; }
        .close:hover, .close:focus { color: black; text-decoration: none; cursor: pointer; }
        .modal input, .modal textarea { width: 100%; padding: 10px; margin: 10px 0; display: inline-block; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; }
        .modal button { width: 100%; background-color: #4CAF50; color: white; padding: 14px 20px; margin: 8px 0; border: none; border-radius: 4px; cursor: pointer; }
        .modal button:hover { background-color: #45a049; }
        .remark-input { width: 100%; resize: none; height: 80px; }
    </style>
</head>
<body>
    <div class="header-controls">
        <div>
            <span>欢迎, {{ session['username'] }}</span>
        </div>
        <div>
            <button class="change-password-btn" onclick="openPasswordModal()">修改密码</button>
            {% if session['username'] != 'admin' %}
            <button class="delete-account-btn" onclick="deleteAccount()">注销账号</button>
            {% endif %}
            <button class="logout-btn" onclick="window.location.href='/logout'">退出登录</button>
        </div>
    </div>
    <h1>Token URL & Bin 文件管理</h1>
    
    <div class="upload-section">
        <h3>批量上传 bin 文件</h3>
        <input type="file" id="fileInput" accept=".bin" multiple>
        <button onclick="uploadFile()">上传</button>
    </div>

    <table id="fileTable">
        <thead>
            <tr>
                <th>文件名</th>
                <th>固定链接</th>
                <th>备注</th>
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

    <!-- 备注修改弹窗 -->
    <div id="remarkModal" class="modal">
        <div class="modal-content">
            <span class="close" onclick="closeRemarkModal()">&times;</span>
            <h3>修改备注</h3>
            <textarea id="remarkContent" class="remark-input" placeholder="输入备注内容"></textarea>
            <input type="hidden" id="currentFileId">
            <button onclick="saveRemark()">保存备注</button>
        </div>
    </div>

    <!-- 文件更新弹窗 -->
    <div id="updateFileModal" class="modal">
        <div class="modal-content">
            <span class="close" onclick="closeUpdateFileModal()">&times;</span>
            <h3>更新文件</h3>
            <input type="file" id="updateFileInput" accept=".bin">
            <input type="hidden" id="updateFileId">
            <button onclick="updateFile()">确认更新</button>
        </div>
    </div>

    <script>
        const API_BASE = '';

        // 密码弹窗
        function openPasswordModal() {
            document.getElementById('passwordModal').style.display = "block";
        }
        function closePasswordModal() {
            document.getElementById('passwordModal').style.display = "none";
            document.getElementById('newPassword').value = "";
        }

        // 备注弹窗
        function openRemarkModal(fileId, currentRemark) {
            document.getElementById('currentFileId').value = fileId;
            document.getElementById('remarkContent').value = currentRemark || "";
            document.getElementById('remarkModal').style.display = "block";
        }
        function closeRemarkModal() {
            document.getElementById('remarkModal').style.display = "none";
            document.getElementById('currentFileId').value = "";
            document.getElementById('remarkContent').value = "";
        }

        // 更新文件弹窗
        function openUpdateFileModal(fileId) {
            document.getElementById('updateFileId').value = fileId;
            document.getElementById('updateFileModal').style.display = "block";
        }
        function closeUpdateFileModal() {
            document.getElementById('updateFileModal').style.display = "none";
            document.getElementById('updateFileId').value = "";
            document.getElementById('updateFileInput').value = "";
        }

        // 点击空白处关闭弹窗
        window.onclick = function(event) {
            if (event.target == document.getElementById('passwordModal')) closePasswordModal();
            if (event.target == document.getElementById('remarkModal')) closeRemarkModal();
            if (event.target == document.getElementById('updateFileModal')) closeUpdateFileModal();
        }

        // 修改密码
        async function changePassword() {
            const newPassword = document.getElementById('newPassword').value;
            if (!newPassword) {
                alert("请输入新密码");
                return;
            }
            try {
                const response = await fetch(`${API_BASE}/change_password`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
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
                const response = await fetch(`${API_BASE}/delete_account`, {
                    method: 'POST'
                });
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
                    const fixedUrl = `${window.location.origin}/api/file/${file.file_id}`;
                    tr.innerHTML = `
                        <td>${file.original_filename}</td>
                        <td class="url-cell">
                            <a href="${fixedUrl}" target="_blank">${fixedUrl}</a>
                            <button class="copy-btn" onclick="copyToClipboard('${fixedUrl}')">复制</button>
                        </td>
                        <td>${file.remark || '无备注'}</td>
                        <td>
                            <button class="remark" onclick="openRemarkModal('${file.file_id}', '${file.remark || ''}')">备注</button>
                            <button class="update" onclick="openUpdateFileModal('${file.file_id}')">更新</button>
                            <button class="delete" onclick="deleteFile('${file.file_id}')">删除</button>
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
        async function deleteFile(fileId) {
            if (!confirm(`确定要删除该文件吗？`)) return;
            try {
                const response = await fetch(`${API_BASE}/api/files/${fileId}`, {
                    method: 'DELETE'
                });
                if (response.ok) {
                    alert('删除成功');
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

        // 保存备注
        async function saveRemark() {
            const fileId = document.getElementById('currentFileId').value;
            const remark = document.getElementById('remarkContent').value;
            if (!fileId) return;
            try {
                const response = await fetch(`${API_BASE}/api/files/${fileId}/remark`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ remark: remark })
                });
                if (response.ok) {
                    alert('备注保存成功');
                    closeRemarkModal();
                    loadFiles();
                } else {
                    const result = await response.json();
                    alert('保存失败: ' + (result.error || '未知错误'));
                }
            } catch (error) {
                console.error('保存备注失败:', error);
                alert('保存出错');
            }
        }

        // 更新文件
        async function updateFile() {
            const fileId = document.getElementById('updateFileId').value;
            const fileInput = document.getElementById('updateFileInput');
            const file = fileInput.files[0];
            if (!fileId || !file) {
                alert('请选择要更新的.bin文件');
                return;
            }
            if (!file.name.endsWith('.bin')) {
                alert('只能上传.bin文件');
                return;
            }
            const formData = new FormData();
            formData.append('file', file);
            try {
                const response = await fetch(`${API_BASE}/api/files/${fileId}/update`, {
                    method: 'POST',
                    body: formData
                });
                if (response.ok) {
                    alert('文件更新成功');
                    closeUpdateFileModal();
                    loadFiles();
                } else {
                    const result = await response.json();
                    alert('更新失败: ' + (result.error || '未知错误'));
                }
            } catch (error) {
                console.error('更新文件失败:', error);
                alert('更新出错');
            }
        }

        // 复制到剪贴板
        function copyToClipboard(text) {
            navigator.clipboard.writeText(text).then(() => {
                alert('复制成功');
            }).catch(() => {
                alert('复制失败，请手动复制');
            });
        }

        // 页面加载时加载文件列表
        window.onload = loadFiles;
    </script>
</body>
</html>
    ''', session=session)

# ========== API 路由 - 文件操作 ==========
@app.route('/api/upload', methods=['POST'])
@login_required
def api_upload():
    """文件上传接口"""
    username = session.get('username')
    if 'files' not in request.files:
        return jsonify({"error": "未选择文件"}), 400
    
    files = request.files.getlist('files')
    uploaded_count = 0
    
    for file in files:
        if file.filename == '' or not file.filename.endswith('.bin'):
            continue
        # 保存文件到用户目录
        file_path = get_user_bin_path(username, file.filename)
        file.save(file_path)
        # 创建元数据（如果不存在）
        if not get_file_meta_by_filename(file.filename, username):
            create_file_meta(username, file.filename)
        uploaded_count += 1
    
    return jsonify({
        "uploaded_count": uploaded_count,
        "message": f"成功上传 {uploaded_count} 个文件"
    })

@app.route('/api/files', methods=['GET'])
@login_required
def api_list_files():
    """获取用户文件列表（含元数据）"""
    username = session.get('username')
    user_dir = get_user_bin_dir(username)
    # 读取用户文件
    files = []
    if os.path.exists(user_dir):
        for filename in os.listdir(user_dir):
            if filename.endswith('.bin'):
                # 获取元数据
                file_meta = get_file_meta_by_filename(filename, username)
                if not file_meta:
                    # 为旧文件创建元数据
                    file_id = create_file_meta(username, filename)
                    file_meta = get_file_meta_by_id(file_id, username)
                files.append({
                    "file_id": file_meta["file_id"],
                    "original_filename": filename,
                    "remark": file_meta["remark"],
                    "update_time": file_meta["update_time"]
                })
    return jsonify(files)

@app.route('/api/file/<file_id>', methods=['GET'])
@login_required
def api_get_file_by_id(file_id):
    """通过固定ID获取文件（固定链接核心）"""
    username = session.get('username')
    file_meta = get_file_meta_by_id(file_id, username)
    if not file_meta:
        return jsonify({"error": "文件不存在"}), 404
    # 获取文件路径
    file_path = get_user_bin_path(username, file_meta["original_filename"])
    if not os.path.exists(file_path):
        return jsonify({"error": "文件已被删除"}), 404
    # 返回文件
    return send_from_directory(
        os.path.dirname(file_path),
        os.path.basename(file_path),
        as_attachment=False
    )

@app.route('/api/files/<file_id>', methods=['DELETE'])
@login_required
def api_delete_file(file_id):
    """删除文件（通过固定ID）"""
    username = session.get('username')
    file_meta = get_file_meta_by_id(file_id, username)
    if not file_meta:
        return jsonify({"error": "文件不存在"}), 404
    # 删除文件
    file_path = get_user_bin_path(username, file_meta["original_filename"])
    if os.path.exists(file_path):
        os.remove(file_path)
    # 删除元数据
    delete_file_meta(file_id, username)
    return jsonify({"message": "文件删除成功"})

@app.route('/api/files/<file_id>/remark', methods=['POST'])
@login_required
def api_update_remark(file_id):
    """更新文件备注"""
    username = session.get('username')
    data = request.get_json()
    if not data or 'remark' not in data:
        return jsonify({"error": "备注内容不能为空"}), 400
    # 更新元数据
    success = update_file_meta(file_id, username, {"remark": data["remark"]})
    if success:
        return jsonify({"message": "备注更新成功"})
    else:
        return jsonify({"error": "文件不存在"}), 404

@app.route('/api/files/<file_id>/update', methods=['POST'])
@login_required
def api_update_file(file_id):
    """更新文件内容（保持固定ID不变）"""
    username = session.get('username')
    if 'file' not in request.files:
        return jsonify({"error": "未选择文件"}), 400
    # 验证文件
    file = request.files['file']
    if file.filename == '' or not file.filename.endswith('.bin'):
        return jsonify({"error": "只能上传.bin文件"}), 400
    # 获取元数据
    file_meta = get_file_meta_by_id(file_id, username)
    if not file_meta:
        return jsonify({"error": "文件不存在"}), 404
    # 覆盖保存文件
    file_path = get_user_bin_path(username, file_meta["original_filename"])
    file.save(file_path)
    # 更新元数据的更新时间
    update_file_meta(file_id, username, {"update_time": os.path.getmtime(file_path)})
    return jsonify({"message": "文件更新成功"})

# ========== Render 部署适配 - 启动配置 ==========
if __name__ == '__main__':
    # Render 使用 PORT 环境变量，且需要监听 0.0.0.0
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)  # 生产环境关闭debug
