from flask import render_template, request, redirect, url_for, session, flash, jsonify, send_file, abort
import sqlite3
import json
import os
import re
import shutil
import uuid
import zipfile
from itertools import chain
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime, timedelta
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


def _can_manage_projects():
    return (session.get('user', {}).get('role') == 'admin'
            and has_permission('users'))


def _project_date_to_storage(value):
    """Convert a Shamsi project date from the form to the stored ISO date."""
    value = (value or '').strip()
    if not value:
        return None
    persian_digits = '۰۱۲۳۴۵۶۷۸۹'
    arabic_digits = '٠١٢٣٤٥٦٧٨٩'
    value = value.translate(str.maketrans(persian_digits + arabic_digits,
                                         '0123456789' * 2)).replace('-', '/').replace('.', '/')
    parts = value.split('/')
    if len(parts) != 3:
        raise ValueError('تاریخ شروع را به صورت شمسی وارد کنید.')
    try:
        jalali_date = jdatetime.date(*(int(part) for part in parts))
        return jalali_date.togregorian().isoformat()
    except (TypeError, ValueError):
        raise ValueError('تاریخ شمسی واردشده معتبر نیست.')


def _project_date_to_jalali(value):
    """Format stored Gregorian ISO project dates for Persian page display."""
    if not value:
        return ''
    try:
        gregorian_date = datetime.strptime(value[:10], '%Y-%m-%d').date()
        jalali_date = jdatetime.date.fromgregorian(date=gregorian_date)
        return jalali_date.strftime('%Y/%m/%d')
    except (TypeError, ValueError):
        return value


def _excel_jalali_date_text(value):
    """Convert ISO Gregorian date/date-time text to a Shamsi date for exports."""
    if not isinstance(value, str):
        return value
    match = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})(.*)', value.strip())
    if not match:
        return value
    try:
        gregorian_date = datetime.strptime('-'.join(match.group(i) for i in (1, 2, 3)), '%Y-%m-%d').date()
        jalali_date = jdatetime.date.fromgregorian(date=gregorian_date)
        persian_date = jalali_date.strftime('%Y/%m/%d').translate(str.maketrans(
            '0123456789', '۰۱۲۳۴۵۶۷۸۹'
        ))
        return persian_date + match.group(4)
    except ValueError:
        return value


def _project_lock_response(project_id, endpoint):
    if not project_id or endpoint == 'users':
        return None
    lock = utils.get_project_deletion_lock(project_id)
    if not lock or not lock['deletion_token']:
        return None
    started_at = lock['deletion_started_at']
    try:
        started = datetime.strptime(started_at, '%Y-%m-%d %H:%M:%S')
        if datetime.utcnow() - started > timedelta(hours=12):
            old_token = lock['deletion_token']
            utils.unlock_project(project_id, old_token)
            shutil.rmtree(os.path.join(utils.PROJECT_EXPORT_FOLDER, old_token), ignore_errors=True)
            return None
    except (TypeError, ValueError):
        pass
    return render_template('project_locked.html', project_name=utils.get_project_name(project_id),
                           is_admin=session.get('user', {}).get('role') == 'admin')


def _project_export_paths(token):
    if not re.fullmatch(r'[0-9a-f]{32}', token or ''):
        abort(404)
    export_dir = os.path.join(utils.PROJECT_EXPORT_FOLDER, token)
    return export_dir, os.path.join(export_dir, 'project_data.xlsx'), os.path.join(export_dir, 'project_archive.zip')


