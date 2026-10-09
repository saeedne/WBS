from flask import Flask, has_request_context, session
import sqlite3
import os
import re
import shutil
from datetime import datetime
import json
from functools import wraps
import jdatetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, 'time_tracker.db')
PROJECT_DB_DIR = os.path.join(BASE_DIR, 'project_databases')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
PROJECT_UPLOAD_FOLDER = os.path.join(BASE_DIR, 'project_uploads')
PROJECT_EXPORT_FOLDER = os.path.join(BASE_DIR, 'project_exports')

def get_auth_db_connection():
    """Connect to the shared database containing accounts and project registry."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def get_project_db_path(project_id):
    """Return the SQLite path for a project; project 1 keeps the original DB."""
    project_id = int(project_id)
    if project_id == 1:
        return DB_FILE
    return os.path.join(PROJECT_DB_DIR, f'project_{project_id}.db')


def get_db_connection(project_id=None):
    """Connect to the active project's isolated SQLite database."""
    if project_id is None:
        project_id = 1
        if has_request_context():
            project_id = session.get('active_project_id', 1)
    db_path = get_project_db_path(project_id)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def list_projects():
    conn = get_auth_db_connection()
    try:
        expired = [row['deletion_token'] for row in conn.execute(
            '''SELECT deletion_token FROM projects
               WHERE deletion_token IS NOT NULL AND
               datetime(deletion_started_at) < datetime('now', '-12 hours')'''
        ).fetchall()]
        if expired:
            conn.execute(
                '''UPDATE projects SET deletion_token = NULL, deletion_started_at = NULL
                   WHERE deletion_token IS NOT NULL AND
                   datetime(deletion_started_at) < datetime('now', '-12 hours')'''
            )
            conn.commit()
        projects = conn.execute('SELECT * FROM projects ORDER BY id').fetchall()
    finally:
        conn.close()
    for token in expired:
        if token and re.fullmatch(r'[0-9a-f]{32}', token):
            shutil.rmtree(os.path.join(PROJECT_EXPORT_FOLDER, token), ignore_errors=True)
    return projects


def get_project_name(project_id):
    if not project_id:
        return None
    conn = get_auth_db_connection()
    try:
        row = conn.execute('SELECT name FROM projects WHERE id = ?', (int(project_id),)).fetchone()
        return row['name'] if row else None
    finally:
        conn.close()


def get_project_deletion_lock(project_id):
    if not project_id:
        return None
    conn = get_auth_db_connection()
    try:
        return conn.execute(
            'SELECT deletion_token, deletion_started_at FROM projects WHERE id = ?',
            (int(project_id),)
        ).fetchone()
    finally:
        conn.close()


def lock_project_for_export(project_id, token):
    conn = get_auth_db_connection()
    try:
        existing = conn.execute(
            'SELECT deletion_token FROM projects WHERE id = ?', (int(project_id),)
        ).fetchone()
        if not existing:
            return None
        old_token = existing['deletion_token']
        cursor = conn.execute(
            '''UPDATE projects SET deletion_token = ?, deletion_started_at = ?
               WHERE id = ? AND (deletion_token IS NULL OR
               datetime(deletion_started_at) < datetime('now', '-12 hours'))''',
            (token, datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), int(project_id))
        )
        conn.commit()
        return (old_token or '') if cursor.rowcount else False
    finally:
        conn.close()


def unlock_project(project_id, token):
    conn = get_auth_db_connection()
    try:
        conn.execute(
            '''UPDATE projects SET deletion_token = NULL, deletion_started_at = NULL
               WHERE id = ? AND deletion_token = ?''',
            (int(project_id), token)
        )
        conn.commit()
    finally:
        conn.close()


def get_user_project_id(username):
    project_ids = get_user_project_ids(username)
    return project_ids[0] if project_ids else None


