#!/usr/bin/env python
import testsupport
import os, string, sys, types, unittest, weakref
import sqlite3 as sqlite

class DBAPICompliance(unittest.TestCase):
    def CheckAPILevel(self):
        self.assertEqual(sqlite.apilevel, '2.0',
                         'apilevel is %s, should be 2.0' % sqlite.apilevel)

    def CheckThreadSafety(self):
        self.assertEqual(sqlite.threadsafety, 1,
                         'threadsafety is %d, should be 1' % sqlite.threadsafety)

    def CheckParamStyle(self):
        self.assertEqual(sqlite.paramstyle, 'qmark',
                         'paramstyle is "%s", should be "qmark"' %
                         sqlite.paramstyle)

    def CheckWarning(self):
        self.assertTrue(issubclass(sqlite.Warning, Exception),
                     'Warning is not a subclass of StandardError')

    def CheckError(self):
        self.assertTrue(issubclass(sqlite.Error, Exception),
                        'Error is not a subclass of StandardError')

    def CheckInterfaceError(self):
        self.assertTrue(issubclass(sqlite.InterfaceError, sqlite.Error),
                        'InterfaceError is not a subclass of Error')

    def CheckDatabaseError(self):
        self.assertTrue(issubclass(sqlite.DatabaseError, sqlite.Error),
                        'DatabaseError is not a subclass of Error')

    def CheckDataError(self):
        self.assertTrue(issubclass(sqlite.DataError, sqlite.DatabaseError),
                        'DataError is not a subclass of DatabaseError')

    def CheckOperationalError(self):
        self.assertTrue(issubclass(sqlite.OperationalError, sqlite.DatabaseError),
                        'OperationalError is not a subclass of DatabaseError')

    def CheckIntegrityError(self):
        self.assertTrue(issubclass(sqlite.IntegrityError, sqlite.DatabaseError),
                        'IntegrityError is not a subclass of DatabaseError')

    def CheckInternalError(self):
        self.assertTrue(issubclass(sqlite.InternalError, sqlite.DatabaseError),
                        'InternalError is not a subclass of DatabaseError')

    def CheckProgrammingError(self):
        self.assertTrue(issubclass(sqlite.ProgrammingError, sqlite.DatabaseError),
                        'ProgrammingError is not a subclass of DatabaseError')

    def CheckNotSupportedError(self):
        self.assertTrue(issubclass(sqlite.NotSupportedError,
                                   sqlite.DatabaseError),
                        'NotSupportedError is not a subclass of DatabaseError')

