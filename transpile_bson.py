from copy import deepcopy

component_mappings = {
    "_id": "_id.$oid",
    "_parentId": "_parentId.$oid",
    "_pageLevelProgress": "_extensions._pageLevelProgress",
    "_tutor": "_extensions._tutor",
    "_additionalResources": "_extensions._additionalResources"
}

block_mappings = {
    "_id": "_id.$oid",
    "_parentId": "_parentId.$oid",
    "_pageLevelProgress": "_extensions._pageLevelProgress",
    "_assessment": "_extensions._assessment",
    "_trickle": "_extensions._trickle",
    "_vanilla": "themeSettings._vanilla",
    "_additionalResources": "_extensions._additionalResources"
}

special_mappings = {**component_mappings, **block_mappings}

deep_copy_fields = ["_items"]

# List of keys to ignore
ignored_keys = [
    "_tutor",
    "_componentType",
    "_tenantId",
    "_courseId",
    "createdBy",
    "createdAt",
    "updatedAt"
]


def transpile_data(original_data):
    transpiled_data = []
        
    for item in original_data:
        new_item = {}
        
        # Handle special mappings
        for new_key, original_key in special_mappings.items():
            keys = original_key.split('.')
            temp = item
            try:
                for k in keys:
                    temp = temp[k]
                new_item[new_key] = temp
            except (KeyError, TypeError):
                pass  # Key doesn't exist in the original item
        
        # Handle the properties object
        if "properties" in item:
            new_item.update(item["properties"])
        
        # Handle other fields
        for key, value in item.items():
            # Skip if the key is part of a special mapping or is "properties"
            if any(key in special for special in special_mappings.values()) or key == "properties":
                continue

            if isinstance(value, dict) or (key in deep_copy_fields):
                new_item[key] = deepcopy(value)
                continue
            
            new_item[key] = value
        
        for key in ignored_keys:
            new_item.pop(key, None)
        
        transpiled_data.append(new_item)

    return transpiled_data