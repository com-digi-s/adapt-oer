from flask_admin.contrib.pymongo.filters import BasePyMongoFilter
from bson import ObjectId, regex

class CustomFilter(BasePyMongoFilter):
    def apply(self, query, value):
        if ObjectId.is_valid(value):
            _filter = {self.column: ObjectId(value)}
        else:
            _filter = {self.column: value}    
        query.append(_filter)

        return query

    def operation(self):
        return "="

class CustomClassesFilter(BasePyMongoFilter):
    def apply(self, query, value):
        _filter = { "_classes": { "$regex": f"\\b{value}\\b" } }
        query.append(_filter)

        return query

    def operation(self):
        return "="
