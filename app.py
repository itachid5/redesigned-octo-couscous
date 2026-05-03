import os
import json
import subprocess
import uuid
import threading
from flask import Flask, render_template, request, redirect, jsonify
from flask_socketio import SocketIO, emit
import paramiko

app = Flask(__name__)
app.config['SECRET_KEY'] = 'super-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

DATA_FILE = 'data.json'
processes = {}
ssh_sessions = {}

if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, 'w') as f:
        json.dump([], f)

def load_data():
    with open(DATA_FILE, 'r') as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=4)

def start_autossh(server):
    ssh_id = server['id']
    log_path = f"logs/{ssh_id}.log"
    key_path = f"keys/{server['key_file']}"
    
    # Fingerprint check completely disabled here for background process
    cmd = [
        "autossh", "-M", "0",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR", 
        "-i", key_path,
        "-N", f"{server['user']}@{server['host']}",
        "-p", str(server.get('port', 22))
    ]
    
    if os.path.exists(key_path):
        os.chmod(key_path, 0o600)
    
    log_file = open(log_path, "a")
    p = subprocess.Popen(cmd, stdout=log_file, stderr=log_file)
    processes[ssh_id] = p

# Start existing connections on boot
for server in load_data():
    start_autossh(server)

@app.route('/')
def index():
    servers = load_data()
    return render_template('index.html', servers=servers)

@app.route('/add', methods=['POST'])
def add_server():
    data = load_data()
    server_id = str(uuid.uuid4())[:8]
    
    key_content = request.form['key_content']
    key_filename = f"key_{server_id}.pem"
    key_path = os.path.join("keys", key_filename)
    
    with open(key_path, 'w') as f:
        f.write(key_content.strip() + '\n')
    os.chmod(key_path, 0o600)

    new_server = {
        "id": server_id,
        "name": request.form['name'],
        "host": request.form['host'],
        "port": int(request.form['port']),
        "user": request.form['user'],
        "key_file": key_filename
    }
    data.append(new_server)
    save_data(data)
    start_autossh(new_server)
    return redirect('/')

@app.route('/terminal/<ssh_id>')
def terminal_page(ssh_id):
    servers = load_data()
    server = next((s for s in servers if s['id'] == ssh_id), None)
    if not server:
        return "Server not found", 404
    return render_template('terminal.html', server=server)

@app.route('/api/logs/<ssh_id>')
def view_logs_api(ssh_id):
    log_path = f"logs/{ssh_id}.log"
    if os.path.exists(log_path):
        with open(log_path, 'r') as f:
            lines = f.readlines()
            return jsonify({"status": "success", "logs": "".join(lines[-100:])})
    return jsonify({"status": "error", "logs": "No background logs found yet."})

# --- Real-Time Status Checker API (Shows actual background logs on failure) ---
@app.route('/api/status/<ssh_id>')
def check_status(ssh_id):
    servers = load_data()
    server = next((s for s in servers if s['id'] == ssh_id), None)
    if not server:
        return jsonify({"status": "Error", "color": "#f56565", "reason": "Server not found in DB"})
    
    key_path = f"keys/{server['key_file']}"
    log_path = f"logs/{ssh_id}.log"

    def get_recent_logs(lines_count=5):
        if os.path.exists(log_path):
            with open(log_path, 'r') as f:
                lines = f.readlines()
                logs = [line.strip() for line in lines if line.strip()]
                if logs:
                    return "\n".join(logs[-lines_count:])
        return "Log file empty or not created yet."
    
    cmd = [
        "ssh", "-o", "BatchMode=yes", 
        "-o", "ConnectTimeout=10", 
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-i", key_path,
        "-p", str(server.get('port', 22)),
        f"{server['user']}@{server['host']}",
        "echo", "ok"
    ]
    
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12)
        
        if result.returncode == 0:
            return jsonify({"status": "Online", "color": "#48bb78", "reason": ""})
        else:
            return jsonify({"status": "Failed", "color": "#f56565", "reason": get_recent_logs()})
            
    except subprocess.TimeoutExpired:
        return jsonify({"status": "Failed", "color": "#f56565", "reason": get_recent_logs()})
    except Exception as e:
        return jsonify({"status": "Failed", "color": "#f56565", "reason": str(e)})

# --- WebSocket Logic for Web Terminal ---
def read_from_terminal(channel, sid):
    while True:
        try:
            data = channel.recv(1024).decode('utf-8', errors='replace')
            if not data:
                break
            socketio.emit('terminal_output', {'output': data}, to=sid)
        except Exception:
            socketio.emit('terminal_status', {'status': 'Disconnected', 'color': '#f56565'}, to=sid)
            break

@socketio.on('connect_terminal')
def connect_terminal(data):
    ssh_id = data['ssh_id']
    sid = request.sid
    servers = load_data()
    server = next((s for s in servers if s['id'] == ssh_id), None)
    
    if not server:
        emit('terminal_output', {'output': '\r\nServer not found.\r\n'})
        return

    key_path = f"keys/{server['key_file']}"
    emit('terminal_status', {'status': 'Connecting...', 'color': '#ecc94b'})
    
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=server['host'],
            port=server['port'],
            username=server['user'],
            key_filename=key_path,
            timeout=15,
            banner_timeout=15
        )
        channel = client.invoke_shell()
        ssh_sessions[sid] = {'client': client, 'channel': channel}
        
        emit('terminal_status', {'status': 'Connected', 'color': '#48bb78'})
        emit('terminal_output', {'output': '\r\n*** Successfully connected! ***\r\n\r\n'})
        
        threading.Thread(target=read_from_terminal, args=(channel, sid), daemon=True).start()
        
    except Exception as e:
        emit('terminal_status', {'status': 'Connection Failed', 'color': '#f56565'})
        emit('terminal_output', {'output': f'\r\n[Error] Connection Failed: {str(e)}\r\n'})

@socketio.on('terminal_input')
def terminal_input(data):
    sid = request.sid
    if sid in ssh_sessions:
        channel = ssh_sessions[sid]['channel']
        try:
            channel.send(data['input'])
        except Exception:
            pass

@socketio.on('disconnect')
def disconnect_terminal():
    sid = request.sid
    if sid in ssh_sessions:
        ssh_sessions[sid]['client'].close()
        del ssh_sessions[sid]

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000)
