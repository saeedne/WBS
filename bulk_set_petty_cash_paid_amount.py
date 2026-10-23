"""Set paid_amount to total_amount in every petty-cash database.

This is a one-time maintenance script. It creates a SQLite backup beside each
database before changing it. Run it from the project folder, locally or on the
host, after stopping the web application if possible.
"""

from datetime import datetime
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parent
DATABASES = [ROOT / "time_tracker.db"]
PROJECT_DATABASES = ROOT / "project_databases"
if PROJECT_DATABASES.is_dir():
    DATABASES.extend(sorted(PROJECT_DATABASES.glob("project_*.db")))


def backup_database(database_path):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = database_path.with_name(
        f"{database_path.stem}.before_paid_amount_{timestamp}{database_path.suffix}"
    )
    with sqlite3.connect(database_path) as source:
        with sqlite3.connect(backup_path) as backup:
            source.backup(backup)
    return backup_path


def update_database(database_path):
    if not database_path.is_file():
        return None

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "petty_cash" not in tables:
            return None

        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(petty_cash)")
        }
        if "total_amount" not in columns:
            raise RuntimeError(f"{database_path}: petty_cash has no total_amount column")

        paid_column_missing = "paid_amount" not in columns
        if not paid_column_missing:
            rows_to_change = connection.execute(
                '''SELECT COUNT(*) FROM petty_cash
                   WHERE COALESCE(paid_amount, -1) != COALESCE(total_amount, 0)'''
            ).fetchone()[0]
            if rows_to_change == 0:
                print(f"Already up to date: {database_path}")
                return None

        backup_path = backup_database(database_path)
        if paid_column_missing:
            connection.execute(
                "ALTER TABLE petty_cash ADD COLUMN paid_amount REAL NOT NULL DEFAULT 0"
            )
        cursor = connection.execute(
            "UPDATE petty_cash SET paid_amount = COALESCE(total_amount, 0)"
        )
        connection.commit()
        return backup_path, cursor.rowcount


def main():
    updated = 0
    for database_path in DATABASES:
        result = update_database(database_path)
        if result is None:
            continue
        backup_path, row_count = result
        updated += 1
        print(f"Updated {row_count} rows in: {database_path}")
        print(f"Backup saved to: {backup_path}")

    if updated == 0:
        raise SystemExit("No petty_cash databases were found.")
    print("Done. paid_amount now matches total_amount in all found records.")


if __name__ == "__main__":
    main()

