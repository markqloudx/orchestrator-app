from flask import Flask, render_template, request, jsonify
import os
import json
import requests
import sqlite3
import random
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'default-secret-key')

# ============================================================
# ⚠️ GITHUB TOKEN - PUT YOUR ACTUAL TOKEN HERE
# ============================================================

# 🔑 PASTE YOUR TOKEN BETWEEN THE QUOTES
GITHUB_TOKEN = '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'  # ← REPLACE THIS

# ============================================================
# DEBUG - Check if token is loaded
# ============================================================

print("=" * 60)
print("🔑 GITHUB TOKEN DEBUG")
print("=" * 60)
print(f"Token exists: {bool(GITHUB_TOKEN)}")
if GITHUB_TOKEN:
    print(f"Token length: {len(GITHUB_TOKEN)}")
    print(f"Token starts with 'ghp_': {GITHUB_TOKEN.startswith('ghp_')}")
    print(f"Token: {GITHUB_TOKEN[:8]}...{GITHUB_TOKEN[-4:]}")
else:
    print("❌ TOKEN IS EMPTY!")
print("=" * 60)

# ============================================================
# DATABASE
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'orchestrator.db')

if os.path.exists(DB_PATH):
    try:
        os.remove(DB_PATH)
        print("🗑️ Fresh database created!")
    except:
        pass

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    try:
        with get_db() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    source_repo_url TEXT,
                    source_repo_name TEXT,
                    target_repo_url TEXT,
                    target_repo_name TEXT,
                    branch TEXT DEFAULT 'main',
                    source_status TEXT DEFAULT 'PENDING',
                    target_status TEXT DEFAULT 'PENDING',
                    status TEXT DEFAULT 'PENDING',
                    last_sync TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            conn.execute('''
                CREATE TABLE IF NOT EXISTS changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    change_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL,
                    pr_number INTEGER,
                    pr_url TEXT,
                    pr_title TEXT,
                    pr_author TEXT,
                    commit_sha TEXT,
                    description TEXT,
                    files_changed TEXT,
                    source_branch TEXT DEFAULT 'main',
                    target_branch TEXT DEFAULT 'main',
                    approval_status TEXT DEFAULT 'PENDING',
                    deployment_status TEXT DEFAULT 'BLOCKED',
                    github_action_run_id INTEGER,
                    github_action_status TEXT DEFAULT 'NOT_TRIGGERED',
                    github_action_url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    approved_at TIMESTAMP,
                    approved_by TEXT,
                    merged_at TIMESTAMP,
                    merged_by TEXT,
                    rejected_reason TEXT,
                    FOREIGN KEY (project_id) REFERENCES projects(project_id)
                )
            ''')
            
            conn.execute('''
                CREATE TABLE IF NOT EXISTS audit_trail (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    event TEXT,
                    change_id TEXT,
                    project_id TEXT,
                    result TEXT,
                    details TEXT
                )
            ''')
            
            conn.commit()
            print("✅ Database initialized")
            return True
    except Exception as e:
        print(f"❌ DB init error: {e}")
        return False

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
        self.is_configured = bool(token) and token.startswith('ghp_')
        print(f"🔑 GitHub Client initialized: {self.is_configured}")
    
    def verify_repo(self, repo_name):
        if not self.is_configured:
            print(f"❌ Not configured - skipping verify for {repo_name}")
            return False
        try:
            url = f'https://api.github.com/repos/{repo_name}'
            print(f"🔍 Verifying: {url}")
            r = requests.get(url, headers=self.headers)
            print(f"📡 Status: {r.status_code}")
            return r.status_code == 200
        except Exception as e:
            print(f"❌ Error: {e}")
            return False
    
    def get_pull_requests(self, repo_name, state='open'):
        if not self.is_configured:
            return []
        try:
            r = requests.get(
                f'https://api.github.com/repos/{repo_name}/pulls?state={state}&per_page=100',
                headers=self.headers
            )
            if r.status_code == 200:
                return r.json()
            return []
        except:
            return []
    
    def get_pr_files(self, repo_name, pr_number):
        if not self.is_configured:
            return []
        try:
            r = requests.get(
                f'https://api.github.com/repos/{repo_name}/pulls/{pr_number}/files',
                headers=self.headers
            )
            if r.status_code == 200:
                return r.json()
            return []
        except:
            return []
    
    def trigger_github_action(self, repo_name, workflow_id='sync.yml', ref='main'):
        if not self.is_configured:
            return False
        url = f'https://api.github.com/repos/{repo_name}/actions/workflows/{workflow_id}/dispatches'
        payload = {'ref': ref}
        try:
            r = requests.post(url, headers=self.headers, json=payload)
            return r.status_code == 204
        except:
            return False
    
    def get_workflow_runs(self, repo_name, limit=1):
        if not self.is_configured:
            return []
        url = f'https://api.github.com/repos/{repo_name}/actions/runs'
        try:
            r = requests.get(url, headers=self.headers, params={'per_page': limit})
            if r.status_code == 200:
                return r.json().get('workflow_runs', [])
            return []
        except:
            return []
    
    def get_workflow_run_status(self, repo_name, run_id):
        if not self.is_configured:
            return None
        url = f'https://api.github.com/repos/{repo_name}/actions/runs/{run_id}'
        try:
            r = requests.get(url, headers=self.headers)
            if r.status_code == 200:
                data = r.json()
                return {
                    'status': data.get('status'),
                    'conclusion': data.get('conclusion'),
                    'html_url': data.get('html_url')
                }
            return None
        except:
            return None
    
    def sync_prs_to_db(self, source_repo, project_id):
        if not self.is_configured:
            return 0
        prs = self.get_pull_requests(source_repo)
        synced_count = 0
        
        with get_db() as conn:
            for pr in prs:
                existing = conn.execute(
                    'SELECT * FROM changes WHERE pr_number = ? AND project_id = ?',
                    (pr['number'], project_id)
                ).fetchone()
                
                if not existing:
                    files = self.get_pr_files(source_repo, pr['number'])
                    files_changed = [{'file': f['filename'], 'type': 'modified'} for f in files]
                    
                    change_id = f"CHG-{datetime.now().strftime('%Y%m%d')}-{random.randint(1000, 9999)}"
                    
                    conn.execute('''
                        INSERT INTO changes 
                        (change_id, project_id, pr_number, pr_url, pr_title, pr_author, 
                         commit_sha, description, files_changed, source_branch, target_branch, 
                         approval_status, github_action_status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 'NOT_TRIGGERED')
                    ''', (
                        change_id,
                        project_id,
                        pr['number'],
                        pr['html_url'],
                        pr['title'],
                        pr['user']['login'],
                        pr['head']['sha'],
                        pr['body'] or '',
                        json.dumps(files_changed),
                        pr['head']['ref'],
                        pr['base']['ref']
                    ))
                    
                    synced_count += 1
            
            conn.execute('''
                UPDATE projects SET last_sync = CURRENT_TIMESTAMP WHERE project_id = ?
            ''', (project_id,))
            conn.commit()
        
        return synced_count

