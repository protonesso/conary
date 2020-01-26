#!/usr/bin/env python
#-*- coding: ISO-8859-1 -*-
import testsupport
import types, unittest
import sqlite3 as sqlite
import _sqlite3 as _sqlite

try:
    from mx.DateTime import DateTime, DateTimeDelta, DateFrom, \
            TimeFrom, DateTimeDeltaFrom
    have_datetime = 1
except ImportError:
    have_datetime = 0

def sqlite_is_at_least(major, minor, micro):
    version = list(map(int, _sqlite.sqlite_version().split(".")))
    return version >= (major, minor, micro)

class MyType:
    def __init__(self, val):
        self.val = int(val)

    def _quote(self):
        return str(self.val)

    def __repr__(self):
        return "MyType(%s)" % self.val

    def __cmp__(self, other):
        assert(isinstance(other, MyType))
        return cmp(self.val, other.val)

class MyTypeNew(MyType):
    def __quote__(self):
        return str(self.val)

    def __getattr__(self, key):
        # Forbid access to the old-style _quote method
        if key == "_quote":
            raise AttributeError
        else:
            return self.__dict__[key]

class ExpectedTypes(unittest.TestCase, testsupport.TestSupport):
    def setUp(self):
        self.filename = self.getfilename()
        self.cnx = sqlite.connect(self.filename, converters={"mytype": MyType})
        self.cur = self.cnx.cursor()

    def tearDown(self):
        try:
            self.cnx.close()
            self.removefile()
        except AttributeError:
            pass
        except sqlite.InterfaceError:
            pass

    def CheckExpectedTypesStandardTypes(self):
        self.cur.execute("create table test (a, b, c)")
        self.cur.execute("insert into test(a, b, c) values (5, 6.3, 'hello')")
        #self.cur.execute("-- types int, float, str")
        self.cur.execute("select * from test")
        res = self.cur.fetchone()
        self.assertTrue(isinstance(res.a, int),
                        "The built-in int converter didn't work.")
        self.assertTrue(isinstance(res.b, float),
                        "The built-in float converter didn't work.")
        self.assertTrue(isinstance(res.c, bytes),
                        "The built-in string converter didn't work.")

    def CheckExpectedTypesStandardTypesNull(self):
        self.cur.execute("create table test (a, b, c)")
        self.cur.execute("insert into test(a, b, c) values (NULL, NULL, NULL)")
        #self.cur.execute("-- types int, float, str")
        self.cur.execute("select * from test")
        res = self.cur.fetchone()
        self.assertTrue(res.a == None,
                        "The built-in int converter should have returned None.")
        self.assertTrue(res.b == None,
                        "The built-in float converter should have returned None.")
        self.assertTrue(res.c == None,
                        "The built-in string converter should have returned None.")

    def CheckExpectedTypesCustomTypes(self):
        # no longer supported
        return
        value = MyType(10)
        self.cur.execute("create table test (a)")
        self.cur.execute("insert into test(a) values (?)", value)
        self.cur.execute("-- types mytype")
        self.cur.execute("select a from test")
        res = self.cur.fetchone()

        self.assertTrue(isinstance(res.a, MyType),
                        "The converter did return the wrong type.")
        self.assertEqual(value, res.a,
                             "The returned value and the inserted one are different.")

    def CheckNewQuoteMethod(self):
        value = MyTypeNew(10)
        self.cur.execute("create table test (a integer)")
        self.cur.execute("insert into test(a) values (?)", value)
        #self.cur.execute("-- types mytype")
        self.cur.execute("select a from test")
        res = self.cur.fetchone()

        self.assertEqual(10, res.a,
                             "The returned value and the inserted one are different.")

    def CheckExpectedTypesCustomTypesNull(self):
        value = None
        self.cur.execute("create table test (a)")
        self.cur.execute("insert into test(a) values (?)", value)
        self.cur.execute("select a from test")
        res = self.cur.fetchone()

        self.assertTrue(res.a == None,
                        "The converter should have returned None.")

    def CheckResetExpectedTypes(self):
        # no longer supported
        return
        self.cur.execute("create table test (a str)")
        self.cur.execute("insert into test(a) values (5)")
        #self.cur.execute("-- types int")
        self.cur.execute("select a from test")
        res = self.cur.fetchone()
        self.assertTrue(isinstance(res.a, int),
                     "The built-in int converter didn't work.")
        self.cur.execute("select a from test")
        res = self.cur.fetchone()
        self.assertTrue(isinstance(res.a, bytes),
                     "'resetting types' didn't succeed.")

    if have_datetime:
        def CheckDateTypes(self):
            dt = DateTime(2002, 6, 15)
            dtd = DateTimeDelta(0, 0, 0, 1)

            self.cur.execute("create table test (t timestamp)")
            self.cur.execute("insert into test(t) values (?)", (dt,))
            self.cur.execute("select t from test")
            res = self.cur.fetchone()

            self.assertEqual(dt, res.t,
                "DateTime object should have been %s, was %s"
                    % (repr(dt), repr(res.t)))

            self.cur.execute("drop table test")
            self.cur.execute("create table test(i interval)")
            self.cur.execute("insert into test(i) values (?)", (dtd,))
            self.cur.execute("select i from test")
            res = self.cur.fetchone()

            self.assertEqual(dtd, res.i,
                "DateTimeDelta object should have been %s, was %s"
                    % (repr(dtd), repr(res.i)))

