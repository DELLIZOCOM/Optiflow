import unittest
from unittest.mock import patch, MagicMock
from core.db import get_connection, execute_query
import pyodbc

class TestDatabaseConnection(unittest.TestCase):

    @patch('core.db.pyodbc.connect')
    def test_get_connection_success(self, mock_connect):
        """Test getting a connection successfully on the first attempt."""
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        
        conn = get_connection()
        
        self.assertEqual(conn, mock_conn)
        mock_connect.assert_called_once()
        
    @patch('core.db.time.sleep')
    @patch('core.db.pyodbc.connect')
    def test_get_connection_retry_success(self, mock_connect, mock_sleep):
        """Test getting a connection successfully after a retry."""
        mock_conn = MagicMock()
        # Fail first time, succeed second time
        mock_connect.side_effect = [pyodbc.Error("Connection failed"), mock_conn]
        
        conn = get_connection()
        
        self.assertEqual(conn, mock_conn)
        self.assertEqual(mock_connect.call_count, 2)
        mock_sleep.assert_called_once_with(2)

    @patch('core.db.time.sleep')
    @patch('core.db.pyodbc.connect')
    def test_get_connection_failure(self, mock_connect, mock_sleep):
        """Test connection failure after maximum retries."""
        # Fail all attempts
        mock_connect.side_effect = pyodbc.Error("Connection failed")
        
        with self.assertRaises(pyodbc.Error):
            get_connection()
            
        self.assertEqual(mock_connect.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch('core.db.get_connection')
    def test_execute_query_success(self, mock_get_connection):
        """Test executing a query successfully."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        
        mock_get_connection.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        
        # Mock cursor description and fetchall return values
        mock_cursor.description = (('id',), ('name',))
        mock_cursor.fetchall.return_value = [(1, 'test')]
        
        sql = "SELECT id, name FROM test_table"
        result = execute_query(sql)
        
        mock_get_connection.assert_called_once()
        mock_cursor.execute.assert_called_once_with(sql)
        self.assertEqual(result, [{'id': 1, 'name': 'test'}])
        mock_conn.close.assert_called_once()

    @patch('core.db.get_connection')
    def test_execute_query_with_params(self, mock_get_connection):
        """Test executing a query with parameters."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        
        mock_get_connection.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        
        mock_cursor.description = (('count',),)
        mock_cursor.fetchall.return_value = [(5,)]
        
        sql = "SELECT count(*) as count FROM test_table WHERE id = ?"
        params = (1,)
        result = execute_query(sql, params)
        
        mock_cursor.execute.assert_called_once_with(sql, params)
        self.assertEqual(result, [{'count': 5}])

if __name__ == '__main__':
    unittest.main()
