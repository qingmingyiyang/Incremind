"""Enlist existing record-based domain writes in one caller-owned transaction."""
from contextlib import contextmanager


class TransactionRecords:
    """A write-only service seam: no connection creation or qualified reads.

    Domain methods retain their existing begin/commit protocol, but only the
    caller may commit the real SQLite transaction. Exceptions propagate out.
    """

    def __init__(self, transaction):
        self._transaction = transaction._transaction if isinstance(transaction,TransactionRecords) else transaction

    @property
    def connection(self):
        return self._transaction.connection

    @property
    def database_path(self):
        return next(row[2] for row in self._transaction.connection.execute("PRAGMA database_list") if row[1] == "main")

    def read(self, collection, identity):
        return self._transaction.read(collection, identity)

    def list(self, collection):
        return self._transaction.list(collection)

    def put(self, collection, identity, payload, *, expected_revision):
        return self._transaction.put(collection, identity, payload, expected_revision=expected_revision)

    @contextmanager
    def begin(self):
        yield self

    def commit(self):
        # The outer records.begin() owns the only durable commit.
        return ()
