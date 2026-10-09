from flask import render_template, request, redirect, url_for, flash, jsonify, send_file

from datetime import datetime

import io
import re

import utils
import auth


# =========================================================
# ابزارهای عمومی
# =========================================================

WBS_UNITS = [
    'm³',
    'm²',
    'عدد',
    'kg',
    'm³/kg',
    'm²/kg',
    'm',
    'ساعت',
    'روز',
    'تن',
    'لیتر',
    'دستگاه',
    'سایر'
]


def _num(value):
    if value is None or value == '':
        return 0.0

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().translate(
        str.maketrans(
            '۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩',
            '01234567890123456789'
        )
    )

    text = (
        text
        .replace(',', '')
        .replace('٬', '')
        .replace('٫', '.')
        .replace(' ', '')
    )

    try:
        return float(text)
    except ValueError:
        return 0.0


def _wbs_parts(code):
    """
    تبدیل کد WBS به لیست اعداد.

    مثال:
        1       -> [1]
        1.2     -> [1, 2]
        1.2.10  -> [1, 2, 10]
    """

    code = str(code or '').strip()

    if not code:
        return ()

    code = code.replace('/', '.')

    parts = []

    for part in code.split('.'):
        try:
            parts.append(int(part))
        except (TypeError, ValueError):
            parts.append(999999)

    return tuple(parts)


def _wbs_sort_key(code):
    """
    ترتیب واقعی WBS.

    مثلاً:

        1
        1.1
        1.1.1
        1.1.2
        1.2
        1.10
        2

    و نه ترتیب متنی:

        1
        1.1
        1.10
        1.2
    """

    parts = _wbs_parts(code)

    # کد کوتاه‌تر قبل از زیرمجموعه خودش
    return tuple(parts) + (-1,)


def _is_valid_wbs_code(code):
    """
    کد مجاز WBS:

        1
        1.1
        1.1.1
        10.2.15
    """

    code = str(code or '').strip()

    return bool(
        re.fullmatch(
            r'\d+(?:\.\d+)*',
            code
        )
    )


def _is_descendant(code, parent_code):
    """
    بررسی می‌کند آیا code زیرمجموعه parent_code است یا نه.

    1.2.3 زیرمجموعه 1.2 است.
    1.20 زیرمجموعه 1.2 نیست.
    """

    code = str(code or '').strip()
    parent_code = str(parent_code or '').strip()

    return code.startswith(parent_code + '.')


def _replace_wbs_prefix(code, old_prefix, new_prefix):
    """
    تغییر پیشوند WBS.

    مثال:

        old = 1.2
        new = 1.3

        1.2     -> 1.3
        1.2.1   -> 1.3.1
        1.2.5.2 -> 1.3.5.2
    """

    code = str(code or '')

    if code == old_prefix:
        return new_prefix

    if code.startswith(old_prefix + '.'):
        return new_prefix + code[len(old_prefix):]

    return code


# =========================================================
# مرتب‌سازی کل WBS و اصلاح sort_order
# =========================================================

def _rebuild_wbs_sort_order(conn):
    """
    sort_order تمام آیتم‌های WBS را بر اساس خود کد WBS
    دوباره می‌سازد.
    """

    rows = conn.execute(
        'SELECT id, wbs_code FROM project_wbs'
    ).fetchall()

    rows = sorted(
        rows,
        key=lambda r: (
            _wbs_sort_key(r['wbs_code']),
            r['id']
        )
    )

    for order, row in enumerate(rows, start=1):
        conn.execute(
            '''
            UPDATE project_wbs
            SET sort_order=?
            WHERE id=?
            ''',
            (
                order,
                row['id']
            )
        )


# =========================================================
# انتقال یک کد و تمام زیرمجموعه‌های آن
# =========================================================

def _move_wbs_branch(conn, old_code, new_code):
    """
    old_code و تمام فرزندانش را به new_code منتقل می‌کند.

    علاوه بر project_wbs، تمام wbs_code های petty_cash
    نیز تغییر می‌کنند.

    برای جلوگیری از برخورد کدها ابتدا از کد موقت استفاده می‌شود.
    """

    old_code = str(old_code or '').strip()
    new_code = str(new_code or '').strip()

    if not old_code or not new_code:
        return

    if old_code == new_code:
        return

    rows = conn.execute(
        '''
        SELECT id, wbs_code
        FROM project_wbs
        WHERE wbs_code=?
           OR wbs_code LIKE ?
        ORDER BY LENGTH(wbs_code) DESC
        ''',
        (
            old_code,
            old_code + '.%'
        )
    ).fetchall()

    if not rows:
        return

    now = datetime.now().isoformat(
        sep=' ',
        timespec='seconds'
    )

    # -----------------------------------------------------
    # مرحله 1: ایجاد کد موقت برای project_wbs
    # -----------------------------------------------------

    temporary = []

    for row in rows:
        record_id = row['id']
        old_branch_code = row['wbs_code']

        temp_code = (
            f'__WBS_TMP_{record_id}_'
            f'{int(datetime.now().timestamp() * 1000000)}__'
        )

        temporary.append(
            (
                record_id,
                old_branch_code,
                temp_code
            )
        )

        conn.execute(
            '''
            UPDATE project_wbs
            SET
                wbs_code=?,
                updated_at=?
            WHERE id=?
            ''',
            (
                temp_code,
                now,
                record_id
            )
        )

        # petty_cash نیز موقتاً جابه‌جا می‌شود
        conn.execute(
            '''
            UPDATE petty_cash
            SET wbs_code=?
            WHERE wbs_code=?
            ''',
            (
                temp_code,
                old_branch_code
            )
        )

    # -----------------------------------------------------
    # مرحله 2: کد نهایی
    # -----------------------------------------------------

    for record_id, old_branch_code, temp_code in temporary:

        final_code = _replace_wbs_prefix(
            old_branch_code,
            old_code,
            new_code
        )

        conn.execute(
            '''
            UPDATE project_wbs
            SET
                wbs_code=?,
                updated_at=?
            WHERE id=?
            ''',
            (
                final_code,
                now,
                record_id
            )
        )

        conn.execute(
            '''
            UPDATE petty_cash
            SET wbs_code=?
            WHERE wbs_code=?
            ''',
            (
                final_code,
                temp_code
            )
        )


# =========================================================
# بازشماری کامل WBS بدون فاصله
# =========================================================

