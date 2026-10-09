from flask import render_template, request, redirect, url_for, jsonify, Response, session, flash
from datetime import datetime, date
import csv
import io
import jdatetime
import utils
import auth
import uuid
import os
import sqlite3
import xlsxwriter

def init_facilities_routes(app):
    """
    Initializes all facilities-related routes for the Flask application.
    """
    @app.route('/facilities')
    @auth.login_required
    def facilities():
        if not auth.has_permission('facilities'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('admin_dashboard'))
        return render_template('facilities.html')

    @app.route('/add_facilities_log', methods=['POST'])
    @auth.login_required
    def add_facilities_log():
        if not auth.has_permission('facilities'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('facilities'))
        
        conn = None  # تعریف conn قبل از بلوک try
        try:
            date_fa = request.form['date_fa']
            facility_name = request.form['facility_name']
            companion_name = request.form.get('companion_name')
            activity_location = request.form['activity_location']
            activity_description = request.form['activity_description']
            duration = request.form.get('duration')
            materials = request.form.get('materials')
            material_source = request.form.get('material_source')
            
            shamsi_dt = jdatetime.datetime.strptime(f'{date_fa}', '%Y/%m/%d')
            miladi_dt = shamsi_dt.togregorian()
            timestamp = miladi_dt.strftime('%Y-%m-%d %H:%M:%S')

            conn = utils.get_db_connection()
            conn.execute('''
                INSERT INTO facilities_logs (date, facility_name, companion_name, activity_location, activity_description, duration, materials, material_source, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (date_fa, facility_name, companion_name, activity_location, activity_description, duration, materials, material_source, timestamp))
            conn.commit()
            
            flash("گزارش تأسیسات با موفقیت ثبت شد.", "success")
            utils.log_action(session['user']['username'], 'ثبت گزارش تأسیسات', f'نام تأسیسات: {facility_name}, محل فعالیت: {activity_location}')
        except Exception as e:
            flash(f"خطا در ثبت گزارش تأسیسات: {e}", "error")
            print(f"Error adding facilities log: {e}")
        finally:
            if conn:
                conn.close()
        
        return redirect(url_for('facilities'))
    
    @app.route('/autocomplete/present_employees')
    @auth.login_required
    def autocomplete_present_employees():
        conn = None
        try:
            # Find employees who have the latest action as "ورود"
            conn = utils.get_db_connection()
            present_employees_ids = conn.execute('''
                SELECT employee_id FROM time_logs
                WHERE id IN (
                    SELECT MAX(id) FROM time_logs GROUP BY employee_id
                ) AND action = 'ورود'
            ''').fetchall()
            
            present_ids = [str(e['employee_id']) for e in present_employees_ids]

            if not present_ids:
                return jsonify([])

            placeholders = ', '.join(['?'] * len(present_ids))
            employees = conn.execute(f"SELECT name FROM employees WHERE employee_id IN ({placeholders})", present_ids).fetchall()
            
            return jsonify([e['name'] for e in employees])
        finally:
            if conn:
                conn.close()

    @app.route('/autocomplete/facilities_field/<field_name>')
    @auth.login_required
    def autocomplete_facilities_field(field_name):
        conn = None
        try:
            conn = utils.get_db_connection()
            if field_name in ['facility_name', 'activity_location', 'material_source']:
                distinct_values = conn.execute(f"SELECT DISTINCT {field_name} FROM facilities_logs WHERE {field_name} IS NOT NULL AND {field_name} != ''").fetchall()
                return jsonify([row[0] for row in distinct_values])
        finally:
            if conn:
                conn.close()
        return jsonify([])


    @app.route('/facilities_reports')
    @auth.login_required
    def facilities_reports():
        if not auth.has_permission('facilities_reports'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('admin_dashboard'))

        conn = None
        try:
            conn = utils.get_db_connection()
            
            start_date_fa = request.args.get('start_date')
            end_date_fa = request.args.get('end_date')
            companion_filter = request.args.get('companion_filter')
            location_filter = request.args.get('location_filter')
            material_source_filter = request.args.get('material_source_filter')

            query = '''
                SELECT * FROM facilities_logs
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
                    
            if companion_filter and companion_filter != 'all':
                conditions.append('companion_name = ?')
                params.append(companion_filter)

            if location_filter and location_filter != 'all':
                conditions.append('activity_location = ?')
                params.append(location_filter)
                
            if material_source_filter and material_source_filter != 'all':
                conditions.append('material_source = ?')
                params.append(material_source_filter)

            if conditions:
                query += ' WHERE ' + ' AND '.join(conditions)

            query += ' ORDER BY timestamp DESC'
            
            
            records = conn.execute(query, params).fetchall()
            companions = conn.execute("SELECT DISTINCT companion_name FROM facilities_logs WHERE companion_name IS NOT NULL AND companion_name != ''").fetchall()
            locations = conn.execute("SELECT DISTINCT activity_location FROM facilities_logs WHERE activity_location IS NOT NULL AND activity_location != ''").fetchall()
            material_sources_list = conn.execute("SELECT DISTINCT material_source FROM facilities_logs WHERE material_source IS NOT NULL AND material_source != ''").fetchall()
        finally:
            if conn:
                conn.close()

        utils.log_action(session['user']['username'], 'مشاهده گزارش تأسیسات')
        return render_template('facilities_reports.html', 
                               records=records,
                               companions=[c['companion_name'] for c in companions],
                               locations=[l['activity_location'] for l in locations],
                               material_sources_list=[s['material_source'] for s in material_sources_list],
                               start_date=start_date_fa,
                               end_date=end_date_fa,
                               selected_companion=companion_filter,
                               selected_location=location_filter,
                               selected_material_source=material_source_filter)

    @app.route('/export_facilities_reports')
    @auth.login_required
    def export_facilities_reports():
        if not auth.has_permission('facilities_reports'):
            return "Access Denied", 403
        
        conn = None
        try:
            conn = utils.get_db_connection()
            start_date_fa = request.args.get('start_date')
            end_date_fa = request.args.get('end_date')
            companion_filter = request.args.get('companion_filter')
            location_filter = request.args.get('location_filter')
            material_source_filter = request.args.get('material_source_filter')
            
            query = '''
                SELECT * FROM facilities_logs
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
                    
            if companion_filter and companion_filter != 'all':
                conditions.append('companion_name = ?')
                params.append(companion_filter)
                
            if location_filter and location_filter != 'all':
                conditions.append('activity_location = ?')
                params.append(location_filter)
                
            if material_source_filter and material_source_filter != 'all':
                conditions.append('material_source = ?')
                params.append(material_source_filter)
                
            if conditions:
                query += ' WHERE ' + ' AND '.join(conditions)
            
            query += ' ORDER BY timestamp DESC'
            
            
            records = conn.execute(query, params).fetchall()
            
            output = io.BytesIO()
            workbook = xlsxwriter.Workbook(output, {'in_memory': True})
            worksheet = workbook.add_worksheet('گزارشات تأسیسات')
            
            worksheet.right_to_left()
            
            # Define formats
            header_format = utils.excel_add_format(workbook, {'text_wrap': True, 'fg_color': '#D7E4BC', 'border': 1})
            default_format = utils.excel_add_format(workbook, {'border': 1})
            
            # Headers
            headers = ['شناسه', 'تاریخ', 'تاسیسات کار', 'نفر همراه', 'محل فعالیت', 'شرح فعالیت', 'مدت زمان', 'مواد و مصالح', 'محل تامین']
            for col_num, header in enumerate(headers):
                worksheet.write(0, col_num, header, header_format)
                
            row_num = 1
            for record in records:
                worksheet.write(row_num, 0, record['id'], default_format)
                worksheet.write(row_num, 1, utils.excel_date_text(record['date']), default_format)
                worksheet.write(row_num, 2, record['facility_name'], default_format)
                worksheet.write(row_num, 3, record['companion_name'], default_format)
                worksheet.write(row_num, 4, record['activity_location'], default_format)
                worksheet.write(row_num, 5, record['activity_description'], default_format)
                worksheet.write(row_num, 6, record['duration'], default_format)
                worksheet.write(row_num, 7, record['materials'], default_format)
                worksheet.write(row_num, 8, record['material_source'], default_format)
                row_num += 1

            utils.style_xlsxwriter_worksheet(workbook, worksheet, row_num - 1, len(headers) - 1)
            
            workbook.close()
            output.seek(0)
            
            response = Response(output.read(), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            response.headers['Content-Disposition'] = 'attachment; filename=facilities_reports.xlsx'
            
            utils.log_action(session['user']['username'], 'خروجی گرفتن از گزارش تاسیسات')
        except Exception as e:
            flash(f"خطایی در تهیه خروجی اکسل رخ داد: {e}", "error")
            return redirect(url_for('facilities_reports'))
        finally:
            if conn:
                conn.close()
        return response

    @app.route('/edit_facilities_log/<int:record_id>', methods=['POST'])
    @auth.login_required
    def edit_facilities_log(record_id):
        if not auth.has_permission('facilities_reports'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('facilities_reports'))

        conn = None
        try:
            conn = utils.get_db_connection()
            date_fa = request.form['date_fa']
            facility_name = request.form['facility_name']
            companion_name = request.form.get('companion_name')
            activity_location = request.form['activity_location']
            activity_description = request.form['activity_description']
            duration = request.form.get('duration')
            materials = request.form.get('materials')
            material_source = request.form.get('material_source')
            
            shamsi_dt = jdatetime.datetime.strptime(f'{date_fa}', '%Y/%m/%d')
            miladi_dt = shamsi_dt.togregorian()
            timestamp = miladi_dt.strftime('%Y-%m-%d %H:%M:%S')
        
            conn.execute('''
                UPDATE facilities_logs
                SET date = ?, facility_name = ?, companion_name = ?, activity_location = ?, activity_description = ?, duration = ?, materials = ?, material_source = ?, timestamp = ?
                WHERE id = ?
            ''', (date_fa, facility_name, companion_name, activity_location, activity_description, duration, materials, material_source, timestamp, record_id))
            conn.commit()
            flash("رکورد با موفقیت ویرایش شد.", "success")
            utils.log_action(session['user']['username'], 'ویرایش گزارش تاسیسات', f'رکورد با شناسه {record_id} ویرایش شد.')
        except ValueError:
            flash("فرمت تاریخ نامعتبر است.", "error")
            return redirect(url_for('facilities_reports'))
        except Exception as e:
            flash(f"خطایی در ویرایش رکورد رخ داد: {e}", "error")
            print(f"Error editing facilities log: {e}")
        finally:
            if conn:
                conn.close()
        return redirect(url_for('facilities_reports'))

    @app.route('/delete_facilities_log/<int:record_id>', methods=['POST'])
    @auth.login_required
    def delete_facilities_log(record_id):
        if not auth.has_permission('facilities_reports'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('facilities_reports'))
        
        conn = None
        try:
            conn = utils.get_db_connection()
            conn.execute('DELETE FROM facilities_logs WHERE id = ?', (record_id,))
            conn.commit()
            flash("رکورد با موفقیت حذف شد.", "success")
            utils.log_action(session['user']['username'], 'حذف گزارش تاسیسات', f'رکورد با شناسه {record_id} حذف شد.')
        except Exception as e:
            flash(f"خطایی در حذف رکورد رخ داد: {e}", "error")
            print(f"Error deleting facilities log: {e}")
        finally:
            if conn:
                conn.close()
        return redirect(url_for('facilities_reports'))
