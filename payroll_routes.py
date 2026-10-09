# -*- coding: utf-8 -*-
from flask import render_template, request, redirect, url_for, session, flash, Response
import utils
import auth
from datetime import timedelta, datetime
import jdatetime
import io
import xlsxwriter

# --- Constants ---
HOURLY_RATE = 200000
DAILY_HOURS_THRESHOLD = timedelta(hours=8)
NIGHT_SHIFT_HOURS = 8 # 8 hours is considered one night shift

def get_day_of_week_fa(date_str):
    """
    Returns the Persian day of the week for a given Shamsi date string.
    """
    weekdays_fa = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه"]
    try:
        jdate = jdatetime.datetime.strptime(date_str, '%Y/%m/%d')
        return weekdays_fa[jdate.weekday()]
    except:
        return ""

def calculate_night_shift(start_time, end_time):
    """
    Calculates the duration of night shift work (22:00 to 06:00 the next morning)
    within a given time span (start_time to end_time).
    start_time and end_time must be datetime objects.
    """
    night_shift_duration = timedelta(0)
    
    # Iterate through the time period, checking for overlap with the 22:00-06:00 window.
    # We check each day the shift spans.
    current_time = start_time
    
    while current_time < end_time:
        
        # Define the night shift window for the day current_time belongs to
        night_start = current_time.replace(hour=22, minute=0, second=0, microsecond=0)
        night_end = (current_time.date() + timedelta(days=1))
        night_end = datetime.combine(night_end, datetime.min.time()).replace(hour=6, minute=0, second=0, microsecond=0)
        
        # Adjust night_start if the shift already started before 22:00 and continues into the night
        if current_time.hour >= 22:
            night_start = current_time.replace(hour=22, minute=0, second=0, microsecond=0)
        
        # If the night window for the current 24-hour period is entirely after the shift ends, stop.
        if night_start >= end_time:
            break
            
        # Determine the effective start and end of the overlap
        overlap_start = max(start_time, night_start)
        overlap_end = min(end_time, night_end)
        
        if overlap_start < overlap_end:
            night_shift_duration += (overlap_end - overlap_start)
        
        # Move current_time to the next day's 00:00:00 to check the next night window
        current_time = (current_time.date() + timedelta(days=1))
        current_time = datetime.combine(current_time, datetime.min.time())

        # If the night shift duration has been calculated across the entire period, break
        if current_time >= end_time:
             break
            
    return night_shift_duration

def get_all_logs_ordered(conn, start_date_miladi_str, end_date_miladi_str):
    """
    Fetches all logs for the specified period, ordered by employee and timestamp.
    Returns a dictionary of {employee_id: [logs]}
    We expand the search range by one day before the start date to catch cross-day shifts.
    """
    
    # Calculate one day before the start date
    start_date_miladi = datetime.strptime(start_date_miladi_str.split(' ')[0], '%Y-%m-%d')
    extended_start_date_miladi = start_date_miladi - timedelta(days=1)
    extended_start_date_miladi_str = extended_start_date_miladi.strftime('%Y-%m-%d 00:00:00')
    
    # Fetch all time logs for the extended period, ordered by timestamp
    all_logs = conn.execute('''
        SELECT employee_id, timestamp, action, condition
        FROM time_logs
        WHERE timestamp BETWEEN ? AND ?
        ORDER BY employee_id, timestamp ASC
    ''', (extended_start_date_miladi_str, end_date_miladi_str)).fetchall()

    logs_by_employee = {}
    for log in all_logs:
        if log['employee_id'] not in logs_by_employee:
            logs_by_employee[log['employee_id']] = []
        logs_by_employee[log['employee_id']].append(log)
        
    return logs_by_employee

