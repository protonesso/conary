#
# Copyright (c) SAS Institute Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


import os
import re

from conary import sqlite3

from .base_drv import BaseDatabase, BaseCursor, BaseSequence, BaseKeywordDict
from . import sqlerrors, sqllib

class KeywordDict(BaseKeywordDict):
    keys = BaseKeywordDict.keys.copy()
    keys.update({
        'PRIMARYKEY'    : 'INTEGER PRIMARY KEY AUTOINCREMENT',
        'BIGINT'        : 'INTEGER',
        'BIGSERIAL'     : 'INTEGER AUTOINCREMENT',
        'BIGPRIMARYKEY' : 'INTEGER PRIMARY KEY AUTOINCREMENT', 
        'PATHTYPE'      : 'TEXT',
        'STRING'        : 'TEXT',
        })

# implement the regexp function for sqlite
def _regexp(pattern, item):
    regexp = re.compile(pattern)
    return regexp.match(item) is not None
# a timestamp function compatible with other backends
def _timestamp():
    return sqllib.toDatabaseTimestamp()

class Cursor(BaseCursor):
    driver = "sqlite"

    # sqlite is smart - this is only a noop
    def binary(self, s):
        return s

    # capture and translate exceptions
    def _tryExecute(self, func, *params, **kw):
        try:
            ret = func(*params, **kw)
        except sqlite3.ProgrammingError as e:
            if ((e.args[0].startswith("column") and e.args[0].endswith("not unique"))
                    or e.args[0].startswith("UNIQUE constraint failed")):
                raise sqlerrors.ColumnNotUnique(e)
            elif e.args[0] == 'attempt to write a readonly database':
                raise sqlerrors.ReadOnlyDatabase(str(e))
            raise sqlerrors.CursorError(e.args[0], e)
        except sqlite3.DatabaseError as e:
            if e.args[0].startswith('duplicate column name:'):
                raise sqlerrors.DuplicateColumnName(str(e))
            if e.args[0].startswith('database is locked'):
                raise sqlerrors.DatabaseLocked(str(e))
            if e.args[0].startswith("no such table"):
                raise sqlerrors.InvalidTable(str(e))
            raise sqlerrors.CursorError(e.args[0], e)
        return ret

    def execute(self, sql, *args, **kw):
        self._executeCheck(sql)
        args, kw = self._executeArgs(args, kw)
        if len(args) == 0:
            ret = self._tryExecute(self._cursor.execute, sql, **kw)
        else:
            st = kw.pop("start_transaction", True)
            if len(kw):
                raise sqlerrors.CursorError(
                    "Do not pass both positional and named bind arguments",
                    *args, **kw)
            kw["start_transaction"] = st
            ret = self._tryExecute(self._cursor.execute, sql, *args, **kw)
        if ret == self._cursor:
            return self
        return ret

    # we need to wrap this one through the exception translation layer
    def executemany(self, sql, paramList, **kw):
        self._executeCheck(sql)
        return self._tryExecute(self._cursor.executemany, sql, paramList, **kw)

    def compile(self, sql):
        return self._cursor.compile(sql)

    def execstmt(self, stmt, *args):
        return self._tryExecute(self._cursor.execstmt, stmt, *args)

# Sequence implementation for sqlite
class Sequence(BaseSequence):
    def __init__(self, db, name, cu = None):
        BaseSequence.__init__(self, db, name)
        self.cu = cu
        if cu is None:
            self.cu = db.cursor()
        if name in db.sequences:
            return
        self.cu.execute("""
        CREATE TABLE %s_sequence (
            val         INTEGER PRIMARY KEY AUTOINCREMENT
        )""" % (name,))
        # refresh schema
        db.loadSchema()

    def nextval(self):
        # We have to make sure we do this in a transaction
        if not self.db.dbh.inTransaction:
            self.db.transaction()
        self.cu.execute("DELETE FROM %s" % self.seqName)
        self.cu.execute("INSERT INTO %s (val) VALUES(NULL)" % self.seqName)
        self.cu.execute("SELECT val FROM %s" % self.seqName)
        self.__currval = self.cu.fetchone()[0]
        return self.__currval

    # Enforce garbage collection to avoid circular deps
    def __del__(self):
        if self.db.dbh.inTransaction and self.__currval is not None:
            self.db.commit()
        self.db = self.cu = None