github_client = GitHubClient(GITHUB_TOKEN) if GITHUB_TOKEN else None

# ============================================================
# PAGE ROUTES
# ============================================================

@app.route('/')
def index():
    with get_db() as conn:
        projects = conn.execute('SELECT * FROM projects ORDER BY created_at DESC').fetchall()
        changes = conn.execute('SELECT * FROM changes ORDER BY created_at DESC LIMIT 10').fetchall()
        pending = conn.execute("SELECT COUNT(*) FROM changes WHERE approval_status = 'PENDING'").fetchone()[0]
        approved = conn.execute("SELECT COUNT(*) FROM changes WHERE approval_status = 'APPROVED'").fetchone()[0]
        merged = conn.execute("SELECT COUNT(*) FROM changes WHERE deployment_status = 'MERGED'").fetchone()[0]
    
    return render_template('index.html',
                         projects=projects,
                         changes=changes,
                         pending_count=pending,
                         approved_count=approved,
                         merged_count=merged,
                         github_configured=bool(GITHUB_TOKEN))

@app.route('/projects')
def projects_page():
    with get_db() as conn:
        projects = conn.execute('SELECT * FROM projects ORDER BY created_at DESC').fetchall()
    return render_template('projects.html', projects=projects, github_configured=bool(GITHUB_TOKEN))

