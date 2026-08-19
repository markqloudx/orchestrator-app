from flask import Flask, render_template, request, jsonify
import os
import json
import sqlite3
import random
from datetime import datetime

# ============================================================
# CONFIGURATION
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
                    source_repo_url TEXT,
                    source_repo_name TEXT,
                    target_repo_url TEXT,
                    target_repo_name TEXT,
                    branch TEXT DEFAULT 'main',
                    prod_branch TEXT DEFAULT 'main',
                    status TEXT DEFAULT 'PENDING',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Changes table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    change_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL,
                    title TEXT,
                    description TEXT,
                    source_branch TEXT DEFAULT 'main',
                    target_branch TEXT DEFAULT 'main',
                    files_changed TEXT,
                    approval_status TEXT DEFAULT 'PENDING',
                    deployment_status TEXT DEFAULT 'BLOCKED',
                    rejection_reason TEXT,
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
# PAGE ROUTES
# ============================================================

@app.route('/')
def index():
    with get_db() as conn:
        projects = conn.execute('SELECT * FROM projects').fetchall()
        changes = conn.execute('SELECT * FROM changes ORDER BY created_at DESC LIMIT 10').fetchall()
        pending = conn.execute("SELECT COUNT(*) FROM changes WHERE approval_status = 'PENDING'").fetchone()[0]
        approved = conn.execute("SELECT COUNT(*) FROM changes WHERE approval_status = 'APPROVED'").fetchone()[0]
        rejected = conn.execute("SELECT COUNT(*) FROM changes WHERE approval_status = 'REJECTED'").fetchone()[0]
        merged = conn.execute("SELECT COUNT(*) FROM changes WHERE deployment_status = 'MERGED'").fetchone()[0]
        deployed = conn.execute("SELECT COUNT(*) FROM changes WHERE deployment_status = 'DEPLOYED'").fetchone()[0]
    
    return render_template('index.html',
                         projects=projects,
                         changes=changes,
                         pending_count=pending,
                         approved_count=approved,
                         rejected_count=rejected,
                         merged_count=merged,
                         deployed_count=deployed)

@app.route('/projects')
def projects_page():
    with get_db() as conn:
        projects = conn.execute('SELECT * FROM projects').fetchall()
    return render_template('projects.html', projects=projects)

@app.route('/changes')
def changes_page():
    with get_db() as conn:
        changes = conn.execute('SELECT * FROM changes ORDER BY created_at DESC').fetchall()
        projects = conn.execute('SELECT project_id, name FROM projects').fetchall()
    return render_template('changes.html', changes=changes, projects=projects)

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
        return jsonify({'success': True, 'data': [dict(p) for p in conn.execute('SELECT * FROM projects').fetchall()]})

@app.route('/api/projects', methods=['POST'])
def api_create_project():
    try:
        data = request.json
        with get_db() as conn:
            conn.execute('''
                INSERT INTO projects 
                (project_id, name, source_repo_url, source_repo_name, target_repo_url, target_repo_name, branch, prod_branch)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                data['project_id'],
                data['name'],
                data.get('source_repo_url', ''),
                data.get('source_repo_name', ''),
                data.get('target_repo_url', ''),
                data.get('target_repo_name', ''),
                data.get('branch', 'main'),
                data.get('prod_branch', 'main')
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
        
        # Simulate repo verification
        source_repo = project['source_repo_url'] or ''
        target_repo = project['target_repo_url'] or ''
        
        source_valid = bool(source_repo and ('.com' in source_repo or '.org' in source_repo))
        target_valid = bool(target_repo and ('.com' in target_repo or '.org' in target_repo))
        
        status = 'CONNECTED' if (source_valid and target_valid) else 'PARTIAL'
        
        conn.execute('UPDATE projects SET status = ? WHERE project_id = ?', (status, project_id))
        conn.commit()
        
        return jsonify({
            'success': True,
            'source_valid': source_valid,
            'target_valid': target_valid,
            'status': status
        })

@app.route('/api/changes', methods=['GET'])
def api_get_changes():
    with get_db() as conn:
        changes = conn.execute('SELECT * FROM changes ORDER BY created_at DESC').fetchall()
        return jsonify({'success': True, 'data': [dict(c) for c in changes]})

@app.route('/api/changes', methods=['POST'])
def api_create_change():
    try:
        data = request.json
        project_id = data.get('project_id')
        
        with get_db() as conn:
            project = conn.execute('SELECT * FROM projects WHERE project_id = ?', (project_id,)).fetchone()
            if not project:
                return jsonify({'success': False, 'error': 'Project not found'}), 404
            
            change_id = f"CHG-{datetime.now().strftime('%Y%m%d')}-{random.randint(1000, 9999)}"
            
            # Check for potential conflicts (simulated)
            conflict_message = None
            if random.random() < 0.1:  # 10% chance of conflict for demo
                conflict_message = "⚠️ Merge conflict detected: File 'app.py' has conflicting changes."
            
            conn.execute('''
                INSERT INTO changes 
                (change_id, project_id, title, description, source_branch, target_branch, 
                 files_changed, conflict_message, approval_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
            ''', (
                change_id,
                project_id,
                data.get('title', 'Change request'),
                data.get('description', ''),
                data.get('source_branch', project['branch']),
                data.get('target_branch', project['prod_branch']),
                json.dumps(data.get('files_changed', [])),
                conflict_message
            ))
            conn.commit()
            
            conn.execute('''
                INSERT INTO audit_trail (event, change_id, project_id, result, details)
                VALUES (?, ?, ?, ?, ?)
            ''', ('Change Created', change_id, project_id, 'PENDING', 'Change request created'))
            conn.commit()
            
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            
            return jsonify({'success': True, 'data': dict(change)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/changes/<change_id>/approve', methods=['POST'])
def api_approve_change(change_id):
    try:
        with get_db() as conn:
            change = conn.execute('SELECT * FROM changes WHERE change_id = ?', (change_id,)).fetchone()
            if not change:
                return jsonify({'success': False, 'error': 'Change not found'}), 404
            
            if change['approval_status'] != 'PENDING':
                return jsonify({'success': False, 'error': f'Change is already {change["approval_status"]}'}), 400
            
            # Check for conflicts before approving
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
        data = request.json
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
            
            # Check for merge conflicts (simulated)
            if random.random() < 0.05:  # 5% chance of merge conflict
                return jsonify({
                    'success': False,
                    'error': 'Merge conflict detected! Please resolve conflicts manually.',
                    'conflict': True
                }), 409
            
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
    print(f"🚀 Starting app on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)