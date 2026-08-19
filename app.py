from flask import Flask, render_template, request, jsonify
import os
import json
import requests
import base64
from datetime import datetime
import sqlite3
import random
import re

# ============================================================
# CONFIGURATION
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
app.secret_key = os.environ.get('SECRET_KEY', 'default-secret-key')

# ============================================================
# DATABASE
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'orchestrator.db')

if os.path.exists(DB_PATH):
    try:
        os.remove(DB_PATH)
        print("🗑️ Old database deleted - Fresh start!")
    except:
        pass

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    try:
        with get_db() as conn:
            # Cost Centers
            conn.execute('''
                CREATE TABLE IF NOT EXISTS cost_centers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cc_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    perspective TEXT,
                    owner TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Projects
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
                    running INTEGER DEFAULT 0,
                    completed INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (cost_center_id) REFERENCES cost_centers(cc_id)
                )
            ''')
            
            # Changes / PRs
            conn.execute('''
                CREATE TABLE IF NOT EXISTS changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    change_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL,
                    pr_number INTEGER,
                    pr_url TEXT,
                    commit_sha TEXT,
                    release_version TEXT,
                    description TEXT,
                    files_changed TEXT,
                    branch TEXT DEFAULT 'main',
                    approval_status TEXT DEFAULT 'Pending',
                    deployment_status TEXT DEFAULT 'Blocked',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    approved_at TIMESTAMP,
                    approved_by TEXT,
                    merged_at TIMESTAMP,
                    merged_by TEXT,
                    deployed_at TIMESTAMP,
                    deployed_by TEXT,
                    FOREIGN KEY (project_id) REFERENCES projects(project_id)
                )
            ''')
            
            # Deployment Logs
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
            
            # Audit Trail
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
            
            # Insert default data
            if conn.execute('SELECT COUNT(*) FROM cost_centers').fetchone()[0] == 0:
                conn.execute('''
                    INSERT INTO cost_centers (cc_id, name, perspective, owner) VALUES
                    ('CC-2200', 'Finance', 'Finance', 'Finance Owner'),
                    ('CC-1200', 'Sales', 'Sales', 'Sales Owner'),
                    ('CC-3100', 'Operations', 'Operations', 'Operations Owner'),
                    ('CC-4100', 'Customer Data', 'Customer / Data', 'Data Owner')
                ''')
            
            conn.commit()
            print("✅ Database initialized")
            return True
    except Exception as e:
        print(f"❌ DB init error: {e}")
        return False

init_db()

# ============================================================
# GITHUB CLIENT - COMPLETE
# ============================================================

