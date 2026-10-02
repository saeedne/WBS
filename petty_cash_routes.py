from flask import render_template, request, redirect, url_for, jsonify, Response, session, flash
from datetime import datetime
import csv
import io
import jdatetime
import utils
import auth
import uuid
import os
import sqlite3
import xlsxwriter

def _to_number(value):
    """Convert numeric values from forms/SQLite safely, including Persian digits and separators."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return 0.0

    # Normalize Persian/Arabic-Indic digits and common numeric separators.
    translation = str.maketrans(
        '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩',
        '01234567890123456789'
    )
    text = text.translate(translation)
    text = (text.replace(',', '')
                .replace('٬', '')
                .replace(' ', '')
                .replace('٫', '.'))
    return float(text)


def _to_int_number(value):
    """Return a database/display-safe integer for monetary amounts."""
    return int(round(_to_number(value)))


def _to_bool(value):
    return str(value).lower() in ('1', 'true', 'on', 'yes')


def _manager_share(record):
    """Calculate manager share according to petty-cash business rules."""
    amount = _to_number(record['total_amount'])
    if record['settlement_status'] != 'تسویه شده':
        return 0.0

    personal = _to_bool(record['personal_manager_payment'])
    excluded = _to_bool(record['exclude_manager_calculation'])

    if excluded:
        return amount if personal else 0.0

    return (amount * 0.10) + (amount if personal else 0.0)


def _paid_manager_share(record):
    """Treat explicitly marked manager payout expenses as paid manager share."""
    description = str(record['description'] or '').lstrip()
    excluded = _to_bool(record['exclude_manager_calculation'])
    return _to_number(record['total_amount']) if description.startswith('$$') and excluded else 0.0


def _report_totals(records):
    total_amount = 0.0
    personal_amount = 0.0
    excluded_amount = 0.0
    manager_share = 0.0

    for record in records:
        amount = _to_number(record['total_amount'])
        total_amount += amount
        if _to_bool(record['personal_manager_payment']):
            personal_amount += amount
        if _to_bool(record['exclude_manager_calculation']):
            excluded_amount += amount
        manager_share += _manager_share(record)

    return {
        'total_amount': total_amount,
        'personal_amount': personal_amount,
        'excluded_amount': excluded_amount,
        'manager_share': manager_share,
    }


def _parse_wbs_links(form):
    """Read and validate the repeated WBS fields from an expense form."""
    if form.get('wbs_link_count') is None:
        # Backwards compatibility for bookmarked/forms still posting one WBS.
        legacy_code = form.get('wbs_code', '').strip()
        if not legacy_code:
            return []
        legacy_coverage = _to_number(form.get('wbs_coverage_percent'))
        if not 0 <= legacy_coverage <= 100:
            raise ValueError('درصد پوشش باید بین ۰ تا ۱۰۰ باشد.')
        return [(legacy_code, legacy_coverage)]

    try:
        count = int(form.get('wbs_link_count', '0'))
    except (TypeError, ValueError):
        raise ValueError('تعداد بخش‌های مرتبط باید عدد صحیح باشد.')
    if count < 0:
        raise ValueError('تعداد بخش‌های مرتبط نمی‌تواند منفی باشد.')

    codes = form.getlist('wbs_code[]')
    coverages = form.getlist('wbs_coverage_percent[]')
    if len(codes) != count or len(coverages) != count:
        raise ValueError('تعداد ردیف‌های انتخاب‌شده با تعداد اعلام‌شده برابر نیست.')

    links = []
    for code, raw_coverage in zip(codes, coverages):
        code = code.strip()
        if not code:
            raise ValueError('برای همه ردیف‌ها یک بخش ساختار شکست انتخاب کنید.')
        coverage = _to_number(raw_coverage)
        if not 0 <= coverage <= 100:
            raise ValueError('درصد پوشش باید بین ۰ تا ۱۰۰ باشد.')
        links.append((code, coverage))

    if len({code for code, _ in links}) != len(links):
        raise ValueError('هر بخش ساختار شکست فقط یک‌بار قابل انتخاب است.')
    return links


def _save_petty_cash_wbs_links(conn, petty_cash_id, links):
    conn.execute('DELETE FROM petty_cash_wbs WHERE petty_cash_id = ?', (petty_cash_id,))
    if links:
        conn.executemany('''
            INSERT INTO petty_cash_wbs (petty_cash_id, wbs_code, wbs_coverage_percent)
            VALUES (?, ?, ?)
        ''', [(petty_cash_id, code, coverage) for code, coverage in links])


def init_petty_cash_routes(app):
    """
    Initializes all petty cash-related routes for the Flask application.
    """
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
            wbs_options = conn.execute("SELECT wbs_code, activity, unit, weight_percent FROM project_wbs ORDER BY sort_order, id").fetchall()
        finally:
            conn.close()
        
        return render_template('petty_cash.html', records=records, users=users, wbs_options=wbs_options)

    @app.route('/add_petty_cash', methods=['POST'])
    @auth.login_required
    def add_petty_cash():
        if not auth.has_permission('petty_cash'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('petty_cash'))

        conn = None
        try:
            date_fa = request.form['date_fa']
            description = request.form['description']
            unit = request.form['unit']
            amount = _to_number(request.form.get('amount'))
            unit_price = _to_number(request.form.get('unit_price'))
            # Discount may be a percentage (e.g. 10%) or a fixed amount, so keep it as text.
            discount = request.form.get('discount', '').replace(',', '').strip()
            total_amount = _to_int_number(request.form.get('total_amount'))
            location = request.form['location']
            notes = request.form['notes']
            source = request.form['source']
            invoice_number = request.form['invoice_number']
            settlement_status = request.form['settlement_status']
            payer = request.form['payer']
            personal_manager_payment = 1 if request.form.get('personal_manager_payment') == '1' else 0
            exclude_manager_calculation = 1 if request.form.get('exclude_manager_calculation') == '1' else 0

            wbs_links = _parse_wbs_links(request.form)
            conn = utils.get_db_connection()
            for code, _ in wbs_links:
                if not conn.execute('SELECT 1 FROM project_wbs WHERE wbs_code = ?', (code,)).fetchone():
                    raise ValueError('یکی از بخش‌های انتخاب‌شده در ساختار شکست وجود ندارد.')
            
            receipt_image = request.files.get('receipt_image')
            image_path = None
            if receipt_image and receipt_image.filename != '':
                if not os.path.exists(app.config['UPLOAD_FOLDER']):
                    os.makedirs(app.config['UPLOAD_FOLDER'])
                
                filename = str(uuid.uuid4()) + os.path.splitext(receipt_image.filename)[1]
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                
                with open(filepath, 'wb') as f:
                    f.write(receipt_image.read())
                
                image_path = '/static/uploads/' + filename
            
            shamsi_dt = jdatetime.datetime.strptime(f'{date_fa}', '%Y/%m/%d')
            miladi_dt = shamsi_dt.togregorian()
            timestamp = miladi_dt.strftime('%Y-%m-%d %H:%M:%S')

            conn.execute('''
                INSERT INTO petty_cash (date, description, unit, amount, unit_price, discount, total_amount, location, notes, source, invoice_number, settlement_status, payer, wbs_code, wbs_coverage_percent, personal_manager_payment, exclude_manager_calculation, receipt_image_path, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (date_fa, description, unit, amount, unit_price, discount, total_amount, location, notes, source, invoice_number, settlement_status, payer, wbs_links[0][0] if wbs_links else None, wbs_links[0][1] if wbs_links else 0, personal_manager_payment, exclude_manager_calculation, image_path, timestamp))
            petty_cash_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            _save_petty_cash_wbs_links(conn, petty_cash_id, wbs_links)
            conn.commit()
            
            flash("ثبت هزینه با موفقیت انجام شد.", "success")
            utils.log_action(session['user']['username'], 'ثبت هزینه جدید', f'شرح: {description}, مبلغ کل: {total_amount}')
        except Exception as e:
            if conn is not None:
                conn.rollback()
            flash(f"خطا در ثبت هزینه: {e}", "error")
            print(f"Error adding petty cash: {e}")
        finally:
            if conn is not None:
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
        personal_manager_payment_filter = request.args.get('personal_manager_payment_filter')
        exclude_manager_calculation_filter = request.args.get('exclude_manager_calculation_filter')

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

        # Checkbox behavior: checked = filter active records; unchecked = all records.
        if personal_manager_payment_filter == 'yes':
            conditions.append('personal_manager_payment = ?')
            params.append(1)

        if exclude_manager_calculation_filter == 'yes':
            conditions.append('exclude_manager_calculation = ?')
            params.append(1)

        if conditions:
            query += ' WHERE ' + ' AND '.join(conditions)

        query += ' ORDER BY timestamp DESC'
        
        try:
            records = conn.execute(query, params).fetchall()
            show_manager_summary = session.get('user', {}).get('role') == 'admin'
            all_records = conn.execute('SELECT * FROM petty_cash').fetchall() if show_manager_summary else []
            payers = conn.execute("SELECT DISTINCT payer FROM petty_cash").fetchall()
            settlement_statuses = conn.execute("SELECT DISTINCT settlement_status FROM petty_cash").fetchall()
            wbs_options = conn.execute("SELECT wbs_code, activity, unit, weight_percent FROM project_wbs ORDER BY sort_order, id").fetchall()
            record_wbs_links = conn.execute('''
                SELECT petty_cash_id, wbs_code, wbs_coverage_percent
                FROM petty_cash_wbs ORDER BY id
            ''').fetchall()
        finally:
            conn.close()

        links_by_record = {}
        wbs_descriptions = {row['wbs_code']: row['activity'] for row in wbs_options}
        for link in record_wbs_links:
            links_by_record.setdefault(link['petty_cash_id'], []).append({
                'wbs_code': link['wbs_code'],
                'wbs_coverage_percent': _to_number(link['wbs_coverage_percent']),
                'activity': wbs_descriptions.get(link['wbs_code'], '')
            })
        
        # Prepare records for display
        display_records = []
        for record in records:
            display_record = dict(record)
            if record['receipt_image_path']:
                description_text = record['description'] if record['description'] else 'مشاهده رسید'
                display_record['display_description'] = f'<a href="#" onclick="showReceiptModal(\'{record["receipt_image_path"]}\')">{description_text}</a>'
            else:
                display_record['display_description'] = record['description'] if record['description'] else '---'
            
            # Normalize all numeric fields before rendering. This prevents mixed SQLite
            # TEXT/REAL/INTEGER values from disappearing in the table or JS calculations.
            display_record['amount'] = _to_number(display_record.get('amount'))
            display_record['unit_price'] = _to_number(display_record.get('unit_price'))
            display_record['total_amount'] = _to_int_number(display_record.get('total_amount'))
            display_record['wbs_links'] = links_by_record.get(record['id']) or (
                [{'wbs_code': record['wbs_code'],
                  'wbs_coverage_percent': _to_number(record['wbs_coverage_percent']),
                  'activity': wbs_descriptions.get(record['wbs_code'], '')}]
                if record['wbs_code'] else []
            )

            display_records.append(display_record)

        totals = _report_totals(records)
        manager_overview = None
        if show_manager_summary:
            lifetime_manager_share = sum(_manager_share(record) for record in all_records)
            paid_manager_share = sum(_paid_manager_share(record) for record in all_records)
            manager_overview = {
                'lifetime_share': lifetime_manager_share,
                'paid_share': paid_manager_share,
                'remaining_share': lifetime_manager_share - paid_manager_share,
            }

        utils.log_action(session['user']['username'], 'مشاهده گزارش هزینه')
        return render_template('petty_cash_reports.html', 
                               records=display_records,
                               payers=[p['payer'] for p in payers],
                               settlement_statuses=[s['settlement_status'] for s in settlement_statuses],
                               wbs_options=[dict(row) for row in wbs_options],
                               start_date=start_date_fa,
                               end_date=end_date_fa,
                               selected_payer=payer_filter,
                               selected_status=settlement_status_filter,
                               selected_personal_manager_payment=personal_manager_payment_filter,
                               selected_exclude_manager_calculation=exclude_manager_calculation_filter,
                               report_totals=totals,
                               manager_overview=manager_overview)

    @app.route('/export/petty_cash_reports')
    @auth.login_required
    def export_petty_cash_reports():
        if not auth.has_permission('petty_cash_reports'):
            return "Access Denied", 403
        
        start_date_fa = request.args.get('start_date')
        end_date_fa = request.args.get('end_date')
        payer_filter = request.args.get('payer_filter')
        settlement_status_filter = request.args.get('settlement_status_filter')
        personal_manager_payment_filter = request.args.get('personal_manager_payment_filter')
        exclude_manager_calculation_filter = request.args.get('exclude_manager_calculation_filter')
        description_search = request.args.get('description_search')
        
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

        # Checkbox behavior: checked = filter active records; unchecked = all records.
        if personal_manager_payment_filter == 'yes':
            conditions.append('personal_manager_payment = ?')
            params.append(1)

        if exclude_manager_calculation_filter == 'yes':
            conditions.append('exclude_manager_calculation = ?')
            params.append(1)
        
        if description_search:
            conditions.append('description LIKE ?')
            params.append(f'%{description_search}%')
            
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
        worksheet = workbook.add_worksheet('گزارش هزینه')
        
        # Set worksheet direction to right-to-left
        worksheet.right_to_left()
        
        # Define formats
        header_format = workbook.add_format({'bold': True, 'text_wrap': True, 'valign': 'top', 'align': 'center', 'fg_color': '#D7E4BC', 'border': 1})
        number_format = workbook.add_format({'num_format': '#,##0', 'align': 'center', 'border': 1})
        default_format = workbook.add_format({'align': 'center', 'border': 1})
        total_format = workbook.add_format({'bold': True, 'num_format': '#,##0', 'align': 'center', 'border': 1})
        total_label_format = workbook.add_format({'bold': True, 'align': 'right', 'border': 1})
        
        # Headers
        headers = ['ردیف','تاریخ', 'شرح', 'واحد', 'مقدار', 'فی', 'تخفیف', 'مبلغ کل', 'محل استفاده', 'توضیحات', 'محل تامین', 'شماره فاکتور', 'وضعیت تسویه', 'پرداخت کننده']
        for col_num, header in enumerate(headers):
            worksheet.write(0, col_num, header, header_format)
            
        report_totals = _report_totals(records)
        row_num = 1
        for record in records:
            amount = _to_int_number(record['total_amount'])
            quantity = _to_number(record['amount'])
            unit_price = _to_number(record['unit_price'])
            description = record['description'] or ''
            markers = []
            if _to_bool(record['personal_manager_payment']):
                markers.append('پ.م')
            if _to_bool(record['exclude_manager_calculation']):
                markers.append('ع.پ')
            if markers:
                description = f"{description} ({'، '.join(markers)})"
            
            # Write data row, applying number format to relevant columns
            worksheet.write(row_num, 0, row_num, default_format)
            worksheet.write(row_num, 1, record['date'], default_format)
            worksheet.write(row_num, 2, description, default_format)
            worksheet.write(row_num, 3, record['unit'], default_format)
            worksheet.write(row_num, 4, quantity, number_format)
            worksheet.write(row_num, 5, unit_price, number_format)
            # Discount can be either a percentage such as 10% or a fixed numeric amount.
            discount_text = '' if record['discount'] is None else str(record['discount']).strip()
            if discount_text.endswith('%'):
                worksheet.write(row_num, 6, discount_text, default_format)
            else:
                try:
                    worksheet.write(row_num, 6, _to_number(discount_text), number_format)
                except (ValueError, TypeError):
                    worksheet.write(row_num, 6, discount_text, default_format)
            worksheet.write(row_num, 7, amount, number_format)
            worksheet.write(row_num, 8, record['location'], default_format)
            worksheet.write(row_num, 9, record['notes'], default_format)
            worksheet.write(row_num, 10, record['source'], default_format)
            worksheet.write(row_num, 11, record['invoice_number'], default_format)
            worksheet.write(row_num, 12, record['settlement_status'], default_format)
            worksheet.write(row_num, 13, record['payer'], default_format)
            row_num += 1

        # Add report summary rows at the end
        summary_rows = [
            ('جمع کل در بازه زمانی انتخابی', report_totals['total_amount']),
            ('جمع کل پرداختی شخصی مدیر در بازه زمانی انتخابی', report_totals['personal_amount']),
            ('جمع کل غیر قابل محاسبه در بازه زمانی انتخابی', report_totals['excluded_amount']),
            ('جمع کل سهم مدیر در بازه زمانی انتخابی', report_totals['manager_share']),
        ]
        for label, value in summary_rows:
            worksheet.merge_range(row_num, 0, row_num, 6, label, total_label_format)
            worksheet.write(row_num, 7, value, total_format)
            row_num += 1
        
        # Autofit columns
        worksheet.autofit()
        
        workbook.close()
        output.seek(0)
        
        response = Response(output.read(), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response.headers['Content-Disposition'] = 'attachment; filename=petty_cash_reports.xlsx'
        
        utils.log_action(session['user']['username'], 'خروجی گرفتن از گزارش هزینه')
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
        amount = _to_number(request.form.get('amount'))
        unit_price = _to_number(request.form.get('unit_price'))
        # Discount may be a percentage (e.g. 10%) or a fixed amount, so keep it as text.
        discount = request.form.get('discount', '').replace(',', '').strip()
        total_amount = _to_int_number(request.form.get('total_amount'))
        location = request.form['location']
        notes = request.form['notes']
        source = request.form['source']
        invoice_number = request.form['invoice_number']
        settlement_status = request.form['settlement_status']
        payer = request.form['payer']
        try:
            wbs_links = _parse_wbs_links(request.form)
        except ValueError as e:
            flash(str(e), 'error')
            return redirect(url_for('petty_cash_reports'))
        wbs_code = wbs_links[0][0] if wbs_links else None
        wbs_coverage_percent = wbs_links[0][1] if wbs_links else 0.0
        personal_manager_payment = 1 if request.form.get('personal_manager_payment') == '1' else 0
        exclude_manager_calculation = 1 if request.form.get('exclude_manager_calculation') == '1' else 0
        
        try:
            shamsi_dt = jdatetime.datetime.strptime(f'{date_fa}', '%Y/%m/%d')
            miladi_dt = shamsi_dt.togregorian()
            timestamp = miladi_dt.strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            flash("فرمت تاریخ نامعتبر است.", "error")
            return redirect(url_for('petty_cash_reports'))

        conn = utils.get_db_connection()
        try:
            for code, _ in wbs_links:
                if not conn.execute('SELECT 1 FROM project_wbs WHERE wbs_code = ?', (code,)).fetchone():
                    raise ValueError('یکی از بخش‌های انتخاب‌شده در ساختار شکست وجود ندارد.')
            conn.execute('''
                UPDATE petty_cash
                SET date = ?, description = ?, unit = ?, amount = ?, unit_price = ?, discount = ?, total_amount = ?, location = ?, notes = ?, source = ?, invoice_number = ?, settlement_status = ?, payer = ?, wbs_code = ?, wbs_coverage_percent = ?, personal_manager_payment = ?, exclude_manager_calculation = ?, timestamp = ?
                WHERE id = ?
            ''', (date_fa, description, unit, amount, unit_price, discount, total_amount, location, notes, source, invoice_number, settlement_status, payer, wbs_code, wbs_coverage_percent, personal_manager_payment, exclude_manager_calculation, timestamp, record_id))
            _save_petty_cash_wbs_links(conn, record_id, wbs_links)
            conn.commit()
            flash("رکورد هزینه با موفقیت ویرایش شد.", "success")
            utils.log_action(session['user']['username'], 'ویرایش رکورد هزینه', f'رکورد با شناسه {record_id} ویرایش شد.')
        except Exception as e:
            conn.rollback()
            flash(f"خطایی در ویرایش رکورد هزینه رخ داد: {e}", "error")
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
            conn.execute('DELETE FROM petty_cash_wbs WHERE petty_cash_id = ?', (record_id,))
            conn.execute('DELETE FROM petty_cash WHERE id = ?', (record_id,))
            conn.commit()
            flash("رکورد هزینه با موفقیت حذف شد.", "success")
            utils.log_action(session['user']['username'], 'حذف رکورد هزینه', f'رکورد با شناسه {record_id} حذف شد.')
        except Exception as e:
            flash(f"خطایی در حذف رکورد هزینه رخ داد: {e}", "error")
            print(f"Error deleting petty cash: {e}")
        finally:
            conn.close()
        return redirect(url_for('petty_cash_reports'))
