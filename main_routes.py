from flask import render_template, request, redirect, url_for, jsonify, session, flash
from datetime import date
import utils
import auth
import jdatetime


def init_main_routes(app):
    @app.route('/')
    @auth.login_required
    def root_route():
        return redirect(url_for('admin_dashboard'))

    @app.route('/admin_dashboard')
    @auth.login_required
    def admin_dashboard():
        permissions = session.get('user', {}).get('permissions', {})
        is_system_admin = session.get('user', {}).get('username') == 'admin'
        if not is_system_admin and not any(permissions.values()):
            flash('شما به این بخش دسترسی ندارید.', 'error')
            return redirect(url_for('index'))
        return render_template('admin_dashboard.html')

    @app.route('/dashboard')
    @auth.login_required
    def dashboard():
        if not auth.has_permission('dashboard'):
            flash('شما به این بخش دسترسی ندارید.', 'error')
            return redirect(url_for('admin_dashboard'))

        dashboard_sections = {
            'employees': auth.has_permission('reports'),
            'petty_cash': auth.has_permission('petty_cash_reports'),
            'daily_workers': auth.has_permission('daily_worker_reports'),
            'facilities': auth.has_permission('facilities_reports'),
            'project_progress': auth.has_permission('project_progress')
        }
        dashboard_actions = {
            'employees': auth.has_permission('index'),
            'petty_cash': auth.has_permission('petty_cash'),
            'daily_workers': auth.has_permission('daily_worker'),
            'facilities': auth.has_permission('facilities'),
            'project_wbs': auth.has_permission('project_wbs')
        }

        if not any(dashboard_sections.values()):
            flash('شما به هیچ‌یک از بخش‌های داشبورد دسترسی ندارید.', 'error')
            return redirect(url_for('admin_dashboard'))

        present_employees = []
        present_employees_count = 0
        total_employees = 0
        vacation_count = 0
        break_count = 0
        daily_workers = []
        facilities_logs = []

        conn = utils.get_db_connection()
        try:
            if dashboard_sections['employees']:
                employees = conn.execute(
                    'SELECT * FROM employees ORDER BY name'
                ).fetchall()
                employees_by_id = {
                    str(employee['employee_id']): employee['name']
                    for employee in employees
                }
                latest_actions = conn.execute('''
                    SELECT employee_id, action, condition,
                           activity_location, activity_description
                    FROM time_logs
                    WHERE id IN (
                        SELECT MAX(id)
                        FROM time_logs
                        GROUP BY employee_id
                    )
                ''').fetchall()

                present_employees = [
                    {
                        'name': employees_by_id[action['employee_id']],
                        'activity_location': action['activity_location'],
                        'activity_description': action['activity_description']
                    }
                    for action in latest_actions
                    if action['action'] == 'ورود'
                    and action['employee_id'] in employees_by_id
                ]
                present_employees_count = len(present_employees)
                total_employees = len(employees)

                today = date.today().isoformat()
                vacation_count = conn.execute('''
                    SELECT COUNT(DISTINCT employee_id)
                    FROM time_logs
                    WHERE action = 'ورود' AND condition = 'مرخصی'
                      AND timestamp LIKE ?
                ''', (f'{today}%',)).fetchone()[0] or 0
                break_count = conn.execute('''
                    SELECT COUNT(DISTINCT employee_id)
                    FROM time_logs
                    WHERE action = 'ورود' AND condition = 'استراحت'
                      AND timestamp LIKE ?
                ''', (f'{today}%',)).fetchone()[0] or 0

            if dashboard_sections['daily_workers']:
                daily_workers = get_daily_worker_data(conn)
            if dashboard_sections['facilities']:
                facilities_logs = get_facilities_data(conn)
        finally:
            conn.close()

        return render_template(
            'dashboard.html',
            dashboard_sections=dashboard_sections,
            dashboard_actions=dashboard_actions,
            present_employees=present_employees,
            present_employees_count=present_employees_count,
            total_employees=total_employees,
            vacation_count=vacation_count,
            break_count=break_count,
            daily_workers=daily_workers,
            facilities_logs=facilities_logs
        )

    @app.route('/api/petty_cash_by_date_range')
    @auth.login_required
    def petty_cash_by_date_range():
        if not auth.has_permission('petty_cash_reports'):
            return jsonify({'error': 'Access Denied'}), 403
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        if not start_date or not end_date:
            return jsonify({'labels': [], 'amounts': []})

        conn = utils.get_db_connection()
        try:
            start = utils.shamsi_to_miladi(start_date).strftime('%Y-%m-%d 00:00:00')
            end = utils.shamsi_to_miladi(end_date).strftime('%Y-%m-%d 23:59:59')
            records = conn.execute('''
                SELECT date, SUM(total_amount) AS total_amount
                FROM petty_cash
                WHERE timestamp BETWEEN ? AND ?
                GROUP BY date ORDER BY date
            ''', (start, end)).fetchall()
        finally:
            conn.close()
        return jsonify({
            'labels': [record['date'] for record in records],
            'amounts': [record['total_amount'] for record in records]
        })

    @app.route('/api/petty_cash_by_date')
    @auth.login_required
    def petty_cash_by_date():
        if not auth.has_permission('petty_cash_reports'):
            return jsonify({'error': 'Access Denied'}), 403
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        if not start_date or not end_date:
            return jsonify({'records': []})

        conn = utils.get_db_connection()
        try:
            start = utils.shamsi_to_miladi(start_date).strftime('%Y-%m-%d 00:00:00')
            end = utils.shamsi_to_miladi(end_date).strftime('%Y-%m-%d 23:59:59')
            records = conn.execute(
                'SELECT * FROM petty_cash WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp DESC',
                (start, end)
            ).fetchall()
        finally:
            conn.close()
        return jsonify({'records': [dict(record) for record in records]})

    @app.route('/api/petty_cash_chart_data')
    @auth.login_required
    def petty_cash_chart_data():
        if not auth.has_permission('petty_cash_reports'):
            return jsonify({'error': 'Access Denied'}), 403
        conn = utils.get_db_connection()
        try:
            records = conn.execute('''
                SELECT STRFTIME('%Y-%m', timestamp) AS month,
                       SUM(total_amount) AS total_amount
                FROM petty_cash
                GROUP BY month ORDER BY month DESC LIMIT 12
            ''').fetchall()
        finally:
            conn.close()

        records = list(reversed(records))
        labels = []
        for record in records:
            year, month = record['month'].split('-')
            labels.append(
                jdatetime.date.fromgregorian(
                    year=int(year), month=int(month), day=1
                ).strftime('%B %Y')
            )
        return jsonify({'labels': labels, 'amounts': [record['total_amount'] for record in records]})

    @app.route('/api/daily_workers_by_date_range')
    @auth.login_required
    def daily_workers_by_date_range():
        if not auth.has_permission('daily_worker_reports'):
            return jsonify({'error': 'Access Denied'}), 403
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        if not start_date or not end_date:
            return jsonify({'records': []})

        conn = utils.get_db_connection()
        try:
            start = utils.shamsi_to_miladi(start_date).strftime('%Y-%m-%d 00:00:00')
            end = utils.shamsi_to_miladi(end_date).strftime('%Y-%m-%d 23:59:59')
            records = conn.execute(
                'SELECT * FROM daily_workers WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp DESC',
                (start, end)
            ).fetchall()
        finally:
            conn.close()
        return jsonify({'records': [dict(record) for record in records]})

    @app.route('/api/facilities_by_date_range')
    @auth.login_required
    def facilities_by_date_range():
        if not auth.has_permission('facilities_reports'):
            return jsonify({'error': 'Access Denied'}), 403
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        if not start_date or not end_date:
            return jsonify({'records': []})

        conn = utils.get_db_connection()
        try:
            start = utils.shamsi_to_miladi(start_date).strftime('%Y-%m-%d 00:00:00')
            end = utils.shamsi_to_miladi(end_date).strftime('%Y-%m-%d 23:59:59')
            records = conn.execute(
                'SELECT * FROM facilities_logs WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp DESC',
                (start, end)
            ).fetchall()
        finally:
            conn.close()
        return jsonify({'records': [dict(record) for record in records]})


def get_daily_worker_data(conn):
    today = date.today().isoformat()
    return conn.execute('''
        SELECT foreman_name, worker_count, location
        FROM daily_workers WHERE timestamp LIKE ?
    ''', (f'{today}%',)).fetchall()


def get_facilities_data(conn):
    today = date.today().isoformat()
    return conn.execute('''
        SELECT facility_name, companion_name, activity_location,
               activity_description, duration
        FROM facilities_logs WHERE timestamp LIKE ?
    ''', (f'{today}%',)).fetchall()
