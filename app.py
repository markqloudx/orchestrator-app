from flask import Flask, render_template, request, jsonify, send_from_directory
import os
import json
import requests
import base64
from datetime import datetime
import sqlite3
import re
import random

# ============================================================
# CONFIGURATION - Environment Variables
# ============================================================

DATABRICKS_HOST = os.environ.get('DATABRICKS_HOST', '')
DATABRICKS_TOKEN = os.environ.get('DATABRICKS_TOKEN', '')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
GITHUB_DEV_REPO = os.environ.get('GITHUB_DEV_REPO', '')
GITHUB_PROD_REPO = os.environ.get('GITHUB_PROD_REPO', '')

# ============================================================
# FLASK APP
# ============================================================

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'default-secret-key-change-me')

# ============================================================
# DATABASE
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'orchestrator.db')

print(f"📁 Database path: {DB_PATH}")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    try:
        with get_db() as conn:
            # Cost Centers table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS cost_centers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cc_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    perspective TEXT,
                    owner TEXT,
                    status TEXT DEFAULT 'ACTIVE',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Projects table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    cost_center_id TEXT NOT NULL,
                    repository_url TEXT,
                    repository_name TEXT,
                    provider TEXT DEFAULT 'GitHub',
                    branch TEXT DEFAULT 'main',
                    aws_status TEXT DEFAULT 'PENDING',
                    aws_connection TEXT,
                    status TEXT DEFAULT 'ACTIVE',
                    running INTEGER DEFAULT 0,
                    completed INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (cost_center_id) REFERENCES cost_centers(cc_id)
                )
            ''')
            
            # Changes table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    change_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL,
                    commit_sha TEXT,
                    release_version TEXT,
                    description TEXT,
                    files_changed TEXT,
                    branch TEXT DEFAULT 'main',
                    approval_status TEXT DEFAULT 'PENDING',
                    deployment_status TEXT DEFAULT 'BLOCKED',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    approved_at TIMESTAMP,
                    approved_by TEXT,
                    deployed_at TIMESTAMP,
                    deployed_by TEXT,
                    FOREIGN KEY (project_id) REFERENCES projects(project_id)
                )
            ''')
            
            # Deployment logs
            conn.execute('''
                CREATE TABLE IF NOT EXISTS deployment_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    change_id TEXT,
                    action TEXT,
                    status TEXT,
                    details TEXT,
                    stage TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Audit trail
            conn.execute('''
                CREATE TABLE IF NOT EXISTS audit_trail (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    event TEXT,
                    change_id TEXT,
                    project_id TEXT,
                    repository TEXT,
                    commit_sha TEXT,
                    result TEXT,
                    details TEXT
                )
            ''')
            
            # Insert default data if empty
            if conn.execute('SELECT COUNT(*) FROM cost_centers').fetchone()[0] == 0:
                conn.execute('''
                    INSERT INTO cost_centers (cc_id, name, perspective, owner) VALUES
                    ('CC-2200', 'Finance', 'Finance', 'Finance Owner'),
                    ('CC-1200', 'Sales', 'Sales', 'Sales Owner'),
                    ('CC-3100', 'Operations', 'Operations', 'Operations Owner'),
                    ('CC-4100', 'Customer Data', 'Customer / Data', 'Data Owner')
                ''')
                
                conn.execute('''
                    INSERT INTO projects (project_id, name, cost_center_id, repository_url, repository_name, provider, branch, aws_status, aws_connection) VALUES
                    ('PRJ-001', 'Customer Analytics', 'CC-4100', 'https://github.com/example/customer-analytics', 'customer-analytics', 'GitHub', 'main', 'CONNECTED', 'customer-github-connection'),
                    ('PRJ-021', 'Finance Reporting', 'CC-2200', 'https://bitbucket.org/example/finance-reporting', 'finance-reporting', 'Bitbucket', 'main', 'CONNECTED', 'finance-bitbucket-connection')
                ''')
                
                conn.execute('''
                    INSERT INTO changes (change_id, project_id, commit_sha, release_version, description, approval_status, deployment_status) VALUES
                    ('CHG-2026-0042', 'PRJ-001', '8f3a91c2d7', 'v1.0.184', 'Analytics pipeline update', 'Approved', 'Ready'),
                    ('CHG-2026-0047', 'PRJ-021', '72ac111', 'v1.8.52', 'Finance reporting fix', 'Pending', 'Blocked')
                ''')
            
            conn.commit()
            print("✅ Database initialized successfully")
            return True
    except Exception as e:
        print(f"❌ Database initialization error: {e}")
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
    
    def verify_repo(self, repo_name):
        url = f'https://api.github.com/repos/{repo_name}'
        try:
            response = requests.get(url, headers=self.headers)
            return response.status_code == 200
        except:
            return False
    
    def get_repo_info(self, repo_name):
        url = f'https://api.github.com/repos/{repo_name}'
        try:
            response = requests.get(url, headers=self.headers)
            if response.status_code == 200:
                data = response.json()
                return {
                    'exists': True,
                    'name': data.get('name'),
                    'full_name': data.get('full_name'),
                    'description': data.get('description'),
                    'default_branch': data.get('default_branch'),
                    'private': data.get('private', False),
                    'updated_at': data.get('updated_at')
                }
            return {'exists': False}
        except:
            return {'exists': False}
    
    def get_branches(self, repo_name):
        url = f'https://api.github.com/repos/{repo_name}/branches'
        try:
            response = requests.get(url, headers=self.headers)
            if response.status_code == 200:
                return [b['name'] for b in response.json()]
            return []
        except:
            return []
    
    def get_commits(self, repo_name, branch='main', limit=10):
        url = f'https://api.github.com/repos/{repo_name}/commits?sha={branch}&per_page={limit}'
        try:
            response = requests.get(url, headers=self.headers)
            if response.status_code == 200:
                commits = response.json()
                return [{
                    'sha': c['sha'],
                    'message': c['commit']['message'],
                    'author': c['commit']['author']['name'],
                    'date': c['commit']['author']['date'],
                    'url': c['html_url']
                } for c in commits]
            return []
        except:
            return []
    
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

github_client = GitHubClient(GITHUB_TOKEN) if GITHUB_TOKEN else None

# ============================================================
# ROUTES - UI Pages
# ============================================================

@app.route('/')
def index():
    with get_db() as conn:
        pending = conn.execute("SELECT COUNT(*) FROM changes WHERE approval_status = 'Pending'").fetchone()[0]
        approved = conn.execute("SELECT COUNT(*) FROM changes WHERE approval_status = 'Approved'").fetchone()[0]
        deployed = conn.execute("SELECT COUNT(*) FROM changes WHERE deployment_status = 'Deployed'").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
        
        cost_centers = conn.execute('SELECT * FROM cost_centers').fetchall()
        projects = conn.execute('SELECT * FROM projects').fetchall()
        recent_changes = conn.execute('SELECT * FROM changes ORDER BY created_at DESC LIMIT 10').fetchall()
        
        running = conn.execute("SELECT SUM(running) FROM projects").fetchone()[0] or 0
        
        # Get current deployment status
        active_deployment = conn.execute('''
            SELECT * FROM changes 
            WHERE deployment_status = 'Deploying' OR deployment_status = 'Ready'
            ORDER BY created_at DESC LIMIT 1
        ''').fetchone()
    
    return render_template('index.html',
                         pending_count=pending,
                         approved_count=approved,
                         deployed_count=deployed,
                         total_count=total,
                         running_count=running,
                         cost_centers=cost_centers,
                         projects=projects,
                         recent_changes=recent_changes,
                         active_deployment=active_deployment,
                         github_dev=GITHUB_DEV_REPO,
                         github_prod=GITHUB_PROD_REPO)

@app.route('/cost-centers')
def cost_centers_page():
    with get_db() as conn:
        centers = conn.execute('''
            SELECT c.*, 
                   (SELECT COUNT(*) FROM projects WHERE cost_center_id = c.cc_id) as project_count,
                   (SELECT SUM(running) FROM projects WHERE cost_center_id = c.cc_id) as running_count,
                   (SELECT SUM(completed) FROM projects WHERE cost_center_id = c.cc_id) as completed_count
            FROM cost_centers c
            ORDER BY c.created_at DESC
        ''').fetchall()
    return render_template('cost_centers.html', centers=centers)

@app.route('/projects')
def projects_page():
    with get_db() as conn:
        projects = conn.execute('''
            SELECT p.*, c.name as cost_center_name 
            FROM projects p 
            LEFT JOIN cost_centers c ON p.cost_center_id = c.cc_id
            ORDER BY p.created_at DESC
        ''').fetchall()
        
        cost_centers = conn.execute('SELECT cc_id, name FROM cost_centers WHERE status = "ACTIVE"').fetchall()
    
    return render_template('projects.html', projects=projects, cost_centers=cost_centers)

@app.route('/changes')
def changes_page():
    with get_db() as conn:
        changes = conn.execute('''
            SELECT c.*, p.name as project_name, p.repository_name
            FROM changes c
            LEFT JOIN projects p ON c.project_id = p.project_id
            ORDER BY c.created_at DESC
        ''').fetchall()
        
        projects = conn.execute('SELECT project_id, name FROM projects WHERE status = "ACTIVE"').fetchall()
    
    return render_template('changes.html', changes=changes, projects=projects)

@app.route('/approvals')
def approvals_page():
    with get_db() as conn:
        pending = conn.execute('''
            SELECT c.*, p.name as project_name, p.repository_name
            FROM changes c
            LEFT JOIN projects p ON c.project_id = p.project_id
            WHERE c.approval_status = 'Pending'
            ORDER BY c.created_at ASC
        ''').fetchall()
    return render_template('approvals.html', changes=pending)

@app.route('/deployment')
def deployment_page():
    return render_template('deployment.html')

@app.route('/audit')
def audit_page():
    with get_db() as conn:
        audit = conn.execute('''
            SELECT * FROM audit_trail 
            ORDER BY time DESC 
            LIMIT 100
        ''').fetchall()
    return render_template('audit.html', audit=audit)

@app.route('/repository')
def repository_page():
    with get_db() as conn:
        projects = conn.execute('''
            SELECT p.*, c.name as cost_center_name 
            FROM projects p 
            LEFT JOIN cost_centers c ON p.cost_center_id = c.cc_id
            WHERE p.status = 'ACTIVE'
        ''').fetchall()
    return render_template('repository.html', projects=projects)

@app.route('/metadata')
def metadata_page():
    return render_template('metadata.html')

@app.route('/swagger')
def swagger_page():
    return render_template('swagger.html')

@app.route('/health')
def health():
    return jsonify({
        'status': 'healthy',
        'db_path': DB_PATH,
        'github_dev': GITHUB_DEV_REPO,
        'github_prod': GITHUB_PROD_REPO,
        'databricks_configured': bool(DATABRICKS_TOKEN),
        'github_configured': bool(GITHUB_TOKEN)
    })

# ============================================================
# API ROUTES
# ============================================================

@app.route('/api/cost-centers', methods=['GET'])
def api_get_cost_centers():
    with get_db() as conn:
        centers = conn.execute('SELECT * FROM cost_centers').fetchall()
    return jsonify({'success': True, 'data': [dict(c) for c in centers]})

@app.route('/api/cost-centers', methods=['POST'])
def api_create_cost_center():
    try:
        data = request.json
        cc_id = data.get('cc_id')
        name = data.get('name')
        perspective = data.get('perspective', 'General')
        owner = data.get('owner', '')
        
        if not cc_id or not name:
            return jsonify({'success': False, 'error': 'cc_id and name are required'}), 400
        
        with get_db() as conn:
            conn.execute('''
                INSERT INTO cost_centers (cc_id, name, perspective, owner)
                VALUES (?, ?, ?, ?)
            ''', (cc_id, name, perspective, owner))
            conn.commit()
            
            center = conn.execute('SELECT * FROM cost_centers WHERE cc_id = ?', (cc_id,)).fetchone()
            
            conn.execute('''
                INSERT INTO audit_trail (event, result, details)
                VALUES (?, ?, ?)
            ''', ('Cost Center Created', 'Success', f'Created {cc_id} - {name}'))
            conn.commit()
        
        return jsonify({'success': True, 'data': dict(center)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/projects', methods=['GET'])
def api_get_projects():
    with get_db() as conn:
        projects = conn.execute('''
            SELECT p.*, c.name as cost_center_name 
            FROM projects p 
            LEFT JOIN cost_centers c ON p.cost_center_id = c.cc_id
        ''').fetchall()
    return jsonify({'success': True, 'data': [dict(p) for p in projects]})

@app.route('/api/projects', methods=['POST'])
def api_create_project():
    try:
        data = request.json
        project_id = data.get('project_id')
        name = data.get('name')
        cost_center_id = data.get('cost_center_id')
        repository_url = data.get('repository_url', '')
        repository_name = data.get('repository_name', '')
        provider = data.get('provider', 'GitHub')
        branch = data.get('branch', 'main')
        aws_connection = data.get('aws_connection', '')
        
        if not project_id or not name or not cost_center_id:
            return jsonify({'success': False, 'error': 'Required fields missing'}), 400
        
        aws_status = 'PENDING'
        if github_client and repository_url:
            repo_name = repository_url.replace('https://github.com/', '').replace('.git', '')
            if github_client.verify_repo(repo_name):
                aws_status = 'CONNECTED'
        
        with get_db() as conn:
            conn.execute('''
                INSERT INTO projects 
                (project_id, name, cost_center_id, repository_url, repository_name, 
                 provider, branch, aws_status, aws_connection)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (project_id, name, cost_center_id, repository_url, repository_name, 
                  provider, branch, aws_status, aws_connection))
            conn.commit()
            
            project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (project_id,)).fetchone()
            
            conn.execute('''
                INSERT INTO audit_trail (event, project_id, repository, result)
                VALUES (?, ?, ?, ?)
            ''', ('Project Created', project_id, repository_url, 'Success'))
            conn.commit()
        
        return jsonify({'success': True, 'data': dict(project)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/projects/<project_id>/verify', methods=['GET'])
def api_verify_project(project_id):
    with get_db() as conn:
        project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (project_id,)).fetchone()
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        if not github_client:
            return jsonify({'success': False, 'error': 'GitHub client not configured'}), 400
        
        repo_name = project['repository_url'].replace('https://github.com/', '').replace('.git', '')
        is_connected = github_client.verify_repo(repo_name)
        
        conn.execute('''
            UPDATE projects SET aws_status = ? WHERE project_id = ?
        ''', ('CONNECTED' if is_connected else 'NOT CONNECTED', project_id))
        conn.commit()
        
        return jsonify({
            'success': True,
            'connected': is_connected,
            'repository': repo_name,
            'branches': github_client.get_branches(repo_name)
        })

@app.route('/api/projects/<project_id>/branches', methods=['GET'])
def api_get_project_branches(project_id):
    with get_db() as conn:
        project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (project_id,)).fetchone()
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        if not github_client or not project['repository_url']:
            return jsonify({'success': True, 'data': ['main']})
        
        repo_name = project['repository_url'].replace('https://github.com/', '').replace('.git', '')
        branches = github_client.get_branches(repo_name)
        
        return jsonify({'success': True, 'data': branches or ['main']})

