from flask import Flask, render_template, request, jsonify, send_from_directory
import os
import json
import requests
import base64
from datetime import datetime
import sqlite3
import re

# ============================================================
# CONFIGURATION - Reads from Environment Variables
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
# DATABASE - Uses current directory (NO /dbfs)
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
                    approval_status TEXT DEFAULT 'PENDING',
                    deployment_status TEXT DEFAULT 'BLOCKED',
                    files_changed TEXT,
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
        """Verify if repository exists and is accessible"""
        url = f'https://api.github.com/repos/{repo_name}'
        try:
            response = requests.get(url, headers=self.headers)
            return response.status_code == 200
        except:
            return False
    
    def get_repo_info(self, repo_name):
        """Get repository information"""
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
        """Get all branches of a repository"""
        url = f'https://api.github.com/repos/{repo_name}/branches'
        try:
            response = requests.get(url, headers=self.headers)
            if response.status_code == 200:
                return [b['name'] for b in response.json()]
            return []
        except:
            return []
    
    def get_commits(self, repo_name, branch='main', limit=10):
        """Get recent commits from a branch"""
        url = f'https://api.github.com/repos/{repo_name}/commits?sha={branch}&per_page={limit}'
        try:
            response = requests.get(url, headers=self.headers)
            if response.status_code == 200:
                return response.json()
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
        except Exception as e:
            print(f"Error getting files: {e}")
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
        except Exception as e:
            print(f"Error getting file content: {e}")
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
        except Exception as e:
            print(f"Error syncing file: {e}")
            return False

# ============================================================
# DATABRICKS CLIENT
# ============================================================

class DatabricksClient:
    def __init__(self, host, token):
        self.host = host.rstrip('/')
        self.token = token
        self.headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
    
    def list_jobs(self):
        """List all jobs in Databricks workspace"""
        url = f'{self.host}/api/2.0/jobs/list'
        try:
            response = requests.get(url, headers=self.headers)
            return response.json()
        except Exception as e:
            return {'error': str(e)}
    
    def create_job(self, job_name, notebook_path, cluster_id):
        url = f'{self.host}/api/2.0/jobs/create'
        payload = {
            'name': job_name,
            'tasks': [{
                'task_key': 'main_task',
                'notebook_task': {'notebook_path': notebook_path},
                'existing_cluster_id': cluster_id
            }]
        }
        try:
            response = requests.post(url, headers=self.headers, json=payload)
            return response.json()
        except Exception as e:
            return {'error': str(e)}
    
    def run_job(self, job_id):
        url = f'{self.host}/api/2.0/jobs/run-now'
        payload = {'job_id': job_id}
        try:
            response = requests.post(url, headers=self.headers, json=payload)
            return response.json()
        except Exception as e:
            return {'error': str(e)}
    
    def get_job_status(self, run_id):
        url = f'{self.host}/api/2.0/jobs/runs/get'
        params = {'run_id': run_id}
        try:
            response = requests.get(url, headers=self.headers, params=params)
            return response.json()
        except Exception as e:
            return {'error': str(e)}
    
    def deploy_bundle(self, bundle_path, environment='dev'):
        url = f'{self.host}/api/2.0/bundles/deploy'
        payload = {
            'bundle_path': bundle_path,
            'target': environment
        }
        try:
            response = requests.post(url, headers=self.headers, json=payload)
            return response.json()
        except Exception as e:
            return {'error': str(e)}

github_client = GitHubClient(GITHUB_TOKEN) if GITHUB_TOKEN else None
databricks_client = DatabricksClient(DATABRICKS_HOST, DATABRICKS_TOKEN) if DATABRICKS_TOKEN else None

# ============================================================
# ROUTES
# ============================================================

