from flask import render_template, request, redirect, url_for, jsonify, session, flash
from datetime import datetime
import jdatetime
import utils
import auth

def init_financial_routes(app):
    """
    Initializes all financial-related routes for the Flask application.
    """
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
            display_income_records = []
            for rec in income_records:
                miladi_dt = datetime.strptime(rec['timestamp'], '%Y-%m-%d %H:%M:%S')
                shamsi_dt = jdatetime.datetime.fromgregorian(datetime=miladi_dt)
                display_rec = dict(rec)
                display_rec['date_fa'] = shamsi_dt.strftime('%Y/%m/%d')
                display_income_records.append(display_rec)

            display_expense_records = []
            for rec in expense_records:
                miladi_dt = datetime.strptime(rec['timestamp'], '%Y-%m-%d %H:%M:%S')
                shamsi_dt = jdatetime.datetime.fromgregorian(datetime=miladi_dt)
                display_rec = dict(rec)
                display_rec['date_fa'] = shamsi_dt.strftime('%Y/%m/%d')
                display_expense_records.append(display_rec)
                
        finally:
            conn.close()

        utils.log_action(session['user']['username'], 'مشاهده گزارش مالی')
        return render_template('financial_reports.html', 
                               income_records=display_income_records,
                               expense_records=display_expense_records,
                               total_income=total_income,
                               total_expense=total_expense)
