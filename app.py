import os
import json
import subprocess
import uuid
from flask import Flask, render_template, request, redirect

app = Flask(__name__)
DATA_FILE = 'data.json'
processes = {}

# Ensure data file exists
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
    
    # Get the private key content from the form
    key_content = request.form['key_content']
    key_filename = f"key_{server_id}.pem"
    key_path = os.path.join("keys", key_filename)
    
    # Save the key content to a file automatically
    with open(key_path, 'w') as f:
        # strip() and add a newline to ensure SSH reads it correctly
        f.write(key_content.strip() + '\n')
    
    # SSH requires private keys to have strict permissions
    os.chmod(key_path, 0o600)

    new_server = {
        "id": server_id,
        "name": request.form['name'],
        "host": request.form['host'],
        "port": request.form['port'],
        "user": request.form['user'],
        "key_file": key_filename
    }
    data.append(new_server)
    save_data(data)
    start_autossh(new_server)
    return redirect('/')

@app.route('/logs/<ssh_id>')
def view_logs(ssh_id):
    log_path = f"logs/{ssh_id}.log"
    if os.path.exists(log_path):
        with open(log_path, 'r') as f:
            lines = f.readlines()
            return "<pre>" + "".join(lines[-50:]) + "</pre>"
    return "No logs found."

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