def get_user_project_ids(username):
    conn = get_auth_db_connection()
    try:
        rows = conn.execute(
            'SELECT project_id FROM user_projects WHERE username = ? ORDER BY project_id',
            (username,)
        ).fetchall()
        return [row['project_id'] for row in rows]
    finally:
        conn.close()


def init_project_registry():
    """Create the shared project registry and attach legacy users to طرقبه."""
    conn = get_auth_db_connection()
    try:
        projects_table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'projects'"
        ).fetchone() is not None
        conn.execute('''
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL,
                employer_name TEXT,
                start_date TEXT,
                contract_duration_months INTEGER,
                estimate_amount REAL,
                deletion_token TEXT,
                deletion_started_at TEXT
            )
        ''')
        project_columns = {row['name'] for row in conn.execute('PRAGMA table_info(projects)')}
        for column_name, column_type in (
            ('employer_name', 'TEXT'),
            ('start_date', 'TEXT'),
            ('contract_duration_months', 'INTEGER'),
            ('estimate_amount', 'REAL'),
            ('deletion_token', 'TEXT'),
            ('deletion_started_at', 'TEXT'),
        ):
            if column_name not in project_columns:
                conn.execute(f'ALTER TABLE projects ADD COLUMN {column_name} {column_type}')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_projects (
                username TEXT NOT NULL,
                project_id INTEGER NOT NULL,
                PRIMARY KEY (username, project_id),
                FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        ''')
        project_map_info = conn.execute('PRAGMA table_info(user_projects)').fetchall()
        project_map_pk = [row['name'] for row in sorted(project_map_info, key=lambda item: item['pk']) if row['pk']]
        if project_map_pk == ['username']:
            conn.execute('ALTER TABLE user_projects RENAME TO user_projects_legacy')
            conn.execute('''
                CREATE TABLE user_projects (
                    username TEXT NOT NULL,
                    project_id INTEGER NOT NULL,
                    PRIMARY KEY (username, project_id),
                    FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE,
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                )
            ''')
            conn.execute('''INSERT OR IGNORE INTO user_projects (username, project_id)
                             SELECT username, project_id FROM user_projects_legacy''')
            conn.execute('DROP TABLE user_projects_legacy')
        # Seed the legacy project only on the first registry creation. This
        # prevents an intentionally deleted last project from reappearing.
        if not projects_table_exists:
            conn.execute(
                'INSERT OR IGNORE INTO projects (id, name, created_at) VALUES (1, ?, ?)',
                ('طرقبه', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            )
        conn.execute('''
            INSERT OR IGNORE INTO user_projects (username, project_id)
            SELECT username, 1 FROM users WHERE role != 'admin'
        ''')
        conn.execute('''
            DELETE FROM user_projects
            WHERE username IN (SELECT username FROM users WHERE role = 'admin')
        ''')
        conn.commit()
    finally:
        conn.close()


def create_project(name, employer_name=None, start_date=None,
                   contract_duration_months=None, estimate_amount=None):
    name = (name or '').strip()
    if not name:
        raise ValueError('نام پروژه را وارد کنید.')

    conn = get_auth_db_connection()
    try:
        if conn.execute('SELECT 1 FROM projects WHERE name = ? COLLATE NOCASE', (name,)).fetchone():
            raise ValueError('پروژه‌ای با این نام از قبل وجود دارد.')
        project_id = conn.execute('SELECT COALESCE(MAX(id), 0) + 1 FROM projects').fetchone()[0]
        db_path = get_project_db_path(project_id)
        if project_id != 1 and os.path.exists(db_path):
            raise ValueError('پوشهٔ دادهٔ این پروژه از قبل وجود دارد؛ با پشتیبانی تماس بگیرید.')
        init_db(db_path=db_path, seed_users=False)
        conn.execute(
            '''INSERT INTO projects
               (id, name, created_at, employer_name, start_date,
                contract_duration_months, estimate_amount)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (project_id, name, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             (employer_name or '').strip() or None, start_date or None,
             contract_duration_months, estimate_amount)
        )
        conn.commit()
        return project_id
    finally:
        conn.close()


