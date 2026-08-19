from flask import Flask, render_template, request, jsonify
import os
import json
import requests
import base64
import sqlite3
import random
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'default-secret-key')

# ============================================================
# CONFIGURATION
# ============================================================

GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')

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
            # Projects table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    repo_url TEXT,
                    repo_name TEXT,
                    branch TEXT DEFAULT 'main',
                    status TEXT DEFAULT 'PENDING',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Changes / PRs table
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
                    conflict_message TEXT,
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
            
            # Audit Trail
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
    
    def verify_repo(self, repo_name):
        try:
            r = requests.get(f'https://api.github.com/repos/{repo_name}', headers=self.headers)
            return r.status_code == 200
        except:
            return False
    
    def get_repo_info(self, repo_name):
        try:
            r = requests.get(f'https://api.github.com/repos/{repo_name}', headers=self.headers)
            if r.status_code == 200:
                data = r.json()
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
    
    def get_pull_requests(self, repo_name, state='open'):
        """Fetch all open pull requests from GitHub"""
        try:
            r = requests.get(
                f'https://api.github.com/repos/{repo_name}/pulls?state={state}&per_page=100',
                headers=self.headers
            )
            if r.status_code == 200:
                return r.json()
            return []
        except Exception as e:
            print(f"Error fetching PRs: {e}")
            return []
    
    def get_pr_files(self, repo_name, pr_number):
        """Get files changed in a PR"""
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
    
    def merge_pull_request(self, repo_name, pr_number):
        """Merge a PR via GitHub API"""
        url = f'https://api.github.com/repos/{repo_name}/pulls/{pr_number}/merge'
        payload = {
            'commit_title': f'Merge PR #{pr_number}',
            'merge_method': 'merge'
        }
        try:
            r = requests.put(url, headers=self.headers, json=payload)
            if r.status_code == 200:
                return {'success': True}
            return {'success': False, 'error': f'Status: {r.status_code}'}
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    def sync_prs_to_db(self, repo_name, project_id):
        """Sync GitHub PRs to local database"""
        prs = self.get_pull_requests(repo_name)
        synced_count = 0
        
        with get_db() as conn:
            for pr in prs:
                # Check if PR already exists
                existing = conn.execute(
                    'SELECT * FROM changes WHERE pr_number = ? AND project_id = ?',
                    (pr['number'], project_id)
                ).fetchone()
                
                if not existing:
                    # Get PR files
                    files = self.get_pr_files(repo_name, pr['number'])
                    files_changed = [{'file': f['filename'], 'type': 'modified'} for f in files]
                    
                    change_id = f"CHG-{datetime.now().strftime('%Y%m%d')}-{random.randint(1000, 9999)}"
                    
                    # Check for conflicts (simulated)
                    conflict_message = None
                    if random.random() < 0.1:  # 10% chance of conflict
                        conflict_message = "⚠️ Merge conflict detected. Please resolve manually."
                    
                    conn.execute('''
                        INSERT INTO changes 
                        (change_id, project_id, pr_number, pr_url, pr_title, pr_author, 
                         commit_sha, description, files_changed, source_branch, target_branch, 
                         approval_status, conflict_message)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
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
                        pr['base']['ref'],
                        conflict_message
                    ))
                    
                    conn.execute('''
                        INSERT INTO audit_trail (event, change_id, project_id, result, details)
                        VALUES (?, ?, ?, ?, ?)
                    ''', ('PR Detected', change_id, project_id, 'PENDING', f'PR #{pr["number"]} synced from GitHub'))
                    
                    synced_count += 1
            
            conn.commit()
        
        return synced_count

github_client = GitHubClient(GITHUB_TOKEN) if GITHUB_TOKEN else None

# ============================================================
# PAGE ROUTES
# ============================================================

@app.route('/')
def index():
    with get_db() as conn:
        projects = conn.execute('SELECT * FROM projects').fetchall()
        changes = conn.execute('SELECT * FROM changes ORDER BY created_at DESC LIMIT 10').fetchall()
        pending = conn.execute("SELECT COUNT(*) FROM changes WHERE approval_status = 'PENDING'").fetchone()[0]
        approved = conn.execute("SELECT COUNT(*) FROM changes WHERE approval_status = 'APPROVED'").fetchone()[0]
        merged = conn.execute("SELECT COUNT(*) FROM changes WHERE deployment_status = 'MERGED'").fetchone()[0]
        deployed = conn.execute("SELECT COUNT(*) FROM changes WHERE deployment_status = 'DEPLOYED'").fetchone()[0]
    
    return render_template('index.html',
                         projects=projects,
                         changes=changes,
                         pending_count=pending,
                         approved_count=approved,
                         merged_count=merged,
                         deployed_count=deployed)

@app.route('/projects')
def projects_page():
    with get_db() as conn:
        projects = conn.execute('SELECT * FROM projects ORDER BY created_at DESC').fetchall()
    return render_template('projects.html', projects=projects)

@app.route('/changes')
def changes_page():
    """Change Register - Shows ALL changes (PRs) from the repository"""
    with get_db() as conn:
        changes = conn.execute('SELECT * FROM changes ORDER BY created_at DESC').fetchall()
        projects = conn.execute('SELECT project_id, name, repo_name FROM projects').fetchall()
    return render_template('changes.html', changes=changes, projects=projects)

@app.route('/approvals')
def approvals_page():
    """Approvals - Shows ONLY PENDING changes"""
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
        
        if not data.get('project_id') or not data.get('name') or not data.get('repo_url'):
            return jsonify({'success': False, 'error': 'project_id, name and repo_url are required'}), 400
        
        with get_db() as conn:
            existing = conn.execute('SELECT * FROM projects WHERE project_id = ?', (data['project_id'],)).fetchone()
            if existing:
                return jsonify({'success': False, 'error': 'Project ID already exists'}), 400
            
            repo_url = data['repo_url']
            repo_name = repo_url.replace('https://github.com/', '').replace('.git', '')
            
            # Verify repo exists
            repo_valid = False
            if github_client:
                repo_valid = github_client.verify_repo(repo_name)
            
            status = 'CONNECTED' if repo_valid else 'PENDING'
            
            conn.execute('''
                INSERT INTO projects 
                (project_id, name, repo_url, repo_name, branch, status)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                data['project_id'],
                data['name'],
                repo_url,
                repo_name,
                data.get('branch', 'main'),
                status
            ))
            conn.commit()
            
            project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (data['project_id'],)).fetchone()
            
            # Sync PRs from GitHub immediately
            synced = 0
            if github_client and repo_valid:
                synced = github_client.sync_prs_to_db(repo_name, data['project_id'])
            
            conn.execute('''
                INSERT INTO audit_trail (event, project_id, result, details)
                VALUES (?, ?, ?, ?)
            ''', ('Project Created', data['project_id'], 'SUCCESS', f'Project {data["project_id"]} created, {synced} PRs synced'))
            conn.commit()
            
            return jsonify({'success': True, 'data': dict(project), 'prs_synced': synced})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/projects/<project_id>/sync-prs', methods=['POST'])
