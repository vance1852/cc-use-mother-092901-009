import sqlite3
import unittest

from progress_payment.storage import Database


class PaymentStorageTest(unittest.TestCase):
    def test_transaction_rolls_back_on_error(self):
        database = Database()
        with self.assertRaises(RuntimeError):
            with database.transaction():
                database.connection.execute(
                    "INSERT INTO organizations(organization_id,name,created_at) VALUES('o1','机构','now')"
                )
                raise RuntimeError("停止事务")
        count = database.connection.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]
        self.assertEqual(0, count)
        database.close()

    def test_schema_enables_foreign_keys(self):
        database = Database()
        enabled = database.connection.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(1, enabled)
        database.close()

    def test_only_one_open_period_per_project(self):
        database = Database()
        database.connection.execute("INSERT INTO organizations VALUES('o1','机构','now')")
        database.connection.execute(
            "INSERT INTO projects(project_id,organization_id,code,name,created_at) "
            "VALUES('p1','o1','GS','项目','now')")
        database.connection.execute(
            "INSERT INTO periods(period_id,project_id,name,status,opened_at) "
            "VALUES('per1','p1','P-0001','open','now')")
        with self.assertRaises(sqlite3.IntegrityError):
            database.connection.execute(
                "INSERT INTO periods(period_id,project_id,name,status,opened_at) "
                "VALUES('per2','p1','P-0002','open','now')")
        database.close()


if __name__ == "__main__":
    unittest.main()
