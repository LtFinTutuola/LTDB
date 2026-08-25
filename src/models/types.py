import json
from sqlalchemy.types import TypeDecorator, String, JSON

class UppercaseString(TypeDecorator):
    """Converts strings to uppercase on the way in."""
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            return value.strip().upper()
        return value

class LowercaseJSONList(TypeDecorator):
    """Converts a list of strings to lowercase and deduplicates them on the way in."""
    impl = JSON
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and isinstance(value, list):
            # Strip, lowercase, remove empty strings
            processed = [
                item.strip().lower() for item in value 
                if isinstance(item, str) and item.strip()
            ]
            # Order-preserving deduplication
            return list(dict.fromkeys(processed))
        return value
