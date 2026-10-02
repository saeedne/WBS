from flask import Flask, render_template, request, redirect, url_for, jsonify, Response, session, flash, send_from_directory
import sqlite3
import os
import csv
import io
import jdatetime
import urllib.parse
import zipfile
import json
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import utils
import auth
import employees_routes
import petty_cash_routes
import daily_worker_routes
import financial_routes
import main_routes
import facilities_routes
import payroll_routes
import project_wbs_routes
import uuid

DB_FILE = 'time_tracker.db'
app = Flask(__name__)
app.secret_key = 'a_very_secure_and_random_key_that_is_changed_often'
UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER


@app.route('/sw.js')
def service_worker_file():
    """Serve the service worker from the site root so it can control all pages."""
    response = send_from_directory(app.static_folder, 'sw.js', mimetype='application/javascript')
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache'
    return response

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# Initialize modules
utils.init_db()
auth.init_auth_system(app)

# Initialize all route files
employees_routes.init_employees_routes(app)
petty_cash_routes.init_petty_cash_routes(app)
daily_worker_routes.init_daily_worker_routes(app)
financial_routes.init_financial_routes(app)
main_routes.init_main_routes(app)
facilities_routes.init_facilities_routes(app)
payroll_routes.init_payroll_routes(app)
project_wbs_routes.init_project_wbs_routes(app)

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True)
