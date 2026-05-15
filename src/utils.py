import uuid

def short_id(n: int = 8) -> str:
    return uuid.uuid4().hex[:n]

def long_id() -> str:
    return uuid.uuid4().hex