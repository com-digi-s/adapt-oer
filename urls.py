authoring_domain = "https://authoring.creativeartefact.org"

def get_asset_url(asset):
    if type(asset) == dict:
        return authoring_domain + "/api/asset/serve/" + str(asset.get("_id", ""))
    else:
        return "#"

def get_editor_url(course_id, page_id):
    return f"{authoring_domain}/#editor/{course_id}/page/{page_id}"