def update_project(project_id, name, employer_name=None, start_date=None,
                   contract_duration_months=None, estimate_amount=None):
    name = (name or '').strip()
    if not name:
        raise ValueError('نام پروژه را وارد کنید.')
    conn = get_auth_db_connection()
    try:
        cursor = conn.execute(
            '''UPDATE projects SET name = ?, employer_name = ?, start_date = ?,
               contract_duration_months = ?, estimate_amount = ? WHERE id = ?''',
            (name, (employer_name or '').strip() or None, start_date or None,
             contract_duration_months, estimate_amount, int(project_id))
        )
        if cursor.rowcount == 0:
            raise ValueError('پروژه پیدا نشد.')
        conn.commit()
    finally:
        conn.close()


def get_project_upload_folder(project_id=None):
    if project_id is None:
        project_id = session.get('active_project_id', 1) if has_request_context() else 1
    folder = os.path.join(PROJECT_UPLOAD_FOLDER, str(int(project_id)))
    os.makedirs(folder, exist_ok=True)
    return folder


def get_project_upload_url(filename, project_id=None):
    if project_id is None:
        project_id = session.get('active_project_id', 1) if has_request_context() else 1
    return f'/project_uploads/{int(project_id)}/{filename}'

def init_db(db_path=None, seed_users=True):
    """Initializes the database schema if it doesn't exist."""
    db_path = db_path or get_project_db_path(1)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Create tables
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS time_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id TEXT NOT NULL,
            action TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            condition TEXT DEFAULT 'عادی',
            activity_location TEXT,
            activity_description TEXT,
            activity_amount TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS employees (
            employee_id TEXT PRIMARY KEY,
            name TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL,
            permissions TEXT NOT NULL DEFAULT '{}'
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            details TEXT,
            timestamp TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS petty_cash (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            description TEXT,
            unit TEXT,
            amount INTEGER,
            unit_price REAL,
            discount TEXT,
            total_amount REAL,
            location TEXT,
            notes TEXT,
            source TEXT,
            invoice_number TEXT,
            settlement_status TEXT NOT NULL,
            payer TEXT NOT NULL,
            wbs_code TEXT,
            wbs_coverage_percent REAL DEFAULT 0,
            receipt_image_path TEXT,
            timestamp TEXT NOT NULL
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS project_wbs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wbs_code TEXT NOT NULL UNIQUE,
            activity TEXT NOT NULL,
            unit TEXT,
            total_quantity REAL DEFAULT 0,
            weight_percent REAL NOT NULL DEFAULT 0,
            notes TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')

    # Multiple WBS links for a single petty cash expense. Legacy columns on
    # petty_cash remain in place for older reports and integrations.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS petty_cash_wbs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            petty_cash_id INTEGER NOT NULL,
            wbs_code TEXT NOT NULL,
            wbs_coverage_percent REAL NOT NULL DEFAULT 0,
            UNIQUE(petty_cash_id, wbs_code)
        )
    ''')
    petty_wbs_columns = {
        row[1] for row in cursor.execute('PRAGMA table_info(petty_cash_wbs)').fetchall()
    }
    if 'petty_cash_id' not in petty_wbs_columns:
        cursor.execute('ALTER TABLE petty_cash_wbs ADD COLUMN petty_cash_id INTEGER')
    if 'wbs_code' not in petty_wbs_columns:
        cursor.execute('ALTER TABLE petty_cash_wbs ADD COLUMN wbs_code TEXT')
    if 'wbs_coverage_percent' not in petty_wbs_columns:
        cursor.execute('ALTER TABLE petty_cash_wbs ADD COLUMN wbs_coverage_percent REAL NOT NULL DEFAULT 0')

    # Backfill existing single-WBS expenses once; INSERT OR IGNORE makes
    # startup safe when the application is restarted.
    cursor.execute('''
        INSERT OR IGNORE INTO petty_cash_wbs
            (petty_cash_id, wbs_code, wbs_coverage_percent)
        SELECT id, wbs_code, COALESCE(wbs_coverage_percent, 0)
        FROM petty_cash
        WHERE wbs_code IS NOT NULL AND TRIM(wbs_code) != ''
    ''')

    # Keep linked expenses in sync when project WBS codes are renamed or
    # normalized by the existing WBS management routes.
    cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS sync_petty_cash_wbs_code
        AFTER UPDATE OF wbs_code ON project_wbs
        WHEN OLD.wbs_code != NEW.wbs_code
        BEGIN
            UPDATE petty_cash_wbs SET wbs_code = NEW.wbs_code
            WHERE wbs_code = OLD.wbs_code;
        END
    ''')
    cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS delete_petty_cash_wbs_links
        AFTER DELETE ON project_wbs
        BEGIN
            DELETE FROM petty_cash_wbs WHERE wbs_code = OLD.wbs_code;
        END
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS temp_activities (
            employee_id TEXT PRIMARY KEY,
            activity_location TEXT,
            activity_description TEXT,
            activity_amount TEXT
        )
    ''')
    
    # Create daily_workers table with new columns
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_workers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            foreman_name TEXT NOT NULL,
            worker_count INTEGER NOT NULL,
            daily_wage TEXT NOT NULL,
            transport_cost INTEGER NOT NULL,
            total_amount INTEGER NOT NULL,
            location TEXT,
            timestamp TEXT NOT NULL,
            notes TEXT,
            receipt_image_path TEXT
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS income (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            description TEXT,
            amount REAL NOT NULL,
            source TEXT,
            notes TEXT,
            timestamp TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS expense (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            description TEXT,
            amount REAL NOT NULL,
            category TEXT,
            notes TEXT,
            timestamp TEXT NOT NULL
        )
    ''')
    
    # خط جدید برای جدول گزارشات تأسیسات
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS facilities_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            facility_name TEXT,
            companion_name TEXT,
            activity_location TEXT,
            activity_description TEXT,
            duration TEXT,
            materials TEXT,
            material_source TEXT,
            timestamp TEXT NOT NULL
        )
    ''')

    # Update existing tables
    try:
        cursor.execute("SELECT name FROM users LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE users ADD COLUMN name TEXT")
        cursor.execute("UPDATE users SET name = username WHERE name IS NULL")
    
    try:
        cursor.execute("SELECT condition FROM time_logs LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE time_logs ADD COLUMN condition TEXT DEFAULT 'عادی'")
    
    try:
        cursor.execute("SELECT activity_location FROM time_logs LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE time_logs ADD COLUMN activity_location TEXT")
    try:
        cursor.execute("SELECT activity_description FROM time_logs LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE time_logs ADD COLUMN activity_description TEXT")
    try:
        cursor.execute("SELECT activity_amount FROM time_logs LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE time_logs ADD COLUMN activity_amount TEXT")

    try:
        cursor.execute("PRAGMA table_info(petty_cash)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'receipt_image' in columns:
            cursor.execute("ALTER TABLE petty_cash RENAME COLUMN receipt_image TO receipt_image_path")
            cursor.execute("ALTER TABLE petty_cash ADD COLUMN receipt_image_path TEXT")
    except sqlite3.OperationalError:
        pass

    # WBS fields for existing petty_cash tables
    try:
        cursor.execute("PRAGMA table_info(petty_cash)")
        petty_columns = [col[1] for col in cursor.fetchall()]
        if 'wbs_code' not in petty_columns:
            cursor.execute("ALTER TABLE petty_cash ADD COLUMN wbs_code TEXT")
        if 'wbs_coverage_percent' not in petty_columns:
            cursor.execute("ALTER TABLE petty_cash ADD COLUMN wbs_coverage_percent REAL DEFAULT 0")
        if 'personal_manager_payment' not in petty_columns:
            cursor.execute("ALTER TABLE petty_cash ADD COLUMN personal_manager_payment INTEGER DEFAULT 0")
        if 'exclude_manager_calculation' not in petty_columns:
            cursor.execute("ALTER TABLE petty_cash ADD COLUMN exclude_manager_calculation INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    
    conn.commit()
    conn.close()

    if not seed_users:
        return

    # Create admin user if not exists
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        admin_exists = conn.execute("SELECT 1 FROM users WHERE username = 'admin'").fetchone()
        if not admin_exists:
            from werkzeug.security import generate_password_hash
            hashed_password = generate_password_hash('563Signin@')
            permissions = {
                'dashboard': True, 'management': True, 'users': True, 'reports': True,
                'petty_cash': True, 'petty_cash_reports': True,
                'daily_worker': True, 'daily_worker_reports': True,
                'admin_dashboard': True, 'financial_records': True, 'financial_reports': True,
                'payroll_calculation': True, 'project_wbs': True, 'project_progress': True
            }
            conn.execute('INSERT INTO users (username, name, password, role, permissions) VALUES (?, ?, ?, ?, ?)',
                           ('admin', 'مدیر سیستم', hashed_password, 'admin', json.dumps(permissions)))
            conn.commit()
        
        # Update admin permissions to include new pages
        admin_permissions_str = conn.execute("SELECT permissions FROM users WHERE username = 'admin'").fetchone()[0]
        admin_permissions = json.loads(admin_permissions_str)
        admin_permissions.update({
            'daily_worker': True,
            'daily_worker_reports': True,
            'admin_dashboard': True,
            'financial_records': True,
            'financial_reports': True,
            'dashboard': True,
            'facilities': True, # خط جدید
            'facilities_reports': True, # خط جدید
            'payroll_calculation': True, 'project_wbs': True, 'project_progress': True
        })
        conn.execute("UPDATE users SET permissions = ? WHERE username = 'admin'", (json.dumps(admin_permissions),))
        conn.commit()

        # Update permissions for existing users
        user_permissions_str = conn.execute("SELECT permissions FROM users WHERE role = 'user'").fetchone()[0]
        user_permissions = json.loads(user_permissions_str)
        legacy_changed = False
        if 'daily_worker' not in user_permissions:
            user_permissions.update({
                'daily_worker': False,
                'daily_worker_reports': False,
                'admin_dashboard': False,
                'financial_records': False,
                'financial_reports': False,
                'dashboard': False,
                'facilities': False,
                'facilities_reports': False,
                'payroll_calculation': False
            })
            legacy_changed = True
        changed = False
        for permission_name in ('project_wbs', 'project_progress'):
            if permission_name not in user_permissions:
                user_permissions[permission_name] = False
                changed = True
        if changed or legacy_changed:
            conn.execute("UPDATE users SET permissions = ? WHERE role = 'user'", (json.dumps(user_permissions),))
            conn.commit()
    except Exception as e:
        print(f"Error during initial setup or permission update: {e}")
    finally:
        conn.close()

def log_action(username, action, details=""):
    """Logs user actions to the user_logs table."""
    conn = get_db_connection()
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('INSERT INTO user_logs (username, action, details, timestamp) VALUES (?, ?, ?, ?)',
                     (username, action, details, timestamp))
        conn.commit()
    except Exception as e:
        print(f"Error logging action: {e}")
        conn.rollback()
    finally:
        conn.close()

def shamsi_to_miladi(shamsi_date_str):
    """Converts a Shamsi date string to a Gregorian datetime object."""
    try:
        shamsi_date_str_en = shamsi_date_str.translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹', '0123456789'))
        j_date_parts = shamsi_date_str_en.split('/')
        j_date = jdatetime.date(int(j_date_parts[0]), int(j_date_parts[1]), int(j_date_parts[2]))
        g_date = j_date.togregorian()
        return g_date
    except (ValueError, IndexError):
        raise ValueError("Invalid Shamsi date format")