class GitHubClient:
    def __init__(self, token):
        self.token = token
        self.headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json'
        }
    
    def verify_repo(self, repo_name):
        """Check if repository exists and is accessible"""
        try:
            response = requests.get(f'https://api.github.com/repos/{repo_name}', headers=self.headers)
            return response.status_code == 200
        except:
            return False
    
    def get_repo_info(self, repo_name):
        """Get repository information"""
        try:
            response = requests.get(f'https://api.github.com/repos/{repo_name}', headers=self.headers)
            if response.status_code == 200:
                data = response.json()
                return {
                    'exists': True,
                    'name': data.get('name'),
                    'full_name': data.get('full_name'),
                    'default_branch': data.get('default_branch'),
                    'private': data.get('private', False)
                }
            return {'exists': False}
        except:
            return {'exists': False}
    
    def get_branches(self, repo_name):
        """Get all branches of a repository"""
        try:
            response = requests.get(f'https://api.github.com/repos/{repo_name}/branches', headers=self.headers)
            if response.status_code == 200:
                return [b['name'] for b in response.json()]
            return []
        except:
            return []
    
    def get_commits(self, repo_name, branch='main', limit=10):
        """Get recent commits from a branch"""
        try:
            response = requests.get(
                f'https://api.github.com/repos/{repo_name}/commits?sha={branch}&per_page={limit}',
                headers=self.headers
            )
            if response.status_code == 200:
                return [{
                    'sha': c['sha'],
                    'message': c['commit']['message'],
                    'author': c['commit']['author']['name'],
                    'date': c['commit']['author']['date']
                } for c in response.json()]
            return []
        except:
            return []
    
    def create_pull_request(self, repo_name, title, body, head_branch, base_branch='main'):
        """Create a Pull Request"""
        url = f'https://api.github.com/repos/{repo_name}/pulls'
        payload = {
            'title': title,
            'body': body,
            'head': head_branch,
            'base': base_branch
        }
        try:
            response = requests.post(url, headers=self.headers, json=payload)
            if response.status_code in [200, 201]:
                data = response.json()
                return {
                    'success': True,
                    'number': data.get('number'),
                    'url': data.get('html_url'),
                    'state': data.get('state')
                }
            return {'success': False, 'error': f'Status: {response.status_code}'}
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def get_pull_request(self, repo_name, pr_number):
        """Get PR details"""
        url = f'https://api.github.com/repos/{repo_name}/pulls/{pr_number}'
        try:
            response = requests.get(url, headers=self.headers)
            if response.status_code == 200:
                data = response.json()
                return {
                    'number': data.get('number'),
                    'state': data.get('state'),
                    'title': data.get('title'),
                    'body': data.get('body'),
                    'url': data.get('html_url'),
                    'mergeable': data.get('mergeable', False)
                }
            return None
        except:
            return None
    
    def merge_pull_request(self, repo_name, pr_number):
        """Merge a Pull Request"""
        url = f'https://api.github.com/repos/{repo_name}/pulls/{pr_number}/merge'
        payload = {
            'commit_title': f'Merge PR #{pr_number}',
            'merge_method': 'merge'
        }
        try:
            response = requests.put(url, headers=self.headers, json=payload)
            if response.status_code == 200:
                return {'success': True, 'merged': True}
            return {'success': False, 'error': f'Status: {response.status_code}'}
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def get_file_content(self, repo_name, file_path, branch='main'):
        """Get file content from a repository"""
        try:
            response = requests.get(
                f'https://api.github.com/repos/{repo_name}/contents/{file_path}?ref={branch}',
                headers=self.headers
            )
            if response.status_code == 200:
                data = response.json()
                content = base64.b64decode(data['content']).decode('utf-8')
                return {'content': content, 'sha': data['sha']}
            return None
        except:
            return None
    
    def get_all_files(self, repo_name, branch='main'):
        """Get all files from a repository"""
        try:
            response = requests.get(
                f'https://api.github.com/repos/{repo_name}/git/trees/{branch}?recursive=1',
                headers=self.headers
            )
            if response.status_code == 200:
                return [f for f in response.json().get('tree', []) if f['type'] == 'blob']
            return []
        except:
            return []
    
    def compare_repos(self, dev_repo, prod_repo, branch='main'):
        """Compare DEV and PROD repositories"""
        dev_files = self.get_all_files(dev_repo, branch)
        prod_files = self.get_all_files(prod_repo, branch)
        
        dev_paths = {f['path'] for f in dev_files}
        prod_paths = {f['path'] for f in prod_files}
        
        changes = []
        
        # New files
        for path in dev_paths - prod_paths:
            changes.append({'file': path, 'type': 'new'})
        
        # Modified files
        for path in dev_paths & prod_paths:
            dev_content = self.get_file_content(dev_repo, path, branch)
            prod_content = self.get_file_content(prod_repo, path, branch)
            if dev_content and prod_content and dev_content['content'] != prod_content['content']:
                changes.append({'file': path, 'type': 'modified'})
        
        # Deleted files
        for path in prod_paths - dev_paths:
            changes.append({'file': path, 'type': 'deleted'})
        
        return changes
    
    def sync_to_prod(self, dev_repo, prod_repo, file_path, branch='main'):
        """Sync a file from DEV to PROD"""
        content = self.get_file_content(dev_repo, file_path, branch)
        if not content:
            return False
        
        url = f'https://api.github.com/repos/{prod_repo}/contents/{file_path}'
        try:
            # Check if file exists in PROD
            response = requests.get(url, headers=self.headers)
            payload = {
                'message': f'[BOT] Sync {file_path} from DEV',
                'content': base64.b64encode(content['content'].encode()).decode(),
                'branch': branch
            }
            if response.status_code == 200:
                payload['sha'] = response.json()['sha']
            
            response = requests.put(url, headers=self.headers, json=payload)
            return response.status_code in [200, 201]
        except:
            return False