@app.route('/changes')
def changes_page():
    with get_db() as conn:
        changes = conn.execute('SELECT * FROM changes ORDER BY created_at DESC').fetchall()
        projects = conn.execute('SELECT project_id, name, source_repo_name, target_repo_name FROM projects').fetchall()
    return render_template('changes.html', changes=changes, projects=projects, github_configured=bool(GITHUB_TOKEN))

@app.route('/approvals')
def approvals_page():
    with get_db() as conn:
        pending = conn.execute('SELECT * FROM changes WHERE approval_status = "PENDING" ORDER BY created_at ASC').fetchall()
        projects = conn.execute('SELECT project_id, name FROM projects').fetchall()
    return render_template('approvals.html', changes=pending, projects=projects)

@app.route('/deployment')
def deployment_page():
    return render_template('deployment.html')

# ============================================================
# API ROUTES
# ============================================================

@app.route('/api/projects', methods=['GET'])
def api_get_projects():
    with get_db() as conn:
        projects = conn.execute('SELECT * FROM projects ORDER BY created_at DESC').fetchall()
        return jsonify({'success': True, 'data': [dict(p) for p in projects]})

@app.route('/api/projects', methods=['POST'])
def api_create_project():
    try:
        data = request.json
        
        if not data.get('project_id'):
            return jsonify({'success': False, 'error': 'project_id is required'}), 400
        if not data.get('name'):
            return jsonify({'success': False, 'error': 'name is required'}), 400
        if not data.get('source_repo_url'):
            return jsonify({'success': False, 'error': 'source_repo_url is required'}), 400
        if not data.get('target_repo_url'):
            return jsonify({'success': False, 'error': 'target_repo_url is required'}), 400
        
        with get_db() as conn:
            existing = conn.execute('SELECT * FROM projects WHERE project_id = ?', (data['project_id'],)).fetchone()
            if existing:
                return jsonify({'success': False, 'error': 'Project ID already exists'}), 400
            
            source_url = data['source_repo_url']
            target_url = data['target_repo_url']
            
            source_name = source_url.replace('https://github.com/', '').replace('.git', '')
            target_name = target_url.replace('https://github.com/', '').replace('.git', '')
            
            source_valid = False
            target_valid = False
            if github_client and github_client.is_configured:
                source_valid = github_client.verify_repo(source_name)
                target_valid = github_client.verify_repo(target_name)
            
            source_status = 'CONNECTED' if source_valid else 'NOT CONNECTED'
            target_status = 'CONNECTED' if target_valid else 'NOT CONNECTED'
            overall_status = 'CONNECTED' if (source_valid and target_valid) else 'PARTIAL'
            
            conn.execute('''
                INSERT INTO projects 
                (project_id, name, source_repo_url, source_repo_name, target_repo_url, target_repo_name, 
                 branch, source_status, target_status, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                data['project_id'],
                data['name'],
                source_url,
                source_name,
                target_url,
                target_name,
                data.get('branch', 'main'),
                source_status,
                target_status,
                overall_status
            ))
            conn.commit()
            
            project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (data['project_id'],)).fetchone()
            
            synced = 0
            if github_client and github_client.is_configured and source_valid:
                synced = github_client.sync_prs_to_db(source_name, data['project_id'])
            
            return jsonify({'success': True, 'data': dict(project), 'prs_synced': synced})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/projects/<project_id>/verify', methods=['GET'])
def api_verify_project(project_id):
    try:
        with get_db() as conn:
            project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (project_id,)).fetchone()
            if not project:
                return jsonify({'success': False, 'error': 'Project not found'}), 404
            
            if not github_client or not github_client.is_configured:
                return jsonify({
                    'success': False, 
                    'error': f'GitHub not configured. Token exists: {bool(GITHUB_TOKEN)}'
                }), 400
            
            source_name = project['source_repo_name']
            target_name = project['target_repo_name']
            
            source_valid = github_client.verify_repo(source_name)
            target_valid = github_client.verify_repo(target_name)
            
            source_status = 'CONNECTED' if source_valid else 'NOT CONNECTED'
            target_status = 'CONNECTED' if target_valid else 'NOT CONNECTED'
            overall_status = 'CONNECTED' if (source_valid and target_valid) else 'PARTIAL'
            
            conn.execute('''
                UPDATE projects 
                SET source_status = ?, target_status = ?, status = ?
                WHERE project_id = ?
            ''', (source_status, target_status, overall_status, project_id))
            conn.commit()
            
            synced = 0
            if source_valid:
                synced = github_client.sync_prs_to_db(source_name, project_id)
            
            return jsonify({
                'success': True,
                'source_valid': source_valid,
                'source_status': source_status,
                'target_valid': target_valid,
                'target_status': target_status,
                'status': overall_status,
                'prs_synced': synced
            })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/projects/<project_id>/sync-prs', methods=['POST'])
def api_sync_prs(project_id):
    try:
        with get_db() as conn:
            project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (project_id,)).fetchone()
            if not project:
                return jsonify({'success': False, 'error': 'Project not found'}), 404
            
            if not github_client or not github_client.is_configured:
                return jsonify({
                    'success': False, 
                    'error': f'GitHub not configured. Token exists: {bool(GITHUB_TOKEN)}'
                }), 400
            
            source_name = project['source_repo_name']
            synced = github_client.sync_prs_to_db(source_name, project_id)
            
            return jsonify({'success': True, 'synced': synced})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/changes', methods=['GET'])
def api_get_changes():
    with get_db() as conn:
        changes = conn.execute('SELECT * FROM changes ORDER BY created_at DESC').fetchall()
        return jsonify({'success': True, 'data': [dict(c) for c in changes]})

@app.route('/api/changes/<change_id>/approve', methods=['POST'])
def api_approve_change(change_id):
    try:
        with get_db() as conn:
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            if not change:
                return jsonify({'success': False, 'error': 'Change not found'}), 404
            
            if change['approval_status'] != 'PENDING':
                return jsonify({'success': False, 'error': 'Change already ' + change['approval_status']}), 400
            
            conn.execute('''
                UPDATE changes 
                SET approval_status = 'APPROVED', 
                    approved_at = CURRENT_TIMESTAMP, 
                    approved_by = ?
                WHERE change_id = ?
            ''', ('databricks-user', change_id))
            
            conn.execute('''
                INSERT INTO audit_trail (event, change_id, project_id, result, details)
                VALUES (?, ?, ?, ?, ?)
            ''', ('Change Approved', change_id, change['project_id'], 'APPROVED', 'Change approved'))
            conn.commit()
            
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            return jsonify({'success': True, 'data': dict(change)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/changes/<change_id>/reject', methods=['POST'])
def api_reject_change(change_id):
    try:
        data = request.json or {}
        reason = data.get('reason', 'No reason provided')
        
        with get_db() as conn:
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            if not change:
                return jsonify({'success': False, 'error': 'Change not found'}), 404
            
            if change['approval_status'] != 'PENDING':
                return jsonify({'success': False, 'error': 'Change already ' + change['approval_status']}), 400
            
            conn.execute('''
                UPDATE changes 
                SET approval_status = 'REJECTED', 
                    rejected_reason = ?,
                    approved_at = CURRENT_TIMESTAMP, 
                    approved_by = ?
                WHERE change_id = ?
            ''', (reason, 'databricks-user', change_id))
            
            conn.execute('''
                INSERT INTO audit_trail (event, change_id, project_id, result, details)
                VALUES (?, ?, ?, ?, ?)
            ''', ('Change Rejected', change_id, change['project_id'], 'REJECTED', reason))
            conn.commit()
            
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            return jsonify({'success': True, 'data': dict(change)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/changes/<change_id>/merge', methods=['POST'])
def api_merge_change(change_id):
    try:
        with get_db() as conn:
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            if not change:
                return jsonify({'success': False, 'error': 'Change not found'}), 404
            
            if change['approval_status'] != 'APPROVED':
                return jsonify({'success': False, 'error': 'Change must be approved before merging'}), 400
            
            if change['deployment_status'] == 'MERGED':
                return jsonify({'success': False, 'error': 'Change already merged'}), 400
            
            project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (change['project_id'],)).fetchone()
            target_repo = project['target_repo_name']
            
            action_triggered = False
            if github_client and github_client.is_configured:
                action_triggered = github_client.trigger_github_action(target_repo, 'sync.yml', 'main')
                
                if action_triggered:
                    runs = github_client.get_workflow_runs(target_repo, 1)
                    if runs:
                        run = runs[0]
                        conn.execute('''
                            UPDATE changes 
                            SET github_action_run_id = ?,
                                github_action_url = ?,
                                github_action_status = 'RUNNING'
                            WHERE change_id = ?
                        ''', (run.get('id'), run.get('html_url'), change_id))
            
            conn.execute('''
                UPDATE changes 
                SET deployment_status = 'MERGED', 
                    merged_at = CURRENT_TIMESTAMP, 
                    merged_by = ?
                WHERE change_id = ?
            ''', ('databricks-user', change_id))
            
            conn.execute('''
                INSERT INTO audit_trail (event, change_id, project_id, result, details)
                VALUES (?, ?, ?, ?, ?)
            ''', ('Change Merged - Action Triggered', change_id, change['project_id'], 'MERGED', 'GitHub Action triggered'))
            conn.commit()
            
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            return jsonify({
                'success': True, 
                'data': dict(change),
                'action_triggered': action_triggered
            })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/changes/<change_id>/action-status', methods=['GET'])
def api_get_action_status(change_id):
    with get_db() as conn:
        change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
        if not change:
            return jsonify({'success': False, 'error': 'Change not found'}), 404
        
        if not change['github_action_run_id']:
            return jsonify({'success': True, 'status': 'NOT_TRIGGERED'})
        
        if not github_client or not github_client.is_configured:
            return jsonify({'success': True, 'status': change['github_action_status']})
        
        project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (change['project_id'],)).fetchone()
        target_repo = project['target_repo_name']
        
        status_data = github_client.get_workflow_run_status(target_repo, change['github_action_run_id'])
        
        if status_data:
            status = status_data.get('status', 'UNKNOWN')
            conclusion = status_data.get('conclusion', '')
            
            if status == 'completed':
                display_status = 'SUCCESS' if conclusion == 'success' else 'FAILED'
            else:
                display_status = status.upper()
            
            conn.execute('''
                UPDATE changes 
                SET github_action_status = ?
                WHERE change_id = ?
            ''', (display_status, change_id))
            conn.commit()
            
            return jsonify({
                'success': True,
                'status': display_status,
                'url': status_data.get('html_url')
            })
        
        return jsonify({'success': True, 'status': change['github_action_status']})

# ============================================================
# RUN APP
# ============================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("=" * 60)
    print("🚀 GIT TO GIT CI/CD ORCHESTRATOR")
    print("=" * 60)
    print(f"📡 Server running on port: {port}")
    print(f"🔑 GitHub configured: {bool(GITHUB_TOKEN)}")
    if GITHUB_TOKEN:
        print(f"🔑 Token: {GITHUB_TOKEN[:10]}...{GITHUB_TOKEN[-4:]}")
        print(f"🔑 Starts with ghp_: {GITHUB_TOKEN.startswith('ghp_')}")
    else:
        print("⚠️  WARNING: GITHUB_TOKEN is EMPTY!")
    print("=" * 60)
    app.run(host='0.0.0.0', port=port, debug=False)