class UnicodeTestsLatin1(unittest.TestCase, testsupport.TestSupport):
    def setUp(self):
        self.filename = self.getfilename()
        self.cnx = sqlite.connect(self.filename, encoding=("iso-8859-1",))
        self.cur = self.cnx.cursor()

    def tearDown(self):
        try:
            self.cnx.close()
            self.removefile()
        except AttributeError:
            pass
        except sqlite.InterfaceError:
            pass

    def CheckGetSameBack(self):
        test_str = str("�sterreich", "latin1")
        self.cur.execute("create table test (a UNICODE)")
        self.cur.execute("insert into test(a) values (?)", test_str)
        self.cur.execute("select a from test")
        res = self.cur.fetchone()
        self.assertEqual(type(test_str), type(res.a),
            "Something other than a Unicode string was fetched: %s"
                % (str(type(res.a))))
        self.assertEqual(test_str, res.a,
            "Fetching the unicode string doesn't return the inserted one.")

class UnicodeTestsUtf8(unittest.TestCase, testsupport.TestSupport):
    def setUp(self):
        self.filename = self.getfilename()
        self.cnx = sqlite.connect(self.filename, encoding="utf-8")
        self.cur = self.cnx.cursor()

    def tearDown(self):
        try:
            self.cnx.close()
            self.removefile()
        except AttributeError:
            pass
        except sqlite.InterfaceError:
            pass

    def CheckGetSameBack(self):
        # PREZIDENT ROSSI'SKO' FEDERACII �sterreich
        test_str = str("ПРЕЗИДЕНТ РОССИЙСКОЙ ФЕДЕРАЦИИ Österreich", "utf-8")

        self.cur.execute("create table test (a UNICODE)")
        self.cur.execute("insert into test(a) values (?)", test_str)
        self.cur.execute("select a from test")
        res = self.cur.fetchone()
        self.assertEqual(type(test_str), type(res.a),
            "Something other than a Unicode string was fetched: %s"
                % (str(type(res.a))))
        self.assertEqual(test_str, res.a,
            "Fetching the unicode string doesn't return the inserted one.")

class UnicodeTestsKOI8R(unittest.TestCase, testsupport.TestSupport):
    def setUp(self):
        self.filename = self.getfilename()
        self.cnx = sqlite.connect(self.filename, encoding="koi8-r")
        self.cur = self.cnx.cursor()

    def tearDown(self):
        try:
            self.cnx.close()
            self.removefile()
        except AttributeError:
            pass
        except sqlite.InterfaceError:
            pass

    def CheckGetSameBack(self):
        # PREZIDENT ROSSI'SKO' FEDERACII
        # (President of the Russian Federation)
        test_str = str("��������� ���������� ���������", "koi8-r")

        self.cur.execute("create table test (a UNICODE)")
        self.cur.execute("insert into test(a) values (?)", test_str)
        self.cur.execute("select a from test")
        res = self.cur.fetchone()
        self.assertEqual(type(test_str), type(res.a),
            "Something other than a Unicode string was fetched: %s"
                % (str(type(res.a))))
        self.assertEqual(test_str, res.a,
            "Fetching the unicode string doesn't return the inserted one.")