@app.route('/api/projects/<project_id>/commits', methods=['GET'])
def api_get_project_commits(project_id):
    branch = request.args.get('branch', 'main')
    limit = request.args.get('limit', 10, type=int)
    
    with get_db() as conn:
        project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (project_id,)).fetchone()
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        if not github_client or not project['repository_url']:
            return jsonify({'success': True, 'data': []})
        
        repo_name = project['repository_url'].replace('https://github.com/', '').replace('.git', '')
        commits = github_client.get_commits(repo_name, branch, limit)
        
        return jsonify({'success': True, 'data': commits})

@app.route('/api/changes', methods=['GET'])
def api_get_changes():
    status = request.args.get('status')
    with get_db() as conn:
        if status:
            changes = conn.execute('''
                SELECT c.*, p.name as project_name 
                FROM changes c 
                LEFT JOIN projects p ON c.project_id = p.project_id
                WHERE c.approval_status = ? 
                ORDER BY c.created_at DESC
            ''', (status,)).fetchall()
        else:
            changes = conn.execute('''
                SELECT c.*, p.name as project_name 
                FROM changes c 
                LEFT JOIN projects p ON c.project_id = p.project_id
                ORDER BY c.created_at DESC
            ''').fetchall()
    return jsonify({'success': True, 'data': [dict(c) for c in changes]})