def api_sync_prs(project_id):
    """Manually sync PRs from GitHub"""
    try:
        with get_db() as conn:
            project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (project_id,)).fetchone()
            if not project:
                return jsonify({'success': False, 'error': 'Project not found'}), 404
            
            if not github_client:
                return jsonify({'success': False, 'error': 'GitHub not configured. Set GITHUB_TOKEN.'}), 400
            
            repo_name = project['repo_name']
            synced = github_client.sync_prs_to_db(repo_name, project_id)
            
            return jsonify({'success': True, 'synced': synced})
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
        
        repo_name = project['repo_name']
        valid = github_client.verify_repo(repo_name)
        status = 'CONNECTED' if valid else 'NOT CONNECTED'
        
        conn.execute('UPDATE projects SET status = ? WHERE project_id = ?', (status, project_id))
        conn.commit()
        
        # If connected, sync PRs
        synced = 0
        if valid:
            synced = github_client.sync_prs_to_db(repo_name, project_id)
        
        return jsonify({
            'success': True,
            'connected': valid,
            'status': status,
            'prs_synced': synced
        })

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
                return jsonify({'success': False, 'error': f'Change is already {change["approval_status"]}'}), 400
            
            # Check for conflicts
            if change['conflict_message']:
                return jsonify({
                    'success': False,
                    'error': 'Cannot approve due to conflicts',
                    'conflict': change['conflict_message']
                }), 400
            
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
                return jsonify({'success': False, 'error': f'Change is already {change["approval_status"]}'}), 400
            
            conn.execute('''
                UPDATE changes 
                SET approval_status = 'REJECTED', 
                    rejection_reason = ?,
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
            
            # Merge the PR on GitHub
            if github_client and change['pr_number']:
                project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (change['project_id'],)).fetchone()
                repo_name = project['repo_name']
                merge_result = github_client.merge_pull_request(repo_name, change['pr_number'])
                
                if not merge_result.get('success'):
                    return jsonify({
                        'success': False,
                        'error': merge_result.get('error', 'Merge failed on GitHub')
                    }), 400
            
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
            ''', ('Change Merged', change_id, change['project_id'], 'MERGED', 'Merged to target branch'))
            conn.commit()
            
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            return jsonify({'success': True, 'data': dict(change)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/changes/<change_id>/deploy', methods=['POST'])
def api_deploy_change(change_id):
    try:
        with get_db() as conn:
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            if not change:
                return jsonify({'success': False, 'error': 'Change not found'}), 404
            
            if change['deployment_status'] != 'MERGED':
                return jsonify({'success': False, 'error': 'Change must be merged before deploying'}), 400
            
            if change['deployment_status'] == 'DEPLOYED':
                return jsonify({'success': False, 'error': 'Change already deployed'}), 400
            
            conn.execute('''
                UPDATE changes 
                SET deployment_status = 'DEPLOYED', 
                    deployed_at = CURRENT_TIMESTAMP, 
                    deployed_by = ?
                WHERE change_id = ?
            ''', ('databricks-user', change_id))
            
            conn.execute('''
                INSERT INTO audit_trail (event, change_id, project_id, result, details)
                VALUES (?, ?, ?, ?, ?)
            ''', ('Change Deployed', change_id, change['project_id'], 'DEPLOYED', 'Deployed to PROD'))
            conn.commit()
            
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            return jsonify({'success': True, 'data': dict(change)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================================
# RUN APP
# ============================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("=" * 50)
    print("🚀 CHANGE MANAGEMENT ORCHESTRATOR")
    print("=" * 50)
    print(f"📡 Server: http://localhost:{port}")
    print(f"🔑 GitHub configured: {bool(GITHUB_TOKEN)}")
    print("=" * 50)
    app.run(host='0.0.0.0', port=port, debug=False)