def calculate_payroll_data(employee_id_filter, start_date_fa, end_date_fa, holidays_input):
    """
    Performs the payroll calculation logic and returns the results.
    This function centralizes the logic to avoid code duplication.
    Includes logic for pairing Entry/Exit logs and calculating night shift.
    """
    
    holiday_list = [h.strip() for h in holidays_input.split(',')] if holidays_input else []
    conn = utils.get_db_connection()
    
    # Initialize necessary variables
    employees = []
    payroll_results = []
    total_payroll_summary = {
        'total_working_days': 0,
        'total_overtime_undertime': timedelta(),
        'total_friday_logs': 0,
        'total_holiday_logs': 0,
        'total_leave_days': 0,
        'total_rest_days': 0,
        'total_salary_amount': 0,
        'total_night_shift_shifts': 0,
        'total_compensable_days': 0, # New: Compensable holidays/Fridays
        'total_working_days_base': 0, # New: Base days for leave calculation
        'total_working_days_with_leave': 0, # New: Final working days with 0.083 factor
        'leave_bank': 0
    }
    
    try:
        employees = conn.execute('SELECT employee_id, name FROM employees ORDER BY name').fetchall()
        
        start_date_miladi = utils.shamsi_to_miladi(start_date_fa)
        end_date_miladi = utils.shamsi_to_miladi(end_date_fa)
        start_date_miladi_str = start_date_miladi.strftime('%Y-%m-%d 00:00:00')
        end_date_miladi_str = end_date_miladi.strftime('%Y-%m-%d 23:59:59')
        
        employee_ids_to_process = [emp['employee_id'] for emp in employees]
        if employee_id_filter and employee_id_filter != 'all':
            employee_ids_to_process = [employee_id_filter]
            
        logs_by_employee = get_all_logs_ordered(conn, start_date_miladi_str, end_date_miladi_str)
        
        # --- 1. Pair Logs (Entry/Exit) & Calculate Shift Data ---
        
        # paired_shifts structure: {emp_id: {shamsi_date_of_entry: [{start: datetime, end: datetime, duration: timedelta, night_shift: timedelta, ...}]}}
        paired_shifts = {} 
        
        for emp_id in employee_ids_to_process:
            
            paired_shifts[emp_id] = {}
            if emp_id not in logs_by_employee:
                continue

            logs = logs_by_employee[emp_id]
            last_entry_log = None
            
            for log in logs:
                miladi_dt = datetime.strptime(log['timestamp'], '%Y-%m-%d %H:%M:%S')
                
                if log['action'] == 'ورود':
                    last_entry_log = {'time': miladi_dt, 'condition': log['condition']}
                
                elif log['action'] == 'خروج' and last_entry_log:
                    entry_time = last_entry_log['time']
                    exit_time = miladi_dt
                    
                    # Ignore corrupted shifts (Exit before Entry)
                    if exit_time <= entry_time:
                        last_entry_log = None
                        continue
                        
                    shift_duration = exit_time - entry_time
                    night_shift_duration = calculate_night_shift(entry_time, exit_time)
                    
                    # Log date is determined by the ENTRY date (Only if entry date is within the requested period)
                    entry_shamsi_date_obj = jdatetime.datetime.fromgregorian(datetime=entry_time)
                    shamsi_date = entry_shamsi_date_obj.strftime('%Y/%m/%d')
                    
                    # Only process shifts that STARTED within the requested date range
                    if shamsi_date < start_date_fa or shamsi_date > end_date_fa:
                        last_entry_log = None # Do not use this entry for calculation, but reset
                        continue
                    
                    # Store the shift data, linked to the entry date
                    shift_data = {
                        'entry_time_dt': entry_time,
                        'exit_time_dt': exit_time,
                        'entry_time_str': entry_time.strftime('%H:%M:%S'),
                        'exit_time_str': exit_time.strftime('%H:%M:%S'),
                        'duration': shift_duration,
                        'night_shift': night_shift_duration,
                        'condition': last_entry_log['condition'] # Use condition from entry log
                    }
                    
                    if shamsi_date not in paired_shifts[emp_id]:
                        paired_shifts[emp_id][shamsi_date] = []
                    paired_shifts[emp_id][shamsi_date].append(shift_data)
                    
                    last_entry_log = None # Reset after successful pair

        # --- 2. Iterate Days and Summarize Paired Shifts ---

        # Start loop with datetime object for easier timedelta operations
        current_date_dt = jdatetime.datetime.fromgregorian(date=start_date_miladi)
        end_date_j = jdatetime.date.fromgregorian(date=end_date_miladi)

        for emp_id in employee_ids_to_process:
            
            employee_name = next((emp['name'] for emp in employees if emp['employee_id'] == emp_id), 'ناشناس')
            
            # Reset daily summary for current employee
            employee_summary = {
                'daily_summary': {},
                'total_working_days': 0, # Total days with actual shift logs
                'total_overtime_undertime': timedelta(),
                'total_friday_logs': 0,
                'total_holiday_logs': 0,
                'total_leave_days': 0,
                'total_rest_days': 0,
                'total_salary_amount': 0,
                'total_night_shift_shifts': 0,
                'total_compensable_days': 0, # Days with no log but eligible for salary
                'total_working_days_base': 0, # (Total working days + Compensable days) - used for leave calculation base
                'total_working_days_with_leave': 0, # (Base * 1.083) - new requested field
                'leave_bank': 0
            }
            
            total_working_hours_employee = timedelta(0)
            
            # Reset current_date_dt to the exact start date for the loop of each employee
            current_date_dt = jdatetime.datetime.fromgregorian(date=start_date_miladi)
            
            # Loop through all dates in the range
            while current_date_dt.date() <= end_date_j:
                shamsi_date = current_date_dt.strftime('%Y/%m/%d')
                is_friday = current_date_dt.weekday() == 6
                is_holiday = shamsi_date in holiday_list
                
                # Check shifts for the current date (based on entry date)
                shifts_for_day = paired_shifts[emp_id].get(shamsi_date, [])
                has_shifts = len(shifts_for_day) > 0
                
                # Determine daily conditions (Leave/Rest are currently determined by log condition, if any)
                condition = shifts_for_day[0]['condition'] if has_shifts and shifts_for_day[0]['condition'] else ''
                
                is_leave = condition == 'مرخصی'
                is_rest = condition == 'استراحت'
                
                daily_data = {
                    'shifts': shifts_for_day,
                    'total_working_hours': timedelta(0),
                    'total_night_shift': timedelta(0),
                    'overtime_undertime': timedelta(0),
                    'has_shifts': has_shifts,
                    'is_friday': is_friday,
                    'is_holiday': is_holiday,
                    'is_leave': is_leave,
                    'is_rest': is_rest,
                    'is_compensable': False # Flag for compensable day
                }

                if is_leave:
                    employee_summary['total_leave_days'] += 1
                elif is_rest:
                    employee_summary['total_rest_days'] += 1
                    
                
                # --- Day with Actual Work (Has Shifts) ---
                if has_shifts and not is_leave:
                    # Sum up hours and night shift for the day
                    for shift in shifts_for_day:
                        daily_data['total_working_hours'] += shift['duration']
                        daily_data['total_night_shift'] += shift['night_shift']
                        
                    # Calculate daily overtime/undertime
                    overtime_undertime = daily_data['total_working_hours'] - DAILY_HOURS_THRESHOLD
                    daily_data['overtime_undertime'] = overtime_undertime
                    
                    # Update employee totals
                    employee_summary['total_working_days'] += 1
                    total_working_hours_employee += daily_data['total_working_hours']
                    employee_summary['total_overtime_undertime'] += overtime_undertime
                    
                    if is_friday:
                        employee_summary['total_friday_logs'] += 1
                    if is_holiday:
                        employee_summary['total_holiday_logs'] += 1
                
                # --- LOGIC FOR COMPENSABLE HOLIDAYS/FRIDAYS ---
                # A day is compensable if it's a Friday/Holiday AND no shift was logged AND the day is within the range
                # AND there was a shift the day before OR the day after.
                if not has_shifts and (is_friday or is_holiday) and not is_leave and not is_rest:
                    
                    prev_day_dt = current_date_dt - timedelta(days=1)
                    next_day_dt = current_date_dt + timedelta(days=1)
                    
                    prev_day_shamsi = prev_day_dt.strftime('%Y/%m/%d')
                    next_day_shamsi = next_day_dt.strftime('%Y/%m/%d')
                    
                    # Check for shift existence in the paired_shifts for prev/next day (must also be within overall date range)
                    has_shift_prev_day = (prev_day_shamsi >= start_date_fa and prev_day_shamsi <= end_date_fa) and (len(paired_shifts[emp_id].get(prev_day_shamsi, [])) > 0)
                    has_shift_next_day = (next_day_shamsi >= start_date_fa and next_day_shamsi <= end_date_fa) and (len(paired_shifts[emp_id].get(next_day_shamsi, [])) > 0)
                    
                    if has_shift_prev_day or has_shift_next_day:
                        daily_data['is_compensable'] = True
                        employee_summary['total_compensable_days'] += 1
                        
                # Day without log and not compensable: No salary deduction, just skip.
                
                # Add daily data to employee's summary
                employee_summary['daily_summary'][shamsi_date] = daily_data
                current_date_dt += timedelta(days=1)

            # --- 3. Final Calculations & Totals ---
            
            # Calculate total night shift shifts
            total_night_shift_hours = sum(d['total_night_shift'].total_seconds() for d in employee_summary['daily_summary'].values())
            employee_summary['total_night_shift_shifts'] = round(total_night_shift_hours / (3600 * NIGHT_SHIFT_HOURS), 2)
            
            # Calculate salary base
            salary_amount = (total_working_hours_employee.total_seconds() / 3600) * HOURLY_RATE
            
            # Add compensable days (8 hours per day) to salary base
            compensable_salary = employee_summary['total_compensable_days'] * DAILY_HOURS_THRESHOLD.total_seconds() / 3600 * HOURLY_RATE
            salary_amount += compensable_salary
            
            employee_summary['salary_amount'] = int(salary_amount)
            
            # Calculate "کل روز کارکرد (پایه مرخصی)" (Base days for leave bank)
            base_days = employee_summary['total_working_days'] + employee_summary['total_compensable_days']
            employee_summary['total_working_days_base'] = base_days
            
            # Calculate "کل روز کارکرد (با مرخصی)" (New field, Base days * (1 + 0.083))
            employee_summary['total_working_days_with_leave'] = round(base_days * (1 + 0.083), 2)

            # Calculate Leave Bank (0.083 day per calculated working day)
            leave_bank = max(0, (base_days * 0.083) - employee_summary['total_leave_days'])
            employee_summary['leave_bank'] = round(leave_bank, 2)
            
            
            # Append final result
            payroll_results.append({
                'employee_id': emp_id,
                'name': employee_name,
                **employee_summary
            })
            
            # Update overall totals
            total_payroll_summary['total_working_days'] += employee_summary['total_working_days']
            total_payroll_summary['total_overtime_undertime'] += employee_summary['total_overtime_undertime']
            total_payroll_summary['total_friday_logs'] += employee_summary['total_friday_logs']
            total_payroll_summary['total_holiday_logs'] += employee_summary['total_holiday_logs']
            total_payroll_summary['total_leave_days'] += employee_summary['total_leave_days']
            total_payroll_summary['total_rest_days'] += employee_summary['total_rest_days']
            total_payroll_summary['total_salary_amount'] += employee_summary['salary_amount']
            total_payroll_summary['total_night_shift_shifts'] += employee_summary['total_night_shift_shifts']
            total_payroll_summary['total_compensable_days'] += employee_summary['total_compensable_days']
            total_payroll_summary['total_working_days_base'] += employee_summary['total_working_days_base']
            total_payroll_summary['total_working_days_with_leave'] += employee_summary['total_working_days_with_leave']
            total_payroll_summary['leave_bank'] += employee_summary['leave_bank']


        return payroll_results, total_payroll_summary, employees
    except Exception as e:
        print(f"Error calculating payroll: {e}")
        # Return empty data structure on error
        total_payroll_summary['total_overtime_undertime'] = timedelta(0)
        return [], total_payroll_summary, employees
    finally:
        conn.close()