github_client = GitHubClient(GITHUB_TOKEN) if GITHUB_TOKEN else None

# ============================================================
# PAGE ROUTES
# ============================================================

@app.route('/')
def index():
    with get_db() as conn:
        pending = conn.execute("SELECT COUNT(*) FROM changes WHERE approval_status = 'Pending'").fetchone()[0]
        approved = conn.execute("SELECT COUNT(*) FROM changes WHERE approval_status = 'Approved'").fetchone()[0]
        merged = conn.execute("SELECT COUNT(*) FROM changes WHERE deployment_status = 'Merged'").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
        
        cost_centers = conn.execute('SELECT * FROM cost_centers').fetchall()
        projects = conn.execute('SELECT * FROM projects').fetchall()
        recent = conn.execute('SELECT * FROM changes ORDER BY created_at DESC LIMIT 10').fetchall()
        
        # Get running count
        running = conn.execute("SELECT SUM(running) FROM projects").fetchone()[0] or 0
    
    return render_template('index.html',
                         pending_count=pending,
                         approved_count=approved,
                         merged_count=merged,
                         total_count=total,
                         running_count=running,
                         cost_centers=cost_centers,
                         projects=projects,
                         recent_changes=recent,
                         github_dev=GITHUB_DEV_REPO,
                         github_prod=GITHUB_PROD_REPO)

@app.route('/cost-centers')
def cost_centers_page():
    with get_db() as conn:
        centers = conn.execute('SELECT * FROM cost_centers').fetchall()
    return render_template('cost_centers.html', centers=centers)

@app.route('/projects')
def projects_page():
    with get_db() as conn:
        projects = conn.execute('SELECT * FROM projects').fetchall()
        cost_centers = conn.execute('SELECT cc_id, name FROM cost_centers').fetchall()
    return render_template('projects.html', projects=projects, cost_centers=cost_centers)

@app.route('/changes')
def changes_page():
    with get_db() as conn:
        changes = conn.execute('SELECT * FROM changes ORDER BY created_at DESC').fetchall()
        projects = conn.execute('SELECT project_id, name FROM projects').fetchall()
    return render_template('changes.html', changes=changes, projects=projects)

@app.route('/approvals')
def approvals_page():
    with get_db() as conn:
        pending = conn.execute('SELECT * FROM changes WHERE approval_status = "Pending" ORDER BY created_at ASC').fetchall()
    return render_template('approvals.html', changes=pending)

@app.route('/deployment')
def deployment_page():
    return render_template('deployment.html')

@app.route('/audit')
def audit_page():
    with get_db() as conn:
        audit = conn.execute('SELECT * FROM audit_trail ORDER BY time DESC LIMIT 100').fetchall()
    return render_template('audit.html', audit=audit)

@app.route('/repository')
def repository_page():
    return render_template('repository.html', github_dev=GITHUB_DEV_REPO, github_prod=GITHUB_PROD_REPO)

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
        'github_configured': bool(GITHUB_TOKEN),
        'github_dev': GITHUB_DEV_REPO,
        'github_prod': GITHUB_PROD_REPO
    })

# ============================================================
# API ROUTES
# ============================================================

@app.route('/api/cost-centers', methods=['GET'])
def api_get_cost_centers():
    with get_db() as conn:
        return jsonify({'success': True, 'data': [dict(c) for c in conn.execute('SELECT * FROM cost_centers').fetchall()]})

@app.route('/api/cost-centers', methods=['POST'])
def api_create_cost_center():
    try:
        data = request.json
        with get_db() as conn:
            conn.execute('INSERT INTO cost_centers (cc_id, name, perspective, owner) VALUES (?, ?, ?, ?)',
                        (data['cc_id'], data['name'], data.get('perspective', 'General'), data.get('owner', '')))
            conn.commit()
            return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/projects', methods=['GET'])
def api_get_projects():
    with get_db() as conn:
        return jsonify({'success': True, 'data': [dict(p) for p in conn.execute('SELECT * FROM projects').fetchall()]})

