#!/usr/bin/env python
"""
These are the tests for the low-level module _sqlite.

They try to execute as much of the low-level _sqlite module as possible to
facilitate coverage testing with the help of gcov.
"""


import testsupport
import os, unittest, re
import _sqlite3 as _sqlite
from sqlite3 import ProgrammingError

class lowlevelTestCases(unittest.TestCase, testsupport.TestSupport):
    def setUp(self):
        self.filename = self.getfilename()
        self.cnx = _sqlite.connect(self.filename)

    def tearDown(self):
        try:
            self.cnx.close()
            self.removefile()
            del self.cnx
        except AttributeError:
            pass
        except ProgrammingError:
            pass

    def CheckModuleAttributeAccess(self):
        for attr in dir(_sqlite):
            _sqlite.__dict__[attr]

    def CheckConnectionAttributes(self):
        self.cnx.filename
        self.cnx.sql
        self.cnx.sqlite_changes()
        self.cnx.sqlite_last_insert_rowid()

        try:
            self.cnx.foo = 7
            self.fail("Could set attribute. Connection object should be read-only.")
        except TypeError:
            pass
        except AttributeError:
            pass

    def CheckSQLiteExec(self):
        # removed sqlite_exec interface
        return
        self.cnx.execute("create table test(id int, name varchar(20))")
        self.cnx.execute("insert into test(id, name) values (1, 'foo')")
        self.cnx.execute("insert into test(id, name) values (2, 'bar')")

        expected_colnames = ('id', 'name')
        expected_values = [('1', 'foo'), ('2', 'bar')]
        failures = []

        def callback(arg1, items, colnames):
            if colnames != expected_colnames:
                failures.append("expected colnames %s, got %s"
                                % (repr(expected_colnames), repr(colnames)))
            if items not in expected_values:
                failures.append("%s not in expected_values %s"
                                % (repr(items), repr(expected_values)))
            else:
                expected_values.pop(0)

        self.cnx.sqlite_exec("select * from test", callback, None)
        if len(failures) > 0:
            for failure in failures:
                self.fail(failure)

    def CheckSQLiteLastInsertRowID(self):
        self.cnx.execute("create table test(id integer primary key, name varchar(20))")
        self.cnx.execute("insert into test(id, name) values (NULL, 'foo')")
        self.cnx.execute("insert into test(id, name) values (NULL, 'bar')")
        rowid = self.cnx.sqlite_last_insert_rowid()
        self.assertEqual(rowid, 2,
                            "last inserted rowid should have been %i, was %i"
                            % (2, rowid))

    def CheckSQLiteChanges(self):
        self.cnx.execute("create table test(id integer primary key, name varchar(20))")
        self.cnx.execute("insert into test(id, name) values (NULL, 'foo')")
        self.cnx.execute("insert into test(id, name) values (NULL, 'bar')")
        self.cnx.execute("insert into test(id, name) values (NULL, 'baz')")
        self.cnx.execute("delete from test where name='baz'")
        changed = self.cnx.sqlite_changes()
        self.assertEqual(changed, 1,
                            "changed rows should have been %i, was %i"
                            % (1, changed))
        self.cnx.execute("update test set name='foobar' where id < 10")
        changed = self.cnx.sqlite_changes()
        self.assertEqual(changed, 2,
                            "changed rows should have been %i, was %i"
                            % (2, changed))

    def CheckConnectionForProgrammingError(self):
        self.cnx.close()
        self.removefile()

        self.assertRaises(ProgrammingError, self.cnx.close)
        # execute method is gone
        #self.failUnlessRaises(ProgrammingError, self.cnx.execute, "")

    def CheckConnectionForNumberOfArguments(self):
        self.assertRaises(TypeError, self.cnx.close, None)
        self.assertRaises(TypeError, self.cnx.execute, None, None)
        self.assertRaises(TypeError, self.cnx.sqlite_changes, None)
        # sqlite_exec is gone
        # self.failUnlessRaises(TypeError, self.cnx.sqlite_exec, None)
        self.assertRaises(TypeError, self.cnx.sqlite_last_insert_rowid, None)

    def CheckConnectionDestructor(self):
        del self.cnx
        self.removefile()

    def CheckResultObject(self):
        # execute method is gone
        return
        create_statement = "create table test(id INTEGER, name TEXT)"
        self.cnx.execute(create_statement)

        self.assertEqual(create_statement, self.cnx.sql,
            ".sql should have been %s, was %s" % (create_statement, self.cnx.sql))

        self.cnx.execute("insert into test(id, name) values (4, 'foo')")
        self.cnx.execute("insert into test(id, name) values (5, 'bar')")

        res = self.cnx.execute("select id, name from test")
        self.assertTrue(res.rowcount == 2, "Should have returned 2 rows, but was %i" % res.rowcount)

        correct_col_defs = (('id', _sqlite.INTEGER, None, None, None, None, None), \
                            ('name', _sqlite.STRING, None, None, None, None, None))
        self.assertEqual(res.col_defs, correct_col_defs,
            "col_defs should have been %s, was %s" % (repr(correct_col_defs), repr(res.col_defs)))

        correct_row_list = [(4, 'foo'), (5, 'bar')]
        self.assertEqual(res.row_list, correct_row_list,
            "rowlist should have been %s, was %s" % (repr(correct_row_list), repr(res.row_list)))

    def CheckResultAttributes(self):
        # low level api removed
        return
        res = self.cnx.execute("select NULL, max(4,5)")
        try:
            res.foo = 7

        except TypeError:
            pass

    def CheckSQLiteVersion(self):
        try:
            ver = _sqlite.sqlite_version()
        except:
            self.fail('sqlite_version() failed')
        pat = re.compile(r'\d*\.\d*\.\d*')
        if not re.match(pat,ver):
            self.fail('Incorrect sqlite_version() format, '
                'should be digits.digits.digits, was %s'%ver)


def suite():
    return unittest.makeSuite(lowlevelTestCases, "Check")

def main():
    runner = unittest.TextTestRunner()
    runner.run(suite())

if __name__ == "__main__":
    main()
