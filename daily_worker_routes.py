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

def init_daily_worker_routes(app):
    """
    Initializes all daily worker-related routes for the Flask application.
    """
    @app.route('/daily_worker')
    @auth.login_required
    def daily_worker():
        if not auth.has_permission('daily_worker'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('admin_dashboard'))
        
        conn = utils.get_db_connection()
        try:
            foremen = conn.execute("SELECT DISTINCT foreman_name FROM daily_workers WHERE foreman_name IS NOT NULL AND foreman_name != ''").fetchall()
            locations = conn.execute("SELECT DISTINCT location FROM daily_workers WHERE location IS NOT NULL AND location != ''").fetchall()
        finally:
            conn.close()

        return render_template('daily_worker.html', foremen=[f['foreman_name'] for f in foremen], locations=[l['location'] for l in locations])

    @app.route('/add_daily_worker', methods=['POST'])
    @auth.login_required
    def add_daily_worker():
        if not auth.has_permission('daily_worker'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('daily_worker'))
        
        conn = utils.get_db_connection()
        try:
            date_fa = request.form['date_fa']
            entry_type = request.form.get('entry_type', 'daily_wage')
            
            if entry_type == 'daily_wage':
                foreman_name = request.form['foreman_name']
            else:
                foreman_name = request.form['deposit_foreman_name']


            shamsi_dt = jdatetime.datetime.strptime(f'{date_fa}', '%Y/%m/%d')
            miladi_dt = shamsi_dt.togregorian()
            timestamp = miladi_dt.strftime('%Y-%m-%d %H:%M:%S')

            worker_count = 0
            daily_wage = None
            transport_cost = 0
            total_amount = 0
            location = None
            notes = None
            receipt_image_path = None

            if entry_type == 'daily_wage':
                worker_count = int(request.form['worker_count'])
                daily_wage = int(request.form['daily_wage'].replace(',', ''))
                transport_cost = int(request.form['transport_cost'].replace(',', ''))
                total_amount = int(request.form['total_amount'].replace(',', ''))
                location = request.form['location']
                notes = location
                
            elif entry_type == 'deposit':
                worker_count = 'واریزی'
                daily_wage = 'واریزی'
                transport_cost = 'واریزی'
                deposit_amount_str = request.form['deposit_amount'].replace(',', '')
                total_amount = -1*int(deposit_amount_str)
                notes = request.form.get('notes', '')
                location = notes

                receipt_image = request.files.get('receipt_image')
                if receipt_image and receipt_image.filename != '':
                    filename = str(uuid.uuid4()) + os.path.splitext(receipt_image.filename)[1]
                    filepath = os.path.join(utils.get_project_upload_folder(), filename)
                    
                    receipt_image.save(filepath)
                    
                    receipt_image_path = utils.get_project_upload_url(filename)

            conn.execute('''
                INSERT INTO daily_workers (date, foreman_name, worker_count, daily_wage, transport_cost, total_amount, location, timestamp, notes, receipt_image_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (date_fa, foreman_name, worker_count, daily_wage, transport_cost, total_amount, location, timestamp, notes, receipt_image_path))
            conn.commit()
            
            if entry_type == 'daily_wage':
                flash(f"تعداد {worker_count} نفر برای {foreman_name} ثبت شد.", "success")
            else:
                flash(f"ثبت {entry_type} با موفقیت انجام شد.", "success")

            utils.log_action(session['user']['username'], f'ثبت {entry_type}', f'سرکارگر: {foreman_name}, مبلغ کل: {total_amount}')
        except Exception as e:
            conn.rollback()
            flash(f"خطا در ثبت کارگر روزمزد: {e}", "error")
            print(f"Error adding daily worker: {e}")
        finally:
            conn.close()
        
        return redirect(url_for('daily_worker'))

    @app.route('/autocomplete/daily_worker_field/<field_name>')
    @auth.login_required
    def autocomplete_daily_worker_field(field_name):
        conn = utils.get_db_connection()
        try:
            if field_name == 'foreman_name':
                distinct_values = conn.execute("SELECT DISTINCT foreman_name FROM daily_workers WHERE foreman_name IS NOT NULL AND foreman_name != ''").fetchall()
                return jsonify([row[0] for row in distinct_values])
            elif field_name == 'location':
                distinct_values = conn.execute("SELECT DISTINCT location FROM daily_workers WHERE location IS NOT NULL AND location != ''").fetchall()
                return jsonify([row[0] for row in distinct_values])
        finally:
            conn.close()
        return jsonify([])

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
        
        # Prepare records for display, including a link for receipts
        display_records = []
        for record in records:
            display_record = dict(record)
            
            if record['receipt_image_path']:
                # If a receipt image exists, create a clickable link.
                link_text = record['notes'] if record['notes'] and record['notes'].strip() != '' else 'تصویر رسید'
                display_record['display_notes'] = f'<a href="#" onclick="showReceiptModal(\'{record["receipt_image_path"]}\')">{link_text}</a>'
            else:
                # If it's a deposit record, just show the notes without a link.
                if record['worker_count'] != 'واریزی':
                    display_record['display_notes'] = record['notes'] if record['notes'] and record['notes'].strip() != '' else '---'
                else:
                    # If it's a daily wage record with no receipt, show the "no receipt" message as a clickable link.
                    link_text = 'عدم وجود رسید'
                    display_record['display_notes'] = f'<a href="#" onclick="showNoReceiptModal()">{link_text}</a>'
            
            display_records.append(display_record)

        utils.log_action(session['user']['username'], 'مشاهده گزارش کارگران روزمزد')
        return render_template('daily_worker_reports.html', 
                               records=display_records,
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
        
        query += ' ORDER BY timestamp ASC'
        
        conn = utils.get_db_connection()
        try:
            records = conn.execute(query, params).fetchall()
        finally:
            conn.close()
            
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        worksheet = workbook.add_worksheet('گزارش کارگران روزمزد')
        
        # Set worksheet direction to right-to-left
        worksheet.right_to_left()
        
        # Define formats
        header_format = workbook.add_format({'bold': True, 'text_wrap': True, 'valign': 'top', 'align': 'center', 'fg_color': '#D7E4BC', 'border': 1})
        number_format = workbook.add_format({'num_format': '#,##0', 'align': 'center', 'border': 1})
        default_format = workbook.add_format({'align': 'center', 'border': 1})
        total_format = workbook.add_format({'bold': True, 'num_format': '#,##0', 'align': 'center', 'border': 1})
        
        # Headers
        headers = ['ردیف', 'تاریخ', 'سرکارگر', 'تعداد نفرات', 'مزد روزانه', 'هزینه ایاب و ذهاب', 'مبلغ کل روز', 'محل انجام کار']
        for col_num, header in enumerate(headers):
            worksheet.write(0, col_num, header, header_format)
            
        total_worker_count = 0
        total_amount_sum = 0
        row_num = 1
        for record in records:
            if isinstance(record['worker_count'], (int, float)):
                total_worker_count += record['worker_count']
            if isinstance(record['total_amount'], (int, float)):
                total_amount_sum += record['total_amount']
            
            # Write data row, applying number format to relevant columns
            worksheet.write(row_num, 0, row_num, default_format)
            worksheet.write(row_num, 1, record['date'], default_format)
            worksheet.write(row_num, 2, record['foreman_name'], default_format)
            
            # Write worker_count as a number if possible, otherwise as text
            if isinstance(record['worker_count'], (int, float)):
                worksheet.write(row_num, 3, record['worker_count'], default_format)
            else:
                worksheet.write(row_num, 3, record['worker_count'], default_format)

            # Write daily_wage as a number if possible, otherwise as text
            if isinstance(record['daily_wage'], (int, float)):
                worksheet.write(row_num, 4, record['daily_wage'], number_format)
            else:
                worksheet.write(row_num, 4, record['daily_wage'], default_format)
            
            # Write transport_cost as a number if possible, otherwise as text
            if isinstance(record['transport_cost'], (int, float)):
                worksheet.write(row_num, 5, record['transport_cost'], number_format)
            else:
                worksheet.write(row_num, 5, record['transport_cost'], default_format)

            # Write total_amount as a number if possible, otherwise as text
            if isinstance(record['total_amount'], (int, float)):
                worksheet.write(row_num, 6, record['total_amount'], number_format)
            else:
                worksheet.write(row_num, 6, record['total_amount'], default_format)

            worksheet.write(row_num, 7, record['location'], default_format)
            row_num += 1

        # Add the total row at the end
        worksheet.write(row_num, 0, 'جمع کل', total_format)
        worksheet.write(row_num, 3, total_worker_count, total_format)
        worksheet.write(row_num, 6, total_amount_sum, total_format)
        
        # Autofit columns
        worksheet.autofit()
        
        workbook.close()
        output.seek(0)
        
        response = Response(output.read(), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response.headers['Content-Disposition'] = 'attachment; filename=daily_worker_reports.xlsx'
        
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
        transport_cost = request.form.get('transport_cost', '0').replace(',', '')
        total_amount = request.form['total_amount'].replace(',', '')
        location = request.form.get('location', '')
        notes = request.form.get('notes', '')
        
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
                SET date = ?, foreman_name = ?, worker_count = ?, daily_wage = ?, transport_cost = ?, total_amount = ?, location = ?, notes = ?, timestamp = ?
                WHERE id = ?
            ''', (date_fa, foreman_name, worker_count, daily_wage, transport_cost, total_amount, location, notes, timestamp, record_id))
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
            flash("شما به این بخش دسترسی ندارید.", "error")
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

    @app.route('/delete_multiple_daily_worker', methods=['POST'])
    @auth.login_required
    def delete_multiple_daily_worker():
        if not auth.has_permission('daily_worker_reports'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('daily_worker_reports'))
        
        record_ids = request.form.getlist('record_ids')
        
        if not record_ids:
            flash("هیچ رکوردی برای حذف انتخاب نشده است.", "error")
            return redirect(url_for('daily_worker_reports'))
            
        conn = utils.get_db_connection()
        try:
            placeholders = ','.join(['?'] * len(record_ids))
            conn.execute(f"DELETE FROM daily_workers WHERE id IN ({placeholders})", record_ids)
            conn.commit()
            
            flash(f"{len(record_ids)} رکورد با موفقیت حذف شد.", "success")
            utils.log_action(session['user']['username'], 'حذف چندگانه رکورد کارگر روزمزد', f'رکوردهای با شناسه {record_ids} حذف شدند.')
        except Exception as e:
            conn.rollback()
            flash(f"خطایی در حذف رکوردها رخ داد: {e}", "error")
            print(f"Error deleting multiple daily workers: {e}")
        finally:
            conn.close()
        return redirect(url_for('daily_worker_reports'))