@app.route('/api/projects', methods=['POST'])
def api_create_project():
    try:
        data = request.json
        with get_db() as conn:
            conn.execute('''
                INSERT INTO projects (project_id, name, cost_center_id, repository_url, repository_name, provider, branch, aws_status, aws_connection)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (data['project_id'], data['name'], data['cost_center_id'], 
                  data.get('repository_url', ''), data.get('repository_name', ''),
                  data.get('provider', 'GitHub'), data.get('branch', 'main'),
                  data.get('aws_status', 'PENDING'), data.get('aws_connection', '')))
            conn.commit()
            return jsonify({'success': True, 'message': 'Project created'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/projects/<project_id>', methods=['DELETE'])
def api_delete_project(project_id):
    try:
        with get_db() as conn:
            conn.execute('DELETE FROM changes WHERE project_id = ?', (project_id,))
            conn.execute('DELETE FROM projects WHERE project_id = ?', (project_id,))
            conn.commit()
            return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/projects/<project_id>/verify', methods=['GET'])
def api_verify_project(project_id):
    with get_db() as conn:
        project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (project_id,)).fetchone()
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        if not github_client:
            return jsonify({'success': False, 'error': 'GitHub not configured. Set GITHUB_TOKEN environment variable.'}), 400
        
        repo_name = project['repository_url'].replace('https://github.com/', '').replace('.git', '')
        
        # Verify repo
        connected = github_client.verify_repo(repo_name)
        branches = github_client.get_branches(repo_name) if connected else []
        
        # Update status
        conn.execute('UPDATE projects SET aws_status = ? WHERE project_id = ?', 
                    ('CONNECTED' if connected else 'NOT CONNECTED', project_id))
        conn.commit()
        
        return jsonify({
            'success': True,
            'connected': connected,
            'branches': branches,
            'repo_name': repo_name
        })

@app.route('/api/projects/<project_id>/branches', methods=['GET'])
def api_get_branches(project_id):
    with get_db() as conn:
        project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (project_id,)).fetchone()
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        if not github_client:
            return jsonify({'success': False, 'error': 'GitHub not configured'}), 400
        
        repo_name = project['repository_url'].replace('https://github.com/', '').replace('.git', '')
        branches = github_client.get_branches(repo_name)
        
        return jsonify({'success': True, 'data': branches})

@app.route('/api/projects/<project_id>/commits', methods=['GET'])
def api_get_commits(project_id):
    branch = request.args.get('branch', 'main')
    limit = request.args.get('limit', 10, type=int)
    
    with get_db() as conn:
        project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (project_id,)).fetchone()
        if not project:
            return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        if not github_client:
            return jsonify({'success': False, 'error': 'GitHub not configured'}), 400
        
        repo_name = project['repository_url'].replace('https://github.com/', '').replace('.git', '')
        commits = github_client.get_commits(repo_name, branch, limit)
        
        return jsonify({'success': True, 'data': commits})

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
            return jsonify({'success': False, 'error': 'GitHub not configured'}), 400
        
        if not GITHUB_PROD_REPO:
            return jsonify({'success': False, 'error': 'GITHUB_PROD_REPO not configured'}), 400
        
        dev_repo = project['repository_url'].replace('https://github.com/', '').replace('.git', '')
        prod_repo = GITHUB_PROD_REPO
        
        # Get diff between DEV and PROD
        changes = github_client.compare_repos(dev_repo, prod_repo, branch)
        commits = github_client.get_commits(dev_repo, branch, 5)
        
        return jsonify({
            'success': True,
            'changes': changes,
            'count': len(changes),
            'commits': commits,
            'dev_repo': dev_repo,
            'prod_repo': prod_repo,
            'branch': branch
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/create-pr', methods=['POST'])
def api_create_pr():
    try:
        data = request.json
        project_id = data.get('project_id')
        branch = data.get('branch', 'main')
        title = data.get('title', 'Auto-generated PR')
        description = data.get('description', '')
        files_changed = data.get('files_changed', [])
        commit_sha = data.get('commit_sha', '')
        release_version = data.get('release_version', '')
        
        if not project_id:
            return jsonify({'success': False, 'error': 'project_id required'}), 400
        
        with get_db() as conn:
            project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (project_id,)).fetchone()
            if not project:
                return jsonify({'success': False, 'error': 'Project not found'}), 404
        
        if not github_client:
            return jsonify({'success': False, 'error': 'GitHub not configured'}), 400
        
        # Create PR
        repo_name = project['repository_url'].replace('https://github.com/', '').replace('.git', '')
        
        # If PROD repo is different, use it
        if GITHUB_PROD_REPO:
            repo_name = GITHUB_PROD_REPO
        
        pr_title = title if title else f"PR: {project_id} - {description[:50]}"
        pr_body = f"""
## Change Request
- **Project**: {project_id}
- **Branch**: {branch}
- **Commit**: {commit_sha}
- **Release**: {release_version or 'N/A'}

## Files Changed
{chr(10).join(['- ' + f.get('file', '') + ' (' + f.get('type', 'modified') + ')' for f in files_changed])}

## Description
{description}
"""
        
        # Create PR
        pr_result = github_client.create_pull_request(
            repo_name=repo_name,
            title=pr_title,
            body=pr_body,
            head_branch=branch,
            base_branch='main'
        )
        
        if not pr_result.get('success'):
            return jsonify({'success': False, 'error': pr_result.get('error', 'Failed to create PR')}), 400
        
        # Save to database
        change_id = f"CHG-2026-{random.randint(1000, 9999)}"
        
        with get_db() as conn:
            conn.execute('''
                INSERT INTO changes 
                (change_id, project_id, pr_number, pr_url, commit_sha, release_version, 
                 description, files_changed, branch, approval_status, deployment_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending', 'Blocked')
            ''', (change_id, project_id, pr_result.get('number'), pr_result.get('url'),
                  commit_sha, release_version, description, json.dumps(files_changed), branch))
            conn.commit()
            
            # Audit trail
            conn.execute('''
                INSERT INTO audit_trail (event, change_id, project_id, commit_sha, result, details)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', ('PR Created', change_id, project_id, commit_sha, 'Pending', f'PR #{pr_result.get("number")} created'))
            conn.commit()
            
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
        
        return jsonify({
            'success': True,
            'data': dict(change),
            'pr': pr_result
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/changes', methods=['GET'])
def api_get_changes():
    status = request.args.get('status')
    with get_db() as conn:
        if status:
            changes = conn.execute('SELECT * FROM changes WHERE approval_status = ? ORDER BY created_at DESC', (status,)).fetchall()
        else:
            changes = conn.execute('SELECT * FROM changes ORDER BY created_at DESC').fetchall()
        return jsonify({'success': True, 'data': [dict(c) for c in changes]})

@app.route('/api/changes', methods=['POST'])
def api_create_change():
    try:
        data = request.json
        change_id = f"CHG-2026-{random.randint(1000, 9999)}"
        
        with get_db() as conn:
            conn.execute('''
                INSERT INTO changes (change_id, project_id, commit_sha, release_version, description, files_changed, branch)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (change_id, data['project_id'], data.get('commit_sha'), data.get('release_version'), 
                  data.get('description', ''), json.dumps(data.get('files_changed', [])), data.get('branch', 'main')))
            conn.commit()
            
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            return jsonify({'success': True, 'data': dict(change)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/changes/<change_id>/approve', methods=['POST'])
def api_approve_change(change_id):
    try:
        with get_db() as conn:
            conn.execute('''
                UPDATE changes SET approval_status = 'Approved', approved_at = CURRENT_TIMESTAMP, approved_by = ?
                WHERE change_id = ? AND approval_status = 'Pending'
            ''', ('databricks-user', change_id))
            
            if conn.total_changes == 0:
                return jsonify({'success': False, 'error': 'Change not found'}), 404
            
            conn.execute('''
                INSERT INTO audit_trail (event, change_id, result, details)
                VALUES (?, ?, ?, ?)
            ''', ('Change Approved', change_id, 'Success', 'Change approved for deployment'))
            conn.commit()
            
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            return jsonify({'success': True, 'data': dict(change) if change else None})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/changes/<change_id>/merge', methods=['POST'])
def api_merge_change(change_id):
    try:
        with get_db() as conn:
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            if not change:
                return jsonify({'success': False, 'error': 'Change not found'}), 404
            
            if change['approval_status'] != 'Approved':
                return jsonify({'success': False, 'error': 'Change must be approved before merging'}), 400
            
            if not github_client:
                return jsonify({'success': False, 'error': 'GitHub not configured'}), 400
            
            # Get project
            project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (change['project_id'],)).fetchone()
            if not project:
                return jsonify({'success': False, 'error': 'Project not found'}), 404
            
            repo_name = project['repository_url'].replace('https://github.com/', '').replace('.git', '')
            if GITHUB_PROD_REPO:
                repo_name = GITHUB_PROD_REPO
            
            # Merge PR
            pr_number = change['pr_number']
            if pr_number:
                merge_result = github_client.merge_pull_request(repo_name, pr_number)
                if not merge_result.get('success'):
                    return jsonify({'success': False, 'error': merge_result.get('error', 'Merge failed')}), 400
            
            conn.execute('''
                UPDATE changes 
                SET deployment_status = 'Merged', 
                    merged_at = CURRENT_TIMESTAMP,
                    merged_by = ?
                WHERE change_id = ?
            ''', ('databricks-user', change_id))
            
            conn.execute('''
                INSERT INTO audit_trail (event, change_id, project_id, result, details)
                VALUES (?, ?, ?, ?, ?)
            ''', ('PR Merged', change_id, change['project_id'], 'Success', f'PR #{pr_number} merged'))
            conn.commit()
            
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            return jsonify({'success': True, 'data': dict(change)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/changes/<change_id>/reject', methods=['POST'])
def api_reject_change(change_id):
    try:
        reason = request.json.get('reason', 'No reason provided')
        with get_db() as conn:
            conn.execute('''
                UPDATE changes SET approval_status = 'Rejected', approved_at = CURRENT_TIMESTAMP, approved_by = ?
                WHERE change_id = ? AND approval_status = 'Pending'
            ''', ('databricks-user', change_id))
            
            conn.execute('''
                INSERT INTO audit_trail (event, change_id, result, details)
                VALUES (?, ?, ?, ?)
            ''', ('Change Rejected', change_id, 'Rejected', reason))
            conn.commit()
            
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            return jsonify({'success': True, 'data': dict(change) if change else None})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/changes/<change_id>/deploy', methods=['POST'])
def api_deploy_change(change_id):
    try:
        with get_db() as conn:
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            if not change:
                return jsonify({'success': False, 'error': 'Change not found'}), 404
            
            if change['approval_status'] != 'Approved':
                return jsonify({'success': False, 'error': 'Change must be approved first'}), 400
            
            if change['deployment_status'] == 'Merged':
                return jsonify({'success': False, 'error': 'Change already merged'}), 400
            
            project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (change['project_id'],)).fetchone()
            
            sync_results = []
            if github_client and project and GITHUB_PROD_REPO:
                dev_repo = project['repository_url'].replace('https://github.com/', '').replace('.git', '')
                files = json.loads(change['files_changed']) if change['files_changed'] else []
                for f in files:
                    if f.get('file'):
                        success = github_client.sync_to_prod(dev_repo, GITHUB_PROD_REPO, f['file'], change['branch'] or 'main')
                        sync_results.append({'file': f['file'], 'success': success})
            
            conn.execute('''
                UPDATE changes SET deployment_status = 'Deployed', deployed_at = CURRENT_TIMESTAMP, deployed_by = ?
                WHERE change_id = ?
            ''', ('databricks-user', change_id))
            
            conn.execute('UPDATE projects SET completed = completed + 1 WHERE project_id = ?', (change['project_id'],))
            conn.commit()
            
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            return jsonify({'success': True, 'data': dict(change), 'sync_results': sync_results})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/deployment-logs', methods=['GET'])
def api_get_deployment_logs():
    with get_db() as conn:
        logs = conn.execute('SELECT * FROM deployment_logs ORDER BY created_at DESC LIMIT 50').fetchall()
        return jsonify({'success': True, 'data': [dict(l) for l in logs]})

@app.route('/api/audit', methods=['GET'])
def api_get_audit():
    with get_db() as conn:
        audit = conn.execute('SELECT * FROM audit_trail ORDER BY time DESC LIMIT 50').fetchall()
        return jsonify({'success': True, 'data': [dict(a) for a in audit]})

# ============================================================
# RUN APP
# ============================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Starting app on port {port}")
    print(f"🔑 GitHub configured: {bool(GITHUB_TOKEN)}")
    print(f"📁 GitHub DEV: {GITHUB_DEV_REPO}")
    print(f"📁 GitHub PROD: {GITHUB_PROD_REPO}")
    app.run(host='0.0.0.0', port=port, debug=False)