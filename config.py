import os

FLASK_ADMIN_SWATCH = 'cosmo'
SECRET_KEY = os.environ.get("SECRET_KEY")
MONGO_URI = 'mongodb://{}:{}/{}'.format(
    os.environ.get("DB_HOST"),
    os.environ.get("MONGODB_PORT"),
    os.environ.get("DB_HOST")
)
BABEL_DEFAULT_LOCALE = 'de'
BABEL_DEFAULT_TIMEZONE = 'Europe/Berlin'
USERNAME = os.environ.get("USER")
PASSWORD = os.environ.get("PASS")
BUILDS_DIR = os.path.expanduser("{}".format(
    os.environ.get("BUILDS_DIR")))
GLOSSARY_JSON = "/app/static/glossary.json"
ADDITIONAL_COMPONENTS = "/app/static/additional_components.json"
WTF_CSRF_ENABLED = True  # enables CSRF protection globally