"""Disposable reserved-task schemas; no benchmark acquisition or policy fitting."""
from contextlib import contextmanager
import re
from .inventory import require


class ReservedSQL:
    def __init__(self, dsn, schema):
        import psycopg
        require(isinstance(schema, str) and re.fullmatch(r'qrl_reserved_[a-f0-9]{32}', schema), 'reserved schema identity required')
        self.driver = psycopg; self.dsn = dsn; self.schema = schema

    @contextmanager
    def connection(self):
        with self.driver.connect(self.dsn, autocommit=True, prepare_threshold=None) as connection:
            connection.execute(self.driver.sql.SQL('SET search_path TO {}, public').format(self.driver.sql.Identifier(self.schema)))
            connection.execute("SET statement_timeout TO '10s'"); connection.execute("SET synchronous_commit TO 'on'")
            yield connection

    def create_schema(self):
        with self.driver.connect(self.dsn, autocommit=True) as connection:
            connection.execute(self.driver.sql.SQL('CREATE SCHEMA {}').format(self.driver.sql.Identifier(self.schema)))

    def drop(self):
        with self.driver.connect(self.dsn, autocommit=True) as connection:
            connection.execute(self.driver.sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(self.driver.sql.Identifier(self.schema)))
