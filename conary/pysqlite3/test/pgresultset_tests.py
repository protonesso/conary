#!/usr/bin/env python
import testsupport
import os, unittest, sys
import sqlite3 as sqlite

class PgResultSetTests(unittest.TestCase, testsupport.TestSupport):
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

    def getResult(self):
        try:
            self.cur.execute("DROP TABLE TEST")
        except sqlite.DatabaseError as reason:
            pass

        self.cur.execute("CREATE TABLE TEST (id, name, age)")
        self.cur.execute("INSERT INTO TEST (id, name, age) VALUES (?, ?, ?)",
                            (5, 'Alice', 29))
        self.cur.execute("SELECT id, name, age FROM TEST")
        return self.cur.fetchone()

    def CheckAttributeAccess(self):
        res = self.getResult()
        if not hasattr(res, "id"):
            self.fail("Resultset doesn't have attribute 'id'")
        if not hasattr(res, "ID"):
            self.fail("Resultset doesn't have attribute 'ID'")

    def CheckAttributeValue(self):
        res = self.getResult()
        if res.id != 5:
            self.fail("id should be 5, is %i" % res.id)
        if res.ID != 5:
            self.fail("ID should be 5, is %i" % res.ID)

    def CheckKeyAccess(self):
        res = self.getResult()
        if not "id" in res:
            self.fail("Resultset doesn't have item 'id'")
        if not "ID" in res:
            self.fail("Resultset doesn't have item 'ID'")

    def CheckKeyValue(self):
        res = self.getResult()
        if res["id"] != 5:
            self.fail("id should be 5, is %i" % res.id)
        if res["ID"] != 5:
            self.fail("ID should be 5, is %i" % res.ID)

    def CheckIndexValue(self):
        res = self.getResult()
        if res[0] != 5:
            self.fail("item 0 should be 5, is %i" % res.id)

    def Check_haskey(self):
        res = self.getResult()
        if "id" not in res:
            self.fail("resultset should have key 'id'")
        if "ID" not in res:
            self.fail("resultset should have key 'ID'")
        if "Id" not in res:
            self.fail("resultset should have key 'Id'")

    def Check_len(self):
        l = len(self.getResult())
        if l != 3:
            self.fail("length of resultset should be 3, is %i", l)

    def Check_keys(self):
        res = self.getResult()
        if list(res.keys()) != ["id", "name", "age"]:
            self.fail("keys() should return %s, returns %s" %
                        (["id", "name", "age"], list(res.keys())))

    def Check_values(self):
        val = list(self.getResult().values())
        if val != (5, 'Alice', 29):
            self.fail("Wrong values(): %s" % val)

    def Check_items(self):
        it = list(self.getResult().items())
        if it != [("id", 5), ("name", 'Alice'), ("age", 29)]:
            self.fail("Wrong items(): %s" % it)

    def Check_get(self):
        res = self.getResult()
        v = res.get("id")
        if v != 5:
            self.fail("Wrong result for get [1]")

        v = res.get("ID")
        if v != 5:
            self.fail("Wrong result for get [2]")

        v = res.get("asdf")
        if v is not None:
            self.fail("Wrong result for get [3]")

        v = res.get("asdf", 6)
        if v != 6:
            self.fail("Wrong result for get [4]")

def suite():
    tests = [unittest.makeSuite(PgResultSetTests, "Check"),
                                unittest.makeSuite(PgResultSetTests, "Check")]
    return unittest.TestSuite(tests)

def main():
    runner = unittest.TextTestRunner()
    runner.run(suite())

if __name__ == "__main__":
    main()
