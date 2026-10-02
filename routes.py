from flask import Flask, render_template, request, redirect, url_for, jsonify, Response, session, flash
import sqlite3
from datetime import datetime, date
import os
import csv
import io
import jdatetime
import urllib.parse
import zipfile
import json
import utils
import uuid
import auth

# this is a temporary list until we have a real database
temp_employees_db = [
    {'employeeId': '101', 'name': 'علی احمدی'},
    {'employeeId': '102', 'name': 'زهرا حسینی'},
    {'employeeId': '103', 'name': 'رضا محمدی'}
]

def init_routes(app):
    @app.route('/')
    @auth.login_required
    def root_route():
        return redirect(url_for('admin_dashboard'))

    @app.route('/index')
    @auth.login_required
    def index():
        if not auth.has_permission('index'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('admin_dashboard'))
            
        conn = utils.get_db_connection()
        try:
            employees = conn.execute('SELECT * FROM employees ORDER BY name').fetchall()
            total_employees_row = conn.execute('SELECT COUNT(*) AS count FROM employees').fetchone()
            total_employees = total_employees_row['count'] if total_employees_row else 0
            
            present_employees = []
            vacation_count = 0
            break_count = 0
            
            employees_dict = {str(emp['employee_id']): {'name': emp['name'], 'last_action': None, 'condition': 'عادی'} for emp in employees}

            latest_actions = conn.execute('''
                SELECT employee_id, action, condition
                FROM time_logs
                WHERE id IN (
                    SELECT MAX(id)
                    FROM time_logs
                    GROUP BY employee_id
                )
            ''').fetchall()

            for action_log in latest_actions:
                if action_log['action'] == 'ورود':
                    employee_id = action_log['employee_id']
                    if employee_id in employees_dict:
                        employees_dict[employee_id]['last_action'] = 'ورود'
                        employees_dict[employee_id]['condition'] = action_log['condition'] if action_log['condition'] else 'عادی'
                        if employees_dict[employee_id]['condition'] == 'مرخصی':
                            vacation_count += 1
                        elif employees_dict[employee_id]['condition'] == 'استراحت':
                            break_count += 1
                        
            present_employees = [{'name': emp['name'], 'condition': emp['condition'], 'id': emp_id} for emp_id, emp in employees_dict.items() if emp['last_action'] == 'ورود']
            present_employees_count = len(present_employees)
        finally:
            conn.close()
        
        return render_template('index.html', 
                               employees=employees,
                               total_employees=total_employees,
                               present_employees=present_employees_count,
                               present_employees_with_condition=present_employees,
                               vacation_count=vacation_count,
                               break_count=break_count)

    @app.route('/check_status', methods=['POST'])
    @auth.login_required
    def check_status():
        if not auth.has_permission('index'):
            return jsonify({'error': 'Access Denied'}), 403
            
        conn = utils.get_db_connection()
        employee_ids = request.json.get('employee_ids')
        status = {}
        try:
            for employee_id in employee_ids:
                latest_action_row = conn.execute('SELECT action FROM time_logs WHERE employee_id = ? ORDER BY timestamp DESC LIMIT 1', (employee_id,)).fetchone()
                status[employee_id] = latest_action_row['action'] if latest_action_row else 'خروج'
        finally:
            conn.close()
        return jsonify(status)

    @app.route('/check_temp_activities', methods=['POST'])
    @auth.login_required
    def check_temp_activities():
        if not auth.has_permission('index'):
            return jsonify({'status': 'error', 'message': 'شما به این بخش دسترسی ندارید.'}), 403
        
        employee_ids = request.json.get('employee_ids', [])
        conn = utils.get_db_connection()
        
        missing_employees = []
        
        for emp_id in employee_ids:
            activity = conn.execute('SELECT * FROM temp_activities WHERE employee_id = ?', (emp_id,)).fetchone()
            if not activity:
                employee_name = conn.execute('SELECT name FROM employees WHERE employee_id = ?', (emp_id,)).fetchone()
                if employee_name:
                    missing_employees.append({'id': emp_id, 'name': employee_name['name']})
        
        conn.close()

        if missing_employees:
            return jsonify({'status': 'missing_data', 'missing_employees': missing_employees}), 200
        else:
            return jsonify({'status': 'ok'}), 200


    @app.route('/management')
    @auth.login_required
    def management():
        if not auth.has_permission('management'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('admin_dashboard'))

        conn = utils.get_db_connection()
        try:
            employees = conn.execute('SELECT * FROM employees ORDER BY CAST(employee_id AS INTEGER) ASC').fetchall()
            next_employee_id = get_next_employee_id(conn)
        finally:
            conn.close()

        utils.log_action(session['user']['username'], 'مشاهده صفحه مدیریت کارکنان')
        return render_template('management.html', employees=employees, next_employee_id=next_employee_id)

    def get_next_employee_id(conn):
        last_id_row = conn.execute('SELECT employee_id FROM employees ORDER BY CAST(employee_id AS INTEGER) DESC LIMIT 1').fetchone()
        if last_id_row:
            try:
                last_id = int(last_id_row['employee_id'])
                return str(last_id + 1)
            except (ValueError, TypeError):
                return '1'
        return '1'

    @app.route('/reports')
    @auth.login_required
    def reports():
        if not auth.has_permission('reports'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('admin_dashboard'))

        conn = utils.get_db_connection()
        
        employee_id_filter = request.args.get('employee_id')
        start_date_fa = request.args.get('start_date')
        end_date_fa = request.args.get('end_date')
        condition_filter = request.args.get('condition_filter')


        query = '''
            SELECT tl.id, tl.employee_id, e.name, tl.action, tl.timestamp, tl.condition, tl.activity_location, tl.activity_description, tl.activity_amount
            FROM time_logs tl
            JOIN employees e ON tl.employee_id = e.employee_id
        '''
        params = []
        conditions = []
        
        if employee_id_filter and employee_id_filter != 'all':
            conditions.append('tl.employee_id = ?')
            params.append(employee_id_filter)
        
        if start_date_fa:
            try:
                g_date = utils.shamsi_to_miladi(start_date_fa)
                conditions.append('tl.timestamp >= ?')
                params.append(g_date.strftime('%Y-%m-%d 00:00:00'))
            except (ValueError, IndexError):
                flash("فرمت تاریخ شروع نامعتبر است.", "error")
                pass
                
        if end_date_fa:
            try:
                g_date = utils.shamsi_to_miladi(end_date_fa)
                conditions.append('tl.timestamp <= ?')
                params.append(g_date.strftime('%Y-%m-%d 23:59:59'))
            except (ValueError, IndexError):
                flash("فرمت تاریخ پایان نامعتبر است.", "error")
                pass
                
        if condition_filter and condition_filter != 'all':
            conditions.append('tl.condition = ?')
            params.append(condition_filter)

        if conditions:
            query += ' WHERE ' + ' AND '.join(conditions)

        query += ' ORDER BY tl.timestamp DESC'

        try:
            records = conn.execute(query, params).fetchall()
            employees = conn.execute('SELECT * FROM employees ORDER BY name').fetchall()
        finally:
            conn.close()

        fa_records = []
        for record in records:
            miladi_dt = datetime.strptime(record['timestamp'], '%Y-%m-%d %H:%M:%S')
            shamsi_dt = jdatetime.datetime.fromgregorian(datetime=miladi_dt)
            fa_records.append({
                'id': record['id'],
                'employee_id': record['employee_id'],
                'name': record['name'],
                'action': record['action'],
                'timestamp_fa': shamsi_dt.strftime('%Y/%m/%d %H:%M:%S'),
                'timestamp_miladi': miladi_dt.strftime('%Y-%m-%d %H:%M:%S'),
                'condition': record['condition'],
                'activity_location': record['activity_location'],
                'activity_description': record['activity_description'],
                'activity_amount': record['activity_amount']
            })

        utils.log_action(session['user']['username'], 'مشاهده صفحه گزارشات')
        return render_template('reports.html', 
                               records=fa_records, 
                               employees=employees,
                               selected_employee_id=employee_id_filter,
                               start_date=start_date_fa,
                               end_date=end_date_fa,
                               selected_condition=condition_filter)

    @app.route('/bulk_delete_logs', methods=['POST'])
    @auth.login_required
    def bulk_delete_logs():
        if not auth.has_permission('reports'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('reports'))
        
        record_ids = request.form.getlist('record_ids')
        
        if not record_ids:
            flash("هیچ رکوردی برای حذف انتخاب نشده است.", "warning")
            return redirect(url_for('reports'))
        
        conn = utils.get_db_connection()
        try:
            placeholders = ', '.join(['?'] * len(record_ids))
            query = f"DELETE FROM time_logs WHERE id IN ({placeholders})"
            conn.execute(query, record_ids)
            conn.commit()
            flash(f"{len(record_ids)} رکورد با موفقیت حذف شدند.", "success")
            utils.log_action(session['user']['username'], 'حذف گروهی رکوردهای تردد', f'رکوردهای با شناسه: {", ".join(record_ids)} حذف شدند.')
        except Exception as e:
            flash("خطایی در حذف رکوردهای انتخاب شده رخ داد.", "error")
            print(f"Error bulk deleting logs: {e}")
        finally:
            conn.close()
            
        return redirect(url_for('reports'))

    @app.route('/bulk_submit', methods=['POST'])
    @auth.login_required
    def bulk_submit():
        if not auth.has_permission('index'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('index'))

        employee_ids = request.form.getlist('employee_ids')
        action = request.form.get('action')
        timestamps_list = request.form.getlist('timestamps')
        
        default_condition = request.form.get('condition', 'عادی')
        
        if not employee_ids or not action or not timestamps_list:
            flash("هیچ کارمندی برای ثبت انتخاب نشد.", "warning")
            return redirect(url_for('index'))

        conn = utils.get_db_connection()
        try:
            for i, employee_id in enumerate(employee_ids):
                shamsi_datetime_str = timestamps_list[i]
                persian_digits = '۰۱۲۳۴۵۶۷۸۹'
                english_digits = '0123456789'
                translation_table = str.maketrans(persian_digits, english_digits)
                shamsi_datetime_str_en = shamsi_datetime_str.translate(translation_table)

                shamsi_dt = jdatetime.datetime.strptime(shamsi_datetime_str_en, '%Y/%m/%d %H:%M:%S')
                miladi_dt = shamsi_dt.togregorian()
                timestamp = miladi_dt.strftime('%Y-%m-%d %H:%M:%S')

                current_condition = default_condition
                activity_location = None
                activity_description = None
                activity_amount = None
                
                if action == 'خروج':
                    # Fetch activity details from temp table
                    temp_activity = conn.execute('SELECT * FROM temp_activities WHERE employee_id = ?', (employee_id,)).fetchone()
                    
                    if temp_activity:
                        activity_location = temp_activity['activity_location']
                        activity_description = temp_activity['activity_description']
                        activity_amount = temp_activity['activity_amount']

                    latest_entry = conn.execute('SELECT condition FROM time_logs WHERE employee_id = ? AND action = ? ORDER BY timestamp DESC LIMIT 1', (employee_id, 'ورود')).fetchone()
                    if latest_entry and latest_entry['condition'] in ['مرخصی', 'استراحت']:
                        current_condition = latest_entry['condition']
                
                conn.execute('INSERT INTO time_logs (employee_id, action, timestamp, condition, activity_location, activity_description, activity_amount) VALUES (?, ?, ?, ?, ?, ?, ?)',
                             (employee_id, action, timestamp, current_condition, activity_location, activity_description, activity_amount))
                
                if action == 'خروج' and temp_activity:
                    conn.execute('DELETE FROM temp_activities WHERE employee_id = ?', (employee_id,))

            conn.commit()
            flash(f"ثبت {action} برای {len(employee_ids)} کارمند با موفقیت انجام شد.", "success")
            utils.log_action(session['user']['username'], f'ثبت {action}', f'برای کارمندان: {", ".join(employee_ids)}')
        except Exception as e:
            conn.rollback()
            flash("خطایی در ثبت اطلاعات رخ داد.", "error")
            print(f"Error submitting bulk data: {e}")
        finally:
            conn.close()

        return redirect(url_for('index'))

    @app.route('/save_activity', methods=['POST'])
    @auth.login_required
    def save_activity():
        if not auth.has_permission('index'):
            return jsonify({'status': 'error', 'message': 'شما به این بخش دسترسی ندارید.'}), 403
        
        employee_id = request.form['employee_ids']
        activity_location = request.form.get('activity_location')
        activity_description = request.form.get('activity_description')
        activity_amount = request.form.get('activity_amount')
        
        if not any([activity_location, activity_description, activity_amount]):
            return jsonify({'status': 'warning', 'message': 'لطفاً حداقل یکی از فیلدهای فعالیت را پر کنید.'}), 200

        conn = utils.get_db_connection()
        try:
            conn.execute('''
                INSERT OR REPLACE INTO temp_activities (employee_id, activity_location, activity_description, activity_amount)
                VALUES (?, ?, ?, ?)
            ''', (employee_id, activity_location, activity_description, activity_amount))
            conn.commit()
            utils.log_action(session['user']['username'], 'ثبت موقت فعالیت', f'فعالیت برای کارمند {employee_id} ثبت شد.')
            return jsonify({'status': 'success', 'message': 'جزئیات فعالیت با موفقیت ذخیره شد.'}), 200
        except Exception as e:
            conn.rollback()
            print(f"Error saving activity: {e}")
            return jsonify({'status': 'error', 'message': 'خطا در ذخیره جزئیات فعالیت.'}), 500
        finally:
            conn.close()

    @app.route('/get_temp_activity/<employee_id>')
    @auth.login_required
    def get_temp_activity(employee_id):
        if not auth.has_permission('index'):
            return jsonify({'status': 'error', 'message': 'Access Denied'}), 403
        
        conn = utils.get_db_connection()
        try:
            activity = conn.execute('SELECT * FROM temp_activities WHERE employee_id = ?', (employee_id,)).fetchone()
            if activity:
                return jsonify({'status': 'success', 'activity': dict(activity)}), 200
            else:
                return jsonify({'status': 'success', 'activity': None}), 200
        except Exception as e:
            print(f"Error fetching temp activity: {e}")
            return jsonify({'status': 'error', 'message': 'خطا در دریافت اطلاعات فعالیت.'}), 500
        finally:
            conn.close()

    @app.route('/edit_log/<int:log_id>', methods=['POST'])
    @auth.login_required
    def edit_log(log_id):
        if not auth.has_permission('reports'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('reports'))

        employee_id = request.form['employeeId']
        action = request.form['action']
        date_fa = request.form['date']
        time_fa = request.form['time']
        condition = request.form.get('condition', 'عادی')
        activity_location = request.form.get('activity_location', '---')
        activity_description = request.form.get('activity_description', '---')
        activity_amount = request.form.get('activity_amount', '---')
        
        if activity_location == '---':
            activity_location = None
        if activity_description == '---':
            activity_description = None
        if activity_amount == '---':
            activity_amount = None

        try:
            shamsi_dt_str = f'{date_fa} {time_fa}'
            shamsi_dt = jdatetime.datetime.strptime(shamsi_dt_str, '%Y/%m/%d %H:%M:%S')
            miladi_dt = shamsi_dt.togregorian()
            timestamp = miladi_dt.strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            flash("فرمت تاریخ یا ساعت نامعتبر است.", "error")
            return redirect(url_for('reports'))

        conn = utils.get_db_connection()
        try:
            conn.execute('''
                UPDATE time_logs
                SET employee_id = ?, action = ?, timestamp = ?, condition = ?, activity_location = ?, activity_description = ?, activity_amount = ?
                WHERE id = ?
            ''', (employee_id, action, timestamp, condition, activity_location, activity_description, activity_amount, log_id))
            conn.commit()
            flash("رکورد با موفقیت ویرایش شد.", "success")
            utils.log_action(session['user']['username'], 'ویرایش رکورد تردد', f'رکورد با شناسه {log_id} ویرایش شد.')
        except Exception as e:
            flash("خطایی در ویرایش رکورد رخ داد.", "error")
            print(f"Error editing log: {e}")
        finally:
            conn.close()
        return redirect(url_for('reports'))

    @app.route('/delete_log/<int:log_id>', methods=['POST'])
    @auth.login_required
    def delete_log(log_id):
        if not auth.has_permission('reports'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('reports'))

        conn = utils.get_db_connection()
        try:
            conn.execute('DELETE FROM time_logs WHERE id = ?', (log_id,))
            conn.commit()
            flash("رکورد با موفقیت حذف شد.", "success")
            utils.log_action(session['user']['username'], 'حذف رکورد تردد', f'رکورد با شناسه {log_id} حذف شد.')
        except Exception as e:
            flash("خطایی در حذف رکورد رخ داد.", "error")
            print(f"Error deleting log: {e}")
        finally:
            conn.close()
        return redirect(url_for('reports'))

    @app.route('/clear_data', methods=['POST'])
    @auth.login_required
    def clear_data():
        if not auth.has_permission('reports'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('reports'))

        conn = utils.get_db_connection()
        try:
            conn.execute('DELETE FROM time_logs')
            conn.commit()
            flash("تمام داده‌ها با موفقیت پاک شدند.", "success")
            utils.log_action(session['user']['username'], 'پاک کردن تمام داده‌های تردد')
        except Exception as e:
            flash("خطایی در پاک کردن داده‌ها رخ داد.", "error")
            print(f"Error clearing data: {e}")
        finally:
            conn.close()
        return redirect(url_for('reports'))

    @app.route('/add_employee', methods=['POST'])
    @auth.login_required
    def add_employee():
        if not auth.has_permission('management'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('admin_dashboard'))

        employee_id = request.form['employeeId']
        name = request.form['name']
        
        conn = utils.get_db_connection()
        try:
            conn.execute('INSERT INTO employees (employee_id, name) VALUES (?, ?)',
                         (employee_id, name))
            conn.commit()
            utils.log_action(session['user']['username'], 'افزودن کارمند', f'کارمند جدید: {name} ({employee_id})')
            flash(f"کارمند '{name}' با موفقیت اضافه شد.", "success")
        except sqlite3.IntegrityError:
            flash(f"شماره پرسنلی '{employee_id}' قبلاً ثبت شده است.", "error")
        finally:
            conn.close()
            
        return redirect(url_for('management'))

    @app.route('/delete_employee', methods=['POST'])
    @auth.login_required
    def delete_employee():
        if not auth.has_permission('management'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('admin_dashboard'))

        employee_id = request.form['employeeId']
        
        conn = utils.get_db_connection()
        try:
            conn.execute('DELETE FROM employees WHERE employee_id = ?', (employee_id,))
            conn.commit()
            flash("کارمند با موفقیت حذف شد.", "success")
            utils.log_action(session['user']['username'], 'حذف کارمند', f'کارمند با شناسه: {employee_id} حذف شد.')
        except Exception as e:
            flash("خطایی در حذف کارمند رخ داد.", "error")
            print(f"Error deleting employee: {e}")
        finally:
            conn.close()
        return redirect(url_for('management'))
        
    @app.route('/lookup_employee', methods=['POST'])
    @auth.login_required
    def lookup_employee():
        if not auth.has_permission('index'):
            return "Access Denied", 403

        conn = utils.get_db_connection()
        query = request.json.get('query')
        query_type = request.json.get('type')
        employee = None
        try:
            if query_type == 'id':
                employee = conn.execute('SELECT * FROM employees WHERE employee_id = ?', (query,)).fetchone()
            elif query_type == 'name':
                employee = conn.execute('SELECT * FROM employees WHERE name LIKE ?', ('%' + query + '%',)).fetchone()
        finally:
            conn.close()
        
        if employee:
            return jsonify({
                'employeeId': employee['employee_id'],
                'name': employee['name']
            })
        else:
            return jsonify({})

    @app.route('/export/all')
    @auth.login_required
    def export_all():
        if not auth.has_permission('reports'):
            return "Access Denied", 403
            
        conn = utils.get_db_connection()
        try:
            records = conn.execute('''
                SELECT tl.id, tl.employee_id, e.name, tl.action, tl.timestamp, tl.condition, tl.activity_location, tl.activity_description, tl.activity_amount
                FROM time_logs tl
                JOIN employees e ON tl.employee_id = e.employee_id
                ORDER BY tl.timestamp DESC
            ''').fetchall()
        finally:
            conn.close()

        si = io.StringIO()
        writer = csv.writer(si, dialect='excel')
        headers = ['شماره پرسنلی', 'نام کارمند', 'عملیات', 'تاریخ', 'ساعت', 'شرایط', 'محل فعالیت', 'شرح فعالیت', 'مقدار فعالیت']
        writer.writerow(headers)

        for record in records:
            miladi_dt = datetime.strptime(record['timestamp'], '%Y-%m-%d %H:%M:%S')
            shamsi_dt = jdatetime.datetime.fromgregorian(datetime=miladi_dt)
            date_part = shamsi_dt.strftime('%Y/%m/%d')
            time_part = shamsi_dt.strftime('%H:%M:%S')
            writer.writerow([record['employee_id'], record['name'], record['action'], date_part, time_part, record['condition'], record['activity_location'], record['activity_description'], record['activity_amount']])

        output = si.getvalue().encode('utf-8-sig') 
        response = Response(output, mimetype='text/csv')
        response.headers['Content-Disposition'] = 'attachment; filename=all_time_logs.csv'
        
        utils.log_action(session['user']['username'], 'خروجی گرفتن از تمام گزارشات تردد')
        return response

    @app.route('/export/employee/<employee_id>')
    @auth.login_required
    def export_employee(employee_id):
        if not auth.has_permission('reports'):
            return "Access Denied", 403
            
        conn = utils.get_db_connection()
        try:
            records = conn.execute('''
                SELECT tl.id, tl.employee_id, e.name, tl.action, tl.timestamp, tl.condition, tl.activity_location, tl.activity_description, tl.activity_amount
                FROM time_logs tl
                JOIN employees e ON tl.employee_id = e.employee_id
                WHERE tl.employee_id = ? 
                ORDER BY tl.timestamp DESC
            ''', (employee_id,)).fetchall()
        finally:
            conn.close()

        if not records:
            return "No data found for this employee.", 404

        si = io.StringIO()
        writer = csv.writer(si, dialect='excel')
        headers = ['شماره پرسنلی', 'نام کارمند', 'عملیات', 'تاریخ', 'ساعت', 'شرایط', 'محل فعالیت', 'شرح فعالیت', 'مقدار فعالیت']
        writer.writerow(headers)

        for record in records:
            miladi_dt = datetime.strptime(record['timestamp'], '%Y-%m-%d %H:%M:%S')
            shamsi_dt = jdatetime.datetime.fromgregorian(datetime=miladi_dt)
            date_part = shamsi_dt.strftime('%Y/%m/%d')
            time_part = shamsi_dt.strftime('%H:%M:%S')
            writer.writerow([record['employee_id'], record['name'], record['action'], date_part, time_part, record['condition'], record['activity_location'], record['activity_description'], record['activity_amount']])

        si.seek(0)
        output = si.getvalue().encode('utf-8-sig')
        
        filename = 'time_log.csv'
        if records and records[0]['name']:
            filename_persian = f"time_log_{records[0]['name'].replace(' ', '_')}.csv"
            filename = urllib.parse.quote(filename_persian)

        response = Response(output, mimetype='text/csv')
        response.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{filename}"
        
        utils.log_action(session['user']['username'], 'خروجی گرفتن از گزارش تردد', f'برای کارمند: {employee_id}')
        return response

    @app.route('/export/zip')
    @auth.login_required
    def export_zip():
        if not auth.has_permission('reports'):
            return "Access Denied", 403
            
        conn = utils.get_db_connection()
        employees = conn.execute('SELECT employee_id, name FROM employees').fetchall()
        conn.close()

        if not employees:
            return "No employees found.", 404

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for employee in employees:
                employee_id = employee['employee_id']
                employee_name = employee['name']
                
                conn = utils.get_db_connection()
                try:
                    records = conn.execute('''
                        SELECT tl.id, tl.employee_id, e.name, tl.action, tl.timestamp, tl.condition, tl.activity_location, tl.activity_description, tl.activity_amount
                        FROM time_logs tl
                        JOIN employees e ON tl.employee_id = e.employee_id
                        WHERE tl.employee_id = ?
                        ORDER BY tl.timestamp DESC
                    ''', (employee_id,)).fetchall()
                finally:
                    conn.close()

                if records:
                    si = io.StringIO()
                    writer = csv.writer(si, dialect='excel')
                    headers = ['شماره پرسنلی', 'نام کارمند', 'عملیات', 'تاریخ', 'ساعت', 'شرایط', 'محل فعالیت', 'شرح فعالیت', 'مقدار فعالیت']
                    writer.writerow(headers)

                    for record in records:
                        miladi_dt = datetime.strptime(record['timestamp'], '%Y-%m-%d %H:%M:%S')
                        shamsi_dt = jdatetime.datetime.fromgregorian(datetime=miladi_dt)
                        date_part = shamsi_dt.strftime('%Y/%m/%d')
                        time_part = shamsi_dt.strftime('%H:%M:%S')
                        writer.writerow([record['employee_id'], record['name'], record['action'], date_part, time_part, record['condition'], record['activity_location'], record['activity_description'], record['activity_amount']])

                    csv_filename = f"گزارش_{employee_name}.csv"
                    zip_file.writestr(csv_filename, si.getvalue().encode('utf-8-sig'))

        zip_buffer.seek(0)
        response = Response(zip_buffer.getvalue(), mimetype='application/zip')
        response.headers['Content-Disposition'] = 'attachment; filename=all_employees_logs.zip'
        
        utils.log_action(session['user']['username'], 'خروجی گرفتن ZIP از تمام گزارشات')
        return response

    @app.route('/export/employees')
    @auth.login_required
    def export_employees():
        if not auth.has_permission('management'):
            return "Access Denied", 403
            
        conn = utils.get_db_connection()
        try:
            employees = conn.execute('SELECT employee_id, name FROM employees ORDER BY CAST(employee_id AS INTEGER) ASC').fetchall()
        finally:
            conn.close()

        si = io.StringIO()
        writer = csv.writer(si, dialect='excel')
        headers = ['شماره پرسنلی', 'نام کارمند']
        writer.writerow(headers)
        for employee in employees:
            writer.writerow([employee['employee_id'], employee['name']])

        output = si.getvalue().encode('utf-8-sig')
        response = Response(output, mimetype='text/csv')
        response.headers['Content-Disposition'] = 'attachment; filename=employees.csv'
        
        utils.log_action(session['user']['username'], 'خروجی گرفتن از لیست کارکنان')
        return response

    @app.route('/import/employees', methods=['POST'])
    @auth.login_required
    def import_employees():
        if not auth.has_permission('management'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('management'))

        file = request.files['file']
        if not file or not file.filename.endswith('.csv'):
            flash("فرمت فایل نامعتبر است. لطفاً یک فایل CSV آپلود کنید.", "error")
            return redirect(url_for('management'))

        conn = utils.get_db_connection()
        conn.execute('BEGIN TRANSACTION')
        try:
            stream = io.StringIO(file.stream.read().decode("utf-8-sig"), newline=None)
            reader = csv.reader(stream, delimiter=',')
            header = next(reader, None)
            
            count = 0
            for row in reader:
                if len(row) >= 2:
                    employee_id = row[0].strip()
                    name = row[1].strip()
                    if employee_id and name:
                        conn.execute('INSERT OR IGNORE INTO employees (employee_id, name) VALUES (?, ?)', (employee_id, name))
                        count += 1
            conn.commit()
            flash(f"{count} کارمند جدید با موفقیت اضافه شد.", "success")
            utils.log_action(session['user']['username'], 'ورودی از فایل CSV', f'{count} کارمند جدید اضافه شد.')
        except Exception as e:
            conn.rollback()
            flash(f"هنگام وارد کردن اطلاعات خطایی رخ داد: {e}", "error")
        finally:
            conn.close()

        return redirect(url_for('management'))

    @app.route('/petty_cash')
    @auth.login_required
    def petty_cash():
        if not auth.has_permission('petty_cash'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('admin_dashboard'))
        
        conn = utils.get_db_connection()
        try:
            records = conn.execute('''
                SELECT * FROM petty_cash ORDER BY timestamp DESC
            ''').fetchall()
            users = conn.execute("SELECT username, name, role FROM users ORDER BY name").fetchall()
        finally:
            conn.close()
        
        return render_template('petty_cash.html', records=records, users=users)

    @app.route('/add_petty_cash', methods=['POST'])
    @auth.login_required
    def add_petty_cash():
        if not auth.has_permission('petty_cash'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('petty_cash'))

        try:
            date_fa = request.form['date_fa']
            description = request.form['description']
            unit = request.form['unit']
            amount = request.form['amount']
            unit_price = request.form['unit_price']
            discount = request.form['discount']
            total_amount = request.form['total_amount']
            location = request.form['location']
            notes = request.form['notes']
            source = request.form['source']
            invoice_number = request.form['invoice_number']
            settlement_status = request.form['settlement_status']
            payer = request.form['payer']
            
            receipt_image = request.files.get('receipt_image')
            image_path = None
            if receipt_image and receipt_image.filename != '':
                if not os.path.exists(utils.UPLOAD_FOLDER):
                    os.makedirs(utils.UPLOAD_FOLDER)
                
                filename = str(uuid.uuid4()) + os.path.splitext(receipt_image.filename)[1]
                filepath = os.path.join(utils.UPLOAD_FOLDER, filename)
                
                with open(filepath, 'wb') as f:
                    f.write(receipt_image.read())
                
                image_path = '/static/uploads/' + filename
            
            shamsi_dt = jdatetime.datetime.strptime(f'{date_fa}', '%Y/%m/%d')
            miladi_dt = shamsi_dt.togregorian()
            timestamp = miladi_dt.strftime('%Y-%m-%d %H:%M:%S')

            conn = utils.get_db_connection()
            conn.execute('''
                INSERT INTO petty_cash (date, description, unit, amount, unit_price, discount, total_amount, location, notes, source, invoice_number, settlement_status, payer, receipt_image_path, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (date_fa, description, unit, amount, unit_price, discount, total_amount, location, notes, source, invoice_number, settlement_status, payer, image_path, timestamp))
            conn.commit()
            
            flash("ثبت تنخواه با موفقیت انجام شد.", "success")
            utils.log_action(session['user']['username'], 'ثبت تنخواه جدید', f'شرح: {description}, مبلغ کل: {total_amount}')
        except Exception as e:
            flash(f"خطا در ثبت تنخواه: {e}", "error")
            print(f"Error adding petty cash: {e}")
        finally:
            conn.close()
        
        return redirect(url_for('petty_cash'))

    @app.route('/autocomplete/petty_cash_field/<field_name>')
    @auth.login_required
    def autocomplete_petty_cash_field(field_name):
        conn = utils.get_db_connection()
        try:
            if field_name in ['description', 'unit', 'location', 'source']:
                distinct_values = conn.execute(f"SELECT DISTINCT {field_name} FROM petty_cash WHERE {field_name} IS NOT NULL AND {field_name} != ''").fetchall()
                return jsonify([row[0] for row in distinct_values])
        finally:
            conn.close()
        return jsonify([])

    @app.route('/autocomplete/daily_worker_field/<field_name>')
    @auth.login_required
    def autocomplete_daily_worker_field(field_name):
        conn = utils.get_db_connection()
        try:
            if field_name in ['foreman_name', 'location']:
                distinct_values = conn.execute(f"SELECT DISTINCT {field_name} FROM daily_workers WHERE {field_name} IS NOT NULL AND {field_name} != ''").fetchall()
                return jsonify([row[0] for row in distinct_values])
        finally:
            conn.close()
        return jsonify([])

    @app.route('/get_users_for_dropdown')
    @auth.login_required
    def get_users_for_dropdown():
        conn = utils.get_db_connection()
        try:
            users = conn.execute("SELECT name FROM users ORDER BY name").fetchall()
            return jsonify([user['name'] for user in users])
        finally:
            conn.close()

    @app.route('/petty_cash_reports')
    @auth.login_required
    def petty_cash_reports():
        if not auth.has_permission('petty_cash_reports'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('admin_dashboard'))

        conn = utils.get_db_connection()
        
        start_date_fa = request.args.get('start_date')
        end_date_fa = request.args.get('end_date')
        payer_filter = request.args.get('payer_filter')
        settlement_status_filter = request.args.get('settlement_status_filter')

        query = '''
            SELECT * FROM petty_cash
        '''
        params = []
        conditions = []
        
        if start_date_fa:
            try:
                g_date = utils.shamsi_to_miladi(start_date_fa)
                conditions.append('timestamp >= ?')
                params.append(g_date.strftime('%Y-%m-%d 00:00:00'))
            except (ValueError, IndexError):
                pass
                
        if end_date_fa:
            try:
                g_date = utils.shamsi_to_miladi(end_date_fa)
                conditions.append('timestamp <= ?')
                params.append(g_date.strftime('%Y-%m-%d 23:59:59'))
            except (ValueError, IndexError):
                pass
                
        if payer_filter and payer_filter != 'all':
            conditions.append('payer = ?')
            params.append(payer_filter)

        if settlement_status_filter and settlement_status_filter != 'all':
            conditions.append('settlement_status = ?')
            params.append(settlement_status_filter)

        if conditions:
            query += ' WHERE ' + ' AND '.join(conditions)

        query += ' ORDER BY timestamp DESC'
        
        try:
            records = conn.execute(query, params).fetchall()
            payers = conn.execute("SELECT DISTINCT payer FROM petty_cash").fetchall()
            settlement_statuses = conn.execute("SELECT DISTINCT settlement_status FROM petty_cash").fetchall()
        finally:
            conn.close()

        utils.log_action(session['user']['username'], 'مشاهده گزارش تنخواه')
        return render_template('petty_cash_reports.html', 
                               records=records,
                               payers=[p['payer'] for p in payers],
                               settlement_statuses=[s['settlement_status'] for s in settlement_statuses],
                               start_date=start_date_fa,
                               end_date=end_date_fa,
                               selected_payer=payer_filter,
                               selected_status=settlement_status_filter)

    @app.route('/export/petty_cash_reports')
    @auth.login_required
    def export_petty_cash_reports():
        if not auth.has_permission('petty_cash_reports'):
            return "Access Denied", 403
        
        start_date_fa = request.args.get('start_date')
        end_date_fa = request.args.get('end_date')
        payer_filter = request.args.get('payer_filter')
        settlement_status_filter = request.args.get('settlement_status_filter')
        
        query = '''
            SELECT * FROM petty_cash
        '''
        params = []
        conditions = []
        
        if start_date_fa:
            try:
                g_date = utils.shamsi_to_miladi(start_date_fa)
                conditions.append('timestamp >= ?')
                params.append(g_date.strftime('%Y-%m-%d 00:00:00'))
            except (ValueError, IndexError):
                pass
        
        if end_date_fa:
            try:
                g_date = utils.shamsi_to_miladi(end_date_fa)
                conditions.append('timestamp <= ?')
                params.append(g_date.strftime('%Y-%m-%d 23:59:59'))
            except (ValueError, IndexError):
                pass
                
        if payer_filter and payer_filter != 'all':
            conditions.append('payer = ?')
            params.append(payer_filter)
            
        if settlement_status_filter and settlement_status_filter != 'all':
            conditions.append('settlement_status = ?')
            params.append(settlement_status_filter)
            
        if conditions:
            query += ' WHERE ' + ' AND '.join(conditions)
        
        query += ' ORDER BY timestamp DESC'
        
        conn = utils.get_db_connection()
        try:
            records = conn.execute(query, params).fetchall()
        finally:
            conn.close()
            
        si = io.StringIO()
        writer = csv.writer(si, dialect='excel')
        headers = ['شناسه', 'تاریخ', 'شرح', 'واحد', 'مقدار', 'فی', 'تخفیف', 'مبلغ کل', 'محل استفاده', 'توضیحات', 'محل تامین', 'شماره فاکتور', 'وضعیت تسویه', 'پرداخت کننده']
        writer.writerow(headers)
        
        for record in records:
            writer.writerow([
                record['id'],
                record['date'],
                record['description'],
                record['unit'],
                record['amount'],
                record['unit_price'],
                record['discount'],
                record['total_amount'],
                record['location'],
                record['notes'],
                record['source'],
                record['invoice_number'],
                record['settlement_status'],
                record['payer']
            ])
            
        output = si.getvalue().encode('utf-8-sig') 
        response = Response(output, mimetype='text/csv')
        response.headers['Content-Disposition'] = 'attachment; filename=petty_cash_reports.csv'
        
        utils.log_action(session['user']['username'], 'خروجی گرفتن از گزارش تنخواه')
        return response

    @app.route('/edit_petty_cash/<int:record_id>', methods=['POST'])
    @auth.login_required
    def edit_petty_cash(record_id):
        if not auth.has_permission('petty_cash_reports'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('petty_cash_reports'))

        date_fa = request.form['date_fa']
        description = request.form['description']
        unit = request.form['unit']
        amount = request.form['amount'].replace(',', '')  # Remove commas
        unit_price = request.form['unit_price'].replace(',', '') # Remove commas
        discount = request.form['discount'].replace(',', '') # Remove commas
        total_amount = request.form['total_amount'].replace(',', '') # Remove commas
        location = request.form['location']
        notes = request.form['notes']
        source = request.form['source']
        invoice_number = request.form['invoice_number']
        settlement_status = request.form['settlement_status']
        payer = request.form['payer']
        
        try:
            shamsi_dt = jdatetime.datetime.strptime(f'{date_fa}', '%Y/%m/%d')
            miladi_dt = shamsi_dt.togregorian()
            timestamp = miladi_dt.strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            flash("فرمت تاریخ نامعتبر است.", "error")
            return redirect(url_for('petty_cash_reports'))

        conn = utils.get_db_connection()
        try:
            conn.execute('''
                UPDATE petty_cash
                SET date = ?, description = ?, unit = ?, amount = ?, unit_price = ?, discount = ?, total_amount = ?, location = ?, notes = ?, source = ?, invoice_number = ?, settlement_status = ?, payer = ?, timestamp = ?
                WHERE id = ?
            ''', (date_fa, description, unit, amount, unit_price, discount, total_amount, location, notes, source, invoice_number, settlement_status, payer, timestamp, record_id))
            conn.commit()
            flash("رکورد تنخواه با موفقیت ویرایش شد.", "success")
            utils.log_action(session['user']['username'], 'ویرایش رکورد تنخواه', f'رکورد با شناسه {record_id} ویرایش شد.')
        except Exception as e:
            flash(f"خطایی در ویرایش رکورد تنخواه رخ داد: {e}", "error")
            print(f"Error editing petty cash: {e}")
        finally:
            conn.close()
        return redirect(url_for('petty_cash_reports'))

    @app.route('/delete_petty_cash/<int:record_id>', methods=['POST'])
    @auth.login_required
    def delete_petty_cash(record_id):
        if not auth.has_permission('petty_cash_reports'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('petty_cash_reports'))

        conn = utils.get_db_connection()
        try:
            conn.execute('DELETE FROM petty_cash WHERE id = ?', (record_id,))
            conn.commit()
            flash("رکورد تنخواه با موفقیت حذف شد.", "success")
            utils.log_action(session['user']['username'], 'حذف رکورد تنخواه', f'رکورد با شناسه {record_id} حذف شد.')
        except Exception as e:
            flash(f"خطایی در حذف رکورد تنخواه رخ داد: {e}", "error")
            print(f"Error deleting petty cash: {e}")
        finally:
            conn.close()
        return redirect(url_for('petty_cash_reports'))

    @app.route('/daily_worker')
    @auth.login_required
    def daily_worker():
        if not auth.has_permission('daily_worker'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('admin_dashboard'))
        return render_template('daily_worker.html')

    @app.route('/add_daily_worker', methods=['POST'])
    @auth.login_required
    def add_daily_worker():
        if not auth.has_permission('daily_worker'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('daily_worker'))
        
        try:
            date_fa = request.form['date_fa']
            foreman_name = request.form['foreman_name']
            worker_count = int(request.form['worker_count'])
            daily_wage = request.form['daily_wage'].replace(',', '')
            transport_cost = int(request.form['transport_cost'].replace(',', ''))
            total_amount = int(request.form['total_amount'].replace(',', ''))
            location = request.form['location']
            
            # Check for negative daily wage
            try:
                daily_wage_int = int(daily_wage)
                if daily_wage_int < 0:
                    daily_wage = f'واریزی با فیش شماره {daily_wage_int * -1}'
                else:
                    daily_wage = daily_wage_int
            except ValueError:
                # If daily_wage is already a string (e.g., "واریزی با فیش شماره...")
                pass
            
            shamsi_dt = jdatetime.datetime.strptime(f'{date_fa}', '%Y/%m/%d')
            miladi_dt = shamsi_dt.togregorian()
            timestamp = miladi_dt.strftime('%Y-%m-%d %H:%M:%S')

            conn = utils.get_db_connection()
            conn.execute('''
                INSERT INTO daily_workers (date, foreman_name, worker_count, daily_wage, transport_cost, total_amount, location, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (date_fa, foreman_name, worker_count, daily_wage, transport_cost, total_amount, location, timestamp))
            conn.commit()
            
            flash("ثبت کارگر روزمزد با موفقیت انجام شد.", "success")
            utils.log_action(session['user']['username'], 'ثبت کارگر روزمزد', f'سرکارگر: {foreman_name}, مبلغ کل: {total_amount}')
        except Exception as e:
            flash(f"خطا در ثبت کارگر روزمزد: {e}", "error")
            print(f"Error adding daily worker: {e}")
        finally:
            conn.close()
        
        return redirect(url_for('daily_worker'))

    @app.route('/daily_worker_reports')
    @auth.login_required
    def daily_worker_reports():
        if not auth.has_permission('daily_worker_reports'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('admin_dashboard'))

        conn = utils.get_db_connection()
        
        start_date_fa = request.args.get('start_date')
        end_date_fa = request.args.get('end_date')
        foreman_filter = request.args.get('foreman_filter')

        query = '''
            SELECT * FROM daily_workers
        '''
        params = []
        conditions = []
        
        if start_date_fa:
            try:
                g_date = utils.shamsi_to_miladi(start_date_fa)
                conditions.append('timestamp >= ?')
                params.append(g_date.strftime('%Y-%m-%d 00:00:00'))
            except (ValueError, IndexError):
                pass
                
        if end_date_fa:
            try:
                g_date = utils.shamsi_to_miladi(end_date_fa)
                conditions.append('timestamp <= ?')
                params.append(g_date.strftime('%Y-%m-%d 23:59:59'))
            except (ValueError, IndexError):
                pass
                
        if foreman_filter and foreman_filter != 'all':
            conditions.append('foreman_name = ?')
            params.append(foreman_filter)

        if conditions:
            query += ' WHERE ' + ' AND '.join(conditions)

        query += ' ORDER BY timestamp DESC'
        
        try:
            records = conn.execute(query, params).fetchall()
            foremen = conn.execute("SELECT DISTINCT foreman_name FROM daily_workers").fetchall()
        finally:
            conn.close()

        utils.log_action(session['user']['username'], 'مشاهده گزارش کارگران روزمزد')
        return render_template('daily_worker_reports.html', 
                               records=records,
                               foremen=[f['foreman_name'] for f in foremen],
                               start_date=start_date_fa,
                               end_date=end_date_fa,
                               selected_foreman=foreman_filter)

    @app.route('/export_daily_worker_reports')
    @auth.login_required
    def export_daily_worker_reports():
        if not auth.has_permission('daily_worker_reports'):
            return "Access Denied", 403
        
        start_date_fa = request.args.get('start_date')
        end_date_fa = request.args.get('end_date')
        foreman_filter = request.args.get('foreman_filter')
        
        query = '''
            SELECT * FROM daily_workers
        '''
        params = []
        conditions = []
        
        if start_date_fa:
            try:
                g_date = utils.shamsi_to_miladi(start_date_fa)
                conditions.append('timestamp >= ?')
                params.append(g_date.strftime('%Y-%m-%d 00:00:00'))
            except (ValueError, IndexError):
                pass
        
        if end_date_fa:
            try:
                g_date = utils.shamsi_to_miladi(end_date_fa)
                conditions.append('timestamp <= ?')
                params.append(g_date.strftime('%Y-%m-%d 23:59:59'))
            except (ValueError, IndexError):
                pass
                
        if foreman_filter and foreman_filter != 'all':
            conditions.append('foreman_name = ?')
            params.append(foreman_filter)
            
        if conditions:
            query += ' WHERE ' + ' AND '.join(conditions)
        
        query += ' ORDER BY timestamp DESC'
        
        conn = utils.get_db_connection()
        try:
            records = conn.execute(query, params).fetchall()
        finally:
            conn.close()
            
        si = io.StringIO()
        writer = csv.writer(si, dialect='excel')
        headers = ['شناسه', 'تاریخ', 'سرکارگر', 'تعداد نفرات', 'مزد روزانه', 'هزینه ایاب و ذهاب', 'مبلغ کل روز', 'محل انجام کار']
        writer.writerow(headers)
        
        for record in records:
            writer.writerow([
                record['id'],
                record['date'],
                record['foreman_name'],
                record['worker_count'],
                record['daily_wage'],
                record['transport_cost'],
                record['total_amount'],
                record['location']
            ])
            
        output = si.getvalue().encode('utf-8-sig') 
        response = Response(output, mimetype='text/csv')
        response.headers['Content-Disposition'] = 'attachment; filename=daily_worker_reports.csv'
        
        utils.log_action(session['user']['username'], 'خروجی گرفتن از گزارش کارگران روزمزد')
        return response

    @app.route('/edit_daily_worker/<int:record_id>', methods=['POST'])
    @auth.login_required
    def edit_daily_worker(record_id):
        if not auth.has_permission('daily_worker_reports'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('daily_worker_reports'))

        date_fa = request.form['date_fa']
        foreman_name = request.form['foreman_name']
        worker_count = int(request.form['worker_count'])
        daily_wage = request.form['daily_wage'].replace(',', '')
        transport_cost = int(request.form['transport_cost'].replace(',', ''))
        total_amount = int(request.form['total_amount'].replace(',', ''))
        location = request.form['location']
        
        try:
            shamsi_dt = jdatetime.datetime.strptime(f'{date_fa}', '%Y/%m/%d')
            miladi_dt = shamsi_dt.togregorian()
            timestamp = miladi_dt.strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            flash("فرمت تاریخ نامعتبر است.", "error")
            return redirect(url_for('daily_worker_reports'))

        conn = utils.get_db_connection()
        try:
            conn.execute('''
                UPDATE daily_workers
                SET date = ?, foreman_name = ?, worker_count = ?, daily_wage = ?, transport_cost = ?, total_amount = ?, location = ?, timestamp = ?
                WHERE id = ?
            ''', (date_fa, foreman_name, worker_count, daily_wage, transport_cost, total_amount, location, timestamp, record_id))
            conn.commit()
            flash("رکورد با موفقیت ویرایش شد.", "success")
            utils.log_action(session['user']['username'], 'ویرایش رکورد کارگر روزمزد', f'رکورد با شناسه {record_id} ویرایش شد.')
        except Exception as e:
            flash(f"خطایی در ویرایش رکورد رخ داد: {e}", "error")
            print(f"Error editing daily worker: {e}")
        finally:
            conn.close()
        return redirect(url_for('daily_worker_reports'))

    @app.route('/delete_daily_worker/<int:record_id>', methods=['POST'])
    @auth.login_required
    def delete_daily_worker(record_id):
        if not auth.has_permission('daily_worker_reports'):
            flash("شما به این بخش دسترسی ندارند.", "error")
            return redirect(url_for('daily_worker_reports'))

        conn = utils.get_db_connection()
        try:
            conn.execute('DELETE FROM daily_workers WHERE id = ?', (record_id,))
            conn.commit()
            flash("رکورد با موفقیت حذف شد.", "success")
            utils.log_action(session['user']['username'], 'حذف رکورد کارگر روزمزد', f'رکورد با شناسه {record_id} حذف شد.')
        except Exception as e:
            flash(f"خطایی در حذف رکورد رخ داد: {e}", "error")
            print(f"Error deleting daily worker: {e}")
        finally:
            conn.close()
        return redirect(url_for('daily_worker_reports'))

    @app.route('/admin_dashboard')
    @auth.login_required
    def admin_dashboard():
        # Only show dashboard if user has permissions for at least one item or is admin
        user_permissions = session.get('user', {}).get('permissions', {})
        is_admin = session.get('user', {}).get('role') == 'admin'
        if not is_admin and not any(user_permissions.values()):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('index'))
        return render_template('admin_dashboard.html')

    @app.route('/financial_records')
    @auth.login_required
    def financial_records():
        if not auth.has_permission('financial_records'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('admin_dashboard'))
        return render_template('financial_records.html')

    @app.route('/add_income', methods=['POST'])
    @auth.login_required
    def add_income():
        if not auth.has_permission('financial_records'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('financial_records'))

        try:
            date_fa = request.form['date_fa']
            description = request.form['description']
            amount = float(request.form['amount'])
            source = request.form['source']
            notes = request.form['notes']

            shamsi_dt = jdatetime.datetime.strptime(date_fa, '%Y/%m/%d')
            miladi_dt = shamsi_dt.togregorian()
            timestamp = miladi_dt.strftime('%Y-%m-%d %H:%M:%S')

            conn = utils.get_db_connection()
            conn.execute('INSERT INTO income (date, description, amount, source, notes, timestamp) VALUES (?, ?, ?, ?, ?, ?)',
                         (date_fa, description, amount, source, notes, timestamp))
            conn.commit()
            flash("درآمد با موفقیت ثبت شد.", "success")
            utils.log_action(session['user']['username'], 'ثبت درآمد', f'مبلغ: {amount}')
        except Exception as e:
            flash(f"خطا در ثبت درآمد: {e}", "error")
            print(f"Error adding income: {e}")
        finally:
            conn.close()

        return redirect(url_for('financial_reports'))

    @app.route('/add_expense', methods=['POST'])
    @auth.login_required
    def add_expense():
        if not auth.has_permission('financial_records'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('financial_records'))

        try:
            date_fa = request.form['date_fa']
            description = request.form['description']
            amount = float(request.form['amount'])
            category = request.form['category']
            notes = request.form['notes']

            shamsi_dt = jdatetime.datetime.strptime(date_fa, '%Y/%m/%d')
            miladi_dt = shamsi_dt.togregorian()
            timestamp = miladi_dt.strftime('%Y-%m-%d %H:%M:%S')

            conn = utils.get_db_connection()
            conn.execute('INSERT INTO expense (date, description, amount, category, notes, timestamp) VALUES (?, ?, ?, ?, ?, ?)',
                         (date_fa, description, amount, category, notes, timestamp))
            conn.commit()
            flash("هزینه با موفقیت ثبت شد.", "success")
            utils.log_action(session['user']['username'], 'ثبت هزینه', f'مبلغ: {amount}')
        except Exception as e:
            flash(f"خطا در ثبت هزینه: {e}", "error")
            print(f"Error adding expense: {e}")
        finally:
            conn.close()

        return redirect(url_for('financial_reports'))

    @app.route('/financial_reports')
    @auth.login_required
    def financial_reports():
        if not auth.has_permission('financial_reports'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('admin_dashboard'))

        conn = utils.get_db_connection()
        try:
            income_records = conn.execute("SELECT * FROM income ORDER BY timestamp DESC").fetchall()
            expense_records = conn.execute("SELECT * FROM expense ORDER BY timestamp DESC").fetchall()
            
            total_income = conn.execute("SELECT SUM(amount) FROM income").fetchone()[0] or 0
            total_expense = conn.execute("SELECT SUM(amount) FROM expense").fetchone()[0] or 0

            # تبدیل تاریخ میلادی به شمسی
            for rec in income_records:
                miladi_dt = datetime.strptime(rec['timestamp'], '%Y-%m-%d %H:%M:%S')
                shamsi_dt = jdatetime.datetime.fromgregorian(datetime=miladi_dt)
                rec['date_fa'] = shamsi_dt.strftime('%Y/%m/%d')
            
            for rec in expense_records:
                miladi_dt = datetime.strptime(rec['timestamp'], '%Y-%m-%d %H:%M:%S')
                shamsi_dt = jdatetime.datetime.fromgregorian(datetime=miladi_dt)
                rec['date_fa'] = shamsi_dt.strftime('%Y/%m/%d')
                
        finally:
            conn.close()

        utils.log_action(session['user']['username'], 'مشاهده گزارش مالی')
        return render_template('financial_reports.html', 
                               income_records=income_records,
                               expense_records=expense_records,
                               total_income=total_income,
                               total_expense=total_expense)

    @app.route('/dashboard')
    @auth.login_required
    def dashboard():
        if not auth.has_permission('dashboard'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('admin_dashboard'))

        conn = utils.get_db_connection()
        
        # Get data from index.html logic
        employees = conn.execute('SELECT * FROM employees ORDER BY name').fetchall()
        employees_dict = {str(emp['employee_id']): {'name': emp['name'], 'last_action': None, 'condition': 'عادی'} for emp in employees}
        latest_actions = conn.execute('''
            SELECT employee_id, action, condition, activity_location, activity_description, activity_amount
            FROM time_logs
            WHERE id IN (
                SELECT MAX(id)
                FROM time_logs
                GROUP BY employee_id
            )
        ''').fetchall()
        present_employees = []
        for action_log in latest_actions:
            if action_log['action'] == 'ورود':
                employee_id = action_log['employee_id']
                if employee_id in employees_dict:
                    present_employees.append({
                        'name': employees_dict[employee_id]['name'],
                        'activity_location': action_log['activity_location'],
                        'activity_description': action_log['activity_description']
                    })

        daily_workers = get_daily_worker_data(conn)
        financial_summary = get_daily_financial_summary(conn)
        
        conn.close()

        return render_template('dashboard.html',
                               present_employees=present_employees,
                               daily_workers=daily_workers,
                               financial_summary=financial_summary)

    @app.route('/api/petty_cash_by_date_range')
    @auth.login_required
    def petty_cash_by_date_range():
        start_date_fa = request.args.get('start_date')
        end_date_fa = request.args.get('end_date')
        
        if not start_date_fa or not end_date_fa:
            return jsonify({'records': []})

        conn = utils.get_db_connection()
        try:
            start_gregorian = utils.shamsi_to_miladi(start_date_fa).strftime('%Y-%m-%d 00:00:00')
            end_gregorian = utils.shamsi_to_miladi(end_date_fa).strftime('%Y-%m-%d 23:59:59')
            
            # Aggregate data by date
            records = conn.execute(
                'SELECT date, SUM(total_amount) AS total_amount FROM petty_cash WHERE timestamp BETWEEN ? AND ? GROUP BY date ORDER BY date', 
                (start_gregorian, end_gregorian)
            ).fetchall()
            
            # Prepare data for chart.js
            labels = [record['date'] for record in records]
            amounts = [record['total_amount'] for record in records]
            
            return jsonify({
                'labels': labels,
                'amounts': amounts
            })
        finally:
            conn.close()

    @app.route('/api/petty_cash_by_date')
    @auth.login_required
    def petty_cash_by_date():
        start_date_fa = request.args.get('start_date')
        end_date_fa = request.args.get('end_date')
        
        if not start_date_fa or not end_date_fa:
            return jsonify({'records': []})

        conn = utils.get_db_connection()
        try:
            start_gregorian = utils.shamsi_to_miladi(start_date_fa).strftime('%Y-%m-%d 00:00:00')
            end_gregorian = utils.shamsi_to_miladi(end_date_fa).strftime('%Y-%m-%d 23:59:59')
            
            records = conn.execute(
                'SELECT * FROM petty_cash WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp DESC', 
                (start_gregorian, end_gregorian)
            ).fetchall()
        finally:
            conn.close()
        
        return jsonify({'records': [dict(r) for r in records]})


    @app.route('/api/petty_cash_chart_data')
    @auth.login_required
    def petty_cash_chart_data():
        conn = utils.get_db_connection()
        
        # Fetch data for the last 12 months
        query = 'SELECT STRFTIME(\'%Y-%m\', timestamp) as month, SUM(total_amount) as total_amount FROM petty_cash GROUP BY month ORDER BY month DESC LIMIT 12'
        
        records = conn.execute(query).fetchall()
        conn.close()

        months_en = [record['month'] for record in records]
        amounts = [record['total_amount'] for record in records]
        
        # Reverse to show in chronological order
        months_en.reverse()
        amounts.reverse()

        # Convert month names to Persian
        months_fa = []
        for month_en in months_en:
            year, month = month_en.split('-')
            jalali_date = jdatetime.date.fromgregorian(year=int(year), month=int(month), day=1)
            months_fa.append(jalali_date.strftime('%B %Y'))

        return jsonify({
            'labels': months_fa,
            'amounts': amounts
        })
    
    def get_daily_worker_data(conn):
        today = date.today().isoformat()
        
        query = '''
            SELECT foreman_name, worker_count, location
            FROM daily_workers
            WHERE timestamp LIKE ?
        '''
        
        records = conn.execute(query, (f"{today}%",)).fetchall()
        return records

    def get_daily_financial_summary(conn):
        today = date.today().isoformat()
        
        total_income = conn.execute("SELECT SUM(amount) FROM income WHERE timestamp LIKE ?", (f"{today}%",)).fetchone()[0] or 0
        total_expense = conn.execute("SELECT SUM(amount) FROM expense WHERE timestamp LIKE ?", (f"{today}%",)).fetchone()[0] or 0
        
        return {'total_income': total_income, 'total_expense': total_expense}