class moduleTestCases(unittest.TestCase, testsupport.TestSupport):
    def setUp(self):
        self.filename = self.getfilename()
        self.cnx = sqlite.connect(self.filename)
        self.cur = self.cnx.cursor()

    def tearDown(self):
        try:
            self.cnx.close()
            self.removefile()
        except AttributeError:
            pass
        except sqlite.ProgrammingError:
            pass

    def CheckConnectionObject(self):
        self.assertTrue(isinstance(self.cnx, sqlite.Connection),
                     'sqlite.connect did not return a Connection object')

    def CheckConnectionClose(self):
        self.assertTrue(hasattr(self.cnx, 'close') and
                     type(self.cnx.close) == types.MethodType,
                     'close is not a method of Connection')
        self.cnx.close()
        self.removefile()
        self.assertRaises(sqlite.ProgrammingError, self.cnx.close)

    def CheckConnectionCommit(self):
        self.assertTrue(hasattr(self.cnx, "commit") and
                     type(self.cnx.commit) == types.MethodType,
                     'commit is not a method of Connection')
        self.cnx.close()
        self.removefile()
        self.assertRaises(sqlite.ProgrammingError, self.cnx.commit)

    def CheckConnectionRollback(self):
        self.assertTrue(hasattr(self.cnx, "rollback") and
                     type(self.cnx.rollback) == types.MethodType,
                     'rollback is not a method of Connection')
        self.cnx.close()
        self.removefile()
        self.assertRaises(sqlite.ProgrammingError, self.cnx.rollback)

    def CheckConnectionCursor(self):
        self.assertTrue(hasattr(self.cnx, "cursor") and
                     type(self.cnx.cursor) == types.MethodType,
                     'cursor is not a method of Connection')
        self.cnx.close()
        self.removefile()
        self.assertRaises(sqlite.ProgrammingError, self.cnx.cursor)

    def CheckCloseConnection(self):
        self.cnx.close()
        self.removefile()

    def CheckCursorObject(self):
        self.assertTrue(isinstance(self.cur, sqlite.Cursor),
                     'cnx.cursor() did not return a Cursor instance')

    def CheckCursorArraysize(self):
        self.assertTrue(self.cur.arraysize == 1,
                     'cur.arraysize is %d, it should be 1' %
                     self.cur.arraysize)

    def CheckCursorDescription(self):
        self.assertTrue(self.cur.description == None,
                     "cur.description should be None at this point, it isn't.")

    def CheckCursorRowcount(self):
        self.assertTrue(self.cur.rowcount == -1,
                     'cur.rowcount is %d, should be -1' % self.cur.rowcount)

    def CheckCursorClose(self):
        self.assertTrue(hasattr(self.cur, "close") and
                     type(self.cur.close) == types.MethodType,
                     'close is not a method of the Cursor object')
        self.cur.close()
        self.assertRaises(sqlite.ProgrammingError, self.cur.close)

    def CheckCursorExecute(self):
        self.assertTrue(hasattr(self.cur, "execute") and
                     type(self.cur.execute) == types.MethodType,
                     'execute is not a method of the Cursor object')
        self.cur.close()
        self.assertRaises(sqlite.ProgrammingError,
                              self.cur.execute, 'SELECT max(3,4)')

    def CheckCursorExecutemany(self):
        self.assertTrue(hasattr(self.cur, "executemany") and
                     type(self.cur.executemany) == types.MethodType,
                     'executemany is not a method of the Cursor object')

        self.cur.close()
        self.assertRaises(sqlite.ProgrammingError,
                              self.cur.executemany, 'SELECT max(3,4)', [1,2])

    def CheckCursorFetchone(self):
        self.assertTrue(hasattr(self.cur, "fetchone") and
                     type(self.cur.fetchone) == types.MethodType,
                     'fetchone is not a method of the Cursor object')
        self.cur.close()
        self.assertRaises(sqlite.ProgrammingError, self.cur.fetchone)

    def CheckCursorFetchMany(self):
        self.assertTrue(hasattr(self.cur, "fetchmany") and
                        type(self.cur.fetchmany) == types.MethodType,
                        'fetchmany is not a method of the Cursor object')

        cursor = self.cnx.cursor()
        cursor.execute("create table test(id int)")
        cursor.executemany("insert into test(id) values ( ? )", list(range(10)))
        cursor.execute("select id from test")
        res = cursor.fetchmany()
        self.assertEqual(len(res), 1, """fetchmany should have returned a
            list of length 1, but the list was %i elements long""" % len(res))
        res = cursor.fetchmany(2)
        self.assertEqual(len(res), 2, """fetchmany should have returned a
            list of length 2, but the list was %i elements long""" % len(res))
        cursor.arraysize = 5
        res = cursor.fetchmany()
        self.assertEqual(len(res), 5, """fetchmany should have returned a
            list of length 5, but the list was %i elements long""" % len(res))

        self.cur.close()
        self.assertRaises(sqlite.ProgrammingError,
                              self.cur.fetchmany, 10)

    def CheckCursorFetchall(self):
        self.assertTrue(hasattr(self.cur, "fetchall") and
                        type(self.cur.fetchall) == types.MethodType,
                        'fetchall is not a method of the Cursor object')
        self.cur.close()
        self.assertRaises(sqlite.ProgrammingError,
                              self.cur.fetchall)

    def CheckCursorSetoutputsize(self):
        self.assertTrue(hasattr(self.cur, "setoutputsize") and
                        type(self.cur.setoutputsize) == types.MethodType,
                        'setoutputsize is not a method of the Cursor object')
        self.cur.close()
        self.assertRaises(sqlite.ProgrammingError,
                              self.cur.setoutputsize, 1024)

    def CheckCursorSetinputsizes(self):
        self.assertTrue(hasattr(self.cur, "setinputsizes") and
                        type(self.cur.setinputsizes) == types.MethodType,
                        'setinputsizes is not a method of the Cursor object')
        self.cur.close()
        self.assertRaises(sqlite.ProgrammingError,
                              self.cur.setinputsizes, [1, 2, 3])

    def CheckExecuteWithSingleton(self):
        """Test execute() with a singleton string as the parameter."""
        try:
            self.cur.execute("select max(3,4)")
        except Exception as msg:
            self.fail(msg)

        self.assertEqual(type(self.cur.description), tuple,
                         "cur.description should be a tuple, but isn't.")

        clen = len(self.cur.description)
        self.assertEqual(clen, 1,
                         "Length of cur.description is %d, it should be %d." %
                         (clen, 1))


        self.assertEqual(len(self.cur.description[0]), 7,
                         "Length of cur.description[0] is %d, it should be 7." %
                         len(self.cur.description[0]))

        self.assertTrue(self.cur.description[0][0] == "max(3,4)"    and
                        self.cur.description[0][1] == sqlite.NUMBER and
                        self.cur.description[0][2] == None          and
                        self.cur.description[0][3] == None          and
                        self.cur.description[0][4] == None          and
                        self.cur.description[0][5] == None          and
                        self.cur.description[0][6] == None,
                        "cur.description[0] does not match the query.")
        self.cur.close()

    def CheckExecuteWithTuple(self):
        """Test execute() with a tuple as the parameter."""
        try:
            self.cur.execute("select max(?, ?)", (4, 5))
        except Exception as msg:
            self.fail(msg)
        self.cur.close()

    def CheckExecuteWithDictionary(self):
        """Test execute() with a dictionary as the parameter."""
        # no longer supported
        return
        try:
            self.cur.execute("select max(%(n1)s, %(n2)s)", {"n1": 5, "n2": 6})
        except Exception as msg:
            self.fail(msg)
        self.cur.close()

    def CheckQuotingOfLong(self):
        """Test whether longs are quoted properly for SQL."""
        try:
            self.cur.execute("select ? + ? as x", (5, 6))
        except Exception as msg:
            self.fail(msg)
        res = self.cur.fetchone()
        self.assertEqual(res.x, 11,
            "The addition of long should have returned %i, returned %i"
                % (11, res.x))

    def CheckCursorIterator(self):
        self.cur.execute("create table test (id, name)")
        self.cur.executemany("insert into test (id) values (?)",
                            [(1,), (2,), (3,)])
        self.cur.execute("select id from test")

        if sys.version_info[:2] >= (2,2):
            counter = 0
            for row in self.cur:
                if counter == 0:
                    self.assertEqual(row.id, 1,
                        "row.id should have been 1, was %i" % row.id)
                elif counter == 1:
                    self.assertEqual(row.id, 2,
                        "row.id should have been 2, was %i" % row.id)
                elif counter == 2:
                    self.assertEqual(row.id, 3,
                        "row.id should have been 3, was %i" % row.id)
                else:
                    self.fail("Iterated over too many rows.")
                counter += 1
        else:
            # Python 2.1
            counter = 0
            try:
                while 1:
                    row = next(self.cur)
                    if counter == 0:
                        self.assertEqual(row.id, 1,
                            "row.id should have been 1, was %i" % row.id)
                    elif counter == 1:
                        self.assertEqual(row.id, 2,
                            "row.id should have been 2, was %i" % row.id)
                    elif counter == 2:
                        self.assertEqual(row.id, 3,
                            "row.id should have been 3, was %i" % row.id)
                    else:
                        self.fail("Iterated over too many rows.")
                    counter += 1
            except IndexError:
                pass
            self.assertEqual(counter, 3,
                "Should have iterated over 3 items, was: %i" % counter)

    def CheckCursorScrollAndRownumber(self):
        self.cur.execute("create table test (id, name)")
        values = [("foo",)] * 20
        self.cur.executemany("insert into test (name) values (?)", values)
        self.cur.execute("select name from test")
        self.assertEqual(self.cur.rownumber, 0,
            "Directly after execute, rownumber must be 0, is: %i"
                % self.cur.rownumber)

        self.cur.scroll(1, "absolute")
        self.cur.scroll(5, "absolute")
        self.assertEqual(self.cur.rownumber, 5,
            "rownumber should be 5, is: %i"
                % self.cur.rownumber)

        self.cur.scroll(1, "relative")
        self.assertEqual(self.cur.rownumber, 6,
            "rownumber should be 6, is: %i"
                % self.cur.rownumber)

        self.assertRaises(sqlite.NotSupportedError, self.cur.scroll,
                              -2, "relative")
        self.assertRaises(sqlite.NotSupportedError, self.cur.scroll,
                              5, "absolute")

        self.assertRaises(IndexError, self.cur.scroll, 1000, "absolute")

    def CheckCursorConnection(self):
        if not isinstance(self.cur.connection, weakref.ProxyType) and \
           not isinstance(self.cur.connection, weakref.CallableProxyType):
            fail("cursor.connection doesn't return the correct type")

    def CheckCursorLastRowID(self):
        self.cur.execute("create table test (id integer primary key, name)")

        self.cur.execute("insert into test(name) values ('foo')")
        self.assertEqual(self.cur.lastrowid, 1,
            "lastrowid should be 1, is %i" % self.cur.lastrowid)

        self.cur.execute("insert into test(name) values ('foo')")
        self.assertEqual(self.cur.lastrowid, 2,
            "lastrowid should be 2, is %i" % self.cur.lastrowid)

    def CheckResultObject(self):
        try:
            self.cur.execute("select max(3,4)")