def _write_project_workbook(project, db_path, output_path):
    import xlsxwriter

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    workbook = xlsxwriter.Workbook(output_path, {'constant_memory': True})
    workbook_closed = False
    used_sheet_names = set()

    def add_sheet(base_name):
        cleaned = re.sub(r'[\\/*?:\[\]]', '_', base_name).strip() or 'Data'
        cleaned = cleaned[:31]
        name = cleaned
        suffix = 2
        while name in used_sheet_names:
            ending = f'_{suffix}'
            name = cleaned[:31 - len(ending)] + ending
            suffix += 1
        used_sheet_names.add(name)
        return workbook.add_worksheet(name)

    try:
        conn.execute('PRAGMA busy_timeout = 30000')
        conn.execute('BEGIN IMMEDIATE')
        metadata_sheet = add_sheet('مشخصات پروژه')
        metadata = (
            ('نام پروژه', project['name']),
            ('نام کارفرما', project['employer_name']),
            ('تاریخ شروع بکار', project['start_date']),
            ('مدت قرارداد (ماه)', project['contract_duration_months']),
            ('مبلغ برآورد', project['estimate_amount']),
            ('شناسه پروژه', project['id']),
        )
        for row_index, (label, value) in enumerate(metadata):
            metadata_sheet.write_string(row_index, 0, label)
            if value is None:
                metadata_sheet.write_blank(row_index, 1, None)
            elif isinstance(value, (int, float)):
                metadata_sheet.write_number(row_index, 1, value)
            else:
                metadata_sheet.write_string(row_index, 1, str(_excel_jalali_date_text(str(value))))
        metadata_sheet.set_column(0, 0, 26)
        metadata_sheet.set_column(1, 1, 40)

        table_names = [row['name'] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall() if row['name'] not in {'users', 'projects', 'user_projects'}]
        for table_name in table_names:
            quoted_table = '"' + table_name.replace('"', '""') + '"'
            columns = [row['name'] for row in conn.execute(f'PRAGMA table_info({quoted_table})').fetchall()]
            if not columns:
                continue
            cursor = conn.execute(f'SELECT * FROM {quoted_table}')
            first_record = cursor.fetchone()
            if first_record is None:
                continue
            sheet = add_sheet(table_name)
            header_format = workbook.add_format({'bold': True, 'bg_color': '#DCE6F1'})
            for col_index, column in enumerate(columns):
                sheet.write_string(0, col_index, column, header_format)
            row_index = 1
            for record in chain((first_record,), cursor):
                for col_index, value in enumerate(record):
                    value = _excel_jalali_date_text(value)
                    if value is None:
                        sheet.write_blank(row_index, col_index, None)
                    elif isinstance(value, str):
                        sheet.write_string(row_index, col_index, value)
                    elif isinstance(value, (int, float)):
                        sheet.write_number(row_index, col_index, value)
                    elif isinstance(value, bytes):
                        sheet.write_string(row_index, col_index, f'[دادهٔ باینری: {len(value)} بایت]')
                    else:
                        sheet.write_string(row_index, col_index, str(value))
                row_index += 1
            sheet.freeze_panes(1, 0)
            sheet.autofilter(0, 0, max(0, row_index - 1), len(columns) - 1)
            sheet.set_row(0, 24)
        workbook.close()
        workbook_closed = True
        conn.commit()
    except Exception:
        if not workbook_closed:
            try:
                workbook.close()
            except Exception:
                pass
        conn.rollback()
        raise
    finally:
        conn.close()


def _create_project_export(project, token=None):
    project_id = int(project['id'])
    db_path = utils.get_project_db_path(project_id)
    if not os.path.isfile(db_path):
        raise ValueError('فایل دیتابیس پروژه پیدا نشد؛ هیچ اطلاعاتی حذف نشد.')

    token = token or uuid.uuid4().hex
    export_dir, xlsx_path, zip_path = _project_export_paths(token)
    os.makedirs(export_dir, exist_ok=False)
    safe_name = re.sub(r'[^\w.-]+', '_', project['name'], flags=re.UNICODE).strip('_') or f'project_{project_id}'
    try:
        _write_project_workbook(project, db_path, xlsx_path)
        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(xlsx_path, arcname=f'{safe_name}/project_data.xlsx')
            archive.writestr(f'{safe_name}/README.txt',
                             'این بسته شامل خروجی اکسل همهٔ جدول‌های پروژه و فایل‌های پیوست‌شده است.\n')
            upload_dirs = [(utils.get_project_upload_folder(project_id), 'project_uploads')]
            if project_id == 1:
                upload_dirs.append((utils.UPLOAD_FOLDER, 'legacy_uploads'))
            for folder, label in upload_dirs:
                if not os.path.isdir(folder):
                    continue
                for root, _, filenames in os.walk(folder):
                    for filename in filenames:
                        full_path = os.path.join(root, filename)
                        relative_path = os.path.relpath(full_path, folder).replace(os.sep, '/')
                        archive.write(full_path, arcname='/'.join(
                            (safe_name, 'attachments', label, relative_path)
                        ))
        return token
    except Exception:
        shutil.rmtree(export_dir, ignore_errors=True)
        raise