class Database(BaseDatabase):
    driver = "sqlite"
    kind = "sqlite"
    alive_check = "select count(*) from sqlite_master"
    cursorClass = Cursor
    sequenceClass = Sequence
    basic_transaction = "begin immediate"
    VIRTUALS = [ ":memory:" ]
    TIMEOUT = 10000
    keywords = KeywordDict()

    def connect(self, **kwargs):
        assert(self.database)
        kwargs.setdefault("timeout", self.TIMEOUT)
        #kwargs.setdefault("command_logfile", open("/tmp/sqlite.log", "a"))
        #kwargs.setdefault("lockJournal", True)
        try:
            self.dbh = sqlite3.connect(self.database, **kwargs)
        except sqlite3.InternalError as e:
            if str(e) == 'database is locked':
                raise sqlerrors.DatabaseLocked(e)
            raise
        except sqlite3.DatabaseError as e:
            raise sqlerrors.DatabaseError(e)
        # add a regexp funtion to enable SELECT FROM bar WHERE bar REGEXP .*
        self.dbh.create_function('regexp', 2, _regexp)
        # add the serialized timestamp function
        self.dbh.create_function("unix_timestamp", 0, _timestamp)
        # reset the tempTables since we just lost them because of the (re)connect
        self.tempTables = sqllib.CaselessDict()
        if self.database in self.VIRTUALS:
            self.inode = (None, None)
            self.closed = False
            return True
        sb = os.stat(self.database)
        self.inode = (sb.st_dev, sb.st_ino)
        self.closed = False
        return True

    def close(self):
        self.inode = (None, None)
        return BaseDatabase.close(self)

    def reopen(self):
        if self.database in self.VIRTUALS:
            return False
        sb = os.stat(self.database)
        inode = (sb.st_dev, sb.st_ino)
        if self.inode != inode and self.dbh:
            self.dbh.close()
            self.dbh = None
        if not self.dbh:
            return self.connect()
        return False

    def loadSchema(self):
        BaseDatabase.loadSchema(self)
        c = self.cursor()
        c.execute("select type, name, tbl_name from sqlite_master")
        slist = c.fetchall()
        if not len(slist):
            return self.version
        for (type, name, tbl_name) in slist:
            if type == "table":
                # skip internal sqlite tables
                if name.startswith('sqlite_'):
                    continue
                if name.endswith("_sequence"):
                    self.sequences.setdefault(name[:-len("_sequence")], None)
                else:
                    self.tables.setdefault(name, [])
            elif type == "view":
                self.views.setdefault(name, None)
            elif type == "index":
                if name.startswith("sqlite_autoindex_"):
                    continue
                self.tables.setdefault(tbl_name, []).append(name)
            elif type == "trigger":
                self.triggers.setdefault(name, tbl_name)
        return self.getVersion()

    def analyze(self, table=""):
        if sqlite3._sqlite.sqlite_version_info() <= (3, 2, 2):
            # ANALYZE didn't appear until 3.2.3
            return
        # sqlite's analyzer has not been tested yet for single table updates.
        if table:
            return

        # perform table analysis to help the optimizer
        doAnalyze = False
        cu = self.cursor()
        # if there are pending changes, just re-run ANALYZE
        if self.dbh.inTransaction:
            doAnalyze = True
        else:
            # check to see if the sqlite_stat1 table exists.
            # ANALYZE creates it.
            if "sqlite_stat1" not in self.tables:
                doAnalyze = True

        if doAnalyze:
            cu.execute('ANALYZE %s' % table)
            self.commit()
            self.loadSchema()

    # Transaction support
    def inTransaction(self, default=None):
        """
        Return C{True} if the connection currently has an active
        transaction.
        """
        return self.dbh.inTransaction

    # A trigger that syncs up the changed column
    def createTrigger(self, table, column, onAction, pinned=None):
        if pinned is not None:
            import warnings
            warnings.warn(
                'The "pinned" kwparam to createTrigger is deprecated and '
                'no longer has any affect on triggers',
                DeprecationWarning)
        onAction = onAction.lower()
        assert(onAction in ["insert", "update"])
        # prepare the sql and the trigger name and pass it to the
        # BaseTrigger for creation
        when = "AFTER"
        sql = """
        UPDATE %s SET %s = unix_timestamp() WHERE _ROWID_ = NEW._ROWID_ ;
        """ % (table, column)
        return BaseDatabase.createTrigger(self, table, when, onAction, sql)

    # extract the sql fields from a schema definition
    def __parseFields(self, fStr):
        types = """((
        PRIMARY\ KEY | NOT\ NULL | AUTOINCREMENT | DEFAULT\ \S+ |
        (VAR)?(CHAR|BINARY) \s* (\(\w+\))? | NUMERIC \s* \(\w+(,\w+)?\) |
        INT | INTEGER | BLOB | TEXT | STR | STRING | UNIQUE ) \s*?)+
        """
        constraint = "\s* ( ( REFERENCES \s* \w+\(\w+\) | ON \s* (DELETE|UPDATE) \s* (CASCADE|RESTRICT) ) \s* )*"
        # extract the fields
        fields = []
        fStr = "(%s)" % (fStr,)
        regex = re.compile(
            """^\s* \( \s*
            (?P<name>\w+) \s+ (?P<type>%s) (?P<constraint>%s)?, \s* (?P<rest>.*?)
            \s* \)  \s* $""" % (types, constraint),
            re.I | re.S | re.X)
        lastField = False
        while 1:
            match = regex.match(fStr)
            if not match:
                if lastField:
                    break
                # try it as the last field
                regex = re.compile(
                    """^\s* \( \s*
                    (?P<name>\w+) \s+ (?P<type>%s) (?P<constraint>%s)?
                    \s* \)  \s* $""" % (types, constraint),
                    re.I | re.S | re.X)
                match = regex.match(fStr)
                if not match:
                    break
                lastField = True
            d = match.groupdict()
            fields.append((d['name'], d["type"], d["constraint"]))
            if lastField:
                break
            fStr = "(%s)" % (d["rest"],)
        assert(fields), "Could not parse table fields:\n%s" %(stmt["fields"],)
        return fields

    # grab the SQL definition of this table
    def __getSQLstmt(self, table):
        cu = self.dbh.cursor()
        cu.execute("SELECT sql FROM sqlite_master WHERE "
                   "lower(tbl_name) = lower('%s') AND type = 'table'" % (table,))
        oldSql = cu.fetchone()[0]
        stmt = {}
        regex = re.compile(
            """^\s*
            (?P<header>CREATE\s+TABLE\s+\w+) \s*
            \( \s*
            (?P<fields>.*) \s*
            (?P<constraint>, \s* CONSTRAINT .*?)?
            \s* \)  \s*
            (?P<final> .*)$""",
            re.I | re.S | re.X)
        match = regex.match(oldSql)
        assert(match), "CREATE TABLE statement does not match regex:\n%s" % (oldSql,)
        stmt.update(match.groupdict())
        if stmt["constraint"] is None:
            stmt["constraint"] = ""
        return stmt

    # since sqlite does not provide us with SQL-accessible information
    # about the column options such as DEFAULT and/or NOT NULL, the
    # safest way to affect a column change is to grab the old table
    # definition from the sqlite_master and whish for the best...
    def renameColumn(self, table, oldName, newName):
        # avoid busywork
        if oldName.lower() == newName.lower():
            return True
        assert(self.dbh)
        stmt = self.__getSQLstmt(table)
        # parse the table fields
        fields = self.__parseFields(stmt["fields"])
        if oldName.lower() not in [x[0].lower() for x in fields]:
            raise sqlerrors.DatabaseError(
                "Table %s does not have a column named %s" % (table, oldName),
                table, oldName, newName)
        newFields = []
        for (n, t, c) in fields:
            if n.lower() == oldName.lower():
                newFields.append((newName, t, c))
            else:
                newFields.append((n, t, c))
        stmt["fields"] = ",\n".join(["%s %s %s" % x for x in newFields])
        # real databases automatically commit on DDL, os we shouldn't be any different
        self.dbh.commit()
        # sqlite can roll back DDL
        cu = self.dbh.cursor()
        cu.execute(self.basic_transaction)
        cu.execute("ALTER TABLE %s RENAME TO %s_tmp" % (table, table))
        # FIXME: the constraint should also be updated in case we're
        # changing the fields listed in the constraint. sqlite doesn't care...
        newSql = "%(header)s (\n%(fields)s %(constraint)s\n) %(final)s" % stmt
        cu.execute(newSql)
        # sanity runtime check - the new table should have the same
        # number of columns as the old one.
        # select 0 rows to get descriptions back
        cu.execute("select * from %s_tmp where 1 is NULL" % (table,))
        set1 = [x[0].lower() for x in cu.description]
        cu.execute("select * from %s where 1 is NULL" % (table,))
        set2 = [x[0].lower() for x in cu.description]
        # make sure that the differences are only the columns we are modifying
        if len(set1) != len(set2) or \
           set(set1) - set(set2) != set([oldName.lower()]) or \
           set(set2) - set(set1) != set([newName.lower()]):
            # oops, don't mess with it
            self.dbh.rollback()
            raise RuntimeError("""Could not parse SQL definition for table %s
            while attempting column rename %s -> %s""" % (table, oldName, newName),
                               table, set1, set2)
        cu.execute("INSERT INTO %s (%s) SELECT %s from %s_tmp" % (
            table, ",".join([x[0] for x in newFields]),
            ",".join([x[0] for x in fields]), table))
        cu.execute("DROP TABLE %s_tmp" % (table,))
        return True

    def dropColumn(self, table, name):
        assert(self.dbh)

    # sqlite is more peculiar when it comes to firing off transactions
    def transaction(self, name = None):
        assert(self.dbh)
        cu = self.cursor()
        if self.dbh.inTransaction:
            return cu
        try:
            self.dbh._begin()
        except sqlite3.ProgrammingError as e:
            if str(e) == 'attempt to write a readonly database':
                raise sqlerrors.ReadOnlyDatabase(str(e))
            raise
        except sqlite3.DatabaseError as e:
            if e.args[0].startswith('database is locked'):
                raise sqlerrors.DatabaseLocked(str(e))
            raise
        return cu

    def rollback(self, name=None):
        assert not name
        # When using pysqlite3, which contains mixed python and C code, a
        # signal arriving during a commit or rollback results in the sqlite
        # library committing but the pysqlite3 transaction state not being
        # updated. Try to fix this when the subsequent rollback happens.

        # With the Python stdlib bindings everything is C, including the
        # transaction flag, so this does not happen. The signal exception still
        # gets raised after the db has already finished committing but the
        # connection works normally afterwards.
        try:
            self.dbh.rollback()
        except sqlite3.ProgrammingError as err:
            if 'no transaction is active' in str(err):
                self.dbh.inTransaction = 0
            else:
                raise

    # sqlite doesn't support SQL language to add a foreign key constraint
    def addForeignKey(self, *args, **kw):
        return True
    # same goes true for drop constraint
    def dropForeignKey(self, *args, **kw):
        return True

    # resetting the auto increment values of primary keys
    def setAutoIncrement(self, table, column, value = None):
        cu = self.cursor()
        if value is None:
            cu.execute("select coalesce(max(%s),0) from %s" % (column, table))
            value = int(cu.fetchall()[0][0])
        cu.execute("update sqlite_sequence set seq = ? where lower(name) = lower(?)",
                   (value, table))
        return True

    def use(self, dbName, **kwargs):
        self.close()
        dbName = os.path.join(os.path.dirname(self.database), dbName)
        self.database = dbName
        return self.connect(**kwargs)
