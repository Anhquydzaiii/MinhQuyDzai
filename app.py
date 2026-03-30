import threading
import time
import requests
import base64
import json
import socket
import os
import urllib3
import random
import string
import telebot
from datetime import datetime
from collections import deque, defaultdict
from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

# --- MODULE ĐẶC THÙ (GIỮ NGUYÊN) ---
import MajorLogin_res_pb2
import mjologin

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- CẤU HÌNH ---
TOKEN_BOT = "8686632612:AAHbuZkKVBpfT14tLO9EBO-R_NdcUBeMew8" 
ADMIN_ID = 6054457515 
DB_FILE = 'accounts.json'
KEY_FILE = 'keys.json'
db_lock = threading.Lock()

app = Flask(__name__)
app.secret_key = "gemini_mobile_2026"

user_states = defaultdict(lambda: {"logs": deque(maxlen=30), "is_running": False})

def load_json(file):
    with db_lock:
        if not os.path.exists(file): return {}
        with open(file, 'r', encoding='utf-8') as f:
            try: return json.load(f)
            except: return {}

def save_json(file, data):
    with db_lock:
        with open(file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)

# --- BOT TELEGRAM ADMIN ---
bot = telebot.TeleBot(TOKEN_BOT)

@bot.message_handler(commands=['start', 'help'])
def send_welcome(m):
    if m.from_user.id != ADMIN_ID: return
    msg = "🎮 **ADMIN PANEL**\n\n"
    msg += "/genkey [n] - Tạo mã thuê\n"
    msg += "/ban [user] - Khóa tài khoản\n"
    msg += "/unban [user] - Mở khóa\n"
    msg += "/listkey - Xem key chưa dùng"
    bot.reply_to(m, msg, parse_mode="Markdown")

@bot.message_handler(commands=['genkey'])
def gen_key(m):
    if m.from_user.id != ADMIN_ID: return
    try:
        num = int(m.text.split()[1]) if len(m.text.split()) > 1 else 1
        keys = load_json(KEY_FILE)
        new_keys = []
        for _ in range(num):
            nk = "KEY-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
            keys[nk] = {"used": False, "by": ""}
            new_keys.append(nk)
        save_json(KEY_FILE, keys)
        bot.reply_to(m, f"✅ Đã tạo {num} key:\n`" + "\n".join(new_keys) + "`", parse_mode="Markdown")
    except: bot.reply_to(m, "Lỗi! VD: /genkey 5")

@bot.message_handler(commands=['ban'])
def ban_user(m):
    if m.from_user.id != ADMIN_ID: return
    try:
        u = m.text.split()[1]
        db = load_json(DB_FILE)
        if u in db:
            db[u]['is_banned'] = True
            save_json(DB_FILE, db)
            user_states[u]["is_running"] = False
            bot.reply_to(m, f"🚫 Đã khóa: {u}")
    except: pass

@bot.message_handler(commands=['unban'])
def unban_user(m):
    if m.from_user.id != ADMIN_ID: return
    try:
        u = m.text.split()[1]
        db = load_json(DB_FILE)
        if u in db:
            db[u]['is_banned'] = False
            save_json(DB_FILE, db)
            bot.reply_to(m, f"✅ Đã mở: {u}")
    except: pass

@bot.message_handler(commands=['listkey'])
def list_keys(m):
    if m.from_user.id != ADMIN_ID: return
    keys = load_json(KEY_FILE)
    avail = [k for k, v in keys.items() if not v['used']]
    bot.reply_to(m, f"🔑 Key khả dụng ({len(avail)}):\n`" + "\n".join(avail[:15]) + "`", parse_mode="Markdown")

def run_bot():
    bot.infinity_polling()

