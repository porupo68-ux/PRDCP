from uuid import uuid4


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def new_workflow_id() -> str:
    return str(uuid4())

