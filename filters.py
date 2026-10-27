from flask_admin.contrib.pymongo.filters import BasePyMongoFilter
from bson import ObjectId, regex
import re

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
        _filter = { "_classes": { "$regex": r"(?:^|\s)" + re.escape(value) + r"(?:\s|$)" } }
        query.append(_filter)

        return query

    def operation(self):
        return "="


class AssignmentFilter(BasePyMongoFilter):
    """Resolve display fields through the export normalizer, not title slices."""
    def __init__(self, column, name, courses, normalize, course_field='_courseId'):
        self.courses = courses
        self.normalize = normalize
        self.course_field = course_field
        super().__init__(column, name)

    def get_options(self, view):
        values = set()
        for course in self.courses():
            try:
                values.add(str(self.normalize(course)[self.column]))
            except (ValueError, OSError):
                continue
        return [(value, value) for value in sorted(values, key=lambda value: (0, int(value)) if value.isdigit() else (1, value))]

    def apply(self, query, value):
        ids = []
        for course in self.courses():
            try:
                if str(self.normalize(course)[self.column]) == str(value):
                    ids.append(course['_id'])
            except (ValueError, OSError):
                continue
        query.append({self.course_field: {'$in': ids}})
        return query

    def operation(self):
        return '='