def init_payroll_routes(app):
    """
    Initializes all payroll-related routes for the Flask application.
    """
    @app.route('/payroll_calculation')
    @auth.login_required
    def payroll_calculation():
        if not auth.has_permission('payroll_calculation'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('admin_dashboard'))
        
        conn = utils.get_db_connection()
        try:
            employees = conn.execute('SELECT employee_id, name FROM employees ORDER BY name').fetchall()
        finally:
            conn.close()

        payroll_results = []
        
        return render_template('payroll_calculation.html', employees=employees, payroll_results=payroll_results)
    
    @app.route('/calculate_payroll', methods=['GET'])
    @auth.login_required
    def calculate_payroll():
        if not auth.has_permission('payroll_calculation'):
            flash("شما به این بخش دسترسی ندارید.", "error")
            return redirect(url_for('admin_dashboard'))

        employee_id_filter = request.args.get('employee_id')
        start_date_fa = request.args.get('start_date')
        end_date_fa = request.args.get('end_date')
        holidays_input = request.args.get('holidays', '').strip()
        
        conn = utils.get_db_connection()
        employees = conn.execute('SELECT employee_id, name FROM employees ORDER BY name').fetchall()
        conn.close()

        if not start_date_fa or not end_date_fa:
            flash("لطفا تاریخ شروع و پایان را انتخاب کنید.", "error")
            return render_template('payroll_calculation.html',
                                   employees=employees,
                                   payroll_results=[],
                                   total_payroll_summary={},
                                   selected_employee_id=employee_id_filter,
                                   start_date=start_date_fa,
                                   end_date=end_date_fa,
                                   holidays_input=holidays_input)

        try:
            utils.shamsi_to_miladi(start_date_fa)
            utils.shamsi_to_miladi(end_date_fa)
        except ValueError:
            flash("فرمت تاریخ نامعتبر است.", "error")
            return render_template('payroll_calculation.html',
                               employees=employees,
                               payroll_results=[],
                               total_payroll_summary={},
                               selected_employee_id=employee_id_filter,
                               start_date=start_date_fa,
                               end_date=end_date_fa,
                               holidays_input=holidays_input)

        payroll_results, total_payroll_summary, _ = calculate_payroll_data(employee_id_filter, start_date_fa, end_date_fa, holidays_input)
        
        total_overtime_undertime_seconds = total_payroll_summary['total_overtime_undertime'].total_seconds()
        total_overtime_undertime_hours = int(abs(total_overtime_undertime_seconds) // 3600)
        total_overtime_undertime_minutes = int((abs(total_overtime_undertime_seconds) % 3600) // 60)
        total_payroll_summary['total_overtime_undertime_formatted'] = f"{'-' if total_overtime_undertime_seconds < 0 else ''}{total_overtime_undertime_hours} ساعت و {total_overtime_undertime_minutes} دقیقه"
        
        return render_template('payroll_calculation.html',
                               employees=employees,
                               payroll_results=payroll_results,
                               total_payroll_summary=total_payroll_summary,
                               selected_employee_id=employee_id_filter,
                               start_date=start_date_fa,
                               end_date=end_date_fa,
                               holidays_input=holidays_input,
                               get_day_of_week=get_day_of_week_fa)
                               
    @app.route('/export/payroll_calculation')
    @auth.login_required
    def export_payroll_calculation():
        if not auth.has_permission('payroll_calculation'):
            return "Access Denied", 403
            
        employee_id_filter = request.args.get('employee_id')
        start_date_fa = request.args.get('start_date')
        end_date_fa = request.args.get('end_date')
        holidays_input = request.args.get('holidays', '').strip()
        
        if not start_date_fa or not end_date_fa:
            return "لطفاً تاریخ شروع و پایان را انتخاب کنید.", 400
            
        try:
            utils.shamsi_to_miladi(start_date_fa)
            utils.shamsi_to_miladi(end_date_fa)
        except ValueError:
            return "فرمت تاریخ نامعتبر است.", 400

        payroll_results, total_payroll_summary, employees = calculate_payroll_data(employee_id_filter, start_date_fa, end_date_fa, holidays_input)

        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        
        # Define formats once
        header_format = utils.excel_add_format(workbook, {'text_wrap': True, 'fg_color': '#D7E4BC', 'border': 1})
        default_format = utils.excel_add_format(workbook, {'border': 1})
        text_format = utils.excel_add_format(workbook, {'border': 1, 'num_format': '@'})
        number_format = utils.excel_add_format(workbook, {'num_format': '#,##0', 'border': 1})
        float_format = utils.excel_add_format(workbook, {'num_format': '0.00', 'border': 1})
        red_bg = utils.excel_add_format(workbook, {'border': 1, 'bg_color': '#FFC7CE', 'font_color': '#9C0006'})
        blue_bg = utils.excel_add_format(workbook, {'border': 1, 'bg_color': '#B4C6E7', 'font_color': '#0000FF'})
        yellow_bg = utils.excel_add_format(workbook, {'border': 1, 'bg_color': '#ffefc7'})
        orange_bg = utils.excel_add_format(workbook, {'border': 1, 'bg_color': '#ffcc99'})
        compensable_bg = utils.excel_add_format(workbook, {'border': 1, 'bg_color': '#d9ead3'})
        bold_format = utils.excel_add_format(workbook, {'border': 1})
        
        # --- Function to write employee-specific data (Detailed Sheet) ---
        
        headers = ['شماره پرسنلی', 'نام کارمند', 'تاریخ', 'روز هفته', 'ورود', 'خروج', 'ساعات کارکرد روزانه', 'ساعات شب‌کاری', 'مجموع اضافه‌کاری و کسری کار']

        def write_employee_sheet(worksheet, result, start_date_fa, end_date_fa, holiday_list):
            
            # Write headers
            for col_num, header in enumerate(headers):
                worksheet.write(0, col_num, header, header_format)
            
            row_num = 1
            worksheet.merge_range(f'A{row_num+1}:I{row_num+1}', f'گزارش کارکرد {result["name"]} در بازه {utils.excel_date_text(start_date_fa)} تا {utils.excel_date_text(end_date_fa)}', header_format)
            row_num += 1

            for date, summary in result['daily_summary'].items():
                
                row_format = default_format
                
                # Check formatting conditions
                is_friday = get_day_of_week_fa(date) == 'جمعه'
                is_holiday = date in holiday_list

                if summary['is_compensable']:
                    row_format = compensable_bg
                elif is_holiday:
                    row_format = orange_bg
                elif is_friday:
                    row_format = red_bg
                elif summary['is_leave']:
                    row_format = yellow_bg
                elif summary['is_rest']:
                    row_format = yellow_bg
                elif not summary['has_shifts']:
                    row_format = blue_bg

                entry_times = ' / '.join([shift['entry_time_str'] for shift in summary['shifts']])
                exit_times = ' / '.join([shift['exit_time_str'] for shift in summary['shifts']])
                
                daily_hours_in_seconds = summary['total_working_hours'].total_seconds()
                total_night_shift_seconds = summary['total_night_shift'].total_seconds()
                overtime_undertime_seconds = summary['overtime_undertime'].total_seconds()
                
                worksheet.write(row_num, 0, result['employee_id'], row_format)
                worksheet.write(row_num, 1, result['name'], row_format)
                worksheet.write(row_num, 2, utils.excel_date_text(date), row_format)
                worksheet.write(row_num, 3, get_day_of_week_fa(date), row_format)
                
                
                # Handle Non-work days (Leave, Rest, Compensable, No-Log)
                if summary['is_leave']:
                    worksheet.merge_range(row_num, 4, row_num, 8, 'مرخصی', bold_format)
                elif summary['is_rest']:
                    worksheet.merge_range(row_num, 4, row_num, 8, 'استراحت', bold_format)
                elif summary['is_compensable']:
                    worksheet.merge_range(row_num, 4, row_num, 8, f'تعطیل', bold_format)
                elif not summary['has_shifts']:
                    worksheet.merge_range(row_num, 4, row_num, 8, 'بدون اطلاعات', bold_format)
                else:
                    # Logged shifts
                    worksheet.write(row_num, 4, entry_times, row_format)
                    worksheet.write(row_num, 5, exit_times, row_format)
                    
                    # Daily Working Hours
                    worksheet.write(row_num, 6, f"{int(daily_hours_in_seconds // 3600):02}:{int((daily_hours_in_seconds % 3600) // 60):02}", text_format)
                    
                    # Night Shift Hours
                    worksheet.write(row_num, 7, f"{int(total_night_shift_seconds // 3600):02}:{int((total_night_shift_seconds % 3600) // 60):02}", text_format)
                    
                    # Overtime/Undertime
                    if overtime_undertime_seconds < 0:
                        undertime_hours = int(abs(overtime_undertime_seconds) // 3600)
                        undertime_minutes = int((abs(overtime_undertime_seconds) % 3600) // 60)
                        worksheet.write(row_num, 8, f"-{undertime_hours:02}:{undertime_minutes:02}", text_format)
                    else:
                        overtime_hours = int(overtime_undertime_seconds // 3600)
                        overtime_minutes = int((overtime_undertime_seconds % 3600) // 60)
                        worksheet.write(row_num, 8, f"+{overtime_hours:02}:{overtime_minutes:02}", text_format)

                row_num += 1
            
            # --- Employee Totals (as per Image structure in previous steps) ---
            total_overtime_undertime_seconds = result['total_overtime_undertime'].total_seconds()
            
            row_num += 1
            worksheet.merge_range(f'A{row_num}:I{row_num}', f'خلاصه گزارش برای {result["name"]}', header_format)
            row_num += 1
            
            # New structure based on image from earlier request
            # Column 3 is the value, Column 4 is the unit
            
            worksheet.merge_range(row_num, 0, row_num, 2, 'مجموع روزکارکرد', default_format)
            worksheet.write(row_num, 3, result["total_working_days"], default_format)
            worksheet.write(row_num, 4, 'روز', default_format)
            row_num += 1
            
            worksheet.merge_range(row_num, 0, row_num, 2, 'تعداد روزهای تعطیل محاسبه شده', default_format)
            worksheet.write(row_num, 3, result["total_compensable_days"], default_format)
            worksheet.write(row_num, 4, 'روز', default_format)
            row_num += 1
            
            worksheet.merge_range(row_num, 0, row_num, 2, 'کل روز کارکرد (پایه مرخصی)', default_format)
            worksheet.write(row_num, 3, result['total_working_days_base'], default_format)
            worksheet.write(row_num, 4, 'روز', default_format)
            row_num += 1
            
            worksheet.merge_range(row_num, 0, row_num, 2, 'کل روز کارکرد (با مرخصی)', default_format)
            worksheet.write(row_num, 3, result['total_working_days_with_leave'], float_format)
            worksheet.write(row_num, 4, 'روز', default_format)
            row_num += 1
            
            worksheet.merge_range(row_num, 0, row_num, 2, 'ذخیره مرخصی', default_format)
            worksheet.write(row_num, 3, result['leave_bank'], float_format)
            worksheet.write(row_num, 4, 'روز', default_format)
            row_num += 1

            worksheet.merge_range(row_num, 0, row_num, 2, 'مجموع اضافه‌کاری و کسری کار', default_format)
            if total_overtime_undertime_seconds < 0:
                undertime_hours = int(abs(total_overtime_undertime_seconds) // 3600)
                undertime_minutes = int((abs(total_overtime_undertime_seconds) % 3600) // 60)
                worksheet.write(row_num, 3, f"-{undertime_hours:02}:{undertime_minutes:02}", text_format)
            else:
                overtime_hours = int(total_overtime_undertime_seconds // 3600)
                overtime_minutes = int((total_overtime_undertime_seconds % 3600) // 60)
                worksheet.write(row_num, 3, f"+{overtime_hours:02}:{overtime_minutes:02}", text_format)
            worksheet.write(row_num, 4, 'ساعت', default_format)
            row_num += 1

            worksheet.merge_range(row_num, 0, row_num, 2, 'شیفت کاری شب', default_format)
            worksheet.write(row_num, 3, result['total_night_shift_shifts'], float_format)
            worksheet.write(row_num, 4, 'شیفت', default_format)
            row_num += 1
            
            worksheet.merge_range(row_num, 0, row_num, 2, 'جمعه کاری', default_format)
            worksheet.write(row_num, 3, result["total_friday_logs"], default_format)
            worksheet.write(row_num, 4, 'روز', default_format)
            row_num += 1

            worksheet.merge_range(row_num, 0, row_num, 2, 'تعطیل کاری', default_format)
            worksheet.write(row_num, 3, result["total_holiday_logs"], default_format)
            worksheet.write(row_num, 4, 'روز', default_format)
            row_num += 1

            worksheet.merge_range(row_num, 0, row_num, 2, 'مجموع مرخصی‌ها', default_format)
            worksheet.write(row_num, 3, result["total_leave_days"], default_format)
            worksheet.write(row_num, 4, 'روز', default_format)
            row_num += 1

            worksheet.merge_range(row_num, 0, row_num, 2, 'حقوق کل', default_format)
            worksheet.write(row_num, 3, result['salary_amount'], number_format)
            worksheet.write(row_num, 4, 'ریال', default_format)
            row_num += 2
        
            # Autofit all columns
            worksheet.set_column('A:A', 12)
            worksheet.set_column('B:B', 15)
            worksheet.set_column('C:C', 10)
            worksheet.set_column('D:D', 10)
            worksheet.set_column('E:F', 15) # Entry/Exit
            worksheet.set_column('G:G', 20)
            worksheet.set_column('H:H', 15) # Night Shift
            worksheet.set_column('I:I', 25) # Overtime/Undertime
            
            # Set print area to cover all written rows
            last_row = row_num -1
            worksheet.set_paper(9)
            worksheet.set_page_view(view=2)
            worksheet.print_area(0, 0, last_row, len(headers) - 1) 
            worksheet.fit_to_pages(1, 1)
            utils.style_xlsxwriter_worksheet(
                workbook, worksheet, last_row, len(headers) - 1, autofit=False
            )
        
        # --- Write data to Excel file ---
        
        if not payroll_results:
            # If no data, create a single sheet and show a message
            worksheet = workbook.add_worksheet('گزارش حقوق')
            worksheet.right_to_left()
            worksheet.write(0, 0, "برای این بازه زمانی اطلاعاتی یافت نشد.", utils.excel_add_format(workbook, {'border': 1}))
            utils.style_xlsxwriter_worksheet(workbook, worksheet, 0, 0, autofit=False)
        else:
            
            # 1. Create Detail Sheet for each employee
            for result in payroll_results:
                # Sanitize sheet name to avoid illegal characters
                sheet_name = result['name'].replace('[', '').replace(']', '').replace('*', '').replace('?', '').replace('/', '').replace('\\', '').replace(':', '')[:31]
                worksheet = workbook.add_worksheet(sheet_name)
                worksheet.right_to_left()
                holiday_list = [h.strip() for h in holidays_input.split(',')] if holidays_input else []
                write_employee_sheet(worksheet, result, start_date_fa, end_date_fa, holiday_list)

            # 2. Create Summary Sheet (New Tabular Structure)
            summary_worksheet = workbook.add_worksheet('گزارش کلی')
            summary_worksheet.right_to_left()
            
            # Define headers for the Tabular Summary Sheet
            summary_headers = [
                'نام خانوادگی', 
                'مجموع روزکارکرد (روز)', 
                'تعداد روزهای تعطیل محاسبه شده (روز)', 
                'کل روز کارکرد (پایه مرخصی) (روز)',
                'کل روز کارکرد (با مرخصی) (روز)', # New Header
                'تعداد روزهای ذخیره مرخصی (روز)', 
                'مجموع اضافه‌کاری و کسری کار (ساعت)',
                'شبکاری (شب)',
                'جمعه کاری (نوبت)',
                'تعطیل کاری (نوبت)',
                'مجموع مرخصی‌ها (روز)',
                'حقوق کل (ریال)'
            ]
            
            # Write Summary Headers (Row 0)
            for col_num, header in enumerate(summary_headers):
                summary_worksheet.write(0, col_num, header, header_format)

            # Write data for each employee (starting Row 1)
            summary_row = 1
            for result in payroll_results:
                col = 0
                
                # 1. نام خانوادگی
                summary_worksheet.write(summary_row, col, result['name'], default_format); col += 1
                
                # 2. مجموع روزکارکرد
                summary_worksheet.write(summary_row, col, result['total_working_days'], default_format); col += 1
                
                # 3. تعداد روزهای تعطیل محاسبه شده (جبرانی)
                summary_worksheet.write(summary_row, col, result['total_compensable_days'], default_format); col += 1
                
                # 4. کل روز کارکرد (پایه مرخصی)
                summary_worksheet.write(summary_row, col, result['total_working_days_base'], default_format); col += 1
                
                # 5. کل روز کارکرد (با مرخصی) - New Field
                summary_worksheet.write(summary_row, col, result['total_working_days_with_leave'], float_format); col += 1
                
                # 6. تعداد روزهای ذخیره مرخصی
                summary_worksheet.write(summary_row, col, result['leave_bank'], float_format); col += 1
                
                # 7. مجموع اضافه‌کاری و کسری کار (ساعت)
                total_overtime_undertime_seconds = result['total_overtime_undertime'].total_seconds()
                overtime_hours = total_overtime_undertime_seconds / 3600
                summary_worksheet.write(summary_row, col, overtime_hours, float_format); col += 1
                
                # 8. شبکاری (شب)
                summary_worksheet.write(summary_row, col, result['total_night_shift_shifts'], float_format); col += 1
                
                # 9. جمعه کاری (نوبت/روز)
                summary_worksheet.write(summary_row, col, result['total_friday_logs'], default_format); col += 1
                
                # 10. تعطیل کاری (نوبت/روز)
                summary_worksheet.write(summary_row, col, result['total_holiday_logs'], default_format); col += 1
                
                # 11. مجموع مرخصی‌ها
                summary_worksheet.write(summary_row, col, result['total_leave_days'], default_format); col += 1
                
                # 12. حقوق کل
                summary_worksheet.write(summary_row, col, result['salary_amount'], number_format); col += 1
                
                summary_row += 1
            
            utils.style_xlsxwriter_worksheet(
                workbook, summary_worksheet, summary_row - 1, len(summary_headers) - 1
            )
            summary_worksheet.set_paper(9)
            summary_worksheet.set_page_view(view=2)
            summary_worksheet.print_area(0, 0, summary_row - 1, len(summary_headers) - 1)
            
        workbook.close()
        output.seek(0)
        
        response = Response(output.read(), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response.headers['Content-Disposition'] = f'attachment; filename=payroll_report_{start_date_fa.replace("/", "-")}_to_{end_date_fa.replace("/", "-")}.xlsx'
        
        utils.log_action(session['user']['username'], 'خروجی گرفتن از گزارش حقوق', f'برای کارمند: {employee_id_filter}')
        return response
