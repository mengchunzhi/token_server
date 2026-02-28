import shutil
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
app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(24))
PORT = int(os.environ.get('PORT', 5000))
CORS(app, supports_credentials=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
BIN_DIR = os.path.join(BASE_DIR, 'bin')
METADATA_FILE = os.path.join(BASE_DIR, 'file_metadata.json')

for dir_path in [BIN_DIR]:
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)

# === 元数据管理（固定ID + 备注）===
def load_metadata():
    default_metadata = {}
    if os.path.exists(METADATA_FILE):
        try:
            with open(METADATA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return default_metadata
    return default_metadata

def save_metadata(metadata):
    with open(METADATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=4, ensure_ascii=False)

def get_file_fixed_id(username, filename):
    metadata = load_metadata()
    user_file_key = f"{username}_{filename}"
    if user_file_key not in metadata:
        fixed_id = str(uuid.uuid4())
        metadata[user_file_key] = {
            "fixed_id": fixed_id,
            "username": username,
            "filename": filename,
            "note": "",
            "update_time": os.time()
        }
        save_metadata(metadata)
    return metadata[user_file_key]["fixed_id"]

def get_filename_by_fixed_id(fixed_id):
    metadata = load_metadata()
    for key, info in metadata.items():
        if info["fixed_id"] == fixed_id:
            return info["username"], info["filename"]
    return None, None

def update_file_note(username, filename, new_note):
    metadata = load_metadata()
    user_file_key = f"{username}_{filename}"
    if user_file_key in metadata:
        metadata[user_file_key]["note"] = new_note.strip()
        save_metadata(metadata)
        return True
    return False

def get_file_note(username, filename):
    metadata = load_metadata()
    user_file_key = f"{username}_{filename}"
    return metadata.get(user_file_key, {}).get("note", "")

# === 配置 ===
def load_config():
    default_config = {
        "users": {
            "admin": {
                "password": os.environ.get('ADMIN_PASSWORD', 'admin123'),
                "token": secrets.token_hex(16)
            }
        }
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return default_config
    save_config(default_config)
    return default_config

def save_config(config_data):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config_data, f, indent=4, ensure_ascii=False)

config = load_config()

# === 装饰器 ===
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def get_user_bin_dir(username):
    user_dir = os.path.join(BIN_DIR, username)
    if not os.path.exists(user_dir):
        os.makedirs(user_dir)
    return user_dir

def get_safe_path(username, filename):
    safe_filename = werkzeug.utils.secure_filename(filename)
    return os.path.join(get_user_bin_dir(username), safe_filename)

# ==============================
# 🔴 修复：访问链接直接返回文本内容（和你原来一样）
# ==============================
@app.route('/token/<fixed_id>')
def access_token_by_fixed_id(fixed_id):
    username, filename = get_filename_by_fixed_id(fixed_id)
    if not username or not filename:
        return "文件不存在", 404

    file_path = get_safe_path(username, filename)
    if not os.path.exists(file_path):
        return "文件不存在", 404

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return content, 200, {"Content-Type": "text/plain; charset=utf-8"}
    except:
        try:
            with open(file_path, 'rb') as f:
                return f.read()
        except:
            return "读取失败", 500

# === API：更新备注 ===
@app.route('/api/update_note/<filename>', methods=['POST'])
@login_required
def update_note(filename):
    username = session.get('username')
    data = request.get_json()
    new_note = data.get('note', '')
    if update_file_note(username, filename, new_note):
        return jsonify({"message": "备注保存成功"})
    return jsonify({"error": "失败"}), 400

# === API：更新文件 ===
@app.route('/api/update_file/<filename>', methods=['POST'])
@login_required
def update_file(filename):
    username = session.get('username')
    file_path = get_safe_path(username, filename)
    if not os.path.exists(file_path):
        return jsonify({"error": "文件不存在"}), 404

    if 'file' in request.files:
        f = request.files['file']
        if f.filename.endswith('.bin'):
            f.save(file_path)
            return jsonify({"message": "文件更新成功"})
        return jsonify({"error": "仅支持bin"}), 400

    if 'content' in request.json:
        content = request.json['content']
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({"message": "内容更新成功"})

    return jsonify({"error": "无有效数据"}), 400

# === 文件列表 ===
@app.route('/api/files')
@login_required
def list_files():
    username = session.get('username')
    user_dir = get_user_bin_dir(username)
    files = []
    if os.path.exists(user_dir):
        for fn in os.listdir(user_dir):
            if fn.endswith('.bin'):
                fixed_id = get_file_fixed_id(username, fn)
                fixed_url = f"{request.host_url}token/{fixed_id}"
                fp = get_safe_path(username, fn)
                size = os.path.getsize(fp) / 1024
                ut = os.path.getmtime(fp)
                note = get_file_note(username, fn)
                files.append({
                    "filename": fn,
                    "note": note,
                    "fixed_url": fixed_url,
                    "file_size": round(size, 2),
                    "update_time": ut
                })
    return jsonify(files)

# === 上传 ===
@app.route('/api/upload', methods=['POST'])
@login_required
def upload_files():
    username = session.get('username')
    if 'files' not in request.files:
        return jsonify({"error": "no files"}), 400
    cnt = 0
    for f in request.files.getlist('files'):
        if f.filename.endswith('.bin'):
            fn = werkzeug.utils.secure_filename(f.filename)
            f.save(get_safe_path(username, fn))
            get_file_fixed_id(username, fn)
            cnt += 1
    return jsonify({"uploaded_count": cnt})

# === 删除 ===
@app.route('/api/files/<filename>', methods=['DELETE'])
@login_required
def delete_file(filename):
    username = session.get('username')
    fp = get_safe_path(username, filename)
    if os.path.exists(fp):
        os.remove(fp)
        meta = load_metadata()
        k = f"{username}_{filename}"
        if k in meta:
            del meta[k]
            save_metadata(meta)
        return jsonify({"message": "删除成功"})
    return jsonify({"error": "不存在"}), 404

# ======================
# 登录/注册/主页 省略（保持不变，和你上一版完全一样）
# 我这里只放【关键修复部分】
# 你直接用下面完整代码即可
# ======================

# --------------------
# 登录页面
# --------------------
@app.route('/login', methods=['GET','POST'])
def login():
    error = None
    if request.method == 'POST':
        u = request.form['username']
        p = request.form['password']
        c = load_config()
        if u in c['users'] and c['users'][u]['password'] == p:
            session['logged_in'] = True
            session['username'] = u
            return redirect('/')
        error = '账号或密码错误'
    return render_template_string('''
<!DOCTYPE html>
<title>登录</title>
<style>
body{display:flex;justify-content:center;align-items:center;height:100vh;background:#f4f4f4;margin:0}
.box{background:white;padding:30px;border-radius:8px;width:320px}
input{width:100%;padding:10px;margin:8px 0;box-sizing:border-box}
button{width:100%;padding:10px;background:#007bff;color:white;border:none;border-radius:4px}
</style>
<div class=box>
<h2>登录</h2>
{% if error %}<p style=color:red>{{error}}</p>{% endif %}
<form method=post>
<input name=username placeholder=用户名 required>
<input name=password type=password placeholder=密码 required>
<button>登录</button>
</form>
</div>
''', error=error)

# --------------------
# 主页（含备注、更新、固定链接）
# --------------------
@app.route('/')
@login_required
def index():
    return render_template_string('''
<!DOCTYPE html>
<meta charset=utf-8>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Token管理</title>
<style>
body{font-family:Arial;padding:20px;max-width:1000px;margin:0 auto}
table{width:100%;border-collapse:collapse;margin-top:20px}
th,td{border:1px solid #ddd;padding:10px;text-align:left}
th{background:#f2f2f2}
button{padding:6px 10px;border:none;border-radius:4px;color:white;cursor:pointer}
button.blue{background:#007bff}
button.green{background:#28a745}
button.yellow{background:#ffc107;color:#000}
button.red{background:#dc3545}
.note-input{padding:6px;width:180px}
.url{font-family:monospace;color:green;word-break:break-all}
</style>

<h1>Token 文件管理</h1>
<div>
    欢迎 {{session.username}}
    <button class=blue onclick=location.href='/logout'>退出</button>
</div>

<h3>上传 .bin 文件</h3>
<input type=file id=f accept=.bin multiple>
<button class=blue onclick=upload()>上传</button>

<table>
<thead>
<tr>
    <th>文件名</th>
    <th>备注名</th>
    <th>大小(KB)</th>
    <th>固定链接</th>
    <th>操作</th>
</tr>
</thead>
<tbody id=list></tbody>
</table>

<script>
async function load(){
    const r = await fetch('/api/files')
    const list = await r.json()
    const tb = document.getElementById('list')
    tb.innerHTML = ''
    list.forEach(f=>{
        const tr = document.createElement('tr')
        tr.innerHTML = `
<td>${f.filename}</td>
<td>
    <input class="note-input" id="note_${f.filename}" value="${f.note||''}">
    <button class=green onclick="saveNote('${f.filename}')">保存</button>
</td>
<td>${f.file_size}</td>
<td class=url>${f.fixed_url}</td>
<td>
    <button class=yellow onclick="update('${f.filename}')">更新</button>
    <button class=red onclick="del('${f.filename}')">删除</button>
</td>
`
        tb.appendChild(tr)
    })
}

async function saveNote(fn){
    const v = document.getElementById('note_'+fn).value
    await fetch('/api/update_note/'+fn, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({note:v})
    })
    load()
}

async function upload(){
    const inp = document.getElementById('f')
    const fd = new FormData()
    for(let x of inp.files) fd.append('files',x)
    await fetch('/api/upload',{method:'POST',body:fd})
    inp.value=''
    load()
}

async function del(fn){
    if(!confirm('确定删除？'))return
    await fetch('/api/files/'+fn,{method:'DELETE'})
    load()
}

async function update(fn){
    const content = prompt('输入新内容（文本）：')
    if(content===null)return
    await fetch('/api/update_file/'+fn,{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({content})
    })
    load()
}

load()
</script>
''', session=session)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

# === 启动 ===
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
