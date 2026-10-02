from flask import Flask, session
import sqlite3
from datetime import datetime
import json
from functools import wraps
import jdatetime

DB_FILE = 'time_tracker.db'
UPLOAD_FOLDER = 'static/uploads'

def get_db_connection():
    """Establishes a connection to the SQLite database."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes the database schema if it doesn't exist."""
    conn = get_db_connection()
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

    # Create admin user if not exists
    conn = get_db_connection()
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