##             self.assertEqual(self.cur.rowcount, 1,
##                              "cur.rowcount is %d, it should be 1." %
##                              self.cur.rowcount)
            self.res = self.cur.fetchall()
        except Exception as msg:
            self.fail(msg)

        self.assertEqual(type(self.res), list,
                         'cur.fetchall() did not return a sequence.')

        self.assertEqual(len(self.res), 1,
                         'Length of the list of results is %d, it should be 1' %
                         len(self.res))

        self.assertTrue(isinstance(self.res[0], sqlite.main.Row),
                        'cur.fetchall() did not return a list of Rows.')

    def CheckResultFetchone(self):
        try:
            self.cur.execute("select max(3,4)")
            self.res = self.cur.fetchone()
##             self.assertEqual(self.cur.rowcount, 1,
##                              'cur.rowcount is %d, it should be 1.' %
##                              self.cur.rowcount)
        except Exception as msg:
            self.fail(msg)

        self.assertTrue(isinstance(self.res, sqlite.main.Row),
                        "cur.fetchone() does not return a Row.")

        try:
            self.res = self.cur.fetchone()
            self.assertEqual(self.res, None,
                             "res should be None at this point, but it isn't.")
        except Exception as msg:
            self.fail(msg)

    def CheckRowCountAfterInsert(self):
        return
        try:
            self.cur.execute("create table test(a)")
            self.cur.execute("insert into test(a) values (5)")
            self.assertEqual(self.cur.rowcount, 1,
                            'cur.rowcount is %d, it should be 1.' %
                            self.cur.rowcount)
        except Exception as msg:
            self.fail(msg)

    def CheckRowCountAfterUpdate(self):
        return
        try:
            self.cur.execute("create table test(a, b)")
            self.cur.execute("insert into test(a, b) values (1, 2)")
            self.cur.execute("insert into test(a, b) values (1, 3)")
            self.cur.execute("insert into test(a, b) values (1, 4)")
            self.cur.execute("update test set b=1 where a=1")
            self.assertEqual(self.cur.rowcount, 3,
                            'cur.rowcount is %d, it should be 3.' %
                            self.cur.rowcount)
        except Exception as msg:
            self.fail(msg)

    def CheckRowCountAfterDelete(self):
        return
        try:
            self.cur.execute("create table test(a, b)")
            self.cur.execute("insert into test(a, b) values (1, 2)")
            self.cur.execute("insert into test(a, b) values (1, 3)")
            self.cur.execute("insert into test(a, b) values (2, 4)")
            self.cur.execute("delete from test where a=1")
            self.assertEqual(self.cur.rowcount, 2,
                            'cur.rowcount is %d, it should be 2.' %
                            self.cur.rowcount)
        except Exception as msg:
            self.fail(msg)

    def CheckSelectOfNonPrintableString(self):
        try:
            a = '\x01\x02\x03\x04'
            self.cur.execute('select ? as a', a)
            r = self.cur.fetchone()
            self.assertEqual(len(r.a), len(a),
                             "Length of result is %d, it should be %d."  %
                             (len(r.a), len(a)))
            self.assertTrue(r.a == a,
                             "Result is '%s', it should be '%s'" % (r.a, a))
        except Exception as msg:
            self.fail(msg)

    def CheckQuotingIntWithPercentS(self):
        try:
            self.cur.execute("create table test(a number)")
            self.cur.execute("insert into test(a) values (?)", (5,))
        except Exception as msg:
            self.fail(msg)

    def CheckQuotingLongWithPercentS(self):
        try:
            self.cur.execute("create table test(a number)")
            self.cur.execute("insert into test(a) values (?)", (50000000,))
        except Exception as msg:
            self.fail(msg)

    def CheckQuotingFloatWithPercentS(self):
        try:
            self.cur.execute("create table test(a number)")
            self.cur.execute("insert into test(a) values (?)", (-3.24,))
        except Exception as msg:
            self.fail(msg)

    def CheckQuotingIntWithPyQuoting(self):
        try:
            self.cur.execute("create table test(a number)")
            self.cur.execute("insert into test(a) values (?)", (5,))
        except Exception as msg:
            self.fail(msg)

    def CheckQuotingLongWithPyQuoting(self):
        try:
            self.cur.execute("create table test(a number)")
            self.cur.execute("insert into test(a) values (?)", (50000000,))
        except Exception as msg:
            self.fail(msg)

    def CheckQuotingFloatWithPyQuoting(self):
        try:
            self.cur.execute("create table test(a number)")
            self.cur.execute("insert into test(a) values (?)", (-3.24,))
        except Exception as msg:
            self.fail(msg)

    def CheckBlob(self):
        """Test whether blobs work as expected."""
        a = 'a\0b'
        self.cur.execute("create table test(a blob)")
        self.cur.execute("insert into test(a) values (?)", a)
        self.cur.execute('select * from test')
        r = self.cur.fetchone()
        self.assertEqual(len(r[0]), len(a),
                         "Length of result is %d, it should be %d."  %
                         (len(r[0]), len(a)))
        self.assertTrue(r.a == a,
                        "Result is '%s', it should be '%s'" % (r.a, a))

    def CheckNone(self):
        """Test whether None is returned when expected."""
        self.cur.execute('create table test(a str)')
        self.cur.execute('insert into test(a) values (NULL)')
        self.cur.execute('insert into test(a) values ("")')
        self.cur.execute('select * from test')
        r = self.cur.fetchone()
        self.assertEqual(r[0], None,
                         "Value is is %s, it should be None."  %r[0])
        r = self.cur.fetchone()
        self.assertEqual(r[0], "",
                         'Value is is %s, it should be "".'  %r[0])

    def CheckBool(self):
        """Test whether a Bool is returned when expected."""
        self.cur.execute('create table test(a bool)')
        self.cur.execute('insert into test(a) values (?)', True)
        self.cur.execute('insert into test(a) values (?)', False)
        self.cur.execute('insert into test(a) values (1)')
        self.cur.execute('insert into test(a) values (0)')
        self.cur.execute('insert into test(a) values (100)')
        self.cur.execute('select * from test')
        r = self.cur.fetchone()
        self.assertEqual(r[0], True,
                         "Value is is %s, it should be True."  %r[0])
        self.assertEqual(type(r[0]), bool,
                         "Type is is %s, it should be <bool>."  %type(r[0]))
        r = self.cur.fetchone()
        self.assertEqual(r[0], False,
                         'Value is is %s, it should be False.'  %r[0])
        self.assertEqual(type(r[0]), bool,
                         "Type is is %s, it should be <bool>."  %type(r[0]))
        r = self.cur.fetchone()
        self.assertEqual(r[0], True,
                         "Value is is %s, it should be True."  %r[0])
        self.assertEqual(type(r[0]), bool,
                         "Type is is %s, it should be <bool>."  %type(r[0]))
        r = self.cur.fetchone()
        self.assertEqual(r[0], False,
                         'Value is is %s, it should be False.'  %r[0])
        self.assertEqual(type(r[0]), bool,
                         "Type is is %s, it should be <bool>."  %type(r[0]))
        r = self.cur.fetchone()
        self.assertEqual(r[0], True,
                         "Value is is %s, it should be True."  %r[0])
        self.assertEqual(type(r[0]), bool,
                         "Type is is %s, it should be <bool>."  %type(r[0]))

    def CheckReadonlyDatabase(self):
        import tempfile, os
        fd, fn = tempfile.mkstemp()
        os.close(fd)
        os.chmod(fn, 0o444)
        cx = sqlite.connect(fn)
        cu = cx.cursor()
        try:
            cu.execute('CREATE TABLE foo (bar)')
        except sqlite.ProgrammingError as e:
            assert (str(e) == "attempt to write a readonly database")

    def CheckNoColumnNameResult(self):
        self.cur.execute('pragma schema_version')
        assert (self.cur.fetchall() == [(0,)])

def suite():
    dbapi_suite = unittest.makeSuite(DBAPICompliance, "Check")
    module_suite = unittest.makeSuite(moduleTestCases, "Check")
    test_suite = unittest.TestSuite((dbapi_suite, module_suite))
    return test_suite

def main():
    runner = unittest.TextTestRunner()
    runner.run(suite())

if __name__ == "__main__":
    main()
