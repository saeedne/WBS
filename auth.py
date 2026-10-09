from flask import render_template, request, redirect, url_for, session, flash, jsonify
import sqlite3
import json
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime
import jdatetime
import utils


def has_permission(page_name):
    user = session.get('user', {})
    # فقط حساب اصلی سیستم دسترسی کامل دارد؛ سایر کاربران، حتی با نقش admin،
    # فقط به بخش‌هایی که در دسترسی‌هایشان فعال شده است وارد می‌شوند.
    if user.get('username') == 'admin':
        return True
    return user.get('permissions', {}).get(page_name, False)


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            flash('لطفاً ابتدا وارد سیستم شوید.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def role_required(role):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user' not in session or session['user']['role'] != role:
                flash('شما به این بخش دسترسی ندارید.', 'error')
                return redirect(url_for('admin_dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def _can_manage_user(username):
    if session.get('user', {}).get('username') == 'admin':
        return True
    conn = utils.get_auth_db_connection()
    try:
        row = conn.execute('''
            SELECT u.role, up.project_id
            FROM users u
            LEFT JOIN user_projects up ON up.username = u.username
            WHERE u.username = ?
        ''', (username,)).fetchone()
        if not row:
            return False
        if row['role'] == 'admin':
            return session.get('user', {}).get('role') == 'admin'
        assigned_projects = utils.get_user_project_ids(username)
        return session.get('active_project_id') in assigned_projects
    finally:
        conn.close()


def init_auth_system(app):
    @app.before_request
    def enforce_active_project():
        endpoint = request.endpoint
        if endpoint in {'login', 'logout', 'static', 'service_worker_file'}:
            return None

        user = session.get('user')
        if endpoint is None:
            return None
        if not user:
            flash('لطفاً ابتدا وارد سیستم شوید.', 'error')
            return redirect(url_for('login'))
        if endpoint in {'select_project', 'create_project_route'}:
            return None

        if user.get('role') == 'admin':
            project_id = session.get('active_project_id')
            project_name = utils.get_project_name(project_id)
            if not project_name:
                session.pop('active_project_id', None)
                session.pop('active_project_name', None)
                return redirect(url_for('select_project'))
            session['active_project_name'] = project_name
            return None

        assigned_project_ids = utils.get_user_project_ids(user['username'])
        assigned_project_ids = [pid for pid in assigned_project_ids if utils.get_project_name(pid)]
        if not assigned_project_ids:
            session.clear()
            flash('برای این کاربر پروژه‌ای تعیین نشده است؛ با مدیر سیستم تماس بگیرید.', 'error')
            return redirect(url_for('login'))

        active_project_id = session.get('active_project_id')
        if active_project_id not in assigned_project_ids:
            session.pop('active_project_id', None)
            session.pop('active_project_name', None)
            if len(assigned_project_ids) > 1:
                return redirect(url_for('select_project'))
            active_project_id = assigned_project_ids[0]
        session['active_project_id'] = active_project_id
        session['active_project_name'] = utils.get_project_name(active_project_id)
        return None

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            username = request.form['username'].lower()
            password = request.form['password']

            conn = utils.get_auth_db_connection()
            try:
                user = conn.execute(
                    'SELECT * FROM users WHERE username = ?', (username,)
                ).fetchone()
            finally:
                conn.close()

            if user and check_password_hash(user['password'], password):
                session.clear()
                session['user'] = {
                    'username': user['username'],
                    'name': user['name'],
                    'role': user['role'],
                    'permissions': json.loads(user['permissions'])
                }
                flash('ورود با موفقیت انجام شد.', 'success')
                if user['role'] == 'admin':
                    utils.log_action(user['username'], 'ورود به سیستم', 'ورود موفق')
                    return redirect(url_for('select_project'))

                project_ids = utils.get_user_project_ids(user['username'])
                project_ids = [pid for pid in project_ids if utils.get_project_name(pid)]
                if not project_ids:
                    session.clear()
                    flash('برای این کاربر پروژه‌ای تعیین نشده است؛ با مدیر سیستم تماس بگیرید.', 'error')
                    return redirect(url_for('login'))
                utils.log_action(user['username'], 'ورود به سیستم', 'ورود موفق')
                if len(project_ids) > 1:
                    return redirect(url_for('select_project'))
                session['active_project_id'] = project_ids[0]
                session['active_project_name'] = utils.get_project_name(project_ids[0])
                return redirect(url_for('dashboard'))

            flash('نام کاربری یا رمز عبور اشتباه است.', 'error')
            return redirect(url_for('login'))

        return render_template('login.html')

    @app.route('/logout')
    def logout():
        username = session.get('user', {}).get('username')
        if username:
            utils.log_action(username, 'خروج از سیستم', 'خروج موفق')
        session.clear()
        flash('شما با موفقیت از سیستم خارج شدید.', 'success')
        return redirect(url_for('login'))

    @app.route('/select_project', methods=['GET', 'POST'])
    @login_required
    def select_project():
        user = session['user']
        if user.get('role') == 'admin':
            projects = utils.list_projects()
            allowed_project_ids = {project['id'] for project in projects}
        else:
            allowed_project_ids = set(utils.get_user_project_ids(user['username']))
            projects = [project for project in utils.list_projects() if project['id'] in allowed_project_ids]
        if not projects:
            flash('برای این کاربر پروژه‌ای تعیین نشده است.', 'error')
            return redirect(url_for('login'))
        if request.method == 'POST':
            project_id = request.form.get('project_id', type=int)
            project_name = utils.get_project_name(project_id)
            if not project_name or project_id not in allowed_project_ids:
                flash('پروژهٔ انتخاب‌شده معتبر نیست.', 'error')
                return redirect(url_for('select_project'))
            session['active_project_id'] = project_id
            session['active_project_name'] = project_name
            return redirect(url_for('dashboard'))

        return render_template('select_project.html', projects=projects,
                               can_create_project=user.get('role') == 'admin')

    @app.route('/create_project', methods=['POST'])
    @login_required
    @role_required('admin')
    def create_project_route():
        try:
            project_id = utils.create_project(request.form.get('name'))
            session['active_project_id'] = project_id
            session['active_project_name'] = utils.get_project_name(project_id)
            flash('پروژهٔ جدید ساخته شد.', 'success')
            return redirect(url_for('dashboard'))
        except ValueError as error:
            flash(str(error), 'error')
        except Exception as error:
            flash(f'ساخت پروژه ناموفق بود: {error}', 'error')
        return redirect(url_for('select_project'))

    @app.route('/users')
    @login_required
    def users():
        if not has_permission('users'):
            flash('شما به این بخش دسترسی ندارید.', 'error')
            return redirect(url_for('admin_dashboard'))

        conn = utils.get_auth_db_connection()
        try:
            if session['user']['username'] == 'admin':
                user_rows = conn.execute('SELECT * FROM users').fetchall()
            elif session['user'].get('role') == 'admin':
                user_rows = conn.execute(
                    '''SELECT DISTINCT u.* FROM users u
                       LEFT JOIN user_projects up ON up.username = u.username
                       WHERE u.role = 'admin' OR up.project_id = ?''',
                    (session.get('active_project_id', 1),)
                ).fetchall()
            else:
                user_rows = conn.execute(
                    '''SELECT u.* FROM users u
                       JOIN user_projects up ON up.username = u.username
                       WHERE up.project_id = ?''',
                    (session.get('active_project_id', 1),)
                ).fetchall()
        finally:
            conn.close()

        current_username = session['user']['username']
        if session['user'].get('role') == 'admin':
            project_choices = utils.list_projects()
        else:
            active_id = session.get('active_project_id')
            project_choices = [project for project in utils.list_projects() if project['id'] == active_id]
        prepared_users = []
        for row in user_rows:
            user_data = dict(row)
            user_data['project_ids'] = utils.get_user_project_ids(user_data['username'])
            prepared_users.append(user_data)

        utils.log_action(session['user']['username'], 'مشاهده صفحه مدیریت کاربران')
        return render_template('users.html', users=prepared_users, project_choices=project_choices)

    @app.route('/get_permissions/<username>')
    @login_required
    def get_permissions_route(username):
        if not has_permission('users'):
            return 'Access Denied', 403
        if not _can_manage_user(username):
            return 'Access Denied', 403
        if username == 'admin' and session['user']['username'] != 'admin':
            return 'Access Denied', 403

        conn = utils.get_auth_db_connection()
        try:
            user = conn.execute(
                'SELECT permissions FROM users WHERE username = ?', (username,)
            ).fetchone()
        finally:
            conn.close()

        if not user:
            return jsonify({}), 404
        return jsonify({'permissions': json.loads(user['permissions'])})

    @app.route('/get_user_logs/<username>')
    @login_required
    def get_user_logs(username):
        current_username = session['user']['username']
        if not has_permission('users') and current_username != username:
            return 'Access Denied', 403
        if current_username != username and not _can_manage_user(username):
            return 'Access Denied', 403
        if username == 'admin' and current_username != 'admin':
            return 'Access Denied', 403

        conn = utils.get_db_connection()
        try:
            logs = conn.execute(
                'SELECT * FROM user_logs WHERE username = ? ORDER BY timestamp DESC',
                (username,)
            ).fetchall()
        finally:
            conn.close()

        fa_logs = []
        for log in logs:
            miladi_dt = datetime.strptime(log['timestamp'], '%Y-%m-%d %H:%M:%S')
            shamsi_dt = jdatetime.datetime.fromgregorian(datetime=miladi_dt)
            fa_logs.append({
                'action': log['action'],
                'details': log['details'],
                'timestamp_fa': shamsi_dt.strftime('%Y/%m/%d %H:%M:%S')
            })
        return jsonify(fa_logs)

    @app.route('/add_user', methods=['POST'])
    @login_required
    def add_user():
        if not has_permission('users'):
            flash('شما به این بخش دسترسی ندارید.', 'error')
            return redirect(url_for('users'))

        username = request.form['username'].lower()
        name = request.form['name']
        password = request.form.get('password', '').strip()
        role = request.form['role']
        if role not in {'user', 'admin'}:
            return 'Invalid role', 400
        if role == 'admin' and session['user'].get('role') != 'admin':
            return 'Access Denied', 403
        permissions = {
            'index': True, 'management': False, 'reports': False, 'users': False,
            'petty_cash': False, 'petty_cash_reports': False,
            'daily_worker': False, 'daily_worker_reports': False,
            'admin_dashboard': False, 'financial_records': False,
            'financial_reports': False, 'dashboard': False, 'facilities': False,
            'facilities_reports': False, 'payroll_calculation': False,
            'project_wbs': False, 'project_progress': False
        }
        if role == 'admin':
            permissions = {key: True for key in permissions}

        conn = utils.get_auth_db_connection()
        try:
            conn.execute(
                'INSERT INTO users (username, name, password, role, permissions) VALUES (?, ?, ?, ?, ?)',
                (username, name, generate_password_hash(password), role, json.dumps(permissions))
            )
            if role == 'admin':
                conn.execute('DELETE FROM user_projects WHERE username = ?', (username,))
            else:
                requested_ids = request.form.getlist('project_ids')
                allowed_ids = {project['id'] for project in utils.list_projects()} if session['user'].get('role') == 'admin' else {session.get('active_project_id')}
                project_ids = {int(pid) for pid in requested_ids if pid.isdigit()} & allowed_ids
                current_ids = set(utils.get_user_project_ids(username))
                project_ids |= current_ids - allowed_ids
                if not project_ids:
                    project_ids = {session.get('active_project_id', 1)}
                conn.executemany(
                    'INSERT INTO user_projects (username, project_id) VALUES (?, ?)',
                    [(username, project_id) for project_id in project_ids]
                )
            conn.commit()
            flash(f"کاربر '{name}' با موفقیت اضافه شد.", 'success')
            utils.log_action(session['user']['username'], 'افزودن کاربر جدید', f'کاربر: {name} با نقش {role}')
        except sqlite3.IntegrityError:
            flash(f"نام کاربری '{username}' قبلاً ثبت شده است.", 'error')
        finally:
            conn.close()
        return redirect(url_for('users'))

    @app.route('/delete_user', methods=['POST'])
    @login_required
    def delete_user():
        if not has_permission('users'):
            flash('شما به این بخش دسترسی ندارید.', 'error')
            return redirect(url_for('users'))

        username = request.form['username']
        if not _can_manage_user(username):
            return 'Access Denied', 403
        if username == 'admin':
            flash("کاربر 'admin' قابل حذف نیست.", 'error')
            return redirect(url_for('users'))

        conn = utils.get_auth_db_connection()
        try:
            conn.execute('DELETE FROM user_projects WHERE username = ?', (username,))
            conn.execute('DELETE FROM users WHERE username = ?', (username,))
            conn.commit()
            flash(f"کاربر '{username}' با موفقیت حذف شد.", 'success')
            utils.log_action(session['user']['username'], 'حذف کاربر', f'کاربر: {username} حذف شد.')
        finally:
            conn.close()
        return redirect(url_for('users'))

    @app.route('/edit_user', methods=['POST'])
    @login_required
    def edit_user():
        if not has_permission('users'):
            flash('شما به این بخش دسترسی ندارید.', 'error')
            return redirect(url_for('users'))

        username = request.form['username']
        if not _can_manage_user(username):
            return 'Access Denied', 403
        if username == 'admin' and session['user']['username'] != 'admin':
            flash('امکان ویرایش کاربر admin را ندارید.', 'error')
            return redirect(url_for('users'))

        name = request.form['name']
        password = request.form['password']
        role = request.form['role']
        if role not in {'user', 'admin'}:
            return 'Invalid role', 400
        if role == 'admin' and session['user'].get('role') != 'admin':
            return 'Access Denied', 403
        conn = utils.get_auth_db_connection()
        try:
            existing_user = conn.execute(
                'SELECT role, permissions FROM users WHERE username = ?', (username,)
            ).fetchone()
            if not existing_user:
                flash('کاربر پیدا نشد.', 'error')
                return redirect(url_for('users'))

            if password:
                conn.execute(
                    'UPDATE users SET name = ?, password = ?, role = ? WHERE username = ?',
                    (name, generate_password_hash(password), role, username)
                )
            else:
                conn.execute(
                    'UPDATE users SET name = ?, role = ? WHERE username = ?',
                    (name, role, username)
                )

            permission_names = (
                'index', 'management', 'reports', 'users', 'petty_cash',
                'petty_cash_reports', 'daily_worker', 'daily_worker_reports',
                'admin_dashboard', 'financial_records', 'financial_reports',
                'dashboard', 'facilities', 'facilities_reports', 'payroll_calculation',
                'project_wbs', 'project_progress'
            )
            if role == 'admin':
                permissions = {key: True for key in permission_names}
                conn.execute('UPDATE users SET permissions = ? WHERE username = ?',
                             (json.dumps(permissions), username))
            elif existing_user['role'] == 'admin':
                permissions = {key: False for key in permission_names}
                permissions['index'] = True
                conn.execute('UPDATE users SET permissions = ? WHERE username = ?',
                             (json.dumps(permissions), username))
            if role == 'admin':
                conn.execute('DELETE FROM user_projects WHERE username = ?', (username,))
            else:
                requested_ids = request.form.getlist('project_ids')
                allowed_ids = {project['id'] for project in utils.list_projects()} if session['user'].get('role') == 'admin' else {session.get('active_project_id')}
                project_ids = {int(pid) for pid in requested_ids if pid.isdigit()} & allowed_ids
                project_ids |= set(utils.get_user_project_ids(username)) - allowed_ids
                if not project_ids:
                    project_ids = {session.get('active_project_id', 1)}
                conn.execute('DELETE FROM user_projects WHERE username = ?', (username,))
                conn.executemany(
                    'INSERT INTO user_projects (username, project_id) VALUES (?, ?)',
                    [(username, project_id) for project_id in project_ids]
                )
            conn.commit()
            flash(f"اطلاعات کاربر '{name}' با موفقیت ویرایش شد.", 'success')
            utils.log_action(session['user']['username'], 'ویرایش کاربر', f'اطلاعات کاربر {name} ویرایش شد.')
        finally:
            conn.close()
        return redirect(url_for('users'))

    @app.route('/update_permissions', methods=['POST'])
    @login_required
    def update_permissions():
        if not has_permission('users'):
            flash('شما به این بخش دسترسی ندارید.', 'error')
            return redirect(url_for('users'))

        username = request.form['username']
        if not _can_manage_user(username):
            return 'Access Denied', 403
        if username == 'admin' and session['user']['username'] != 'admin':
            flash('امکان تغییر دسترسی‌های admin را ندارید.', 'error')
            return redirect(url_for('users'))

        permission_names = (
            'index', 'management', 'reports', 'users', 'petty_cash',
            'petty_cash_reports', 'daily_worker', 'daily_worker_reports',
            'admin_dashboard', 'financial_records', 'financial_reports',
            'dashboard', 'facilities', 'facilities_reports', 'payroll_calculation',
            'project_wbs', 'project_progress'
        )
        permissions = {name: name in request.form for name in permission_names}

        conn = utils.get_auth_db_connection()
        try:
            conn.execute(
                'UPDATE users SET permissions = ? WHERE username = ?',
                (json.dumps(permissions), username)
            )
            conn.commit()
            flash(f"دسترسی‌های کاربر '{username}' با موفقیت به‌روزرسانی شد.", 'success')
            utils.log_action(session['user']['username'], 'به‌روزرسانی دسترسی‌ها', f'دسترسی‌های کاربر {username} به‌روزرسانی شد.')
        finally:
            conn.close()
        return redirect(url_for('users'))
