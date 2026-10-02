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


def init_auth_system(app):
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            username = request.form['username'].lower()
            password = request.form['password']

            conn = utils.get_db_connection()
            try:
                user = conn.execute(
                    'SELECT * FROM users WHERE username = ?', (username,)
                ).fetchone()
            finally:
                conn.close()

            if user and check_password_hash(user['password'], password):
                session['user'] = {
                    'username': user['username'],
                    'name': user['name'],
                    'role': user['role'],
                    'permissions': json.loads(user['permissions'])
                }
                utils.log_action(user['username'], 'ورود به سیستم', 'ورود موفق')
                flash('ورود با موفقیت انجام شد.', 'success')
                return redirect(url_for('dashboard'))

            flash('نام کاربری یا رمز عبور اشتباه است.', 'error')
            return redirect(url_for('login'))

        return render_template('login.html')

    @app.route('/logout')
    def logout():
        username = session.get('user', {}).get('username')
        if username:
            utils.log_action(username, 'خروج از سیستم', 'خروج موفق')
        session.pop('user', None)
        flash('شما با موفقیت از سیستم خارج شدید.', 'success')
        return redirect(url_for('login'))

    @app.route('/users')
    @login_required
    def users():
        if not has_permission('users'):
            flash('شما به این بخش دسترسی ندارید.', 'error')
            return redirect(url_for('admin_dashboard'))

        conn = utils.get_db_connection()
        try:
            if session['user']['username'] == 'admin':
                user_rows = conn.execute('SELECT * FROM users').fetchall()
            else:
                user_rows = conn.execute(
                    "SELECT * FROM users WHERE username <> 'admin'"
                ).fetchall()
        finally:
            conn.close()

        utils.log_action(session['user']['username'], 'مشاهده صفحه مدیریت کاربران')
        return render_template('users.html', users=user_rows)

    @app.route('/get_permissions/<username>')
    @login_required
    def get_permissions_route(username):
        if not has_permission('users'):
            return 'Access Denied', 403
        if username == 'admin' and session['user']['username'] != 'admin':
            return 'Access Denied', 403

        conn = utils.get_db_connection()
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
        password = request.form['password']
        role = request.form['role']
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

        conn = utils.get_db_connection()
        try:
            conn.execute(
                'INSERT INTO users (username, name, password, role, permissions) VALUES (?, ?, ?, ?, ?)',
                (username, name, generate_password_hash(password), role, json.dumps(permissions))
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
        if username == 'admin':
            flash("کاربر 'admin' قابل حذف نیست.", 'error')
            return redirect(url_for('users'))

        conn = utils.get_db_connection()
        try:
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
        if username == 'admin' and session['user']['username'] != 'admin':
            flash('امکان ویرایش کاربر admin را ندارید.', 'error')
            return redirect(url_for('users'))

        name = request.form['name']
        password = request.form['password']
        role = request.form['role']
        conn = utils.get_db_connection()
        try:
            conn.execute(
                'UPDATE users SET name = ?, password = ?, role = ? WHERE username = ?',
                (name, generate_password_hash(password), role, username)
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

        conn = utils.get_db_connection()
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