class SQLiteBuiltinTypeSupport(unittest.TestCase, testsupport.TestSupport):
    def setUp(self):
        self.filename = self.getfilename()
        self.cnx = sqlite.connect(self.filename, encoding="koi8-r")
        self.cur = self.cnx.cursor()

    def tearDown(self):
        try:
            self.cnx.close()
            self.removefile()
        except AttributeError:
            pass
        except sqlite.InterfaceError:
            pass

    def CheckInt(self):
        self.cur.execute("create table test (a INTEGER)")
        self.cur.execute("insert into test(a) values (?)", 5)
        self.cur.execute("select a from test")
        res = self.cur.fetchone()
        self.assertEqual(type(5), type(res.a),
            "Something other than an INTEGER was fetched: %s"
                % (str(type(res.a))))

    def CheckFloat(self):
        self.cur.execute("create table test (a FLOAT)")
        self.cur.execute("insert into test(a) values(?)", 5.7)
        self.cur.execute("select a from test")
        res = self.cur.fetchone()
        self.assertEqual(type(5.7), type(res.a),
            "Something other than a FLOAT was fetched: %s"
                % (str(type(res.a))))

    def CheckString(self):
        self.cur.execute("create table test (a VARCHAR(20))")
        self.cur.execute("insert into test(a) values (?)", "foo")
        self.cur.execute("select a from test")
        res = self.cur.fetchone()
        self.assertEqual(type("foo"), type(res.a),
            "Something other than a VARCHAR was fetched: %s"
                % (str(type(res.a))))

    def CheckBinary(self):
        bindata = "".join([chr(x) for x in range(256)])
        self.cur.execute("create table test(b BINARY)")
        self.cur.execute("insert into test(b) values(?)", bindata)
        self.cur.execute("select b from test")
        res = self.cur.fetchone()
        self.assertEqual(bindata, res.b, "Binary roundtrip didn't produce original string")
        self.assertEqual(self.cur.description[0][1], sqlite.BINARY, "Wrong type code")

    if have_datetime:
        def CheckDate(self):
            self.cur.execute("create table test (a DATE)")
            d = DateFrom("2002-05-07")
            self.cur.execute("insert into test(a) values(?)", d)
            self.cur.execute("select a from test")
            res = self.cur.fetchone()
            if res.a != d:
                self.fail("didn't get back the same DATE")

        def CheckTime(self):
            self.cur.execute("create table test (a TIME)")
            t = TimeFrom("22:15:00")
            self.cur.execute("insert into test(a) values(?)", t)
            self.cur.execute("select a from test")
            res = self.cur.fetchone()
            if res.a != t:
                self.fail("didn't get back the same TIME")

        def CheckTimestamp(self):
            self.cur.execute("create table test (a TIMESTAMP)")
            d = DateFrom("2002-05-07 22:15:00")
            self.cur.execute("insert into test(a) values(?)", d)
            self.cur.execute("select a from test")
            res = self.cur.fetchone()
            if res.a != d:
                self.fail("didn't get back the same TIMESTAMP")

        def CheckInterval(self):
            self.cur.execute("create table test (a INTERVAL)")
            d = DateTimeDeltaFrom("02:00:00")
            self.cur.execute("insert into test(a) values(?)", d)
            self.cur.execute("select a from test")
            res = self.cur.fetchone()
            if res.a != d:
                self.fail("didn't get back the same INTERVAL")

def suite():
    expected_suite = unittest.makeSuite(ExpectedTypes, "Check")
    unicode_suite1 = unittest.makeSuite(UnicodeTestsLatin1, "Check")
    unicode_suite2 = unittest.makeSuite(UnicodeTestsUtf8, "Check")
    unicode_suite3 = unittest.makeSuite(UnicodeTestsKOI8R, "Check")
    builtin_suite = unittest.makeSuite(SQLiteBuiltinTypeSupport, "Check")

    return unittest.TestSuite((expected_suite, unicode_suite1, unicode_suite2,
        unicode_suite3, builtin_suite))

def main():
    runner = unittest.TextTestRunner()
    runner.run(suite())

if __name__ == "__main__":
    main()