# --- LOGIC SPAM CORE (GIỮ NGUYÊN) ---
class SimpleProtobuf:
    @staticmethod
    def encode_varint(value):
        result = bytearray()
        while value > 0x7F:
            result.append((value & 0x7F) | 0x80)
            value >>= 7
        result.append(value & 0x7F)
        return bytes(result)

    @staticmethod
    def encode_string(field_number, value):
        if isinstance(value, str): value = value.encode('utf-8')
        res = bytearray()
        res.extend(SimpleProtobuf.encode_varint((field_number << 3) | 2))
        res.extend(SimpleProtobuf.encode_varint(len(value)))
        res.extend(value)
        return bytes(res)

    @staticmethod
    def create_login_payload(open_id, access_token, platform):
        payload = bytearray()
        curr = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload.extend(SimpleProtobuf.encode_string(3, curr))
        payload.extend(SimpleProtobuf.encode_string(22, open_id))
        payload.extend(SimpleProtobuf.encode_string(23, platform))
        payload.extend(SimpleProtobuf.encode_string(29, access_token))
        payload.extend(SimpleProtobuf.encode_string(99, platform))
        return bytes(payload)

def get_available_room(hex_data):
    try:
        data = bytes.fromhex(hex_data)
        result = {}; index = 0
        while index < len(data):
            tag = data[index]; wire_type = tag & 0x07; field_num = tag >> 3; index += 1
            if wire_type == 0:
                val = 0; shift = 0
                while index < len(data):
                    byte = data[index]; index += 1
                    val |= (byte & 0x7F) << shift
                    if not (byte & 0x80): break
                    shift += 7
                result[str(field_num)] = {"data": val}
            elif wire_type == 2:
                length = 0; shift = 0
                while index < len(data):
                    byte = data[index]; index += 1
                    length |= (byte & 0x7F) << shift
                    if not (byte & 0x80): break
                    shift += 7
                val_bytes = data[index:index + length]; index += length
                try: result[str(field_num)] = {"data": val_bytes.decode('utf-8')}
                except: result[str(field_num)] = {"data": val_bytes.hex()}
            else: break
        return result
    except: return {}

def start_spam_process(username, tokens):
    state = user_states[username]
    state["is_running"] = True
    interval = 2.0 
    headers = {"Host": "loginbp.ggpolarbear.com", "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "FreeFire/2.103.1", "X-GA": "v1 1", "ReleaseVersion": "OB52"}
    while state["is_running"]:
        db = load_json(DB_FILE)
        if db.get(username, {}).get('is_banned'): break
        for token in tokens:
            if not state["is_running"]: break
            st = time.time()
            try:
                mjologin.init_system(token)
                r_inspect = requests.get(f"https://100067.connect.garena.com/oauth/token/inspect?token={token}", timeout=10).json()
                if 'error' in r_inspect: continue
                open_id, platform = r_inspect.get('open_id'), str(r_inspect.get('platform'))
                key, iv = b'Yg&tc%DEuh6%Zc^8', b'6oyZDr22E3ychjM%'
                pb_payload = SimpleProtobuf.create_login_payload(open_id, token, platform)
                enc_payload = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(pb_payload, 16))
                r1 = requests.post("https://loginbp.ggpolarbear.com/MajorLogin", headers=headers, data=enc_payload, timeout=15, verify=False)
                resp_pb = MajorLogin_res_pb2.MajorLoginRes()
                try:
                    dec = unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(r1.content), 16)
                    resp_pb.ParseFromString(dec)
                except: resp_pb.ParseFromString(r1.content)
                headers["Host"] = "clientbp.ggpolarbear.com"; headers["Authorization"] = f"Bearer {resp_pb.account_jwt}"
                r2 = requests.post("https://clientbp.ggpolarbear.com/GetLoginData", headers=headers, data=enc_payload, timeout=12, verify=False)
                room_info = get_available_room(r2.content.hex())
                addr = room_info.get('14', {}).get('data')
                if not addr: continue
                online_ip, online_port = addr[:-6], int(addr[-5:])
                jwt_payload = json.loads(base64.urlsafe_b64decode(resp_pb.account_jwt.split('.')[1] + "==").decode())
                acc_id = int(jwt_payload.get("account_id", 0))
                exp_adj = max(int(jwt_payload.get("exp", 0)) - 28800, 0)
                cipher_jwt = AES.new(resp_pb.key, AES.MODE_CBC, resp_pb.iv)
                enc_jwt = cipher_jwt.encrypt(pad(resp_pb.account_jwt.encode(), 16))
                final_packet = bytes.fromhex("0115" + acc_id.to_bytes(8, "big").hex() + exp_adj.to_bytes(4, "big").hex() + len(enc_jwt).to_bytes(4, "big").hex()) + enc_jwt
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(3); s.connect((online_ip, online_port)); s.sendall(final_packet)
                    state["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] ID:{acc_id} SUCCESS")
            except Exception as e: 
                state["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] ERR: {str(e)[:15]}")
            elapsed = time.time() - st
            time.sleep(max(0, interval - elapsed))

