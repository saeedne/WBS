from flask import render_template, request, redirect, url_for, jsonify, Response, session, flash
from datetime import datetime, timedelta
import csv
import io
import jdatetime
import urllib.parse
import zipfile
import utils
import auth
import sqlite3
import xlsxwriter

def init_employees_routes(app):
    """
    Initializes all employee-related routes for the Flask application.
    """
    
    # ----------------------------------------------------
    # NEW: Database migration check
    # ----------------------------------------------------
    def check_and_update_employee_table():
        conn = utils.get_db_connection()
        try:
            cursor = conn.cursor()
            # Get existing column names
            cursor.execute("PRAGMA table_info(employees)")
            columns = [info[1] for info in cursor.fetchall()]

            # UPDATED: Added national_code
            new_columns = {
                'marital_status': 'TEXT DEFAULT "مجرد"',
                'children_count': 'INTEGER DEFAULT 0',
                'base_salary': 'REAL DEFAULT 0.0',
                'seniority_pay': 'REAL DEFAULT 0.0',
                'insurance_number': 'TEXT DEFAULT ""', 
                'bank_account_number': 'TEXT DEFAULT ""',
                'national_code': 'TEXT DEFAULT ""' # NEW: شماره ملی
            }
            
            for col_name, col_type in new_columns.items():
                if col_name not in columns:
                    try:
                        cursor.execute(f"ALTER TABLE employees ADD COLUMN {col_name} {col_type}")
                        conn.commit()
                        print(f"INFO: Column '{col_name}' added to 'employees' table.")
                    except sqlite3.OperationalError as e:
                        print(f"WARNING: Failed to add column {col_name}: {e}")
                        
        except Exception as e:
            print(f"ERROR: Database check/update failed: {e}")
        finally:
            conn.close()

    check_and_update_employee_table()
    # ----------------------------------------------------
    # End of new migration check
    # ----------------------------------------------------

    @app.route('/index')
    @auth.login_required
    def index():
        if not auth.has_permission('index'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('admin_dashboard'))
            
        conn = utils.get_db_connection()
        try:
            # UPDATED: Selecting all columns including the new ones for completeness
            employees = conn.execute('SELECT * FROM employees ORDER BY name').fetchall()
            total_employees_row = conn.execute('SELECT COUNT(*) AS count FROM employees').fetchone()
            total_employees = total_employees_row['count'] if total_employees_row else 0
            
            present_employees = []
            vacation_count = 0
            break_count = 0
            
            employees_dict = {str(emp['employee_id']): {'name': emp['name'], 'last_action': None, 'condition': 'عادی'} for emp in employees}

            latest_actions = conn.execute('''
                SELECT employee_id, action, condition, timestamp
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
                        employees_dict[employee_id]['last_entry_time'] = action_log['timestamp']
                        if employees_dict[employee_id]['condition'] == 'مرخصی':
                            vacation_count += 1
                        elif employees_dict[employee_id]['condition'] == 'استراحت':
                            break_count += 1
                        
            present_employees = [{'name': emp['name'], 'condition': emp['condition'], 'id': emp_id, 'last_entry_time': emp['last_entry_time']} for emp_id, emp in employees_dict.items() if emp['last_action'] == 'ورود']
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
            # UPDATED: Fetching all columns including the new ones
            employees = conn.execute('SELECT *, IFNULL(marital_status, "مجرد") AS marital_status, IFNULL(children_count, 0) AS children_count, IFNULL(base_salary, 0.0) AS base_salary, IFNULL(seniority_pay, 0.0) AS seniority_pay, IFNULL(insurance_number, "") AS insurance_number, IFNULL(bank_account_number, "") AS bank_account_number, IFNULL(national_code, "") AS national_code FROM employees ORDER BY employee_id').fetchall()
            
            # Find the next available employee ID
            last_employee = conn.execute('SELECT employee_id FROM employees ORDER BY CAST(employee_id AS INTEGER) DESC LIMIT 1').fetchone()
            try:
                next_employee_id = str(int(last_employee['employee_id']) + 1) if last_employee and last_employee['employee_id'].isdigit() else '1001'
            except:
                next_employee_id = '1001'

        finally:
            conn.close()
        
        return render_template('management.html', employees=employees, next_employee_id=next_employee_id)


    @app.route('/add_employee', methods=['POST'])
    @auth.login_required
    def add_employee():
        if not auth.has_permission('management'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('management'))
            
        employee_id = request.form['employeeId']
        name = request.form['name']
        
        # New/Updated fields
        marital_status = request.form.get('marital_status', 'مجرد')
        children_count = request.form.get('children_count', 0)
        base_salary = request.form.get('base_salary', 0.0)
        seniority_pay = request.form.get('seniority_pay', 0.0)
        insurance_number = request.form.get('insurance_number', '') 
        bank_account_number = request.form.get('bank_account_number', '') 
        national_code = request.form.get('national_code', '') # NEW

        # Data validation and conversion
        try:
            children_count = int(children_count)
            base_salary = float(base_salary)
            seniority_pay = float(seniority_pay)
        except ValueError:
            flash("مقادیر حقوق یا تعداد فرزند نامعتبر است.", "error")
            return redirect(url_for('management'))

        conn = utils.get_db_connection()
        try:
            # Check if employee ID already exists
            existing_employee = conn.execute('SELECT employee_id FROM employees WHERE employee_id = ?', (employee_id,)).fetchone()
            if existing_employee:
                flash(f"شماره پرسنلی {employee_id} قبلاً ثبت شده است. از یک شماره دیگر استفاده کنید.", "error")
                return redirect(url_for('management'))
                
            # UPDATED: Insert query now includes national_code
            conn.execute('''
                INSERT INTO employees 
                (employee_id, name, marital_status, children_count, base_salary, seniority_pay, insurance_number, bank_account_number, national_code) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (employee_id, name, marital_status, children_count, base_salary, seniority_pay, insurance_number, bank_account_number, national_code))
            conn.commit()
            
            flash(f"کارمند {name} با شماره پرسنلی {employee_id} با موفقیت اضافه شد.", "success")
            utils.log_action(session['user']['username'], 'افزودن کارمند', f'کارمند جدید: {name} ({employee_id})')
        except Exception as e:
            flash(f"خطا در اضافه کردن کارمند: {e}", "error")
            conn.rollback()
        finally:
            conn.close()

        return redirect(url_for('management'))


    @app.route('/edit_employee', methods=['POST'])
    @auth.login_required
    def edit_employee():
        if not auth.has_permission('management'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('management'))
            
        original_employee_id = request.form['originalEmployeeId']
        new_employee_id = request.form['employeeId']
        name = request.form['name']
        
        # New/Updated fields
        marital_status = request.form.get('marital_status', 'مجرد')
        children_count = request.form.get('children_count', 0)
        base_salary = request.form.get('base_salary', 0.0)
        seniority_pay = request.form.get('seniority_pay', 0.0)
        insurance_number = request.form.get('insurance_number', '') 
        bank_account_number = request.form.get('bank_account_number', '') 
        national_code = request.form.get('national_code', '') # NEW

        # Data validation and conversion
        try:
            children_count = int(children_count)
            base_salary = float(base_salary)
            seniority_pay = float(seniority_pay)
        except ValueError:
            flash("مقادیر حقوق یا تعداد فرزند نامعتبر است.", "error")
            return redirect(url_for('management'))


        conn = utils.get_db_connection()
        try:
            # Check if the new employee ID already exists for another employee
            if original_employee_id != new_employee_id:
                existing_employee = conn.execute('SELECT employee_id FROM employees WHERE employee_id = ? AND employee_id != ?', (new_employee_id, original_employee_id)).fetchone()
                if existing_employee:
                    flash(f"شماره پرسنلی جدید ({new_employee_id}) قبلاً برای کارمند دیگری ثبت شده است.", "error")
                    return redirect(url_for('management'))
                    
            # UPDATED: Update query now includes national_code
            conn.execute('''
                UPDATE employees 
                SET employee_id = ?, name = ?, marital_status = ?, children_count = ?, base_salary = ?, seniority_pay = ?, insurance_number = ?, bank_account_number = ?, national_code = ?
                WHERE employee_id = ?
            ''', (new_employee_id, name, marital_status, children_count, base_salary, seniority_pay, insurance_number, bank_account_number, national_code, original_employee_id))
            
            # If the ID changed, update time_logs as well for consistency
            if new_employee_id != original_employee_id:
                conn.execute('UPDATE time_logs SET employee_id = ? WHERE employee_id = ?', (new_employee_id, original_employee_id))

            conn.commit()
            
            flash(f"مشخصات کارمند {name} با موفقیت به‌روزرسانی شد.", "success")
            utils.log_action(session['user']['username'], 'ویرایش کارمند', f'کارمند: {name} ({new_employee_id})')
        except Exception as e:
            flash(f"خطا در ویرایش کارمند: {e}", "error")
            conn.rollback()
        finally:
            conn.close()

        return redirect(url_for('management'))

    @app.route('/delete_employee', methods=['POST'])
    @auth.login_required
    def delete_employee():
        if not auth.has_permission('management'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('management'))
            
        employee_id = request.form['employeeId']
        conn = utils.get_db_connection()
        try:
            employee_name_row = conn.execute('SELECT name FROM employees WHERE employee_id = ?', (employee_id,)).fetchone()
            employee_name = employee_name_row['name'] if employee_name_row else 'ناشناس'

            # Delete related time logs first (cascading delete if foreign keys were set, but explicit is safer here)
            conn.execute('DELETE FROM time_logs WHERE employee_id = ?', (employee_id,))
            conn.execute('DELETE FROM employees WHERE employee_id = ?', (employee_id,))
            conn.commit()
            
            flash(f"کارمند {employee_name} با شماره پرسنلی {employee_id} با موفقیت حذف شد.", "success")
            utils.log_action(session['user']['username'], 'حذف کارمند', f'کارمند حذف شده: {employee_name} ({employee_id})')
        except Exception as e:
            flash(f"خطا در حذف کارمند: {e}", "error")
            conn.rollback()
        finally:
            conn.close()

        return redirect(url_for('management'))

    @app.route('/get_employee_data/<employee_id>')
    @auth.login_required
    def get_employee_data(employee_id):
        if not auth.has_permission('management'):
            return jsonify({'error': 'Access Denied'}), 403
            
        conn = utils.get_db_connection()
        try:
            # UPDATED: Select all fields including national_code
            employee = conn.execute('SELECT *, IFNULL(insurance_number, "") AS insurance_number, IFNULL(bank_account_number, "") AS bank_account_number, IFNULL(national_code, "") AS national_code FROM employees WHERE employee_id = ?', (employee_id,)).fetchone()
            if employee:
                # Convert Row object to dictionary for JSON serialization
                employee_data = dict(employee)
                return jsonify(employee_data)
            else:
                return jsonify({'error': 'Employee not found'}), 404
        finally:
            conn.close()

    # --- Export to XLSX ---
    @app.route('/export/employees')
    @auth.login_required
    def export_employees():
        if not auth.has_permission('management'):
            return "Access Denied", 403
            
        conn = utils.get_db_connection()
        try:
            # UPDATED: Fetch all new columns for export
            employees = conn.execute('''
                SELECT employee_id, name, 
                       IFNULL(marital_status, "مجرد") AS marital_status, 
                       IFNULL(children_count, 0) AS children_count, 
                       IFNULL(base_salary, 0.0) AS base_salary, 
                       IFNULL(seniority_pay, 0.0) AS seniority_pay,
                       IFNULL(insurance_number, "") AS insurance_number,
                       IFNULL(bank_account_number, "") AS bank_account_number,
                       IFNULL(national_code, "") AS national_code
                FROM employees 
                ORDER BY CAST(employee_id AS INTEGER) ASC
            ''').fetchall()
        finally:
            conn.close()

        # --- MODIFIED: Export to XLSX using xlsxwriter ---
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        worksheet = workbook.add_worksheet('لیست کارکنان')
        worksheet.right_to_left()

        # Define formats
        header_format = utils.excel_add_format(workbook, {'text_wrap': True, 'fg_color': '#D7E4BC', 'border': 1})
        default_format = utils.excel_add_format(workbook, {'border': 1})
        number_format = utils.excel_add_format(workbook, {'border': 1, 'num_format': '#,##0'})

        # UPDATED Headers to include new fields
        headers = [
            'شماره پرسنلی', 'نام کارمند', 'وضعیت تاهل', 'تعداد فرزند', 
            'مبلغ حقوق پایه (ریال)', 'مبلغ پایه سنوات (ریال)', 
            'شماره بیمه', 'شماره حساب', 'شماره ملی' # NEW
        ]
        for col_num, header in enumerate(headers):
            worksheet.write(0, col_num, header, header_format)

        # Set column widths
        worksheet.set_column('A:B', 15)
        worksheet.set_column('E:F', 18)
        worksheet.set_column('G:I', 20) # Updated range for new fields

        row_num = 1
        for employee in employees:
            worksheet.write(row_num, 0, employee['employee_id'], default_format)
            worksheet.write(row_num, 1, employee['name'], default_format)
            worksheet.write(row_num, 2, employee['marital_status'], default_format)
            worksheet.write(row_num, 3, employee['children_count'], default_format)
            # Use number format for money fields
            worksheet.write(row_num, 4, employee['base_salary'], number_format)
            worksheet.write(row_num, 5, employee['seniority_pay'], number_format)
            worksheet.write(row_num, 6, employee['insurance_number'], default_format)
            worksheet.write(row_num, 7, employee['bank_account_number'], default_format)
            worksheet.write(row_num, 8, employee['national_code'], default_format) # NEW
            row_num += 1

        utils.style_xlsxwriter_worksheet(workbook, worksheet, row_num - 1, len(headers) - 1)
        workbook.close()
        output.seek(0)
        
        response = Response(output.read(), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response.headers['Content-Disposition'] = 'attachment; filename=employees.xlsx'
        
        utils.log_action(session['user']['username'], 'خروجی گرفتن از لیست کارکنان')
        return response

    @app.route('/import/employees', methods=['POST'])
    @auth.login_required
    def import_employees():
        if not auth.has_permission('management'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('management'))

        file = request.files['file']
        
        if not file or not (file.filename.endswith('.csv') or file.filename.endswith('.xlsx')):
            flash("فرمت فایل نامعتبر است. لطفاً یک فایل CSV یا XLSX آپلود کنید.", "error")
            return redirect(url_for('management'))

        conn = utils.get_db_connection()
        conn.execute('BEGIN TRANSACTION')
        try:
            if file.filename.endswith('.xlsx'):
                flash("ورود داده از فایل XLSX در حال حاضر پشتیبانی نمی‌شود. لطفاً فایل را به فرمت CSV تبدیل کرده و دوباره امتحان کنید.", "warning")
                return redirect(url_for('management'))

            stream = io.StringIO(file.stream.read().decode("utf-8-sig"), newline=None)
            reader = csv.reader(stream, delimiter=',')
            # Read header row to determine column order
            header = next(reader, None)
            
            # Expected headers for mapping (case-insensitive and tolerant) - UPDATED
            expected_headers = {
                'شماره پرسنلی': 0, 'نام کارمند': 1, 'وضعیت تاهل': 2, 
                'تعداد فرزند': 3, 'مبلغ حقوق پایه': 4, 'مبلغ پایه سنوات': 5,
                'شماره بیمه': 6, 'شماره حساب': 7, 'شماره ملی': 8 # NEW
            }
            
            # Map column indices from CSV header to expected order
            header_map = {}
            if header:
                for i, col_name in enumerate(header):
                    normalized_col = col_name.strip().replace(' ', '').lower()
                    for expected_name, index in expected_headers.items():
                        # Increased tolerance for mapping
                        if expected_name.replace(' ', '').lower() in normalized_col or normalized_col in expected_name.replace(' ', '').lower():
                            header_map[expected_name] = i
                            break

            # Check for minimum required columns (ID and Name)
            if 'شماره پرسنلی' not in header_map or 'نام کارمند' not in header_map:
                flash("فایل ورودی باید حداقل ستون‌های 'شماره پرسنلی' و 'نام کارمند' را داشته باشد.", "error")
                return redirect(url_for('management'))
            
            count = 0
            for row in reader:
                if len(row) > 0:
                    
                    # Safely access fields, defaulting to empty string if column is missing or row is too short
                    def safe_get(key, default=''):
                        idx = header_map.get(key, -1)
                        if idx != -1 and len(row) > idx:
                            return row[idx].strip()
                        return default

                    employee_id = safe_get('شماره پرسنلی')
                    name = safe_get('نام کارمند')
                    marital_status = safe_get('وضعیت تاهل', 'مجرد')
                    
                    children_count_str = safe_get('تعداد فرزند').replace(',', '')
                    base_salary_str = safe_get('مبلغ حقوق پایه').replace(',', '')
                    seniority_pay_str = safe_get('مبلغ پایه سنوات').replace(',', '')
                    
                    insurance_number = safe_get('شماره بیمه')
                    bank_account_number = safe_get('شماره حساب')
                    national_code = safe_get('شماره ملی') # NEW

                    # Clean and convert data
                    try:
                        children_count = int(children_count_str or 0)
                        base_salary = float(base_salary_str or 0.0)
                        seniority_pay = float(seniority_pay_str or 0.0)
                    except ValueError:
                        flash(f"خطا در تبدیل داده‌های عددی (تعداد فرزند، حقوق یا سنوات) در ردیف {count + 1} رخ داد. رکورد نادیده گرفته شد.", "warning")
                        continue # Skip this row
                    
                    # Ensure basic data presence
                    if employee_id and name:
                        # UPDATED: Insert query now includes national_code
                        conn.execute('''
                            INSERT OR REPLACE INTO employees 
                            (employee_id, name, marital_status, children_count, base_salary, seniority_pay, insurance_number, bank_account_number, national_code) 
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (employee_id, name, marital_status, children_count, base_salary, seniority_pay, insurance_number, bank_account_number, national_code))
                        count += 1
            
            conn.commit()
            flash(f"{count} کارمند جدید با موفقیت اضافه/به‌روزرسانی شد.", "success")
            utils.log_action(session['user']['username'], 'ورودی از فایل CSV', f'{count} کارمند جدید اضافه شد.')
        except Exception as e:
            conn.rollback()
            flash(f"خطای کلی در ورود داده‌ها: {e}", "error")
            print(f"Error importing employees: {e}")
        finally:
            conn.close()

        return redirect(url_for('management'))

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

        # --- NEW DEFAULT FILTER LOGIC ---
        # If no start_date and end_date are provided in the URL, set default for last 5 days
        if not start_date_fa and not end_date_fa:
            # Calculate today and 5 days ago in Gregorian
            today_gregorian = datetime.now()
            five_days_ago_gregorian = today_gregorian - timedelta(days=3)

            # Convert Gregorian dates to Shamsi (Persian) string format
            end_date_fa_default = jdatetime.datetime.fromgregorian(datetime=today_gregorian).strftime('%Y/%m/%d')
            start_date_fa_default = jdatetime.datetime.fromgregorian(datetime=five_days_ago_gregorian).strftime('%Y/%m/%d')
            
            start_date_fa = start_date_fa_default
            end_date_fa = end_date_fa_default
        # --------------------------------

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
                # If error, clear the invalid date to prevent applying half-filter
                start_date_fa = None 
                pass
                
        if end_date_fa:
            try:
                g_date = utils.shamsi_to_miladi(end_date_fa)
                conditions.append('tl.timestamp <= ?')
                params.append(g_date.strftime('%Y-%m-%d 23:59:59'))
            except (ValueError, IndexError):
                flash("فرمت تاریخ پایان نامعتبر است.", "error")
                # If error, clear the invalid date
                end_date_fa = None 
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
            # Safety check: ensure timestamp is not None
            if record['timestamp']:
                miladi_dt = datetime.strptime(record['timestamp'], '%Y-%m-%d %H:%M:%S')
                shamsi_dt = jdatetime.datetime.fromgregorian(datetime=miladi_dt)
                timestamp_fa = shamsi_dt.strftime('%Y/%m/%d %H:%M:%S')
                timestamp_miladi = miladi_dt.strftime('%Y-%m-%d %H:%M:%S')
            else:
                timestamp_fa = '---'
                timestamp_miladi = '---'

            fa_records.append({
                'id': record['id'],
                'employee_id': record['employee_id'],
                'name': record['name'],
                'action': record['action'],
                'timestamp_fa': timestamp_fa,
                'timestamp_miladi': timestamp_miladi,
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
                               # Pass the calculated default or requested filter dates back to the template
                               start_date=start_date_fa, 
                               end_date=end_date_fa,
                               selected_condition=condition_filter)

    @app.route('/export/filtered')
    @auth.login_required
    def export_filtered():
        if not auth.has_permission('reports'):
            return "Access Denied", 403
            
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
                pass
                
        if end_date_fa:
            try:
                g_date = utils.shamsi_to_miladi(end_date_fa)
                conditions.append('tl.timestamp <= ?')
                params.append(g_date.strftime('%Y-%m-%d 23:59:59'))
            except (ValueError, IndexError):
                pass
                
        if condition_filter and condition_filter != 'all':
            conditions.append('tl.condition = ?')
            params.append(condition_filter)

        if conditions:
            query += ' WHERE ' + ' AND '.join(conditions)

        query += ' ORDER BY tl.timestamp DESC'

        try:
            records = conn.execute(query, params).fetchall()
        finally:
            conn.close()
            
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        worksheet = workbook.add_worksheet('گزارش تردد')

        worksheet.right_to_left()
        
        # Define formats
        header_format = utils.excel_add_format(workbook, {'text_wrap': True, 'fg_color': '#D7E4BC', 'border': 1})
        default_format = utils.excel_add_format(workbook, {'border': 1})
        
        # Headers
        headers = ['شماره پرسنلی', 'نام کارمند', 'عملیات', 'تاریخ', 'ساعت', 'شرایط', 'محل فعالیت', 'شرح فعالیت', 'مقدار فعالیت']
        for col_num, header in enumerate(headers):
            worksheet.write(0, col_num, header, header_format)

        row_num = 1
        for record in records:
            miladi_dt = datetime.strptime(record['timestamp'], '%Y-%m-%d %H:%M:%S')
            shamsi_dt = jdatetime.datetime.fromgregorian(datetime=miladi_dt)
            date_part = shamsi_dt.strftime('%Y/%m/%d')
            time_part = shamsi_dt.strftime('%H:%M:%S')

            worksheet.write(row_num, 0, record['employee_id'], default_format)
            worksheet.write(row_num, 1, record['name'], default_format)
            worksheet.write(row_num, 2, record['action'], default_format)
            worksheet.write(row_num, 3, utils.excel_date_text(date_part), default_format)
            worksheet.write(row_num, 4, time_part, default_format)
            worksheet.write(row_num, 5, record['condition'], default_format)
            worksheet.write(row_num, 6, record['activity_location'], default_format)
            worksheet.write(row_num, 7, record['activity_description'], default_format)
            worksheet.write(row_num, 8, record['activity_amount'], default_format)
            row_num += 1

        utils.style_xlsxwriter_worksheet(workbook, worksheet, row_num - 1, len(headers) - 1)

        workbook.close()
        output.seek(0)
        
        response = Response(output.read(), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response.headers['Content-Disposition'] = 'attachment; filename=filtered_time_logs.xlsx'
        
        utils.log_action(session['user']['username'], 'خروجی گرفتن از گزارش تردد فیلترشده')
        return response

    @app.route('/bulk_delete_logs', methods=['POST'])
    @auth.login_required
    def bulk_delete_logs():
        if not auth.has_permission('reports'):
            flash("شما به این بخش دسترسی ندارید.", "warning")
            return redirect(url_for('reports'))
        
        record_ids = request.form.getlist('record_ids')

        # Get filter parameters from the form
        employee_id_filter = request.form.get('employee_id_filter')
        start_date = request.form.get('start_date_filter')
        end_date = request.form.get('end_date_filter')
        condition = request.form.get('condition_filter')
        
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
            
        # Redirect back to reports page with the filter parameters
        return redirect(url_for('reports',
                                employee_id=employee_id_filter,
                                start_date=start_date,
                                end_date=end_date,
                                condition_filter=condition))

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
            return redirect(url_for('admin_dashboard'))

        employee_id = request.form['employeeId']
        action = request.form['action']
        date_fa = request.form['date']
        time_fa = request.form['time']
        condition = request.form.get('condition', 'عادی')
        activity_location = request.form.get('activity_location', '---')
        activity_description = request.form.get('activity_description', '---')
        activity_amount = request.form.get('activity_amount', '---')

        # Get filter parameters from the form
        employee_id_filter = request.form.get('employee_id_filter')
        start_date = request.form.get('start_date_filter')
        end_date = request.form.get('end_date_filter')
        condition_filter = request.form.get('condition_filter')
        
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
        # Redirect back to reports page with the filter parameters
        return redirect(url_for('reports',
                                employee_id=employee_id_filter,
                                start_date=start_date,
                                end_date=end_date,
                                condition_filter=condition_filter))

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
            
    @app.route('/export/all_to_single_xlsx')
    @auth.login_required
    def export_all_to_single_xlsx():
        if not auth.has_permission('reports'):
            return "Access Denied", 403
        
        # 1. Get filter parameters from URL
        start_date_fa = request.args.get('start_date')
        end_date_fa = request.args.get('end_date')

        start_timestamp = None
        end_timestamp = None

        if start_date_fa:
            try:
                g_date = utils.shamsi_to_miladi(start_date_fa)
                start_timestamp = g_date.strftime('%Y-%m-%d 00:00:00')
            except (ValueError, IndexError):
                pass
                
        if end_date_fa:
            try:
                g_date = utils.shamsi_to_miladi(end_date_fa)
                end_timestamp = g_date.strftime('%Y-%m-%d 23:59:59')
            except (ValueError, IndexError):
                pass
            
        conn = utils.get_db_connection()
        try:
            employees = conn.execute('SELECT employee_id, name FROM employees ORDER BY CAST(employee_id AS INTEGER) ASC').fetchall()
        finally:
            conn.close()

        if not employees:
            return "No employees found.", 404

        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        
        # Define formats
        header_format = utils.excel_add_format(workbook, {'text_wrap': True, 'fg_color': '#D7E4BC', 'border': 1})
        default_format = utils.excel_add_format(workbook, {'border': 1})
        link_format = utils.excel_add_format(workbook, {'color': 'blue', 'underline': 1})
        
        # Create Summary Worksheet
        summary_worksheet = workbook.add_worksheet('خلاصه')
        summary_worksheet.right_to_left()
        
        summary_headers = ['شماره پرسنلی', 'نام کارمند']
        for col_num, header in enumerate(summary_headers):
            summary_worksheet.write(0, col_num, header, header_format)
            
        summary_row_num = 1
        for employee in employees:
            worksheet_name = f"{employee['employee_id']}_{employee['name']}"[:31]  # Excel sheet name limit is 31 characters
            summary_worksheet.write(summary_row_num, 0, employee['employee_id'], default_format)
            summary_worksheet.write_url(summary_row_num, 1, f"internal:'{worksheet_name}'!A1", link_format, employee['name'])
            summary_row_num += 1

        utils.style_xlsxwriter_worksheet(workbook, summary_worksheet, summary_row_num - 1, len(summary_headers) - 1)

        # Create individual worksheets for each employee
        for employee in employees:
            employee_id = employee['employee_id']
            employee_name = employee['name']
            
            worksheet_name = f"{employee_id}_{employee_name}"[:31]
            worksheet = workbook.add_worksheet(worksheet_name)
            worksheet.right_to_left()
            
            # Build the query with date filters (MODIFIED QUERY EXECUTION)
            query = '''
                SELECT tl.id, tl.employee_id, e.name, tl.action, tl.timestamp, tl.condition, tl.activity_location, tl.activity_description, tl.activity_amount
                FROM time_logs tl
                JOIN employees e ON tl.employee_id = e.employee_id
                WHERE tl.employee_id = ?
            '''
            params = [employee_id]
            
            if start_timestamp:
                query += ' AND tl.timestamp >= ?'
                params.append(start_timestamp)
                
            if end_timestamp:
                query += ' AND tl.timestamp <= ?'
                params.append(end_timestamp)
            
            query += ' ORDER BY tl.timestamp DESC'

            conn = utils.get_db_connection()
            try:
                # Use tuple(params) to ensure correct argument passing, especially for single-element lists
                records = conn.execute(query, tuple(params)).fetchall()
            finally:
                conn.close()
            
            # Write headers for the individual sheet
            headers = ['شماره پرسنلی', 'نام کارمند', 'عملیات', 'تاریخ', 'ساعت', 'شرایط', 'محل فعالیت', 'شرح فعالیت', 'مقدار فعالیت']
            for col_num, header in enumerate(headers):
                worksheet.write(0, col_num, header, header_format)
            
            row_num = 1
            for record in records:
                miladi_dt = datetime.strptime(record['timestamp'], '%Y-%m-%d %H:%M:%S')
                shamsi_dt = jdatetime.datetime.fromgregorian(datetime=miladi_dt)
                date_part = shamsi_dt.strftime('%Y/%m/%d')
                time_part = shamsi_dt.strftime('%H:%M:%S')
                
                worksheet.write(row_num, 0, record['employee_id'], default_format)
                worksheet.write(row_num, 1, record['name'], default_format)
                worksheet.write(row_num, 2, record['action'], default_format)
                worksheet.write(row_num, 3, utils.excel_date_text(date_part), default_format)
                worksheet.write(row_num, 4, time_part, default_format)
                worksheet.write(row_num, 5, record['condition'], default_format)
                worksheet.write(row_num, 6, record['activity_location'], default_format)
                worksheet.write(row_num, 7, record['activity_description'], default_format)
                worksheet.write(row_num, 8, record['activity_amount'], default_format)
                row_num += 1
            
            utils.style_xlsxwriter_worksheet(workbook, worksheet, row_num - 1, len(headers) - 1)
        
        workbook.close()
        output.seek(0)
        
        response = Response(output.read(), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response.headers['Content-Disposition'] = 'attachment; filename=all_employees_logs.xlsx'
        
        utils.log_action(session['user']['username'], 'خروجی گرفتن اکسل با شیت‌های جداگانه')
        return response
