"""
Abstract base class for database connectors.

To add a new database type (PostgreSQL, MySQL), create a new file
implementing DatabaseConnector from this base.
"""

from abc import ABC, abstractmethod


class DatabaseConnector(ABC):
    @abstractmethod
    def connect(self, server: str, database: str, user: str, password: str): ...

    @abstractmethod
    def execute_query(self, sql: str, params: list = None) -> list: ...

    @abstractmethod
    def discover_schema(self, db_name: str) -> dict: ...

    @abstractmethod
    def check_permissions(self) -> dict: ...

    @abstractmethod
    def test_connection(self) -> dict: ...

    @abstractmethod
    def close(self): ...
