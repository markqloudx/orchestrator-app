from flask import Flask, render_template, request, jsonify
import os
import json
import requests
import base64
from datetime import datetime
import sqlite3

# ============================================================
# CONFIGURATION
# ============================================================

DATABRICKS_HOST = os.environ.get('DATABRICKS_HOST', 'https://community.cloud.databricks.com')
DATABRICKS_TOKEN = os.environ.get('DATABRICKS_TOKEN', '')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
GITHUB_DEV_REPO = os.environ.get('GITHUB_DEV_REPO', '')
GITHUB_PROD_REPO = os.environ.get('GITHUB_PROD_REPO', '')

# ============================================================
# FLASK APP
# ============================================================

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'databricks-app-secret')

# ============================================================
# DATABASE
# ============================================================

def get_db():
    os.makedirs('/dbfs/apps', exist_ok=True)
    conn = sqlite3.connect('/dbfs/apps/orchestrator.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS change_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                change_type TEXT NOT NULL,
                description TEXT NOT NULL,
                files_changed TEXT,
                status TEXT DEFAULT 'PENDING',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                approved_at TIMESTAMP,
                approved_by TEXT,
                rejected_reason TEXT,
                deployed_at TIMESTAMP,
                deployed_by TEXT
            )
        ''')
        
        conn.execute('''
            CREATE TABLE IF NOT EXISTS deployment_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                change_id INTEGER,
                action TEXT,
                status TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()

init_db()

# ============================================================
# GITHUB CLIENT
# ============================================================

class GitHubClient:
    def __init__(self, token):
        self.token = token
        self.headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json'
        }
    
    def get_all_files(self, repo_name, branch='main'):
        url = f'https://api.github.com/repos/{repo_name}/git/trees/{branch}?recursive=1'
        try:
            response = requests.get(url, headers=self.headers)
            if response.status_code == 200:
                data = response.json()
                return [f for f in data.get('tree', []) if f['type'] == 'blob']
            return []
        except:
            return []
    
    def get_file_content(self, repo_name, file_path, branch='main'):
        url = f'https://api.github.com/repos/{repo_name}/contents/{file_path}?ref={branch}'
        try:
            response = requests.get(url, headers=self.headers)
            if response.status_code == 200:
                data = response.json()
                content = base64.b64decode(data['content']).decode('utf-8')
                return {'content': content, 'sha': data['sha']}
            return None
        except:
            return None
    
    def compare_repos(self, dev_repo, prod_repo, branch='main'):
        dev_files = self.get_all_files(dev_repo, branch)
        prod_files = self.get_all_files(prod_repo, branch)
        
        dev_paths = {f['path'] for f in dev_files}
        prod_paths = {f['path'] for f in prod_files}
        
        changes = []
        
        for path in dev_paths - prod_paths:
            changes.append({'file': path, 'type': 'new'})
        
        for path in dev_paths & prod_paths:
            dev_content = self.get_file_content(dev_repo, path, branch)
            prod_content = self.get_file_content(prod_repo, path, branch)
            if dev_content and prod_content:
                if dev_content['content'] != prod_content['content']:
                    changes.append({'file': path, 'type': 'modified'})
        
        for path in prod_paths - dev_paths:
            changes.append({'file': path, 'type': 'deleted'})
        
        return changes
    
    def sync_to_prod(self, dev_repo, prod_repo, file_path, branch='main'):
        dev_content = self.get_file_content(dev_repo, file_path, branch)
        if not dev_content:
            return False
        
        url = f'https://api.github.com/repos/{prod_repo}/contents/{file_path}'
        
        try:
            response = requests.get(url, headers=self.headers)
            if response.status_code == 200:
                prod_data = response.json()
                payload = {
                    'message': f'[BOT] Sync {file_path} from DEV',
                    'content': base64.b64encode(dev_content['content'].encode()).decode(),
                    'sha': prod_data['sha'],
                    'branch': branch
                }
                response = requests.put(url, headers=self.headers, json=payload)
            else:
                payload = {
                    'message': f'[BOT] Create {file_path} from DEV',
                    'content': base64.b64encode(dev_content['content'].encode()).decode(),
                    'branch': branch
                }
                response = requests.put(url, headers=self.headers, json=payload)
            
            return response.status_code in [200, 201]
        except:
            return False