@app.route('/api/changes', methods=['POST'])
def api_create_change():
    try:
        data = request.json
        project_id = data.get('project_id')
        commit_sha = data.get('commit_sha')
        release_version = data.get('release_version')
        description = data.get('description', '')
        files_changed = json.dumps(data.get('files_changed', []))
        branch = data.get('branch', 'main')
        
        if not project_id or not commit_sha:
            return jsonify({'success': False, 'error': 'project_id and commit_sha required'}), 400
        
        change_id = f"CHG-2026-{random.randint(1000, 9999)}"
        
        with get_db() as conn:
            conn.execute('''
                INSERT INTO changes 
                (change_id, project_id, commit_sha, release_version, description, files_changed, branch)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (change_id, project_id, commit_sha, release_version, description, files_changed, branch))
            conn.commit()
            
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            
            conn.execute('''
                INSERT INTO audit_trail (event, change_id, project_id, commit_sha, result, details)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', ('Change Created', change_id, project_id, commit_sha, 'Pending', description))
            conn.commit()
        
        return jsonify({'success': True, 'data': dict(change)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/detect-changes', methods=['POST'])
def api_detect_changes():
    try:
        data = request.json
        project_id = data.get('project_id')
        branch = data.get('branch', 'main')
        
        if not project_id:
            return jsonify({'success': False, 'error': 'project_id required'}), 400
        
        with get_db() as conn:
            project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (project_id,)).fetchone()
            if not project:
                return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        if not github_client:
            return jsonify({'success': False, 'error': 'GitHub client not configured'}), 400
        
        dev_repo = project['repository_url'].replace('https://github.com/', '').replace('.git', '')
        prod_repo = GITHUB_PROD_REPO or dev_repo
        
        changes = github_client.compare_repos(dev_repo, prod_repo, branch)
        commits = github_client.get_commits(dev_repo, branch, 5)
        
        return jsonify({
            'success': True,
            'changes': changes,
            'count': len(changes),
            'commits': commits,
            'branch': branch,
            'dev_repo': dev_repo,
            'prod_repo': prod_repo
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/changes/<change_id>/approve', methods=['POST'])
def api_approve_change(change_id):
    try:
        data = request.json
        approver = data.get('approver', 'databricks-user')
        
        with get_db() as conn:
            conn.execute('''
                UPDATE changes 
                SET approval_status = 'Approved', 
                    approved_at = CURRENT_TIMESTAMP,
                    approved_by = ?
                WHERE change_id = ? AND approval_status = 'Pending'
            ''', (approver, change_id))
            
            if conn.total_changes == 0:
                return jsonify({'success': False, 'error': 'Change not found or not pending'}), 404
            
            conn.execute('''
                INSERT INTO deployment_logs (change_id, action, status, details, stage)
                VALUES (?, 'approve', 'Approved', ?, 'Approval')
            ''', (change_id, f'Approved by {approver}'))
            
            conn.execute('''
                INSERT INTO audit_trail (event, change_id, result, details)
                VALUES (?, ?, ?, ?)
            ''', ('Change Approved', change_id, 'Success', f'Approved by {approver}'))
            conn.commit()
            
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
        
        return jsonify({'success': True, 'data': dict(change)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/changes/<change_id>/reject', methods=['POST'])
def api_reject_change(change_id):
    try:
        data = request.json
        approver = data.get('approver', 'databricks-user')
        reason = data.get('reason', 'No reason provided')
        
        with get_db() as conn:
            conn.execute('''
                UPDATE changes 
                SET approval_status = 'Rejected', 
                    approved_at = CURRENT_TIMESTAMP,
                    approved_by = ?
                WHERE change_id = ? AND approval_status = 'Pending'
            ''', (approver, change_id))
            
            if conn.total_changes == 0:
                return jsonify({'success': False, 'error': 'Change not found or not pending'}), 404
            
            conn.execute('''
                INSERT INTO deployment_logs (change_id, action, status, details, stage)
                VALUES (?, 'reject', 'Rejected', ?, 'Approval')
            ''', (change_id, f'Rejected by {approver}: {reason}'))
            
            conn.execute('''
                INSERT INTO audit_trail (event, change_id, result, details)
                VALUES (?, ?, ?, ?)
            ''', ('Change Rejected', change_id, 'Rejected', reason))
            conn.commit()
            
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
        
        return jsonify({'success': True, 'data': dict(change)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/changes/<change_id>/deploy', methods=['POST'])
def api_deploy_change(change_id):
    try:
        data = request.json
        deployer = data.get('deployer', 'databricks-user')
        
        with get_db() as conn:
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            if not change:
                return jsonify({'success': False, 'error': 'Change not found'}), 404
            
            if change['approval_status'] != 'Approved':
                return jsonify({'success': False, 'error': 'Change must be approved first'}), 400
            
            project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (change['project_id'],)).fetchone()
            
            sync_results = []
            if github_client and project and GITHUB_PROD_REPO:
                dev_repo = project['repository_url'].replace('https://github.com/', '').replace('.git', '')
                branch = change['branch'] or 'main'
                files_changed = json.loads(change['files_changed']) if change['files_changed'] else []
                
                conn.execute('''
                    UPDATE changes SET deployment_status = 'Deploying' WHERE change_id = ?
                ''', (change_id,))
                
                for file_info in files_changed:
                    file_path = file_info.get('file')
                    if file_path:
                        success = github_client.sync_to_prod(dev_repo, GITHUB_PROD_REPO, file_path, branch)
                        sync_results.append({'file': file_path, 'success': success})
            
            conn.execute('''
                UPDATE changes 
                SET deployment_status = 'Deployed', 
                    deployed_at = CURRENT_TIMESTAMP,
                    deployed_by = ?
                WHERE change_id = ?
            ''', (deployer, change_id))
            
            conn.execute('''
                UPDATE projects 
                SET completed = completed + 1
                WHERE project_id = ?
            ''', (change['project_id'],))
            
            conn.execute('''
                INSERT INTO deployment_logs (change_id, action, status, details, stage)
                VALUES (?, 'deploy', 'Deployed', ?, 'Deployment')
            ''', (change_id, f'Deployed by {deployer}. Synced {len(sync_results)} files.'))
            
            conn.execute('''
                INSERT INTO audit_trail (event, change_id, project_id, result, details)
                VALUES (?, ?, ?, ?, ?)
            ''', ('Change Deployed', change_id, change['project_id'], 'Success', f'Deployed by {deployer}'))
            conn.commit()
            
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
        
        return jsonify({'success': True, 'data': dict(change), 'sync_results': sync_results})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/deployment-logs', methods=['GET'])
def api_get_deployment_logs():
    change_id = request.args.get('change_id')
    with get_db() as conn:
        if change_id:
            logs = conn.execute('SELECT * FROM deployment_logs WHERE change_id = ? ORDER BY created_at DESC', (change_id,)).fetchall()
        else:
            logs = conn.execute('SELECT * FROM deployment_logs ORDER BY created_at DESC LIMIT 50').fetchall()
    return jsonify({'success': True, 'data': [dict(log) for log in logs]})

@app.route('/api/audit', methods=['GET'])
def api_get_audit():
    limit = request.args.get('limit', 50, type=int)
    with get_db() as conn:
        audit = conn.execute('SELECT * FROM audit_trail ORDER BY time DESC LIMIT ?', (limit,)).fetchall()
    return jsonify({'success': True, 'data': [dict(a) for a in audit]})

@app.route('/api/projects/<project_id>', methods=['DELETE'])
def api_delete_project(project_id):
    try:
        with get_db() as conn:
            # Check if project exists
            project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (project_id,)).fetchone()
            if not project:
                return jsonify({'success': False, 'error': 'Project not found'}), 404
            
            # Delete associated changes first
            conn.execute('DELETE FROM changes WHERE project_id = ?', (project_id,))
            
            # Delete project
            conn.execute('DELETE FROM projects WHERE project_id = ?', (project_id,))
            
            # Log to audit
            conn.execute('''
                INSERT INTO audit_trail (event, project_id, result, details)
                VALUES (?, ?, ?, ?)
            ''', ('Project Deleted', project_id, 'Success', f'Deleted project {project_id}'))
            conn.commit()
        
        return jsonify({'success': True, 'message': f'Project {project_id} deleted'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
# ============================================================
# RUN APP
# ============================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Starting app on port {port}")
    print(f"📁 Database: {DB_PATH}")
    print(f"🔑 GitHub configured: {bool(GITHUB_TOKEN)}")
    print(f"🔑 Databricks configured: {bool(DATABRICKS_TOKEN)}")
    app.run(host='0.0.0.0', port=port, debug=False)