def _normalize_all_wbs_codes(conn):
    """
    تمام شماره‌های WBS را در هر سطح بدون فاصله مرتب می‌کند.

    نکته مهم:
    شماره‌های سطح 1 تغییر نمی‌کنند.

    مثال:

        1
        1.1
        1.3
        1.3.1
        1.3.2
        1.7
        1.7.1
        2
        2.4

    تبدیل می‌شود به:

        1
        1.1
        1.2
        1.2.1
        1.2.2
        1.3
        1.3.1
        2
        2.1

    یعنی:

        1.3 -> 1.2
        1.7 -> 1.3
        2.4 -> 2.1

    و تمام زیرشاخه‌ها نیز همراه والد خود جابه‌جا می‌شوند.

    شماره‌های سطح 1 مانند 1، 2، 3، ... عمداً حفظ می‌شوند
    تا نام‌های STANDARD_GROUP_NAMES تغییر نکنند.

    همچنین wbs_code تمام رکوردهای petty_cash
    همراه با WBS اصلاح می‌شود.
    """

    rows = conn.execute(
        '''
        SELECT id, wbs_code
        FROM project_wbs
        ORDER BY id
        '''
    ).fetchall()

    if not rows:
        return

    old_codes = []

    for row in rows:
        code = str(
            row['wbs_code'] or ''
        ).strip()

        if code:
            old_codes.append(code)

    if not old_codes:
        return

    # -----------------------------------------------------
    # تمام prefix های واقعی و مجازی WBS
    # -----------------------------------------------------

    all_prefixes = set()

    for code in old_codes:
        parts = _wbs_parts(code)

        if not parts:
            continue

        for level in range(1, len(parts) + 1):
            prefix = '.'.join(
                str(x)
                for x in parts[:level]
            )

            all_prefixes.add(prefix)

    # -----------------------------------------------------
    # برای هر parent، فرزندان مستقیم را پیدا می‌کنیم
    # -----------------------------------------------------

    children_by_parent = {}

    for prefix in all_prefixes:
        parts = _wbs_parts(prefix)

        if not parts:
            continue

        if len(parts) == 1:
            parent = ''
            child_number = parts[0]
        else:
            parent = '.'.join(
                str(x)
                for x in parts[:-1]
            )

            child_number = parts[-1]

        children_by_parent.setdefault(
            parent,
            set()
        ).add(child_number)

    # -----------------------------------------------------
    # نگاشت شماره‌های هر parent
    # -----------------------------------------------------

    sibling_mapping = {}

    for parent, children in children_by_parent.items():

        sorted_children = sorted(
            children
        )

        mapping = {}

        for index, old_number in enumerate(
            sorted_children,
            start=1
        ):
            mapping[old_number] = index

        sibling_mapping[parent] = mapping

    # -----------------------------------------------------
    # ساخت mapping قدیم -> جدید
    # -----------------------------------------------------

    normalized_cache = {}

    def normalize_code(old_code):
        old_code = str(
            old_code or ''
        ).strip()

        if old_code in normalized_cache:
            return normalized_cache[old_code]

        parts = _wbs_parts(old_code)

        if not parts:
            return old_code

        # -------------------------------------------------
        # سطح 1 هرگز تغییر نمی‌کند
        # -------------------------------------------------

        if len(parts) == 1:
            new_code = str(parts[0])

            normalized_cache[
                old_code
            ] = new_code

            return new_code

        # -------------------------------------------------
        # ابتدا parent را normalize می‌کنیم
        # -------------------------------------------------

        old_parent = '.'.join(
            str(x)
            for x in parts[:-1]
        )

        new_parent = normalize_code(
            old_parent
        )

        # -------------------------------------------------
        # شماره جدید فرزند
        # -------------------------------------------------

        mapping = sibling_mapping.get(
            old_parent,
            {}
        )

        old_number = parts[-1]

        new_number = mapping.get(
            old_number,
            old_number
        )

        new_code = (
            f'{new_parent}.{new_number}'
        )

        normalized_cache[
            old_code
        ] = new_code

        return new_code

    code_mapping = {}

    for old_code in old_codes:
        code_mapping[old_code] = normalize_code(
            old_code
        )

    # -----------------------------------------------------
    # اگر هیچ تغییری لازم نیست فقط sort_order اصلاح شود
    # -----------------------------------------------------

    if all(
        old_code == new_code
        for old_code, new_code
        in code_mapping.items()
    ):
        _rebuild_wbs_sort_order(conn)
        return

    now = datetime.now().isoformat(
        sep=' ',
        timespec='seconds'
    )

    # -----------------------------------------------------
    # مرحله اول:
    # همه WBSها به کدهای موقت منتقل می‌شوند
    # -----------------------------------------------------

    temporary = []

    for index, row in enumerate(
        rows,
        start=1
    ):

        old_code = str(
            row['wbs_code'] or ''
        ).strip()

        if old_code not in code_mapping:
            continue

        temp_code = (
            f'__WBS_NORMALIZE_TMP__'
            f'{row["id"]}__'
            f'{index}__'
        )

        temporary.append(
            (
                row['id'],
                old_code,
                temp_code
            )
        )

        conn.execute(
            '''
            UPDATE project_wbs
            SET
                wbs_code=?,
                updated_at=?
            WHERE id=?
            ''',
            (
                temp_code,
                now,
                row['id']
            )
        )

        # هزینه‌ها هم همراه WBS به کد موقت منتقل می‌شوند
        conn.execute(
            '''
            UPDATE petty_cash
            SET wbs_code=?
            WHERE wbs_code=?
            ''',
            (
                temp_code,
                old_code
            )
        )

    # -----------------------------------------------------
    # مرحله دوم:
    # کدهای نهایی اعمال می‌شوند
    # -----------------------------------------------------

    for record_id, old_code, temp_code in temporary:

        new_code = code_mapping.get(
            old_code,
            old_code
        )

        conn.execute(
            '''
            UPDATE project_wbs
            SET
                wbs_code=?,
                updated_at=?
            WHERE id=?
            ''',
            (
                new_code,
                now,
                record_id
            )
        )

        conn.execute(
            '''
            UPDATE petty_cash
            SET wbs_code=?
            WHERE wbs_code=?
            ''',
            (
                new_code,
                temp_code
            )
        )

    # -----------------------------------------------------
    # بازسازی ترتیب نمایش
    # -----------------------------------------------------

    _rebuild_wbs_sort_order(conn)


# =========================================================
# انتقال شماره‌های بعد از حذف
# =========================================================

def _shift_sibling_branches_after_delete(conn, deleted_code):
    """
    برای سازگاری با نسخه‌های قبلی نگه داشته شده است.

    در نسخه جدید، بعد از حذف از
    _normalize_all_wbs_codes()
    استفاده می‌شود تا تمام Gap ها به‌صورت کامل اصلاح شوند.

    این تابع در صورت استفاده مستقیم، همچنان شماره‌های بعدی
    را یک واحد کاهش می‌دهد.
    """

    deleted_code = str(
        deleted_code or ''
    ).strip()

    parts = _wbs_parts(
        deleted_code
    )

    if not parts:
        return

    deleted_number = parts[-1]

    parent_prefix = '.'.join(
        str(x)
        for x in parts[:-1]
    )

    if parent_prefix:
        prefix = parent_prefix + '.'

        rows = conn.execute(
            '''
            SELECT id, wbs_code
            FROM project_wbs
            WHERE wbs_code LIKE ?
            ''',
            (
                prefix + '%',
            )
        ).fetchall()
    else:
        rows = conn.execute(
            '''
            SELECT id, wbs_code
            FROM project_wbs
            '''
        ).fetchall()

    candidates = []

    for row in rows:

        code = str(
            row['wbs_code']
        )

        row_parts = _wbs_parts(
            code
        )

        if len(row_parts) != len(parts):
            continue

        if row_parts[:-1] != parts[:-1]:
            continue

        if row_parts[-1] > deleted_number:
            candidates.append(row)

    candidates.sort(
        key=lambda r: _wbs_parts(
            r['wbs_code']
        ),
        reverse=True
    )

    for row in candidates:

        old_code = row['wbs_code']

        old_parts = list(
            _wbs_parts(old_code)
        )

        old_parts[-1] -= 1

        new_code = '.'.join(
            str(x)
            for x in old_parts
        )

        _move_wbs_branch(
            conn,
            old_code,
            new_code
        )


# =========================================================
# محاسبه Coverage
# =========================================================

def _coverage(records_by_code, code):
    return min(
        100.0,
        sum(
            _num(
                r['wbs_coverage_percent']
            )
            for r in records_by_code.get(
                code,
                []
            )
        )
    )


# =========================================================
# نام استاندارد بخش‌های اصلی
# =========================================================

STANDARD_GROUP_NAMES = {
    '1': 'گودبرداری و آماده‌سازی',
    '2': 'فونداسیون',
    '3': 'اسکلت فلزی',
    '4': 'سقف',
    '5': 'سفت‌کاری',
    '6': 'آسانسور',
    '7': 'تأسیسات مکانیکی',
    '8': 'تأسیسات برقی',
    '9': 'عایق‌کاری',
    '10': 'کف‌سازی',
    '11': 'نازک‌کاری',
    '12': 'نما',
    '13': 'درب و پنجره',
    '14': 'رنگ‌آمیزی',
    '15': 'تجهیزات داخلی',
    '16': 'انباری و مشاعات',
    '17': 'محوطه‌سازی',
    '18': 'تست، رفع نقص و تحویل'
}