# ============================================================
# ROUTES
# ============================================================

@app.route('/')
def index():
    with get_db() as conn:
        pending = conn.execute("SELECT COUNT(*) FROM change_requests WHERE status = 'PENDING'").fetchone()[0]
        approved = conn.execute("SELECT COUNT(*) FROM change_requests WHERE status = 'APPROVED'").fetchone()[0]
        deployed = conn.execute("SELECT COUNT(*) FROM change_requests WHERE status = 'DEPLOYED'").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM change_requests").fetchone()[0]
        recent = conn.execute('SELECT * FROM change_requests ORDER BY created_at DESC LIMIT 5').fetchall()
    
    return render_template('index.html',
                         pending_count=pending,
                         approved_count=approved,
                         deployed_count=deployed,
                         total_count=total,
                         recent_changes=recent,
                         github_dev=GITHUB_DEV_REPO,
                         github_prod=GITHUB_PROD_REPO,
                         databricks_host=DATABRICKS_HOST)

@app.route('/changes')
def changes():
    with get_db() as conn:
        all_changes = conn.execute('SELECT * FROM change_requests ORDER BY created_at DESC').fetchall()
    return render_template('changes.html', changes=all_changes)

@app.route('/approvals')
def approvals():
    with get_db() as conn:
        pending = conn.execute('SELECT * FROM change_requests WHERE status = "PENDING" ORDER BY created_at ASC').fetchall()
    return render_template('approvals.html', changes=pending)

# ============================================================
# API ROUTES
# ============================================================

