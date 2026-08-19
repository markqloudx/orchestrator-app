from flask import Flask, render_template, request, jsonify
import os
import json
import requests
import base64
import sqlite3
import random
from datetime import datetime

# ============================================================
# CONFIGURATION
# ============================================================

DATABRICKS_HOST = os.environ.get('DATABRICKS_HOST', '')
DATABRICKS_TOKEN = os.environ.get('DATABRICKS_TOKEN', '')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
GITHUB_DEV_REPO = os.environ.get('GITHUB_DEV_REPO', '')  # e.g., "username/databricks-dev"
GITHUB_PROD_REPO = os.environ.get('GITHUB_PROD_REPO', '')  # e.g., "username/databricks-prod"

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
        print("🗑️ Fresh start!")
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
                    dev_repo_url TEXT,
                    dev_repo_name TEXT,
                    prod_repo_url TEXT,
                    prod_repo_name TEXT,
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
                    databricks_deployed_at TIMESTAMP,
                    databricks_deployed_by TEXT,
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
            
            # Default data
            if conn.execute('SELECT COUNT(*) FROM cost_centers').fetchone()[0] == 0:
                conn.execute('''
                    INSERT INTO cost_centers (cc_id, name, perspective, owner) VALUES
                    ('CC-2200', 'Finance', 'Finance', 'Finance Owner'),
                    ('CC-1200', 'Sales', 'Sales', 'Sales Owner'),
                    ('CC-3100', 'Operations', 'Operations', 'Operations Owner'),
                    ('CC-4100', 'Customer Data', 'Customer / Data', 'Data Owner')
                ''')
                
                # Default project with DEV and PROD repos
                conn.execute('''
                    INSERT INTO projects (project_id, name, cost_center_id, dev_repo_url, dev_repo_name, prod_repo_url, prod_repo_name, provider, branch, aws_status)
                    VALUES (
                        'PRJ-001', 
                        'Customer Analytics', 
                        'CC-4100',
                        'https://github.com/username/databricks-dev',
                        'databricks-dev',
                        'https://github.com/username/databricks-prod',
                        'databricks-prod',
                        'GitHub',
                        'main',
                        'PENDING'
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
    
    def verify_repo(self, repo_name):
        try:
            r = requests.get(f'https://api.github.com/repos/{repo_name}', headers=self.headers)
            return r.status_code == 200
        except:
            return False
    
    def get_branches(self, repo_name):
        try:
            r = requests.get(f'https://api.github.com/repos/{repo_name}/branches', headers=self.headers)
            if r.status_code == 200:
                return [b['name'] for b in r.json()]
            return []
        except:
            return []
    
    def get_commits(self, repo_name, branch='main', limit=10):
        try:
            r = requests.get(f'https://api.github.com/repos/{repo_name}/commits?sha={branch}&per_page={limit}', headers=self.headers)
            if r.status_code == 200:
                return [{
                    'sha': c['sha'],
                    'message': c['commit']['message'],
                    'author': c['commit']['author']['name'],
                    'date': c['commit']['author']['date']
                } for c in r.json()]
            return []
        except:
            return []
    
    def get_all_files(self, repo_name, branch='main'):
        try:
            r = requests.get(f'https://api.github.com/repos/{repo_name}/git/trees/{branch}?recursive=1', headers=self.headers)
            if r.status_code == 200:
                return [f for f in r.json().get('tree', []) if f['type'] == 'blob']
            return []
        except:
            return []
    
    def get_file_content(self, repo_name, file_path, branch='main'):
        try:
            r = requests.get(f'https://api.github.com/repos/{repo_name}/contents/{file_path}?ref={branch}', headers=self.headers)
            if r.status_code == 200:
                data = r.json()
                content = base64.b64decode(data['content']).decode('utf-8')
                return {'content': content, 'sha': data['sha']}
            return None
        except:
            return None
    
    def compare_repos(self, dev_repo, prod_repo, branch='main'):
        """Compare DEV and PROD repositories"""
        dev_files = self.get_all_files(dev_repo, branch)
        prod_files = self.get_all_files(prod_repo, branch)
        
        dev_paths = {f['path'] for f in dev_files}
        prod_paths = {f['path'] for f in prod_files}
        
        changes = []
        
        # New files (in DEV but not PROD)
        for path in dev_paths - prod_paths:
            changes.append({'file': path, 'type': 'new'})
        
        # Modified files (content different)
        for path in dev_paths & prod_paths:
            dev_content = self.get_file_content(dev_repo, path, branch)
            prod_content = self.get_file_content(prod_repo, path, branch)
            if dev_content and prod_content and dev_content['content'] != prod_content['content']:
                changes.append({'file': path, 'type': 'modified'})
        
        # Deleted files (in PROD but not DEV)
        for path in prod_paths - dev_paths:
            changes.append({'file': path, 'type': 'deleted'})
        
        return changes
    
    def create_pull_request(self, repo_name, title, body, head_branch, base_branch='main'):
        """Create a Pull Request to PROD repo"""
        url = f'https://api.github.com/repos/{repo_name}/pulls'
        payload = {
            'title': title,
            'body': body,
            'head': head_branch,
            'base': base_branch
        }
        try:
            r = requests.post(url, headers=self.headers, json=payload)
            if r.status_code in [200, 201]:
                data = r.json()
                return {
                    'success': True,
                    'number': data.get('number'),
                    'url': data.get('html_url'),
                    'state': data.get('state')
                }
            return {'success': False, 'error': f'Status: {r.status_code}'}
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def merge_pull_request(self, repo_name, pr_number):
        """Merge PR to PROD repo"""
        url = f'https://api.github.com/repos/{repo_name}/pulls/{pr_number}/merge'
        payload = {
            'commit_title': f'Merge PR #{pr_number} from DEV',
            'merge_method': 'merge'
        }
        try:
            r = requests.put(url, headers=self.headers, json=payload)
            if r.status_code == 200:
                return {'success': True, 'merged': True}
            return {'success': False, 'error': f'Status: {r.status_code}'}
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def sync_to_prod(self, dev_repo, prod_repo, file_path, branch='main'):
        """Sync a file from DEV to PROD repo"""
        content = self.get_file_content(dev_repo, file_path, branch)
        if not content:
            return False
        
        url = f'https://api.github.com/repos/{prod_repo}/contents/{file_path}'
        try:
            r = requests.get(url, headers=self.headers)
            payload = {
                'message': f'[BOT] Sync {file_path} from DEV',
                'content': base64.b64encode(content['content'].encode()).decode(),
                'branch': branch
            }
            if r.status_code == 200:
                payload['sha'] = r.json()['sha']
            
            r = requests.put(url, headers=self.headers, json=payload)
            return r.status_code in [200, 201]
        except:
            return False

github_client = GitHubClient(GITHUB_TOKEN) if GITHUB_TOKEN else None

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
    
    def deploy_to_databricks(self, project_path, environment='prod'):
        """Deploy to Databricks using Databricks CLI or API"""
        # For Databricks Community Edition, we'll simulate
        # In real implementation, this would use databricks bundle deploy
        print(f"📦 Deploying to Databricks: {project_path} -> {environment}")
        
        # Return success for demo
        return {
            'success': True,
            'message': f'Deployed to Databricks {environment}',
            'timestamp': datetime.now().isoformat()
        }
    
    def run_job(self, job_id):
        """Run a Databricks job"""
        url = f'{self.host}/api/2.0/jobs/run-now'
        payload = {'job_id': job_id}
        try:
            r = requests.post(url, headers=self.headers, json=payload)
            return r.json()
        except Exception as e:
            return {'error': str(e)}

databricks_client = DatabricksClient(DATABRICKS_HOST, DATABRICKS_TOKEN) if DATABRICKS_TOKEN else None

# ============================================================
# PAGE ROUTES
# ============================================================

@app.route('/')
def index():
    with get_db() as conn:
        pending = conn.execute("SELECT COUNT(*) FROM changes WHERE approval_status = 'Pending'").fetchone()[0]
        approved = conn.execute("SELECT COUNT(*) FROM changes WHERE approval_status = 'Approved'").fetchone()[0]
        merged = conn.execute("SELECT COUNT(*) FROM changes WHERE deployment_status = 'Merged'").fetchone()[0]
        deployed = conn.execute("SELECT COUNT(*) FROM changes WHERE deployment_status = 'Deployed'").fetchone()[0]
        
        cost_centers = conn.execute('SELECT * FROM cost_centers').fetchall()
        projects = conn.execute('SELECT * FROM projects').fetchall()
        recent = conn.execute('SELECT * FROM changes ORDER BY created_at DESC LIMIT 10').fetchall()
        running = conn.execute("SELECT SUM(running) FROM projects").fetchone()[0] or 0
    
    return render_template('index.html',
                         pending_count=pending,
                         approved_count=approved,
                         merged_count=merged,
                         deployed_count=deployed,
                         total_count=pending + approved + merged + deployed,
                         running_count=running,
                         cost_centers=cost_centers,
                         projects=projects,
                         recent_changes=recent,
                         github_dev=GITHUB_DEV_REPO,
                         github_prod=GITHUB_PROD_REPO)

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

@app.route('/cost-centers')
def cost_centers_page():
    with get_db() as conn:
        centers = conn.execute('SELECT * FROM cost_centers').fetchall()
    return render_template('cost_centers.html', centers=centers)

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
        'github_prod': GITHUB_PROD_REPO,
        'databricks_configured': bool(DATABRICKS_TOKEN)
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
                INSERT INTO projects (project_id, name, cost_center_id, dev_repo_url, dev_repo_name, prod_repo_url, prod_repo_name, provider, branch, aws_status, aws_connection)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                data['project_id'], 
                data['name'], 
                data['cost_center_id'],
                data.get('dev_repo_url', ''),
                data.get('dev_repo_name', ''),
                data.get('prod_repo_url', ''),
                data.get('prod_repo_name', ''),
                data.get('provider', 'GitHub'),
                data.get('branch', 'main'),
                data.get('aws_status', 'PENDING'),
                data.get('aws_connection', '')
            ))
            conn.commit()
            return jsonify({'success': True})
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
            return jsonify({'success': False, 'error': 'GitHub not configured'}), 400
        
        # Verify DEV repo
        dev_repo_name = project['dev_repo_name'] or project['dev_repo_url'].replace('https://github.com/', '').replace('.git', '')
        dev_connected = github_client.verify_repo(dev_repo_name)
        dev_branches = github_client.get_branches(dev_repo_name) if dev_connected else []
        
        # Verify PROD repo
        prod_repo_name = project['prod_repo_name'] or project['prod_repo_url'].replace('https://github.com/', '').replace('.git', '')
        prod_connected = github_client.verify_repo(prod_repo_name)
        prod_branches = github_client.get_branches(prod_repo_name) if prod_connected else []
        
        status = 'CONNECTED' if (dev_connected and prod_connected) else ('PARTIAL' if (dev_connected or prod_connected) else 'NOT CONNECTED')
        
        conn.execute('UPDATE projects SET aws_status = ? WHERE project_id = ?', (status, project_id))
        conn.commit()
        
        return jsonify({
            'success': True,
            'dev_connected': dev_connected,
            'dev_branches': dev_branches,
            'prod_connected': prod_connected,
            'prod_branches': prod_branches,
            'status': status
        })

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
        
        # Get DEV and PROD repo names
        dev_repo = project['dev_repo_name'] or project['dev_repo_url'].replace('https://github.com/', '').replace('.git', '')
        prod_repo = project['prod_repo_name'] or project['prod_repo_url'].replace('https://github.com/', '').replace('.git', '')
        
        if not prod_repo:
            prod_repo = GITHUB_PROD_REPO
        
        # Compare repos
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
        title = data.get('title', 'Auto-generated PR from DEV to PROD')
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
        
        # Use PROD repo for PR
        prod_repo = project['prod_repo_name'] or project['prod_repo_url'].replace('https://github.com/', '').replace('.git', '')
        if not prod_repo:
            prod_repo = GITHUB_PROD_REPO
        
        pr_title = title if title else f"PR: {project_id} - Sync from DEV to PROD"
        pr_body = f"""
## 🔄 Sync from DEV to PROD
- **Project**: {project_id}
- **Branch**: {branch}
- **Commit**: {commit_sha}
- **Release**: {release_version or 'N/A'}

## 📄 Files Changed
{chr(10).join(['- ' + f.get('file', '') + ' (' + f.get('type', 'modified') + ')' for f in files_changed])}

## 📝 Description
{description or 'Auto-generated PR from DEV to PROD sync'}
"""
        
        pr_result = github_client.create_pull_request(
            repo_name=prod_repo,
            title=pr_title,
            body=pr_body,
            head_branch=branch,
            base_branch='main'
        )
        
        if not pr_result.get('success'):
            return jsonify({'success': False, 'error': pr_result.get('error', 'Failed to create PR')}), 400
        
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
            
            conn.execute('''
                INSERT INTO audit_trail (event, change_id, project_id, commit_sha, result, details)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', ('PR Created', change_id, project_id, commit_sha, 'Pending', f'PR #{pr_result.get("number")} created in PROD repo'))
            conn.commit()
            
            conn.execute('''
                INSERT INTO deployment_logs (change_id, action, status, details, stage)
                VALUES (?, ?, ?, ?, ?)
            ''', (change_id, 'pr_created', 'Pending', f'PR #{pr_result.get("number")} created in PROD repo', 'PR Creation'))
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
            ''', ('Change Approved', change_id, 'Success', 'Change approved for merge'))
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
            
            if change['approval_status'] != 'Approved':
                return jsonify({'success': False, 'error': 'Change must be approved before merging'}), 400
            
            if not github_client:
                return jsonify({'success': False, 'error': 'GitHub not configured'}), 400
            
            project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (change['project_id'],)).fetchone()
            if not project:
                return jsonify({'success': False, 'error': 'Project not found'}), 404
            
            prod_repo = project['prod_repo_name'] or project['prod_repo_url'].replace('https://github.com/', '').replace('.git', '')
            if not prod_repo:
                prod_repo = GITHUB_PROD_REPO
            
            pr_number = change['pr_number']
            if pr_number:
                merge_result = github_client.merge_pull_request(prod_repo, pr_number)
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
            ''', ('PR Merged to PROD', change_id, change['project_id'], 'Success', f'PR #{pr_number} merged to PROD repo'))
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
            return jsonify({'success': True, 'data': dict(change)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/changes/<change_id>/deploy-databricks', methods=['POST'])
def api_deploy_databricks(change_id):
    try:
        with get_db() as conn:
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            if not change:
                return jsonify({'success': False, 'error': 'Change not found'}), 404
            
            if change['deployment_status'] != 'Merged':
                return jsonify({'success': False, 'error': 'Change must be merged to PROD before deploying to Databricks'}), 400
            
            if not databricks_client:
                return jsonify({'success': False, 'error': 'Databricks not configured'}), 400
            
            project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (change['project_id'],)).fetchone()
            
            # Get PROD repo content to deploy
            prod_repo = project['prod_repo_name'] or project['prod_repo_url'].replace('https://github.com/', '').replace('.git', '')
            
            # Simulate Databricks deployment
            deploy_result = databricks_client.deploy_to_databricks(
                project_path=f'/Workspace/{project["name"]}',
                environment='prod'
            )
            
            conn.execute('''
                UPDATE changes 
                SET deployment_status = 'Deployed',
                    databricks_deployed_at = CURRENT_TIMESTAMP,
                    databricks_deployed_by = ?
                WHERE change_id = ?
            ''', ('databricks-user', change_id))
            
            conn.execute('''
                INSERT INTO audit_trail (event, change_id, project_id, result, details)
                VALUES (?, ?, ?, ?, ?)
            ''', ('Deployed to Databricks', change_id, change['project_id'], 'Success', f'Deployed PROD repo to Databricks'))
            conn.commit()
            
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            return jsonify({'success': True, 'data': dict(change), 'deploy_result': deploy_result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================================
# RUN APP
# ============================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Starting app on port {port}")
    print(f"🔑 GitHub configured: {bool(GITHUB_TOKEN)}")
    print(f"📁 GitHub DEV: {GITHUB_DEV_REPO}")
    print(f"📁 GitHub PROD: {GITHUB_PROD_REPO}")
    print(f"🔑 Databricks configured: {bool(DATABRICKS_TOKEN)}")
    app.run(host='0.0.0.0', port=port, debug=False)