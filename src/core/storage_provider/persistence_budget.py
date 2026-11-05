from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


_BUDGET = ContextVar('persistence_byte_budget', default=None)


@contextmanager
def persistence_budget(*, root, databases, consume):
    binding = (Path(root).absolute(), frozenset(Path(path).absolute() for path in databases), consume)
    token = _BUDGET.set(binding)
    try:
        yield
    finally:
        _BUDGET.reset(token)


def consume_record(connection, collection, identity, payload_json, revision):
    binding = _BUDGET.get()
    if binding is None:
        return
    path = next(Path(row[2]).absolute() for row in connection.execute('PRAGMA database_list') if row[1] == 'main')
    if path in binding[1]:
        binding[2](len(payload_json.encode('utf-8')) + len(collection.encode('utf-8'))
                   + len(identity.encode('utf-8')) + len(str(revision).encode('ascii')))


def consume_json(path, encoded_bytes):
    binding = _BUDGET.get()
    if binding is not None and Path(path).absolute().is_relative_to(binding[0]):
        binding[2](encoded_bytes)