# --- UI HTML ---
HTML_MAIN = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FF PANEL EVOLUTION</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;700&display=swap');
        :root { --primary: #00f0ff; --bg: #06090f; --accent: #ff0050; }
        body { background: var(--bg); color: #fff; font-family: 'Rajdhani', sans-serif; overflow-x: hidden; }
        .gaming-card { background: rgba(15,23,42,0.98); border: 1px solid rgba(0,240,255,0.2); padding: 25px; border-radius: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); position: relative; }
        .ff-input { background: #111827 !important; color: var(--primary) !important; border: 1px solid #334155 !important; border-radius: 12px !important; margin-bottom: 12px; }
        .btn-ff { background: linear-gradient(45deg, var(--primary), #0072ff); color: #000; font-weight: 800; border: none; border-radius: 15px; padding: 12px; width: 100%; transition: 0.3s; cursor:pointer;}
        .btn-stop { background: linear-gradient(45deg, #ff4655, #ff0050) !important; color: #fff !important; }
        .video-guide { display: inline-flex; align-items: center; color: var(--accent); text-decoration: none; font-weight: bold; margin-bottom: 15px; border: 1px solid var(--accent); padding: 5px 15px; border-radius: 50px; font-size: 0.8rem; transition: 0.3s; }
        .video-guide:hover { background: var(--accent); color: #fff; }
        .log-box { height: 350px; overflow-y: auto; background: #000; border-radius: 15px; font-family: monospace; font-size: 13px; color: #4ade80; padding: 15px; border: 1px solid #222; }
        .note-box { border-radius: 15px; background: rgba(255,190,0,0.05); border: 1px solid rgba(255,190,0,0.2); padding: 12px; font-size: 0.8rem; margin-bottom: 15px; }
        .ban-screen { position: absolute; top:0; left:0; width:100%; height:100%; background: rgba(0,0,0,0.9); z-index: 100; border-radius: 20px; display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center; padding: 20px; backdrop-filter: blur(5px); }
        .tab-content { display: none; } .tab-content.active { display: block; }
    </style>
</head>
<body>
    <div class="container py-4">
        <div id="tab-login" class="tab-content {% if not session.get('user') %}active{% endif %}">
            <div class="row justify-content-center pt-5">
                <div class="col-md-5 gaming-card text-center">
                    <h2 id="auth-title" style="color:var(--primary);">FF LOGIN</h2>
                    <input type="text" id="l-user" class="form-control ff-input mt-3" placeholder="USERNAME">
                    <input type="password" id="l-pass" class="form-control ff-input" placeholder="PASSWORD">
                    <div id="captcha-area" style="display:none;">
                        <div class="d-flex gap-2 mb-2">
                            <div id="captcha-q" class="form-control ff-input" style="flex:1; background:#1e293b !important; pointer-events:none;"></div>
                            <input type="text" id="l-captcha" class="form-control ff-input" style="width:80px;" placeholder="Ans?">
                        </div>
                    </div>
                    <button id="btn-auth" onclick="handleAuth()" class="btn-ff">ĐĂNG NHẬP / LOGIN</button>
                    <div class="mt-3"><a href="javascript:void(0)" onclick="toggleAuthMode()" id="auth-toggle" class="text-secondary small text-decoration-none">Chưa có tài khoản? Đăng ký ngay</a></div>
                </div>
            </div>
        </div>

        <div id="tab-dash" class="tab-content {% if session.get('user') %}active{% endif %}">
            {% if session.get('user') %}
            <div class="row g-4">
                <div class="col-lg-4">
                    <div class="gaming-card">
                        {% if is_banned %}
                        <div class="ban-screen">
                            <i class="fas fa-user-slash fa-3x mb-3" style="color:#ff4655;"></i>
                            <h4 style="color:#ff4655;">ACCOUNT BANNED</h4>
                            <p class="small">Tài khoản đã bị khóa bởi quản trị viên.<br>Your account has been locked by Admin.</p>
                            <a href="/logout" class="btn btn-sm btn-outline-light">Thoát / Logout</a>
                        </div>
                        {% endif %}

                        <div class="d-flex justify-content-between mb-3">
                             <small>USER: <b>{{ session['user'] }}</b></small>
                             <small>UID: <b id="display-uid" class="text-info">---</b></small>
                        </div>

                        <a href="https://youtube.com" target="_blank" class="video-guide">
                            <i class="fas fa-play-circle me-2"></i> HƯỚNG DẪN / WATCH VIDEO
                        </a>

                        <div class="note-box">
                            <div class="row">
                                <div class="col-6 border-end"><b class="text-warning">LƯU Ý:</b><br>Nhấn LƯU trước khi Start. Treo máy ổn định.</div>
                                <div class="col-6"><b class="text-warning">NOTE:</b><br>Save tokens before Start. Stable background.</div>
                            </div>
                        </div>

                        <textarea id="tokens" class="form-control ff-input" rows="5" placeholder="Dán Token vào đây...">{{ saved_tokens }}</textarea>
                        <button onclick="save()" class="btn btn-outline-info w-100 mb-2 btn-sm" style="border-radius:12px">LƯU TOKEN / SAVE</button>
                        <button id="ctrlBtn" onclick="toggle()" class="btn-ff {% if is_running %}btn-stop{% endif %}">
                            {% if is_running %}DỪNG / STOP{% else %}BẮT ĐẦU / START{% endif %}
                        </button>
                        <a href="/logout" class="text-danger d-block text-center mt-3 small text-decoration-none">LOGOUT</a>
                    </div>
                </div>
                <div class="col-lg-8">
                    <div class="gaming-card">
                        <h5 style="color:var(--primary)">LIVE SPAM LOGS</h5>
                        <div id="logs" class="log-box"></div>
                    </div>
                </div>
            </div>
            {% endif %}
        </div>
    </div>

    <script>
        let isReg = false;
        function toggleAuthMode() {
            isReg = !isReg;
            document.getElementById('auth-title').innerText = isReg ? "FF REGISTER" : "FF LOGIN";
            document.getElementById('auth-title').style.color = isReg ? "#4ade80" : "#00f0ff";
            document.getElementById('btn-auth').innerText = isReg ? "ĐĂNG KÝ / REGISTER" : "ĐĂNG NHẬP / LOGIN";
            document.getElementById('captcha-area').style.display = isReg ? "block" : "none";
            if(isReg) refreshCaptcha();
        }
        function refreshCaptcha() {
            fetch('/api/get_captcha').then(r=>r.json()).then(d=>document.getElementById('captcha-q').innerText = d.q);
        }
        function handleAuth() {
            const u = document.getElementById('l-user').value;
            const p = document.getElementById('l-pass').value;
            const c = document.getElementById('l-captcha').value;
            fetch(isReg ? '/api/register' : '/api/auth', {
                method:'POST',headers:{'Content-Type':'application/json'},
                body:JSON.stringify({u,p,c})
            }).then(r=>r.json()).then(d=>{ if(d.s==='ok') location.reload(); else { alert(d.m); if(isReg) refreshCaptcha(); } });
        }
        function save() { 
            fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({t:document.getElementById('tokens').value})})
            .then(r=>r.json()).then(d=>alert(d.m)); 
        }
        function toggle() {
            const b = document.getElementById('ctrlBtn');
            const isStart = b.innerText.includes('START');
            fetch(isStart ? '/api/start' : '/api/stop').then(r=>r.json()).then(d=>{ if(d.s==='ok') location.reload(); else alert(d.m); });
        }
        {% if session.get('user') %}
        setInterval(()=>{
            fetch('/api/status').then(r=>r.json()).then(d=>{
                if(d.banned && !document.querySelector('.ban-screen')) location.reload();
                const lb = document.getElementById('logs');
                lb.innerHTML = d.logs.map(l => `<div>> ${l}</div>`).join('');
                lb.scrollTop = lb.scrollHeight;
                if(d.logs.length > 0) {
                    const last = d.logs[d.logs.length-1];
                    if(last.includes('ID:')) document.getElementById('display-uid').innerText = last.split('ID:')[1].split(' ')[0];
                }
            });
        }, 1500);
        {% endif %}
    </script>
</body>
</html>
"""

# --- API ROUTES ---
@app.route('/')
def home():
    db = load_json(DB_FILE); u = session.get('user', ''); ud = db.get(u, {})
    return render_template_string(HTML_MAIN, saved_tokens=ud.get('tokens',''), is_banned=ud.get('is_banned', False), is_running=user_states[u]["is_running"])

@app.route('/api/get_captcha')
def get_captcha():
    a, b = random.randint(1, 9), random.randint(1, 9)
    session['captcha_res'] = str(a + b)
    return jsonify({"q": f"{a} + {b} = ?" })

@app.route('/api/auth', methods=['POST'])
def api_auth():
    data = request.json; u, p = data.get('u'), data.get('p')
    db = load_json(DB_FILE)
    if u in db and db[u]['p'] == p:
        session['user'] = u; return jsonify({"s":"ok"})
    return jsonify({"s":"err","m":"Sai tài khoản hoặc mật khẩu!"})

@app.route('/api/register', methods=['POST'])
def api_register():
    data = request.json; u, p, c = data.get('u'), data.get('p'), data.get('c')
    if c != session.get('captcha_res'): return jsonify({"s":"err","m":"Captcha không chính xác!"})
    db = load_json(DB_FILE)
    if u in db: return jsonify({"s":"err","m":"Tài khoản đã tồn tại!"})
    db[u] = {"p":p, "tokens":"", "is_banned": False}
    save_json(DB_FILE, db); session['user'] = u; return jsonify({"s":"ok"})

@app.route('/api/save', methods=['POST'])
def api_save():
    u = session.get('user'); db = load_json(DB_FILE)
    db[u]['tokens'] = request.json.get('t', ''); save_json(DB_FILE, db)
    return jsonify({"m":"Đã lưu thành công!"})

@app.route('/api/start')
def api_start():
    u = session.get('user'); db = load_json(DB_FILE)
    if db.get(u,{}).get('is_banned'): return jsonify({"s":"err","m":"Tài khoản bị khóa!"})
    tokens = [t.strip() for t in db.get(u,{}).get('tokens','').split('\n') if t.strip()]
    if not tokens: return jsonify({"s":"err","m":"Hãy nhập Token trước!"})
    if not user_states[u]["is_running"]:
        threading.Thread(target=start_spam_process, args=(u, tokens), daemon=True).start()
    return jsonify({"s":"ok"})

@app.route('/api/stop')
def api_stop():
    user_states[session.get('user', '')]["is_running"] = False
    return jsonify({"s":"ok"})

@app.route('/api/status')
def api_status():
    u = session.get('user',''); db = load_json(DB_FILE)
    return jsonify({"logs": list(user_states[u]["logs"]), "is_running": user_states[u]["is_running"], "banned": db.get(u, {}).get('is_banned', False)})

@app.route('/logout')
def logout(): session.clear(); return redirect('/')
if __name__ == '__main__':
    # Khởi tạo file dữ liệu nếu chưa tồn tại
    if not os.path.exists(DB_FILE): 
        save_json(DB_FILE, {})
    if not os.path.exists(KEY_FILE): 
        save_json(KEY_FILE, {})

    # Chạy Bot Telegram ở luồng riêng
    threading.Thread(target=run_bot, daemon=True).start()

    # Render sẽ tự cấp PORT qua biến môi trường, 
    # nếu chạy local sẽ mặc định là 10000
    port = int(os.environ.get("PORT", 10000))
    
    # Chạy ứng dụng
    app.run(host='0.0.0.0', port=port)