# =========================================================
# درج یک WBS جدید
# =========================================================

def _insert_wbs_with_shift(
    conn,
    code,
    activity,
    unit,
    total_quantity,
    weight,
    notes
):
    """
    درج WBS جدید.

    رفتار جدید:

    1. اگر کد جدید آزاد باشد:
       هیچ آیتم موجودی صرفاً به دلیل شماره جدید جابه‌جا نمی‌شود.

       مثال:

           1.1
           1.3

       اضافه کردن 1.2 باعث نمی‌شود 1.3 -> 1.4 شود.

       در پایان normalize انجام شده و نتیجه:

           1.1
           1.2
           1.3

    2. اگر دقیقاً همان کد قبلاً وجود داشته باشد:
       برای جلوگیری از تداخل، آیتم موجود و زیرشاخه‌هایش
       یک شماره جلو می‌روند.

    3. بعد از درج، کل WBS بدون Gap بازشماری می‌شود.
    """

    code = str(
        code or ''
    ).strip()

    parts = _wbs_parts(code)

    if not parts:
        raise ValueError(
            'کد WBS معتبر نیست.'
        )

    if not _is_valid_wbs_code(code):
        raise ValueError(
            'کد WBS معتبر نیست.'
        )

    # -----------------------------------------------------
    # بررسی وجود دقیق همین کد
    # -----------------------------------------------------

    existing = conn.execute(
        '''
        SELECT id
        FROM project_wbs
        WHERE wbs_code=?
        ''',
        (code,)
    ).fetchone()

    # -----------------------------------------------------
    # فقط در صورت برخورد واقعی، شاخه موجود را جلو می‌بریم
    # -----------------------------------------------------

    if existing:

        parent_prefix = '.'.join(
            str(x)
            for x in parts[:-1]
        )

        new_number = parts[-1]

        if parent_prefix:

            prefix = parent_prefix + '.'

            rows = conn.execute(
                '''
                SELECT id, wbs_code
                FROM project_wbs
                WHERE wbs_code LIKE ?
                ''',
                (
                    prefix + '%',
                )
            ).fetchall()

        else:

            rows = conn.execute(
                '''
                SELECT id, wbs_code
                FROM project_wbs
                '''
            ).fetchall()

        candidates = []

        for row in rows:

            row_code = str(
                row['wbs_code']
            )

            row_parts = _wbs_parts(
                row_code
            )

            if len(row_parts) != len(parts):
                continue

            if row_parts[:-1] != parts[:-1]:
                continue

            if row_parts[-1] >= new_number:
                candidates.append(row)

        candidates.sort(
            key=lambda r: _wbs_parts(
                r['wbs_code']
            ),
            reverse=True
        )

        for row in candidates:

            old_code = row['wbs_code']

            old_parts = list(
                _wbs_parts(old_code)
            )

            old_parts[-1] += 1

            shifted_code = '.'.join(
                str(x)
                for x in old_parts
            )

            _move_wbs_branch(
                conn,
                old_code,
                shifted_code
            )

    # -----------------------------------------------------
    # درج آیتم جدید
    # -----------------------------------------------------

    now = datetime.now().isoformat(
        sep=' ',
        timespec='seconds'
    )

    conn.execute(
        '''
        INSERT INTO project_wbs
        (
            wbs_code,
            activity,
            unit,
            total_quantity,
            weight_percent,
            notes,
            sort_order,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            code,
            activity,
            unit,
            total_quantity,
            weight,
            notes,
            0,
            now,
            now
        )
    )


# =========================================================
# محاسبه اطلاعات پیشرفت
# =========================================================

def _progress_data(conn, exclude_petty_cash_id=None):

    wbs = conn.execute(
        '''
        SELECT *
        FROM project_wbs
        ORDER BY sort_order, id
        '''
    ).fetchall()

    expense_query = '''
        SELECT
            pw.wbs_code,
            pw.wbs_coverage_percent,
            pc.id AS expense_id,
            pc.date,
            pc.description,
            pc.unit,
            pc.amount,
            pc.unit_price,
            pc.discount,
            pc.total_amount,
            pc.location,
            pc.notes,
            pc.source,
            pc.invoice_number,
            pc.settlement_status,
            pc.payer,
            pc.personal_manager_payment,
            pc.exclude_manager_calculation,
            pc.receipt_image_path
        FROM petty_cash_wbs AS pw
        JOIN petty_cash AS pc ON pc.id = pw.petty_cash_id
        WHERE pw.wbs_code IS NOT NULL
          AND pw.wbs_code != ""
    '''
    expense_params = []
    if exclude_petty_cash_id is not None:
        expense_query += ' AND pw.petty_cash_id != ?'
        expense_params.append(exclude_petty_cash_id)
    expenses = conn.execute(expense_query, expense_params).fetchall()

    grouped = {}

    for row in expenses:

        grouped.setdefault(
            str(row['wbs_code']),
            []
        ).append(row)

    items = []
    groups = {}

    for row in wbs:

        code = row['wbs_code']

        weight = _num(
            row['weight_percent']
        )

        coverage = _coverage(
            grouped,
            code
        )

        weighted = (
            weight * coverage / 100.0
        )

        linked_expenses = []
        for expense in grouped.get(str(code), []):
            linked_expenses.append({
                'id': expense['expense_id'],
                'date': expense['date'] or '',
                'description': expense['description'] or '',
                'unit': expense['unit'] or '',
                'amount': _num(expense['amount']),
                'unit_price': _num(expense['unit_price']),
                'discount': expense['discount'] or '',
                'total_amount': _num(expense['total_amount']),
                'location': expense['location'] or '',
                'notes': expense['notes'] or '',
                'source': expense['source'] or '',
                'invoice_number': expense['invoice_number'] or '',
                'settlement_status': expense['settlement_status'] or '',
                'payer': expense['payer'] or '',
                'personal_manager_payment': bool(expense['personal_manager_payment']),
                'exclude_manager_calculation': bool(expense['exclude_manager_calculation']),
                'receipt_image_path': expense['receipt_image_path'] or '',
                'wbs_coverage_percent': _num(expense['wbs_coverage_percent'])
            })

        item = {
            'id': row['id'],
            'wbs_code': code,
            'activity': row['activity'],
            'unit': row['unit'] or '',
            'total_quantity': _num(
                row['total_quantity']
            ),
            'weight_percent': weight,
            'coverage_percent': coverage,
            'weighted_progress': weighted,
            'notes': row['notes'] or '',
            'linked_expenses': linked_expenses
        }

        items.append(item)

        top = (
            code.split('.')[0]
            if '.' in code
            else code.split('/')[0]
        )

        group = groups.setdefault(
            top,
            {
                'code': top,
                'name': STANDARD_GROUP_NAMES.get(
                    top,
                    f'بخش {top}'
                ),
                'weight': 0.0,
                'weighted_progress': 0.0,
                'items': 0
            }
        )

        group['weight'] += weight
        group['weighted_progress'] += weighted
        group['items'] += 1

    total_weight = sum(
        i['weight_percent']
        for i in items
    )

    total_progress = sum(
        i['weighted_progress']
        for i in items
    )

    return {
        'overall': {
            'progress': total_progress,
            'weight': total_weight,
            'items': len(items)
        },
        'items': items,
        'groups': sorted(
            groups.values(),
            key=lambda x: (
                float(x['code'])
                if x['code'].replace('.', '', 1).isdigit()
                else 9999,
                x['code']
            )
        )
    }


# =========================================================
# Excel header
# =========================================================

def _header_map(headers):

    aliases = {
        'code': [
            'کد wbs',
            'کد',
            'wbs code',
            'wbs'
        ],
        'activity': [
            'شرح فعالیت',
            'شرح',
            'فعالیت',
            'activity'
        ],
        'unit': [
            'واحد',
            'unit'
        ],
        'total_quantity': [
            'مقدار کل',
            'متره کل',
            'مقدار',
            'total quantity'
        ],
        'weight_percent': [
            'وزن فعالیت (%)',
            'وزن فعالیت',
            'درصد وزنی',
            'درصد وزن',
            'weight'
        ],
        'notes': [
            'توضیحات',
            'توضیح',
            'notes'
        ]
    }

    normalized = {}

    for idx, h in enumerate(headers):

        if h is None:
            continue

        key = (
            str(h)
            .strip()
            .lower()
            .replace('٪', '%')
        )

        normalized[key] = idx

    result = {}

    for name, vals in aliases.items():

        for v in vals:

            if v.lower() in normalized:

                result[name] = normalized[
                    v.lower()
                ]

                break

    return result


# =========================================================
# Excel template
# =========================================================

def _create_wbs_template():

    import openpyxl

    from openpyxl.styles import (
        Font,
        Alignment,
        PatternFill,
        Border,
        Side
    )

    from openpyxl.worksheet.datavalidation import (
        DataValidation
    )

    wb = openpyxl.Workbook()

    ws = wb.active

    ws.title = 'ساختار شکست'

    headers = [
        'سطح',
        'کد WBS',
        'شرح فعالیت',
        'واحد',
        'مقدار کل',
        'وزن فعالیت (%)',
        'توضیحات'
    ]

    ws.append(headers)

    sample_rows = [
        [
            1,
            '1',
            'عملیات خاکی و آماده‌سازی',
            '',
            '',
            8,
            'سرفصل اصلی'
        ],
        [
            2,
            '1.1',
            'گودبرداری',
            '',
            '',
            3,
            ''
        ],
        [
            3,
            '1.1.1',
            'پیاده‌سازی محدوده گود',
            'm²',
            100,
            0.5,
            ''
        ],
        [
            3,
            '1.1.2',
            'گودبرداری',
            'm³',
            500,
            2,
            ''
        ],
        [
            3,
            '1.1.3',
            'بارگیری و حمل خاک',
            'm³',
            500,
            0.5,
            ''
        ],
        [
            2,
            '1.2',
            'پایدارسازی گود',
            '',
            '',
            3,
            ''
        ],
        [
            3,
            '1.2.1',
            'اجرای سازه نگهبان',
            'm²',
            200,
            2,
            ''
        ],
        [
            3,
            '1.2.2',
            'مهاربندی گود',
            'm²',
            100,
            1,
            ''
        ],
        [
            1,
            '2',
            'اجرای فونداسیون',
            '',
            '',
            15,
            'سرفصل اصلی'
        ],
        [
            2,
            '2.1',
            'قالب‌بندی فونداسیون',
            '',
            '',
            3,
            ''
        ],
        [
            3,
            '2.1.1',
            'قالب‌بندی',
            'm²',
            300,
            3,
            ''
        ],
        [
            2,
            '2.2',
            'آرماتوربندی فونداسیون',
            '',
            '',
            7,
            ''
        ],
        [
            3,
            '2.2.1',
            'آرماتور پایین',
            'kg',
            5000,
            3,
            ''
        ],
        [
            3,
            '2.2.2',
            'آرماتور بالا',
            'kg',
            4000,
            3,
            ''
        ],
        [
            3,
            '2.2.3',
            'آرماتورهای تقویتی',
            'kg',
            1500,
            1,
            ''
        ],
        [
            2,
            '2.3',
            'بتن‌ریزی فونداسیون',
            '',
            '',
            5,
            ''
        ],
        [
            3,
            '2.3.1',
            'بتن مگر',
            'm³',
            20,
            1,
            ''
        ],
        [
            3,
            '2.3.2',
            'بتن فونداسیون',
            'm³',
            100,
            4,
            ''
        ],
        [
            1,
            '3',
            'اجرای اسکلت فلزی',
            '',
            '',
            12,
            'سرفصل اصلی'
        ],
        [
            2,
            '3.1',
            'ساخت و نصب ستون‌ها',
            '',
            '',
            5,
            ''
        ],
        [
            3,
            '3.1.1',
            'ساخت ستون‌های فلزی',
            'kg',
            10000,
            2.5,
            ''
        ]
    ]

    for row in sample_rows:
        ws.append(row)

    # -----------------------------------------------------
    # ظاهر
    # -----------------------------------------------------

    header_fill = PatternFill(
        fill_type='solid',
        fgColor='1F4E78'
    )

    header_font = Font(
        bold=True,
        color='FFFFFF'
    )

    thin = Side(
        style='thin',
        color='D9E1F2'
    )

    for cell in ws[1]:

        cell.fill = header_fill
        cell.font = header_font

        cell.alignment = Alignment(
            horizontal='center',
            vertical='center'
        )

        cell.border = Border(
            left=thin,
            right=thin,
            top=thin,
            bottom=thin
        )

    # -----------------------------------------------------
    # سرفصل‌های سطح 1
    # -----------------------------------------------------

    for row in range(
        2,
        ws.max_row + 1
    ):

        level = ws.cell(
            row=row,
            column=1
        ).value

        if level == 1:

            for col in range(1, 8):

                ws.cell(
                    row=row,
                    column=col
                ).font = Font(
                    bold=True
                )

    widths = {
        'A': 10,
        'B': 15,
        'C': 40,
        'D': 18,
        'E': 18,
        'F': 20,
        'G': 30
    }

    for col, width in widths.items():
        ws.column_dimensions[col].width = width

    # -----------------------------------------------------
    # لیست واحدها
    # -----------------------------------------------------

    unit_validation = DataValidation(
        type='list',
        formula1='"' + ','.join(WBS_UNITS) + '"',
        allow_blank=True
    )

    ws.add_data_validation(
        unit_validation
    )

    unit_validation.add(
        f'D2:D{ws.max_row}'
    )

    ws.freeze_panes = 'A2'

    ws.auto_filter.ref = ws.dimensions

    for row in ws.iter_rows():

        for cell in row:

            cell.alignment = Alignment(
                horizontal='right',
                vertical='center'
            )

    utils.style_openpyxl_worksheet(ws)

    ws.row_dimensions[1].height = 25

    output = io.BytesIO()

    wb.save(output)

    output.seek(0)

    return output



def _create_current_wbs_export(conn):
    """ساخت فایل Excel از WBS فعلی پروژه."""
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'ساختار شکست'

    headers = [
        'سطح', 'کد WBS', 'شرح فعالیت', 'واحد',
        'مقدار کل', 'وزن فعالیت (%)', 'توضیحات'
    ]
    ws.append(headers)

    rows = conn.execute("""
        SELECT wbs_code, activity, unit, total_quantity,
               weight_percent, notes
        FROM project_wbs
        ORDER BY sort_order, id
    """).fetchall()

    for r in rows:
        code = str(r['wbs_code'] or '').strip()
        ws.append([
            len(_wbs_parts(code)),
            code,
            r['activity'] or '',
            r['unit'] or '',
            r['total_quantity'] if r['total_quantity'] is not None else '',
            r['weight_percent'] if r['weight_percent'] is not None else 0,
            r['notes'] or ''
        ])

    header_fill = PatternFill(fill_type='solid', fgColor='1F4E78')
    header_font = Font(bold=True, color='FFFFFF')
    thin = Side(style='thin', color='D9E1F2')

    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for row_idx in range(2, ws.max_row + 1):
        if ws.cell(row=row_idx, column=1).value == 1:
            for col in range(1, 8):
                ws.cell(row=row_idx, column=col).font = Font(bold=True)

    widths = {'A': 10, 'B': 15, 'C': 40, 'D': 18, 'E': 18, 'F': 20, 'G': 30}
    for col, width in widths.items():
        ws.column_dimensions[col].width = width

    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions

    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = Alignment(horizontal='right', vertical='center')

    utils.style_openpyxl_worksheet(ws)

    ws.row_dimensions[1].height = 25

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output


def _create_project_progress_export(conn):
    """ساخت فایل Excel از آیتم‌های WBS و درصد پیشرفت فعلی آن‌ها."""
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    data = _progress_data(conn)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'جزئیات پیشرفت'
    ws.sheet_view.rightToLeft = True

    ws.append([
        'کد WBS', 'شرح فعالیت', 'واحد', 'مقدار کل',
        'وزن فعالیت (%)', 'پیشرفت آیتم (%)', 'پیشرفت وزنی (%)', 'توضیحات'
    ])

    for item in data['items']:
        ws.append([
            item['wbs_code'] or '',
            item['activity'] or '',
            item['unit'],
            item['total_quantity'],
            item['weight_percent'],
            item['coverage_percent'],
            item['weighted_progress'],
            item['notes']
        ])

    header_fill = PatternFill(fill_type='solid', fgColor='1F4E78')
    header_font = Font(bold=True, color='FFFFFF')
    thin = Side(style='thin', color='D9E1F2')
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)

    widths = {'A': 16, 'B': 42, 'C': 16, 'D': 16, 'E': 20, 'F': 22, 'G': 22, 'H': 32}
    for column, width in widths.items():
        ws.column_dimensions[column].width = width

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(horizontal='right', vertical='center')
    for row in ws.iter_rows(min_row=2, min_col=5, max_col=7):
        for cell in row:
            cell.number_format = '0.00'

    utils.style_openpyxl_worksheet(ws)

    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions
    ws.row_dimensions[1].height = 25

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output


# =========================================================
# ROUTES
# =========================================================

def init_project_wbs_routes(app):

    # -----------------------------------------------------
    # صفحه WBS
    # -----------------------------------------------------

    @app.route('/project_wbs')
    @auth.login_required
    def project_wbs():

        if not auth.has_permission(
            'project_wbs'
        ):

            flash(
                'شما به این بخش دسترسی ندارید.',
                'error'
            )

            return redirect(
                url_for('admin_dashboard')
            )

        conn = utils.get_db_connection()

        try:

            # -------------------------------------------------
            # هر بار ورود به صفحه، Gap های احتمالی اصلاح می‌شوند
            # -------------------------------------------------

            _normalize_all_wbs_codes(
                conn
            )

            conn.commit()

            rows = conn.execute(
                '''
                SELECT *
                FROM project_wbs
                ORDER BY sort_order, id
                '''
            ).fetchall()

            data = _progress_data(
                conn
            )

        finally:

            conn.close()

        return render_template(
            'project_wbs.html',
            records=rows,
            total_weight=data['overall']['weight'],
            wbs_units=WBS_UNITS
        )

    # -----------------------------------------------------
    # دانلود template
    # -----------------------------------------------------

    @app.route(
        '/download_project_wbs_template'
    )
    @auth.login_required
    def download_project_wbs_template():

        if not auth.has_permission(
            'project_wbs'
        ):

            return redirect(
                url_for('admin_dashboard')
            )

        output = _create_wbs_template()

        return send_file(
            output,
            as_attachment=True,
            download_name='نمونه_ساختار_شکست_WBS.xlsx',
            mimetype=(
                'application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.sheet'
            )
        )

    # -----------------------------------------------------
    # خروجی Excel از WBS فعلی
    # -----------------------------------------------------

    @app.route('/download_project_wbs_current')
    @auth.login_required
    def download_project_wbs_current():

        if not auth.has_permission('project_wbs'):
            return redirect(url_for('admin_dashboard'))

        conn = utils.get_db_connection()
        try:
            exists = conn.execute('SELECT 1 FROM project_wbs LIMIT 1').fetchone()
            if not exists:
                flash('هنوز ساختار WBS فعالی برای خروجی وجود ندارد.', 'error')
                return redirect(url_for('project_wbs'))
            output = _create_current_wbs_export(conn)
        finally:
            conn.close()

        return send_file(
            output,
            as_attachment=True,
            download_name='WBS_فعلی_پروژه.xlsx',
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )

    # -----------------------------------------------------
    # خروجی Excel از جزئیات پیشرفت WBS فعلی
    # -----------------------------------------------------

    @app.route('/download_project_progress_excel')
    @auth.login_required
    def download_project_progress_excel():

        if (
            not auth.has_permission('project_progress')
            and not auth.has_permission('project_wbs')
        ):
            return redirect(url_for('admin_dashboard'))

        conn = utils.get_db_connection()
        try:
            output = _create_project_progress_export(conn)
        finally:
            conn.close()

        return send_file(
            output,
            as_attachment=True,
            download_name='گزارش_پیشرفت_WBS.xlsx',
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )

    # -----------------------------------------------------
    # افزودن دستی WBS
    # -----------------------------------------------------

    @app.route(
        '/add_project_wbs',
        methods=['POST']
    )
    @auth.login_required
    def add_project_wbs():

        if not auth.has_permission(
            'project_wbs'
        ):

            return redirect(
                url_for('admin_dashboard')
            )

        code = request.form.get(
            'wbs_code',
            ''
        ).strip()

        activity = request.form.get(
            'activity',
            ''
        ).strip()

        unit = request.form.get(
            'unit',
            ''
        ).strip()

        # -----------------------------------------------------
        # واحد فقط برای سطح 3 مجاز است
        # -----------------------------------------------------

        wbs_level = len(
            _wbs_parts(code)
        )

        if wbs_level < 3:
            unit = ''

        weight = _num(
            request.form.get(
                'weight_percent'
            )
        )

        total_quantity = _num(
            request.form.get(
                'total_quantity'
            )
        )

        notes = request.form.get(
            'notes',
            ''
        ).strip()

        # -----------------------------------------------------
        # اعتبارسنجی
        # -----------------------------------------------------

        if not code or not activity:

            flash(
                'کد WBS و شرح فعالیت الزامی است.',
                'error'
            )

            return redirect(
                url_for('project_wbs')
            )

        if not _is_valid_wbs_code(code):

            flash(
                'کد WBS معتبر نیست. مثال صحیح: 1.2.3',
                'error'
            )

            return redirect(
                url_for('project_wbs')
            )

        if weight < 0:

            flash(
                'وزن فعالیت نمی‌تواند منفی باشد.',
                'error'
            )

            return redirect(
                url_for('project_wbs')
            )

        conn = utils.get_db_connection()

        try:

            conn.execute('BEGIN')

            # -------------------------------------------------
            # درج
            # -------------------------------------------------

            _insert_wbs_with_shift(
                conn,
                code,
                activity,
                unit,
                total_quantity,
                weight,
                notes
            )

            # -------------------------------------------------
            # بسیار مهم:
            # بعد از افزودن، شماره‌ها بدون Gap می‌شوند.
            #
            # مثال:
            #
            # 1.1
            # 1.3
            #
            # افزودن 1.2
            #
            # نتیجه:
            #
            # 1.1
            # 1.2
            # 1.3
            #
            # و 1.3 به 1.4 تبدیل نمی‌شود.
            # -------------------------------------------------

            _normalize_all_wbs_codes(
                conn
            )

            conn.commit()

            flash(
                'آیتم ساختار شکست با موفقیت اضافه شد و ترتیب WBS اصلاح گردید.',
                'success'
            )

        except Exception as e:

            conn.rollback()

            flash(
                f'خطا در افزودن آیتم: {e}',
                'error'
            )

        finally:

            conn.close()

        return redirect(
            url_for('project_wbs')
        )

    # -----------------------------------------------------
    # ویرایش WBS
    # -----------------------------------------------------

    @app.route(
        '/edit_project_wbs/<int:record_id>',
        methods=['POST']
    )
    @auth.login_required
    def edit_project_wbs(record_id):

        if not auth.has_permission(
            'project_wbs'
        ):

            return redirect(
                url_for('admin_dashboard')
            )

        new_code = request.form.get(
            'wbs_code',
            ''
        ).strip()

        activity = request.form.get(
            'activity',
            ''
        ).strip()

        unit = request.form.get(
            'unit',
            ''
        ).strip()

        # واحد فقط برای آیتم نهایی (بدون فرزند) مجاز است.
        new_code_parts = _wbs_parts(new_code)

        total_quantity = _num(
            request.form.get(
                'total_quantity'
            )
        )

        weight = _num(
            request.form.get(
                'weight_percent'
            )
        )

        notes = request.form.get(
            'notes',
            ''
        ).strip()

        if (
            not new_code
            or not activity
            or weight < 0
        ):

            flash(
                'کد، شرح و وزن معتبر الزامی است.',
                'error'
            )

            return redirect(
                url_for('project_wbs')
            )

        if not _is_valid_wbs_code(
            new_code
        ):

            flash(
                'کد WBS معتبر نیست. مثال صحیح: 1.2.3',
                'error'
            )

            return redirect(
                url_for('project_wbs')
            )

        conn = utils.get_db_connection()

        try:

            conn.execute('BEGIN')

            current = conn.execute(
                '''
                SELECT *
                FROM project_wbs
                WHERE id=?
                ''',
                (record_id,)
            ).fetchone()

            if not current:

                raise ValueError(
                    'آیتم موردنظر پیدا نشد.'
                )

            old_code = str(
                current['wbs_code']
            ).strip()

            # -------------------------------------------------
            # اگر کد تغییر کرده باشد
            # -------------------------------------------------

            if old_code != new_code:

                # -------------------------------------------------
                # اگر کد جدید متعلق به رکورد دیگری است،
                # آن شاخه را یک شماره جلو می‌بریم.
                # -------------------------------------------------

                collision = conn.execute(
                    '''
                    SELECT id
                    FROM project_wbs
                    WHERE wbs_code=?
                      AND id<>?
                    ''',
                    (
                        new_code,
                        record_id
                    )
                ).fetchone()

                if collision:

                    new_parts = _wbs_parts(
                        new_code
                    )

                    old_number = new_parts[-1]

                    parent_prefix = '.'.join(
                        str(x)
                        for x in new_parts[:-1]
                    )

                    if parent_prefix:

                        prefix = (
                            parent_prefix + '.'
                        )

                        siblings = conn.execute(
                            '''
                            SELECT id, wbs_code
                            FROM project_wbs
                            WHERE wbs_code LIKE ?
                            ''',
                            (
                                prefix + '%',
                            )
                        ).fetchall()

                    else:

                        siblings = conn.execute(
                            '''
                            SELECT id, wbs_code
                            FROM project_wbs
                            '''
                        ).fetchall()

                    candidates = []

                    for sibling in siblings:

                        if sibling['id'] == record_id:
                            continue

                        sibling_parts = _wbs_parts(
                            sibling['wbs_code']
                        )

                        if (
                            len(sibling_parts)
                            != len(new_parts)
                        ):
                            continue

                        if (
                            sibling_parts[:-1]
                            != new_parts[:-1]
                        ):
                            continue

                        if (
                            sibling_parts[-1]
                            >= old_number
                        ):

                            candidates.append(
                                sibling
                            )

                    candidates.sort(
                        key=lambda x:
                            _wbs_parts(
                                x['wbs_code']
                            ),
                        reverse=True
                    )

                    for sibling in candidates:

                        old_sibling_code = sibling[
                            'wbs_code'
                        ]

                        sibling_parts = list(
                            _wbs_parts(
                                old_sibling_code
                            )
                        )

                        sibling_parts[-1] += 1

                        shifted_code = '.'.join(
                            str(x)
                            for x in sibling_parts
                        )

                        _move_wbs_branch(
                            conn,
                            old_sibling_code,
                            shifted_code
                        )

                # -------------------------------------------------
                # انتقال خود شاخه و تمام زیرشاخه‌ها
                # -------------------------------------------------

                _move_wbs_branch(
                    conn,
                    old_code,
                    new_code
                )

            # -----------------------------------------------------
            # اطلاعات خود رکورد
            # -----------------------------------------------------

            conn.execute(
                '''
                UPDATE project_wbs
                SET
                    activity=?,
                    unit=?,
                    total_quantity=?,
                    weight_percent=?,
                    notes=?,
                    updated_at=?
                WHERE id=?
                ''',
                (
                    activity,
                    unit,
                    total_quantity,
                    weight,
                    notes,
                    datetime.now().isoformat(
                        sep=' ',
                        timespec='seconds'
                    ),
                    record_id
                )
            )

            # -----------------------------------------------------
            # اصلاح Gap های WBS بعد از ویرایش
            # -----------------------------------------------------

            _normalize_all_wbs_codes(
                conn
            )

            conn.commit()

            flash(
                'آیتم ساختار شکست ویرایش شد و ترتیب WBS اصلاح گردید.',
                'success'
            )

        except Exception as e:

            conn.rollback()

            flash(
                f'خطا در ویرایش: {e}',
                'error'
            )

        finally:

            conn.close()

        return redirect(
            url_for('project_wbs')
        )

    # -----------------------------------------------------
    # ذخیره گروهی تغییرات WBS
    # -----------------------------------------------------

    @app.route('/save_selected_project_wbs', methods=['POST'])
    @auth.login_required
    def save_selected_project_wbs():

        if not auth.has_permission('project_wbs'):
            return redirect(url_for('admin_dashboard'))

        selected_ids = request.form.getlist('selected_ids')
        if not selected_ids:
            flash('حداقل یک ردیف را برای ذخیره انتخاب کنید.', 'error')
            return redirect(url_for('project_wbs'))

        conn = utils.get_db_connection()
        changed = 0
        try:
            conn.execute('BEGIN')

            for raw_id in selected_ids:
                try:
                    record_id = int(raw_id)
                except (TypeError, ValueError):
                    continue

                current = conn.execute(
                    'SELECT * FROM project_wbs WHERE id=?',
                    (record_id,)
                ).fetchone()
                if not current:
                    continue

                prefix = f'wbs_{record_id}_'
                new_code = request.form.get(prefix + 'code', '').strip()
                activity = request.form.get(prefix + 'activity', '').strip()
                unit = request.form.get(prefix + 'unit', '').strip()
                total_quantity = _num(request.form.get(prefix + 'quantity'))
                weight = _num(request.form.get(prefix + 'weight'))

                if not new_code or not activity or weight < 0 or not _is_valid_wbs_code(new_code):
                    raise ValueError(f'اطلاعات ردیف {record_id} معتبر نیست.')

                old_code = str(current['wbs_code']).strip()
                if old_code != new_code:
                    collision = conn.execute(
                        'SELECT id FROM project_wbs WHERE wbs_code=? AND id<>?',
                        (new_code, record_id)
                    ).fetchone()
                    if collision:
                        raise ValueError(f'کد WBS «{new_code}» از قبل وجود دارد. ابتدا کدها را اصلاح کنید.')
                    _move_wbs_branch(conn, old_code, new_code)

                conn.execute(
                    """
                    UPDATE project_wbs
                    SET activity=?, unit=?, total_quantity=?, weight_percent=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        activity, unit, total_quantity, weight,
                        datetime.now().isoformat(sep=' ', timespec='seconds'),
                        record_id
                    )
                )
                changed += 1

            # هر رکوردی که فرزند دارد، سرفصل است و واحد/مقدار ندارد.
            rows_now = conn.execute('SELECT id, wbs_code FROM project_wbs').fetchall()
            codes_now = [str(r['wbs_code']).strip() for r in rows_now]
            for r in rows_now:
                code_now = str(r['wbs_code']).strip()
                if any(c.startswith(code_now + '.') for c in codes_now):
                    conn.execute(
                        "UPDATE project_wbs SET unit='', total_quantity=0 WHERE id=?",
                        (r['id'],)
                    )

            _normalize_all_wbs_codes(conn)
            _rebuild_wbs_sort_order(conn)
            conn.commit()
            flash(f'{changed} ردیف با موفقیت ذخیره شد.', 'success')

        except Exception as e:
            conn.rollback()
            flash(f'خطا در ذخیره گروهی: {e}', 'error')
        finally:
            conn.close()

        return redirect(url_for('project_wbs'))

    # -----------------------------------------------------
    # حذف
    # -----------------------------------------------------

    @app.route(
        '/delete_project_wbs/<int:record_id>',
        methods=['POST']
    )
    @auth.login_required
    def delete_project_wbs(record_id):

        if not auth.has_permission(
            'project_wbs'
        ):

            return redirect(
                url_for('admin_dashboard')
            )

        # -------------------------------------------------
        # آیا کاربر بعد از مشاهده هشدار، حذف را تأیید کرده؟
        # -------------------------------------------------

        confirm_delete = (
            request.form.get(
                'confirm_delete',
                ''
            ).strip().lower()
            in (
                '1',
                'true',
                'yes',
                'on'
            )
        )

        conn = utils.get_db_connection()

        try:

            row = conn.execute(
                '''
                SELECT *
                FROM project_wbs
                WHERE id=?
                ''',
                (record_id,)
            ).fetchone()

            if not row:

                flash(
                    'آیتم موردنظر پیدا نشد.',
                    'error'
                )

                return redirect(
                    url_for('project_wbs')
                )

            wbs_code = str(
                row['wbs_code']
            ).strip()

            activity = (
                row['activity']
                or ''
            )

            # -------------------------------------------------
            # بررسی هزینه‌های متصل به این WBS
            # -------------------------------------------------

            linked_rows = conn.execute(
                '''
                SELECT
                    pc.id,
                    pc.date,
                    pc.description,
                    pc.amount,
                    pc.unit_price,
                    pc.total_amount,
                    COALESCE(pw.wbs_coverage_percent, pc.wbs_coverage_percent) AS wbs_coverage_percent
                FROM petty_cash AS pc
                LEFT JOIN petty_cash_wbs AS pw
                    ON pw.petty_cash_id = pc.id AND pw.wbs_code = ?
                WHERE pw.id IS NOT NULL OR (pw.id IS NULL AND pc.wbs_code = ?)
                ORDER BY pc.id
                ''',
                (wbs_code, wbs_code)
            ).fetchall()

            linked_count = len(
                linked_rows
            )

            # -------------------------------------------------
            # اگر هزینه وجود دارد ولی تأیید نشده:
            # فقط هشدار بده و حذف نکن
            # -------------------------------------------------

            if linked_count and not confirm_delete:

                total_amount = 0.0

                coverage_total = 0.0

                for item in linked_rows:

                    total_amount += _num(
                        item['total_amount']
                        if item['total_amount'] is not None
                        else item['amount']
                    )

                    coverage_total += _num(
                        item[
                            'wbs_coverage_percent'
                        ]
                    )

                flash(
                    (
                        f'برای آیتم «{wbs_code} - {activity}» '
                        f'{linked_count} رکورد هزینه ثبت شده است. '
                        f'مبلغ مجموع این رکوردها '
                        f'{total_amount:,.0f} ریال است. '
                        f'در صورت تأیید، فقط آیتم WBS حذف می‌شود '
                        f'و هیچ‌یک از هزینه‌های ثبت‌شده حذف نخواهد شد.'
                    ),
                    'warning'
                )

                return redirect(
                    url_for('project_wbs')
                )

            # -------------------------------------------------
            # حذف قطعی
            # -------------------------------------------------

            conn.execute('BEGIN')

            # -------------------------------------------------
            # فقط خود WBS حذف می‌شود.
            #
            # petty_cash عمداً حذف نمی‌شود.
            # -------------------------------------------------

            conn.execute(
                '''
                DELETE FROM project_wbs
                WHERE id=?
                ''',
                (record_id,)
            )

            # -------------------------------------------------
            # بسیار مهم:
            #
            # دیگر از shift ساده استفاده نمی‌کنیم.
            #
            # کل ساختار WBS دوباره شماره‌گذاری می‌شود.
            #
            # مثال:
            #
            # 1.1
            # 1.3
            # 1.4
            #
            # بعد از حذف/اصلاح:
            #
            # 1.1
            # 1.2
            # 1.3
            #
            # و petty_cash نیز همراه کدها اصلاح می‌شود.
            # -------------------------------------------------

            _normalize_all_wbs_codes(
                conn
            )

            _rebuild_wbs_sort_order(
                conn
            )

            conn.commit()

            if linked_count:

                flash(
                    (
                        f'آیتم «{wbs_code} - {activity}» حذف شد. '
                        f'{linked_count} رکورد هزینه مرتبط با آن '
                        f'حذف نشده و همچنان در سیستم باقی مانده است.'
                    ),
                    'success'
                )

            else:

                flash(
                    (
                        f'آیتم «{wbs_code} - {activity}» حذف شد '
                        f'و ترتیب شماره‌های WBS اصلاح گردید.'
                    ),
                    'success'
                )

        except Exception as e:

            conn.rollback()

            flash(
                f'خطا در حذف آیتم: {e}',
                'error'
            )

        finally:

            conn.close()

        return redirect(
            url_for('project_wbs')
        )

    # -----------------------------------------------------
    # حذف گروهی
    # -----------------------------------------------------

    @app.route(
        '/delete_selected_project_wbs',
        methods=['POST']
    )
    @auth.login_required
    def delete_selected_project_wbs():

        if not auth.has_permission(
            'project_wbs'
        ):

            return redirect(
                url_for('admin_dashboard')
            )

        selected_ids = request.form.getlist(
            'selected_ids'
        )

        if not selected_ids:

            flash(
                'هیچ آیتمی برای حذف انتخاب نشده است.',
                'error'
            )

            return redirect(
                url_for('project_wbs')
            )

        conn = utils.get_db_connection()

        deleted_count = 0
        blocked_count = 0

        try:

            conn.execute('BEGIN')

            for record_id in selected_ids:

                try:
                    record_id = int(
                        record_id
                    )
                except (
                    TypeError,
                    ValueError
                ):
                    continue

                row = conn.execute(
                    '''
                    SELECT wbs_code
                    FROM project_wbs
                    WHERE id=?
                    ''',
                    (record_id,)
                ).fetchone()

                if not row:
                    continue

                linked = conn.execute(
                    '''
                    SELECT COUNT(*)
                    FROM petty_cash
                    WHERE wbs_code=?
                    ''',
                    (row['wbs_code'],)
                ).fetchone()[0]

                if linked:

                    blocked_count += 1
                    continue

                conn.execute(
                    '''
                    DELETE FROM project_wbs
                    WHERE id=?
                    ''',
                    (record_id,)
                )

                deleted_count += 1

            # -------------------------------------------------
            # بعد از حذف گروهی، کل WBS بدون Gap می‌شود
            # -------------------------------------------------

            _normalize_all_wbs_codes(
                conn
            )

            _rebuild_wbs_sort_order(
                conn
            )

            conn.commit()

            if deleted_count:

                flash(
                    f'{deleted_count} آیتم با موفقیت حذف شد و شماره‌های WBS اصلاح گردید.',
                    'success'
                )

            if blocked_count:

                flash(
                    (
                        f'{blocked_count} آیتم به هزینه‌های ثبت‌شده '
                        f'متصل بود و حذف نشد.'
                    ),
                    'error'
                )

            if (
                deleted_count == 0
                and blocked_count == 0
            ):

                flash(
                    'هیچ آیتمی حذف نشد.',
                    'error'
                )

        except Exception as e:

            conn.rollback()

            flash(
                f'خطا در حذف گروهی: {e}',
                'error'
            )

        finally:

            conn.close()

        return redirect(
            url_for('project_wbs')
        )

    # -----------------------------------------------------
    # Excel Import
    # -----------------------------------------------------

    @app.route(
        '/import_project_wbs',
        methods=['POST']
    )
    @auth.login_required
    def import_project_wbs():

        if not auth.has_permission(
            'project_wbs'
        ):

            return redirect(
                url_for('admin_dashboard')
            )

        uploaded = request.files.get(
            'wbs_file'
        )

        if (
            not uploaded
            or not uploaded.filename
        ):

            flash(
                'فایل Excel انتخاب نشده است.',
                'error'
            )

            return redirect(
                url_for('project_wbs')
            )

        try:

            import openpyxl

            file_data = uploaded.read()

            wb = openpyxl.load_workbook(
                io.BytesIO(file_data),
                data_only=True,
                read_only=True
            )

            ws = wb.active

            rows = ws.iter_rows(
                values_only=True
            )

            headers = next(
                rows,
                None
            )

            mapping = _header_map(
                headers or []
            )

            if (
                'code' not in mapping
                or 'activity' not in mapping
                or 'weight_percent' not in mapping
            ):

                raise ValueError(
                    'ستون‌های «کد WBS»، «شرح فعالیت» و «وزن فعالیت (%)» در فایل پیدا نشد.'
                )

            conn = utils.get_db_connection()

            count = 0

            try:

                conn.execute('BEGIN')

                for row in rows:

                    code = (
                        str(
                            row[mapping['code']]
                        ).strip()
                        if row[mapping['code']]
                        is not None
                        else ''
                    )

                    activity = (
                        str(
                            row[mapping['activity']]
                        ).strip()
                        if row[mapping['activity']]
                        is not None
                        else ''
                    )

                    if (
                        not code
                        or not activity
                        or code in (
                            'جمع',
                            'جمع کل',
                            'راهنما'
                        )
                    ):
                        continue

                    if not _is_valid_wbs_code(
                        code
                    ):
                        continue

                    # واحد برای آیتم نهایی نگهداری می‌شود؛
                    # سرفصل‌ها بعداً بر اساس داشتن فرزند بدون واحد می‌شوند.
                    unit = (
                        str(
                            row[mapping['unit']]
                        ).strip()
                        if (
                            'unit' in mapping
                            and row[mapping['unit']]
                            is not None
                        )
                        else ''
                    )

                    total_quantity = (
                        _num(
                            row[
                                mapping[
                                    'total_quantity'
                                ]
                            ]
                        )
                        if 'total_quantity'
                        in mapping
                        else 0
                    )

                    weight = _num(
                        row[
                            mapping[
                                'weight_percent'
                            ]
                        ]
                    )

                    notes = (
                        str(
                            row[
                                mapping['notes']
                            ]
                        ).strip()
                        if (
                            'notes' in mapping
                            and row[
                                mapping['notes']
                            ] is not None
                        )
                        else ''
                    )

                    existing = conn.execute(
                        '''
                        SELECT id
                        FROM project_wbs
                        WHERE wbs_code=?
                        ''',
                        (code,)
                    ).fetchone()

                    now = datetime.now().isoformat(
                        sep=' ',
                        timespec='seconds'
                    )

                    if existing:

                        conn.execute(
                            '''
                            UPDATE project_wbs
                            SET
                                activity=?,
                                unit=?,
                                total_quantity=?,
                                weight_percent=?,
                                notes=?,
                                updated_at=?
                            WHERE id=?
                            ''',
                            (
                                activity,
                                unit,
                                total_quantity,
                                weight,
                                notes,
                                now,
                                existing['id']
                            )
                        )

                    else:

                        _insert_wbs_with_shift(
                            conn,
                            code,
                            activity,
                            unit,
                            total_quantity,
                            weight,
                            notes
                        )

                    count += 1

                # سرفصل واقعی = هر رکوردی که فرزند مستقیم دارد.
                all_rows = conn.execute(
                    'SELECT id, wbs_code FROM project_wbs'
                ).fetchall()
                all_codes = [str(r['wbs_code']).strip() for r in all_rows]
                for r in all_rows:
                    code_now = str(r['wbs_code']).strip()
                    has_child = any(c.startswith(code_now + '.') for c in all_codes)
                    if has_child:
                        conn.execute(
                            "UPDATE project_wbs SET unit='', total_quantity=0 WHERE id=?",
                            (r['id'],)
                        )

                # -------------------------------------------------
                # بعد از Import نیز کل ساختار بدون Gap می‌شود.
                # -------------------------------------------------

                _normalize_all_wbs_codes(
                    conn
                )

                _rebuild_wbs_sort_order(
                    conn
                )

                conn.commit()

            except Exception:

                conn.rollback()
                raise

            finally:

                conn.close()
                wb.close()

            flash(
                f'{count} آیتم از فایل Excel وارد/به‌روزرسانی شد و ترتیب WBS اصلاح گردید.',
                'success'
            )

        except Exception as e:

            flash(
                f'خطا در وارد کردن Excel: {e}',
                'error'
            )

        return redirect(
            url_for('project_wbs')
        )

    # -----------------------------------------------------
    # project_progress
    # -----------------------------------------------------

    @app.route('/project_progress')
    @auth.login_required
    def project_progress():

        if not auth.has_permission(
            'project_progress'
        ):

            flash(
                'شما به این بخش دسترسی ندارید.',
                'error'
            )

            return redirect(
                url_for('admin_dashboard')
            )

        return render_template(
            'project_progress.html'
        )

    # -----------------------------------------------------
    # API پیشرفت
    # -----------------------------------------------------

    @app.route(
        '/api/project_progress_data'
    )
    @auth.login_required
    def project_progress_data():

        if (
            not auth.has_permission(
                'project_progress'
            )
            and not auth.has_permission(
                'project_wbs'
            )
            and not auth.has_permission(
                'petty_cash'
            )
            and not auth.has_permission(
                'petty_cash_reports'
            )
        ):

            return jsonify(
                {
                    'error':
                    'Access Denied'
                }
            ), 403

        conn = utils.get_db_connection()

        try:

            exclude_id = request.args.get('exclude_petty_cash_id', type=int)

            return jsonify(
                _progress_data(
                    conn,
                    exclude_petty_cash_id=exclude_id
                )
            )

        finally:

            conn.close()

    # -----------------------------------------------------
    # گزینه‌های WBS
    # -----------------------------------------------------

    @app.route(
        '/api/project_wbs_options'
    )
    @auth.login_required
    def project_wbs_options():

        if not auth.has_permission(
            'petty_cash'
        ):

            return jsonify(
                {
                    'error':
                    'Access Denied'
                }
            ), 403

        conn = utils.get_db_connection()

        try:

            rows = conn.execute(
                '''
                SELECT
                    wbs_code,
                    activity,
                    unit,
                    weight_percent
                FROM project_wbs
                ORDER BY sort_order, id
                '''
            ).fetchall()

            return jsonify(
                [
                    dict(r)
                    for r in rows
                ]
            )

        finally:

            conn.close()