@app.route('/api/detect-changes', methods=['POST'])
def detect_changes():
    try:
        if not GITHUB_TOKEN or not GITHUB_DEV_REPO or not GITHUB_PROD_REPO:
            return jsonify({'success': False, 'error': 'GitHub configuration missing'}), 400
        
        client = GitHubClient(GITHUB_TOKEN)
        changes = client.compare_repos(GITHUB_DEV_REPO, GITHUB_PROD_REPO)
        
        return jsonify({'success': True, 'changes': changes, 'count': len(changes)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/create-change-request', methods=['POST'])
def create_change_request():
    try:
        data = request.json
        files_changed = json.dumps(data.get('files_changed', []))
        
        with get_db() as conn:
            cursor = conn.execute('''
                INSERT INTO change_requests 
                (project_id, change_type, description, files_changed, status)
                VALUES (?, ?, ?, ?, 'PENDING')
            ''', ('PRJ-001', 'sync', data.get('description', 'Sync from DEV to PROD'), files_changed))
            
            change_id = cursor.lastrowid
            conn.commit()
            
            change = conn.execute('SELECT * FROM change_requests WHERE id = ?', (change_id,)).fetchone()
            
            conn.execute('''
                INSERT INTO deployment_logs (change_id, action, status, details)
                VALUES (?, 'create_change_request', 'PENDING', 'Change request created')
            ''', (change_id,))
            conn.commit()
        
        return jsonify({'success': True, 'change': dict(change)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/approve-change/<int:change_id>', methods=['POST'])
def approve_change(change_id):
    try:
        data = request.json
        approver = data.get('approver', 'databricks-user')
        
        with get_db() as conn:
            conn.execute('''
                UPDATE change_requests 
                SET status = 'APPROVED', approved_at = CURRENT_TIMESTAMP, approved_by = ?
                WHERE id = ? AND status = 'PENDING'
            ''', (approver, change_id))
            
            conn.execute('''
                INSERT INTO deployment_logs (change_id, action, status, details)
                VALUES (?, 'approve_change', 'APPROVED', ?)
            ''', (change_id, f'Approved by {approver}'))
            conn.commit()
            
            change = conn.execute('SELECT * FROM change_requests WHERE id = ?', (change_id,)).fetchone()
        
        return jsonify({'success': True, 'change': dict(change)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/reject-change/<int:change_id>', methods=['POST'])
def reject_change(change_id):
    try:
        data = request.json
        approver = data.get('approver', 'databricks-user')
        reason = data.get('reason', 'No reason provided')
        
        with get_db() as conn:
            conn.execute('''
                UPDATE change_requests 
                SET status = 'REJECTED', approved_at = CURRENT_TIMESTAMP, approved_by = ?, rejected_reason = ?
                WHERE id = ? AND status = 'PENDING'
            ''', (approver, reason, change_id))
            
            conn.execute('''
                INSERT INTO deployment_logs (change_id, action, status, details)
                VALUES (?, 'reject_change', 'REJECTED', ?)
            ''', (change_id, f'Rejected by {approver}: {reason}'))
            conn.commit()
            
            change = conn.execute('SELECT * FROM change_requests WHERE id = ?', (change_id,)).fetchone()
        
        return jsonify({'success': True, 'change': dict(change)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/deploy-change/<int:change_id>', methods=['POST'])
def deploy_change(change_id):
    try:
        data = request.json
        deployer = data.get('deployer', 'databricks-user')
        
        with get_db() as conn:
            change = conn.execute('SELECT * FROM change_requests WHERE id = ?', (change_id,)).fetchone()
            
            if not change:
                return jsonify({'success': False, 'error': 'Change not found'}), 404
            
            if change['status'] != 'APPROVED':
                return jsonify({'success': False, 'error': 'Change must be approved first'}), 400
            
            # Sync files
            sync_results = []
            if GITHUB_TOKEN and GITHUB_DEV_REPO and GITHUB_PROD_REPO:
                client = GitHubClient(GITHUB_TOKEN)
                files_changed = json.loads(change['files_changed']) if change['files_changed'] else []
                
                for file_info in files_changed:
                    file_path = file_info.get('file')
                    if file_path:
                        success = client.sync_to_prod(GITHUB_DEV_REPO, GITHUB_PROD_REPO, file_path)
                        sync_results.append({'file': file_path, 'success': success})
            
            conn.execute('''
                UPDATE change_requests 
                SET status = 'DEPLOYED', deployed_at = CURRENT_TIMESTAMP, deployed_by = ?
                WHERE id = ?
            ''', (deployer, change_id))
            
            conn.execute('''
                INSERT INTO deployment_logs (change_id, action, status, details)
                VALUES (?, 'deploy_to_prod', 'DEPLOYED', ?)
            ''', (change_id, f'Deployed by {deployer}. Synced {len(sync_results)} files.'))
            conn.commit()
            
            change = conn.execute('SELECT * FROM change_requests WHERE id = ?', (change_id,)).fetchone()
        
        return jsonify({'success': True, 'change': dict(change), 'sync_results': sync_results})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/deployment-logs')
def get_deployment_logs():
    with get_db() as conn:
        logs = conn.execute('SELECT * FROM deployment_logs ORDER BY created_at DESC LIMIT 50').fetchall()
    return jsonify({'success': True, 'logs': [dict(log) for log in logs]})

@app.route('/api/databricks/deploy', methods=['POST'])
def databricks_deploy():
    try:
        data = request.json
        environment = data.get('environment', 'dev')
        bundle_path = data.get('bundle_path', '/Workspace/databricks-poc')
        
        if not DATABRICKS_TOKEN:
            return jsonify({'success': False, 'error': 'Databricks token not configured'}), 400
        
        url = f'{DATABRICKS_HOST}/api/2.0/bundles/deploy'
        payload = {'bundle_path': bundle_path, 'target': environment}
        
        response = requests.post(url, headers={'Authorization': f'Bearer {DATABRICKS_TOKEN}', 'Content-Type': 'application/json'}, json=payload)
        
        return jsonify({'success': True, 'result': response.json()})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/test')
def test():
    return render_template('test.html')

# ============================================================
# RUN APP
# ============================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)