@app.route('/')
def index():
    with get_db() as conn:
        pending = conn.execute("SELECT COUNT(*) FROM changes WHERE approval_status = 'PENDING'").fetchone()[0]
        approved = conn.execute("SELECT COUNT(*) FROM changes WHERE approval_status = 'APPROVED'").fetchone()[0]
        deployed = conn.execute("SELECT COUNT(*) FROM changes WHERE deployment_status = 'DEPLOYED'").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
        recent = conn.execute('SELECT * FROM changes ORDER BY created_at DESC LIMIT 10').fetchall()
        
        # Get projects with repo status
        projects = conn.execute('SELECT * FROM projects WHERE status = "ACTIVE"').fetchall()
    
    return render_template('index.html',
                         pending_count=pending,
                         approved_count=approved,
                         deployed_count=deployed,
                         total_count=total,
                         recent_changes=recent,
                         projects=projects,
                         github_dev=GITHUB_DEV_REPO,
                         github_prod=GITHUB_PROD_REPO)

@app.route('/changes')
def changes():
    with get_db() as conn:
        all_changes = conn.execute('SELECT * FROM changes ORDER BY created_at DESC').fetchall()
    return render_template('changes.html', changes=all_changes)

@app.route('/approvals')
def approvals():
    with get_db() as conn:
        pending = conn.execute('SELECT * FROM changes WHERE approval_status = "PENDING" ORDER BY created_at ASC').fetchall()
    return render_template('approvals.html', changes=pending)

@app.route('/cost-centers')
def cost_centers():
    with get_db() as conn:
        centers = conn.execute('SELECT * FROM cost_centers').fetchall()
    return render_template('cost_centers.html', centers=centers)

@app.route('/projects')
def projects_page():
    with get_db() as conn:
        projects = conn.execute('''
            SELECT p.*, c.name as cost_center_name 
            FROM projects p 
            LEFT JOIN cost_centers c ON p.cost_center_id = c.cc_id
        ''').fetchall()
    return render_template('projects.html', projects=projects)

@app.route('/cicd/<change_id>')
def cicd_pipeline(change_id):
    with get_db() as conn:
        change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
        if not change:
            return "Change not found", 404
        
        # Get deployment logs for this change
        logs = conn.execute('SELECT * FROM deployment_logs WHERE change_id = ? ORDER BY created_at ASC', (change_id,)).fetchall()
    
    return render_template('cicd.html', change=change, logs=logs)

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
# SWAGGER / API DOCUMENTATION
# ============================================================

@app.route('/swagger')
def swagger():
    return render_template('swagger.html')

@app.route('/api/docs')
def api_docs():
    return jsonify({
        'title': 'Databricks Orchestrator API',
        'version': '1.0.0',
        'endpoints': {
            'GET /api/cost-centers': 'Get all cost centers',
            'POST /api/cost-centers': 'Create a new cost center',
            'GET /api/projects': 'Get all projects',
            'POST /api/projects': 'Create a new project',
            'GET /api/projects/{id}': 'Get project details with repo status',
            'GET /api/changes': 'Get all changes',
            'POST /api/changes': 'Create a change request',
            'POST /api/approve/{id}': 'Approve a change',
            'POST /api/reject/{id}': 'Reject a change',
            'POST /api/deploy/{id}': 'Deploy a change to PROD',
            'POST /api/detect-changes': 'Detect changes between DEV and PROD',
            'GET /api/github/verify/{repo}': 'Verify GitHub repository',
            'GET /api/github/branches/{repo}': 'Get repository branches',
            'GET /api/github/commits/{repo}/{branch}': 'Get commits from branch',
            'POST /api/databricks/jobs/list': 'List Databricks jobs',
            'POST /api/databricks/jobs/run': 'Run a Databricks job',
            'GET /api/deployment-logs': 'Get deployment logs',
            'GET /api/audit': 'Get audit trail'
        }
    })

# ============================================================
# API ROUTES
# ============================================================

# === COST CENTER APIs ===

@app.route('/api/cost-centers', methods=['GET'])
def get_cost_centers():
    with get_db() as conn:
        centers = conn.execute('SELECT * FROM cost_centers').fetchall()
    return jsonify({'success': True, 'data': [dict(c) for c in centers]})

