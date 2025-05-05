from flask import render_template, redirect, url_for, request
from flask_login import login_user, logout_user, login_required, current_user
from app import users  # Import users from users.py or the appropriate location

def init_views(app, login_manager):
    @login_manager.user_loader
    def load_user(user_id):
        return next((user for user in users if user.id == int(user_id)), None)

    @app.before_request
    def before_request():
        if not current_user.is_authenticated and request.endpoint not in ['login', 'static']:
            return redirect(url_for('login'))

    @app.route('/')
    def home():
        if current_user.is_authenticated:
            return redirect(url_for('coursesview.index_view'))
        else:
            return redirect(url_for('login'))

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            user = next((user for user in users if user.username == request.form['username']), None)
            if user and user.check_password(request.form['password']):
                login_user(user)
                return redirect(url_for('coursesview.index_view'))
        return render_template('login.html')

    @app.route('/logout')
    @login_required
    def logout():
        logout_user()
        return redirect(url_for('login'))