def _delete_project_data(project_id, token):
    project_id = int(project_id)
    export_dir, _, _ = _project_export_paths(token)
    quarantine = os.path.join(export_dir, 'pending_delete')
    os.makedirs(quarantine, exist_ok=True)
    moved_paths = []

    paths_to_move = [
        (utils.get_project_upload_folder(project_id), os.path.join(quarantine, 'project_uploads')),
    ]
    if project_id == 1:
        paths_to_move.append((utils.UPLOAD_FOLDER, os.path.join(quarantine, 'legacy_uploads')))
    else:
        db_path = utils.get_project_db_path(project_id)
        checkpoint = sqlite3.connect(db_path, timeout=30)
        try:
            checkpoint.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            checkpoint.close()
        for suffix, label in (('', 'project.db'), ('-wal', 'project.db-wal'),
                              ('-shm', 'project.db-shm'), ('-journal', 'project.db-journal')):
            paths_to_move.append((db_path + suffix, os.path.join(quarantine, label)))

    try:
        for source, destination in paths_to_move:
            if os.path.exists(source):
                os.replace(source, destination)
                moved_paths.append((source, destination))

        conn = utils.get_auth_db_connection()
        try:
            if project_id == 1:
                conn.execute('BEGIN IMMEDIATE')
                protected_tables = {'users', 'projects', 'user_projects'}
                table_names = [row['name'] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()]
                for table_name in table_names:
                    if table_name not in protected_tables:
                        quoted = '"' + table_name.replace('"', '""') + '"'
                        conn.execute(f'DROP TABLE IF EXISTS {quoted}')
                conn.execute('DELETE FROM user_projects WHERE project_id = ?', (project_id,))
                conn.execute('DELETE FROM projects WHERE id = ?', (project_id,))
            else:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute('DELETE FROM user_projects WHERE project_id = ?', (project_id,))
                conn.execute('DELETE FROM projects WHERE id = ?', (project_id,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        for source, destination in reversed(moved_paths):
            if os.path.exists(destination):
                os.makedirs(os.path.dirname(source), exist_ok=True)
                os.replace(destination, source)
        shutil.rmtree(quarantine, ignore_errors=True)
        raise

    shutil.rmtree(export_dir)


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
        project_management_endpoints = {
            'select_project', 'create_project_route', 'edit_project_route',
            'begin_project_export', 'download_project_export', 'delete_project_route',
            'cancel_project_export_route'
        }
        if endpoint in project_management_endpoints:
            return None

        if user.get('role') == 'admin':
            project_id = session.get('active_project_id')
            project_name = utils.get_project_name(project_id)
            if not project_name:
                projects = utils.list_projects()
                if len(projects) == 1:
                    project_id = projects[0]['id']
                    project_name = projects[0]['name']
                elif endpoint == 'users':
                    session.pop('active_project_id', None)
                    session.pop('active_project_name', None)
                    return None
                else:
                    session.pop('active_project_id', None)
                    session.pop('active_project_name', None)
                    return redirect(url_for('select_project'))
            session['active_project_id'] = project_id
            session['active_project_name'] = project_name
            locked_response = _project_lock_response(project_id, endpoint)
            if locked_response:
                return locked_response
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
        locked_response = _project_lock_response(active_project_id, endpoint)
        if locked_response:
            return locked_response
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
                    projects = utils.list_projects()
                    if len(projects) == 1:
                        if projects[0]['deletion_token']:
                            return redirect(url_for('select_project'))
                        session['active_project_id'] = projects[0]['id']
                        session['active_project_name'] = projects[0]['name']
                        utils.log_action(user['username'], 'ورود به پروژه', projects[0]['name'])
                        return redirect(url_for('dashboard'))
                    return redirect(url_for('select_project'))

                project_ids = utils.get_user_project_ids(user['username'])
                available_projects = [project for project in utils.list_projects()
                                      if project['id'] in project_ids]
                if not available_projects:
                    session.clear()
                    flash('برای این کاربر پروژه‌ای تعیین نشده است؛ با مدیر سیستم تماس بگیرید.', 'error')
                    return redirect(url_for('login'))
                if len(available_projects) > 1:
                    return redirect(url_for('select_project'))
                if available_projects[0]['deletion_token']:
                    return redirect(url_for('select_project'))
                session['active_project_id'] = available_projects[0]['id']
                session['active_project_name'] = available_projects[0]['name']
                utils.log_action(user['username'], 'ورود به پروژه', session['active_project_name'])
                return redirect(url_for('dashboard'))

            flash('نام کاربری یا رمز عبور اشتباه است.', 'error')
            return redirect(url_for('login'))

        return render_template('login.html')

    @app.route('/logout')
    def logout():
        username = session.get('user', {}).get('username')
        active_project_id = session.get('active_project_id')
        lock = utils.get_project_deletion_lock(active_project_id) if active_project_id else None
        if username and not (lock and lock['deletion_token']):
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
            flash('پروژه‌ای ثبت نشده است. برای ساخت پروژه از صفحهٔ کاربران اقدام کنید.', 'error')
            return redirect(url_for('users'))
        if len(projects) == 1:
            if projects[0]['deletion_token']:
                if user.get('role') == 'admin':
                    flash('این پروژه در حال خروجی‌گیری و حذف است.', 'error')
                    return redirect(url_for('users'))
                return render_template('project_locked.html', project_name=projects[0]['name'], is_admin=False)
            session['active_project_id'] = projects[0]['id']
            session['active_project_name'] = projects[0]['name']
            utils.log_action(user['username'], 'ورود به پروژه', projects[0]['name'])
            return redirect(url_for('dashboard'))
        if request.method == 'POST':
            project_id = request.form.get('project_id', type=int)
            project_name = utils.get_project_name(project_id)
            if not project_name or project_id not in allowed_project_ids:
                flash('پروژهٔ انتخاب‌شده معتبر نیست.', 'error')
                return redirect(url_for('select_project'))
            project_record = next((item for item in projects if item['id'] == project_id), None)
            if project_record and project_record['deletion_token']:
                flash('این پروژه برای تهیه خروجی و حذف موقتاً قفل شده است.', 'error')
                return redirect(url_for('users') if user.get('role') == 'admin' else url_for('select_project'))
            session['active_project_id'] = project_id
            session['active_project_name'] = project_name
            utils.log_action(user['username'], 'ورود به پروژه', project_name)
            return redirect(url_for('dashboard'))

        return render_template('select_project.html', projects=projects)

    @app.route('/create_project', methods=['POST'])
    @login_required
    @role_required('admin')
    def create_project_route():
        if not _can_manage_projects():
            return 'Access Denied', 403
        try:
            duration_text = request.form.get('contract_duration_months', '').strip()
            amount_text = request.form.get('estimate_amount', '').strip()
            duration = int(duration_text) if duration_text else None
            estimate = float(amount_text) if amount_text else None
            if duration is not None and duration < 0 or estimate is not None and estimate < 0:
                raise ValueError('مدت قرارداد و مبلغ برآورد نمی‌توانند منفی باشند.')
            project_id = utils.create_project(
                request.form.get('name'), request.form.get('employer_name'),
                _project_date_to_storage(request.form.get('start_date')), duration, estimate
            )
            if not utils.get_project_name(session.get('active_project_id')):
                session['active_project_id'] = project_id
                session['active_project_name'] = utils.get_project_name(project_id)
            flash('پروژهٔ جدید ساخته شد.', 'success')
            return redirect(url_for('users'))
        except ValueError as error:
            flash(str(error), 'error')
        except Exception as error:
            flash('ساخت پروژه ناموفق بود؛ اطلاعات را بررسی کنید.', 'error')
        return redirect(url_for('users'))

    @app.route('/edit_project', methods=['POST'])
    @login_required
    def edit_project_route():
        if not _can_manage_projects():
            return 'Access Denied', 403
        try:
            project_id = request.form.get('project_id', type=int)
            lock = utils.get_project_deletion_lock(project_id)
            if lock and lock['deletion_token']:
                flash('ویرایش این پروژه هنگام آماده‌سازی حذف ممکن نیست.', 'error')
                return redirect(url_for('users'))
            duration_text = request.form.get('contract_duration_months', '').strip()
            amount_text = request.form.get('estimate_amount', '').strip()
            duration = int(duration_text) if duration_text else None
            estimate = float(amount_text) if amount_text else None
            if duration is not None and duration < 0 or estimate is not None and estimate < 0:
                raise ValueError('مدت قرارداد و مبلغ برآورد نمی‌توانند منفی باشند.')
            utils.update_project(
                project_id, request.form.get('name'), request.form.get('employer_name'),
                _project_date_to_storage(request.form.get('start_date')), duration, estimate
            )
            if session.get('active_project_id') == project_id:
                session['active_project_name'] = utils.get_project_name(project_id)
            flash('مشخصات پروژه به‌روزرسانی شد.', 'success')
        except ValueError as error:
            flash(str(error), 'error')
        except Exception:
            flash('ویرایش پروژه ناموفق بود؛ اطلاعات را بررسی کنید.', 'error')
        return redirect(url_for('users'))

    @app.route('/projects/<int:project_id>/export-before-delete', methods=['POST'])
    @login_required
    def begin_project_export(project_id):
        if not _can_manage_projects():
            return 'Access Denied', 403
        project = next((item for item in utils.list_projects() if item['id'] == project_id), None)
        if not project:
            flash('پروژه پیدا نشد.', 'error')
            return redirect(url_for('users'))
        try:
            token = uuid.uuid4().hex
            old_token = utils.lock_project_for_export(project_id, token)
            if old_token is False or old_token is None:
                flash('این پروژه از قبل در حال آماده‌سازی خروجی یا حذف است.', 'error')
                return redirect(url_for('users'))
            if old_token:
                shutil.rmtree(os.path.join(utils.PROJECT_EXPORT_FOLDER, old_token), ignore_errors=True)
            token = _create_project_export(project, token)
            session['pending_project_export'] = {
                'token': token, 'project_id': project_id,
                'project_name': project['name'], 'downloaded': []
            }
            return render_template('project_export_ready.html', project=project, token=token)
        except Exception:
            if 'token' in locals():
                utils.unlock_project(project_id, token)
                shutil.rmtree(os.path.join(utils.PROJECT_EXPORT_FOLDER, token), ignore_errors=True)
            flash('ساخت خروجی پروژه ناموفق بود؛ پروژه حذف نشد.', 'error')
            return redirect(url_for('users'))

    @app.route('/projects/<int:project_id>/cancel-export', methods=['POST'])
    @login_required
    def cancel_project_export_route(project_id):
        if not _can_manage_projects():
            return 'Access Denied', 403
        lock = utils.get_project_deletion_lock(project_id)
        if lock and lock['deletion_token']:
            token = lock['deletion_token']
            utils.unlock_project(project_id, token)
            shutil.rmtree(os.path.join(utils.PROJECT_EXPORT_FOLDER, token), ignore_errors=True)
            pending = session.get('pending_project_export') or {}
            if pending.get('token') == token:
                session.pop('pending_project_export', None)
            flash('فرایند خروجی و حذف پروژه لغو شد؛ اطلاعات پروژه باقی ماند.', 'success')
        return redirect(url_for('users'))

    @app.route('/project-export/<token>/<kind>')
    @login_required
    def download_project_export(token, kind):
        if not _can_manage_projects() or kind not in {'excel', 'zip'}:
            return 'Access Denied', 403
        pending = session.get('pending_project_export') or {}
        if pending.get('token') != token:
            abort(404)
        export_dir, xlsx_path, zip_path = _project_export_paths(token)
        file_path = xlsx_path if kind == 'excel' else zip_path
        if not os.path.isfile(file_path):
            abort(404)
        pending['downloaded'] = list(set(pending.get('downloaded', []) + [kind]))
        session['pending_project_export'] = pending
        safe_name = re.sub(r'[^\w.-]+', '_', pending['project_name'], flags=re.UNICODE).strip('_') or 'project'
        return send_file(file_path, as_attachment=True,
                         download_name=f'{safe_name}_data.xlsx' if kind == 'excel' else f'{safe_name}_archive.zip')

    @app.route('/projects/<int:project_id>/delete', methods=['POST'])
    @login_required
    def delete_project_route(project_id):
        if not _can_manage_projects():
            return 'Access Denied', 403
        pending = session.get('pending_project_export') or {}
        if pending.get('project_id') != project_id or set(pending.get('downloaded', [])) != {'excel', 'zip'}:
            flash('ابتدا هر دو فایل اکسل و ZIP خروجی را دریافت کنید.', 'error')
            return redirect(url_for('users'))
        project = next((item for item in utils.list_projects() if item['id'] == project_id), None)
        if not project:
            flash('پروژه قبلاً حذف شده است.', 'error')
            session.pop('pending_project_export', None)
            return redirect(url_for('users'))
        lock = utils.get_project_deletion_lock(project_id)
        if not lock or lock['deletion_token'] != pending.get('token'):
            flash('فرایند حذف اعتبار ندارد؛ دوباره خروجی بگیرید.', 'error')
            session.pop('pending_project_export', None)
            return redirect(url_for('users'))
        confirmation = request.form.get('project_name_confirmation', '').strip()
        if confirmation != project['name']:
            flash('نام پروژه برای تأیید حذف درست وارد نشده است.', 'error')
            return render_template('project_export_ready.html', project=project, token=pending['token'])

        try:
            _delete_project_data(project_id, pending['token'])
            session.pop('pending_project_export', None)
            if session.get('active_project_id') == project_id:
                remaining = utils.list_projects()
                session.pop('active_project_id', None)
                session.pop('active_project_name', None)
                if len(remaining) == 1:
                    session['active_project_id'] = remaining[0]['id']
                    session['active_project_name'] = remaining[0]['name']
            flash('پروژه پس از تهیه و دریافت خروجی، از سرور حذف شد.', 'success')
        except Exception:
            flash('حذف پروژه کامل نشد. فایل خروجی نگهداری شد؛ دوباره تلاش کنید یا با پشتیبانی تماس بگیرید.', 'error')
            return redirect(url_for('users'))
        return redirect(url_for('users'))

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
                active_project_id = session.get('active_project_id')
                if active_project_id:
                    user_rows = conn.execute(
                        '''SELECT DISTINCT u.* FROM users u
                           LEFT JOIN user_projects up ON up.username = u.username
                           WHERE u.role = 'admin' OR up.project_id = ?''',
                        (active_project_id,)
                    ).fetchall()
                else:
                    user_rows = conn.execute("SELECT * FROM users WHERE role = 'admin'").fetchall()
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

        active_lock = utils.get_project_deletion_lock(session.get('active_project_id'))
        if not (active_lock and active_lock['deletion_token']):
            utils.log_action(session['user']['username'], 'مشاهده صفحه مدیریت کاربران')
        projects = [dict(project) for project in utils.list_projects()]
        for project in projects:
            project['start_date_jalali'] = _project_date_to_jalali(project['start_date'])
        return render_template(
            'users.html', users=prepared_users, project_choices=project_choices,
            projects=projects, can_create_project=_can_manage_projects()
        )

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

        project_ids = set()
        if role != 'admin':
            requested_ids = request.form.getlist('project_ids')
            allowed_ids = ({project['id'] for project in utils.list_projects()}
                           if session['user'].get('role') == 'admin'
                           else {session.get('active_project_id')})
            project_ids = {int(pid) for pid in requested_ids if pid.isdigit()} & allowed_ids
            if not project_ids:
                flash('برای کاربر عادی دست‌کم یک پروژه انتخاب کنید.', 'error')
                return redirect(url_for('users'))

        conn = utils.get_auth_db_connection()
        try:
            conn.execute(
                'INSERT INTO users (username, name, password, role, permissions) VALUES (?, ?, ?, ?, ?)',
                (username, name, generate_password_hash(password), role, json.dumps(permissions))
            )
            if role == 'admin':
                conn.execute('DELETE FROM user_projects WHERE username = ?', (username,))
            else:
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
        project_ids = set()
        if role != 'admin':
            requested_ids = request.form.getlist('project_ids')
            allowed_ids = ({project['id'] for project in utils.list_projects()}
                           if session['user'].get('role') == 'admin'
                           else {session.get('active_project_id')})
            project_ids = {int(pid) for pid in requested_ids if pid.isdigit()} & allowed_ids
            project_ids |= set(utils.get_user_project_ids(username)) - allowed_ids
            if not project_ids:
                flash('برای کاربر عادی دست‌کم یک پروژه انتخاب کنید.', 'error')
                return redirect(url_for('users'))
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