@app.route('/api/cost-centers', methods=['POST'])
def create_cost_center():
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
        
        return jsonify({'success': True, 'data': dict(center)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# === PROJECT APIs ===

@app.route('/api/projects', methods=['GET'])
def get_projects():
    with get_db() as conn:
        projects = conn.execute('''
            SELECT p.*, c.name as cost_center_name 
            FROM projects p 
            LEFT JOIN cost_centers c ON p.cost_center_id = c.cc_id
        ''').fetchall()
    return jsonify({'success': True, 'data': [dict(p) for p in projects]})

@app.route('/api/projects', methods=['POST'])
def create_project():
    try:
        data = request.json
        project_id = data.get('project_id')
        name = data.get('name')
        cost_center_id = data.get('cost_center_id')
        repository_url = data.get('repository_url')
        repository_name = data.get('repository_name')
        provider = data.get('provider', 'GitHub')
        branch = data.get('branch', 'main')
        aws_connection = data.get('aws_connection', '')
        
        if not project_id or not name or not cost_center_id:
            return jsonify({'success': False, 'error': 'Required fields missing'}), 400
        
        # Verify GitHub repository if token is available
        aws_status = 'PENDING'
        if github_client and repository_url:
            # Extract repo name from URL
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
            
            # Add audit entry
            conn.execute('''
                INSERT INTO audit_trail (event, project_id, repository, result)
                VALUES (?, ?, ?, ?)
            ''', ('Project Created', project_id, repository_url, 'Success'))
            conn.commit()
        
        return jsonify({'success': True, 'data': dict(project)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/projects/<project_id>', methods=['GET'])
def get_project(project_id):
    with get_db() as conn:
        project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (project_id,)).fetchone()
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        project_dict = dict(project)
        
        # Verify GitHub status
        if github_client and project_dict.get('repository_url'):
            repo_name = project_dict['repository_url'].replace('https://github.com/', '').replace('.git', '')
            repo_info = github_client.get_repo_info(repo_name)
            project_dict['repo_status'] = repo_info
            project_dict['branches'] = github_client.get_branches(repo_name)
            
            # Update aws_status based on repo verification
            if repo_info.get('exists'):
                project_dict['aws_status'] = 'CONNECTED'
            else:
                project_dict['aws_status'] = 'NOT CONNECTED'
        
        return jsonify({'success': True, 'data': project_dict})

@app.route('/api/projects/<project_id>/branches', methods=['GET'])
def get_project_branches(project_id):
    with get_db() as conn:
        project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (project_id,)).fetchone()
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        if github_client and project['repository_url']:
            repo_name = project['repository_url'].replace('https://github.com/', '').replace('.git', '')
            branches = github_client.get_branches(repo_name)
            return jsonify({'success': True, 'data': branches})
        
        return jsonify({'success': True, 'data': []})

@app.route('/api/projects/<project_id>/commits', methods=['GET'])
def get_project_commits(project_id):
    branch = request.args.get('branch', 'main')
    limit = request.args.get('limit', 10, type=int)
    
    with get_db() as conn:
        project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (project_id,)).fetchone()
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        if github_client and project['repository_url']:
            repo_name = project['repository_url'].replace('https://github.com/', '').replace('.git', '')
            commits = github_client.get_commits(repo_name, branch, limit)
            return jsonify({'success': True, 'data': commits})
        
        return jsonify({'success': True, 'data': []})

# === CHANGE APIs ===

@app.route('/api/changes', methods=['GET'])
def get_changes():
    status = request.args.get('status')
    with get_db() as conn:
        if status:
            changes = conn.execute('SELECT * FROM changes WHERE approval_status = ? ORDER BY created_at DESC', (status,)).fetchall()
        else:
            changes = conn.execute('SELECT * FROM changes ORDER BY created_at DESC').fetchall()
    return jsonify({'success': True, 'data': [dict(c) for c in changes]})

@app.route('/api/changes', methods=['POST'])
def create_change():
    try:
        data = request.json
        project_id = data.get('project_id')
        commit_sha = data.get('commit_sha')
        release_version = data.get('release_version')
        description = data.get('description', '')
        files_changed = json.dumps(data.get('files_changed', []))
        
        if not project_id or not commit_sha:
            return jsonify({'success': False, 'error': 'project_id and commit_sha required'}), 400
        
        # Generate change ID
        import random
        change_id = f"CHG-2026-{random.randint(1000, 9999)}"
        
        with get_db() as conn:
            conn.execute('''
                INSERT INTO changes 
                (change_id, project_id, commit_sha, release_version, description, files_changed)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (change_id, project_id, commit_sha, release_version, description, files_changed))
            conn.commit()
            
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            
            # Add audit entry
            conn.execute('''
                INSERT INTO audit_trail (event, change_id, project_id, commit_sha, result)
                VALUES (?, ?, ?, ?, ?)
            ''', ('Change Created', change_id, project_id, commit_sha, 'Pending'))
            conn.commit()
        
        return jsonify({'success': True, 'data': dict(change)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/detect-changes', methods=['POST'])
def detect_changes():
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
        
        # Get DEV repo from project
        dev_repo = project['repository_url'].replace('https://github.com/', '').replace('.git', '')
        
        # Use global PROD repo or derive from project
        prod_repo = GITHUB_PROD_REPO
        
        changes = github_client.compare_repos(dev_repo, prod_repo, branch)
        
        # Get recent commits from DEV branch
        commits = github_client.get_commits(dev_repo, branch, 5) if github_client else []
        
        return jsonify({
            'success': True,
            'changes': changes,
            'count': len(changes),
            'commits': commits,
            'branch': branch
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/approve/<change_id>', methods=['POST'])
def approve_change(change_id):
    try:
        data = request.json
        approver = data.get('approver', 'databricks-user')
        
        with get_db() as conn:
            conn.execute('''
                UPDATE changes 
                SET approval_status = 'APPROVED', 
                    approved_at = CURRENT_TIMESTAMP,
                    approved_by = ?
                WHERE change_id = ? AND approval_status = 'PENDING'
            ''', (approver, change_id))
            
            if conn.total_changes == 0:
                return jsonify({'success': False, 'error': 'Change not found or not pending'}), 404
            
            conn.execute('''
                INSERT INTO deployment_logs (change_id, action, status, details, stage)
                VALUES (?, 'approve', 'APPROVED', ?, 'Approval')
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

@app.route('/api/reject/<change_id>', methods=['POST'])
def reject_change(change_id):
    try:
        data = request.json
        approver = data.get('approver', 'databricks-user')
        reason = data.get('reason', 'No reason provided')
        
        with get_db() as conn:
            conn.execute('''
                UPDATE changes 
                SET approval_status = 'REJECTED', 
                    approved_at = CURRENT_TIMESTAMP,
                    approved_by = ?
                WHERE change_id = ? AND approval_status = 'PENDING'
            ''', (approver, change_id))
            
            if conn.total_changes == 0:
                return jsonify({'success': False, 'error': 'Change not found or not pending'}), 404
            
            conn.execute('''
                INSERT INTO deployment_logs (change_id, action, status, details, stage)
                VALUES (?, 'reject', 'REJECTED', ?, 'Approval')
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

@app.route('/api/deploy/<change_id>', methods=['POST'])
def deploy_change(change_id):
    try:
        data = request.json
        deployer = data.get('deployer', 'databricks-user')
        
        with get_db() as conn:
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            if not change:
                return jsonify({'success': False, 'error': 'Change not found'}), 404
            
            if change['approval_status'] != 'APPROVED':
                return jsonify({'success': False, 'error': 'Change must be approved first'}), 400
            
            # Get project
            project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (change['project_id'],)).fetchone()
            
            # Sync files to PROD
            sync_results = []
            if github_client and project and GITHUB_PROD_REPO:
                dev_repo = project['repository_url'].replace('https://github.com/', '').replace('.git', '')
                files_changed = json.loads(change['files_changed']) if change['files_changed'] else []
                
                for file_info in files_changed:
                    file_path = file_info.get('file')
                    if file_path:
                        success = github_client.sync_to_prod(dev_repo, GITHUB_PROD_REPO, file_path)
                        sync_results.append({'file': file_path, 'success': success})
            
            # Update change status
            conn.execute('''
                UPDATE changes 
                SET deployment_status = 'DEPLOYED', 
                    deployed_at = CURRENT_TIMESTAMP,
                    deployed_by = ?
                WHERE change_id = ?
            ''', (deployer, change_id))
            
            # Update project completed count
            conn.execute('''
                UPDATE projects 
                SET completed = completed + 1
                WHERE project_id = ?
            ''', (change['project_id'],))
            
            conn.execute('''
                INSERT INTO deployment_logs (change_id, action, status, details, stage)
                VALUES (?, 'deploy', 'DEPLOYED', ?, 'Deployment')
            ''', (change_id, f'Deployed by {deployer}. Synced {len(sync_results)} files.'))
            
            conn.execute('''
                INSERT INTO audit_trail (event, change_id, project_id, result, details)
                VALUES (?, ?, ?, ?, ?)
            ''', ('Change Deployed', change_id, change['project_id'], 'Success', f'Deployed by {deployer}'))
            conn.commit()
            
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
        
        return jsonify({
            'success': True, 
            'data': dict(change),
            'sync_results': sync_results
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# === GITHUB APIs ===

@app.route('/api/github/verify/<path:repo_name>', methods=['GET'])
def verify_github_repo(repo_name):
    if not github_client:
        return jsonify({'success': False, 'error': 'GitHub client not configured'}), 400
    
    repo_info = github_client.get_repo_info(repo_name)
    return jsonify({'success': True, 'data': repo_info})

@app.route('/api/github/branches/<path:repo_name>', methods=['GET'])
def get_github_branches(repo_name):
    if not github_client:
        return jsonify({'success': False, 'error': 'GitHub client not configured'}), 400
    
    branches = github_client.get_branches(repo_name)
    return jsonify({'success': True, 'data': branches})

@app.route('/api/github/commits/<path:repo_name>/<branch>', methods=['GET'])
def get_github_commits(repo_name, branch):
    if not github_client:
        return jsonify({'success': False, 'error': 'GitHub client not configured'}), 400
    
    limit = request.args.get('limit', 10, type=int)
    commits = github_client.get_commits(repo_name, branch, limit)
    return jsonify({'success': True, 'data': commits})

# === DATABRICKS APIs ===

@app.route('/api/databricks/jobs/list', methods=['GET'])
def list_databricks_jobs():
    if not databricks_client:
        return jsonify({'success': False, 'error': 'Databricks client not configured'}), 400
    
    result = databricks_client.list_jobs()
    return jsonify({'success': True, 'data': result})

@app.route('/api/databricks/jobs/run', methods=['POST'])
def run_databricks_job():
    if not databricks_client:
        return jsonify({'success': False, 'error': 'Databricks client not configured'}), 400
    
    data = request.json
    job_id = data.get('job_id')
    if not job_id:
        return jsonify({'success': False, 'error': 'job_id required'}), 400
    
    result = databricks_client.run_job(job_id)
    return jsonify({'success': True, 'data': result})

# === LOGS AND AUDIT ===

@app.route('/api/deployment-logs', methods=['GET'])
def get_deployment_logs():
    change_id = request.args.get('change_id')
    with get_db() as conn:
        if change_id:
            logs = conn.execute('SELECT * FROM deployment_logs WHERE change_id = ? ORDER BY created_at DESC', (change_id,)).fetchall()
        else:
            logs = conn.execute('SELECT * FROM deployment_logs ORDER BY created_at DESC LIMIT 50').fetchall()
    return jsonify({'success': True, 'data': [dict(log) for log in logs]})

@app.route('/api/audit', methods=['GET'])
def get_audit_trail():
    limit = request.args.get('limit', 50, type=int)
    with get_db() as conn:
        audits = conn.execute('SELECT * FROM audit_trail ORDER BY time DESC LIMIT ?', (limit,)).fetchall()
    return jsonify({'success': True, 'data': [dict(a) for a in audits]})

# ============================================================
# RUN APP
# ============================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Starting app on port {port}")
    print(f"📁 Database: {DB_PATH}")
    print(f"🔑 GitHub configured: {bool(GITHUB_TOKEN)}")
    print(f"🔑 Databricks configured: {bool(DATABRICKS_TOKEN)}")
    print(f"📁 GitHub DEV Repo: {GITHUB_DEV_REPO}")
    print(f"📁 GitHub PROD Repo: {GITHUB_PROD_REPO}")
    app.run(host='0.0.0.0', port=port, debug=False)
