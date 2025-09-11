from config import AUTHORING_DOMAIN

def get_asset_url(asset):
    if type(asset) == dict:
        return AUTHORING_DOMAIN + "/api/asset/serve/" + str(asset.get("_id", ""))
    else:
        return "#"

def get_editor_url(course_id, page_id):
    return f"{AUTHORING_DOMAIN}/#editor/{course_id}/page/{page_id}"
