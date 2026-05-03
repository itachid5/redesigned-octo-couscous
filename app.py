import os
import json
import subprocess
import uuid
import threading
from flask import Flask, render_template, request, redirect
from flask_socketio import SocketIO, emit
import paramiko

app = Flask(__name__)
app.config['SECRET_KEY'] = 'super-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

DATA_FILE = 'data.json'
processes = {}
ssh_sessions = {} # For active web terminal sessions

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
    
    cmd = [
        "autossh", "-M", "0",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=no",
        "-i", key_path,
        "-N", f"{server['user']}@{server['host']}",
        "-p", str(server.get('port', 22))
    ]
    
    if os.path.exists(key_path):
        os.chmod(key_path, 0o600)
    
    log_file = open(log_path, "a")
    p = subprocess.Popen(cmd, stdout=log_file, stderr=log_file)
    processes[ssh_id] = p

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

# --- WebSocket Terminal Logic ---

def read_from_terminal(channel, sid):
    while True:
        try:
            data = channel.recv(1024).decode('utf-8')
            if not data:
                break
            socketio.emit('terminal_output', {'output': data}, to=sid)
        except Exception:
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
    
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=server['host'],
            port=server['port'],
            username=server['user'],
            key_filename=key_path,
            timeout=10
        )
        channel = client.invoke_shell()
        ssh_sessions[sid] = {'client': client, 'channel': channel}
        
        # Start a thread to read output from the server
        threading.Thread(target=read_from_terminal, args=(channel, sid), daemon=True).start()
        
    except Exception as e:
        emit('terminal_output', {'output': f'\r\nConnection Failed: {str(e)}\r\n'})

@socketio.on('terminal_input')
def terminal_input(data):
    sid = request.sid
    if sid in ssh_sessions:
        channel = ssh_sessions[sid]['channel']
        channel.send(data['input'])

@socketio.on('disconnect')
def disconnect_terminal():
    sid = request.sid
    if sid in ssh_sessions:
        ssh_sessions[sid]['client'].close()
        del ssh_sessions[sid]

if __name__ == '__main__':
    # Use socketio.run instead of app.run for WebSockets
    socketio.run(app, host='0.0.0.0', port=5000)
