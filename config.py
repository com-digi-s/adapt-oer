import os

FLASK_ADMIN_SWATCH = 'cosmo'
SECRET_KEY = os.environ.get("SECRET_KEY")
MONGO_URI = 'mongodb://{}:{}/{}'.format(
    os.environ.get("DB_HOST"),
    os.environ.get("MONGODB_PORT"),
    os.environ.get("DB_NAME")
)
BABEL_DEFAULT_LOCALE = 'de'
BABEL_DEFAULT_TIMEZONE = 'Europe/Berlin'
USERNAME = os.environ.get("USER")
PASSWORD = os.environ.get("PASS")
BUILDS_DIR = os.path.expanduser("{}".format(
    os.environ.get("BUILDS_DIR")))
GLOSSARY_JSON = "./static/glossary.json"
ADDITIONAL_COMPONENTS = "./static/additional_components.json"
WTF_CSRF_ENABLED = True  # enables CSRF protection globally
AUTHORING_DOMAIN = os.environ.get("AUTHORING_DOMAIN")
# A tested deployment can use an immutable, locally built wrapper artifact.
# ZIP root must contain index.html/config.json and no already packaged scos.
LXP_WRAPPER_ZIP = os.environ.get('LXP_WRAPPER_ZIP')
LXP_WRAPPER_SHA256 = os.environ.get('LXP_WRAPPER_SHA256')
LXP_WRAPPER_URL = os.environ.get('LXP_WRAPPER_URL', 'https://github.com/com-digi-s/adapt-lxp/archive/refs/heads/main.zip')
LXP_CATALOGUE = os.environ.get('LXP_CATALOGUE')
# Separate writable override; the configured source catalogue stays read-only.
# None uses Flask's instance/lxp-catalogue.json path.
LXP_CATALOGUE_STORE_PATH = os.environ.get('LXP_CATALOGUE_STORE_PATH')
LXP_ORIGIN_ID = os.environ.get('LXP_ORIGIN_ID', AUTHORING_DOMAIN or 'aat-local')
LXP_REGISTRY_PATH = os.environ.get('LXP_REGISTRY_PATH', './instance/lxp-source-registry.json')
LXP_ASSESSMENT_TEMPLATE = os.environ.get('LXP_ASSESSMENT_TEMPLATE', './static/assessment-template.zip')
LXP_TARGET_PROFILE_ID = os.environ.get('LXP_TARGET_PROFILE_ID', 'scorm-1.2-conservative')
LXP_PREVIEW_TTL_SECONDS = 1800
# Explicit adapter only for this application's known AAT publication source.
# It verifies real Core-build anchors; native registrations do not inherit it.
LXP_AAT_BUILD_PROFILE = os.environ.get('LXP_AAT_BUILD_PROFILE', 'aat-core-build-v1')
