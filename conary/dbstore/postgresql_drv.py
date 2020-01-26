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


import re
import sys
import pgsql

from .base_drv import BaseDatabase, BaseCursor, BaseBinary
from .base_drv import BaseKeywordDict
from . import sqlerrors
from . import sqllib

class KeywordDict(BaseKeywordDict):
    keys = BaseKeywordDict.keys.copy()
    keys.update( {
        'PRIMARYKEY'    : 'SERIAL PRIMARY KEY',
        'BIGINT'        : 'BIGINT',
        'BIGSERIAL'     : 'BIGSERIAL',
        'BIGPRIMARYKEY' : 'BIGSERIAL PRIMARY KEY',
        'BLOB'          : 'BYTEA',
        'MEDIUMBLOB'    : 'BYTEA',
        'PATHTYPE'      : 'BYTEA',
        'STRING'        : 'VARCHAR'
        } )

    def binaryVal(self, len):
        return "BYTEA"

# class for encapsulating binary strings for dumb drivers
class Binary(BaseBinary):
    __binary__ = True
    def __quote__(self):
        return self.s
    def __pg_repr__(self):
        return "decode('%s','hex')" % "".join("%02x" % ord(c) for c in self.s)

# edit the input query to make it postgres compatible
def _mungeSQL(sql):
    keys = [] # needs to be a list because we're dealing with positional args
    def __match(m):
        d = m.groupdict()
        kw = d["kw"][1:]
        if len(kw): # a real keyword
            if kw not in keys:
                keys.append(kw)
            d["kwIdx"] = keys.index(kw)+1
        else: # if we have just the ? then kw is "" here
            keys.append(None)
            d["kwIdx"] = len(keys)
        return "%(pre)s%(s)s$%(kwIdx)d" % d

    sql = re.sub("(?i)(?P<pre>[(,<>=]|(LIKE|AND|BETWEEN|LIMIT|OFFSET)\s)(?P<s>\s*)(?P<kw>:\w+|[?])",
                 __match, sql)
    # force dbi compliance here. args or kw or none, no mixes
    if len(keys) and keys[0] is not None:
        return (sql, keys)
    return (sql, [])

class Cursor(BaseCursor):
    binaryClass = Binary
    driver = "postgresql"

##     def binary(self, s):
##         return s

##     def frombinary(self, s):
##         #return s.decode("string_escape")
##         return s

    # execute with exception translation
    def _tryExecute(self, func, *params, **kw):
        try:
            ret = func(*params, **kw)
        except pgsql.DatabaseError as e:
            msg = e.args[0]
            if msg.find("violates foreign key constraint") > 0:
                raise sqlerrors.ConstraintViolation(msg)
            if re.search('relation \S+ does not exist', msg, re.I):
                raise sqlerrors.InvalidTable(msg)
            if re.search("duplicate key (value )?violates unique constraint", msg):
                raise sqlerrors.ColumnNotUnique(msg)
            raise sqlerrors.CursorError(msg, e)
        return ret

    # we need to "fix" the sql code before calling out
    def execute(self, sql, *args, **kw):
        self._executeCheck(sql)
        keys = []

        kw.pop("start_transaction", True)
        args, kw  = self._executeArgs(args, kw)

        # don't do unnecessary work
        if len(args) or len(kw):
            sql, keys = _mungeSQL(sql)

        # if we have args, we can not have keywords
        if len(args):
            if len(kw) or len(keys):
                raise sqlerrors.CursorError(
                    "Do not pass both positional and named bind arguments",
                    *args, **kw)
            ret = self._tryExecute(self._cursor.execute, sql, args)
        elif len(keys): # check that all keys used in the query appear in the kw
            if False in [x in kw for x in keys]:
                raise CursorError(
                    "Query keys not defined in named argument dict",
                    sorted(keys), sorted(kw.keys()))
            # need to transform kw into pozitional args
            ret = self._tryExecute(self._cursor.execute, sql,
                                   [kw[x] for x in keys])
        else:
            ret = self._tryExecute(self._cursor.execute, sql)
        if ret == self._cursor:
            return self
        return ret

    # executemany - we have to process the query code
    def executemany(self, sql, argList, **kw):
        self._executeCheck(sql)
        kw.pop("start_transaction", True)
        sql, keys = _mungeSQL(sql)
        if len(keys):
            # need to transform the dicts in tuples for the query
            return self._tryExecute(self._cursor.executemany, sql,
                                    (tuple([row[x] for x in keys]) for row in argList))
        return self._tryExecute(self._cursor.executemany, sql, argList)

    # support for prepared statements
    def compile(self, sql):
        self._executeCheck(sql)
        sql, keys = _mungeSQL(sql.strip())
        stmt = self.dbh.prepare(sql)
        stmt.keys = keys
        return stmt
    def execstmt(self, stmt, *args):
        assert(isinstance(stmt, pgsql.PreparedCursor))
        if not len(args):
            ret = self._tryExecute(stmt._source.execute)
        elif isinstance(args[0], (tuple, list)):
            ret = self._tryExecute(stmt._source.execute, *args)
        else:
            ret = self._tryExecute(stmt._source.execute, args)
        if isinstance(ret, int):
            return ret
        return stmt

    # override this with the native version
    def fields(self):
        return self._cursor.fields

    # pgsql has its own fetch*_dict methods
    def fetchone_dict(self):
        ret = self._cursor.fetchone_dict()
        return sqllib.CaselessDict(ret)
    def fetchmany_dict(self, size):
        return [ sqllib.CaselessDict(x) for x in self._cursor.fetchmany_dict(size) ]
    def fetchall_dict(self):
        return [ sqllib.CaselessDict(x) for x in self._cursor.fetchall_dict() ]

    # we have "our own" lastrowid
    def __getattr__(self, name):
        if name == "lastrowid":
            return self.lastid()
        return BaseCursor.__getattr__(self, name)

    # postgresql can not report back the last value from a SERIAL
    # PRIMARY KEY column insert, so we have to look it up ourselves
    def lastid(self):
        ret = self.execute("select lastval()").fetchone()
        if ret is None:
            return 0
        return ret[0]

# A cursor class that wraps PostgreSQL's server side cursors
class IterCursor(Cursor):
    def _getCursor(self):
        assert(self.dbh)
        return self.dbh.itercursor()

class Database(BaseDatabase):
    driver = "postgresql"
    kind = "postgresql"
    alive_check = "select version() as version"
    cursorClass = Cursor
    iterCursorClass = IterCursor
    keywords = KeywordDict()
    basic_transaction = "START TRANSACTION"
    savepoints = True

    def connect(self, **kwargs):
        assert(self.database)
        cdb = self._connectData()
        if not cdb.get("port", None):
            cdb["port"] = -1
        try:
            self.dbh = pgsql.connect(**cdb)
        except pgsql.DatabaseError as err:
            exc_info = sys.exc_info()
            newerr = sqlerrors.DatabaseError(
                    "Could not connect to database %s: %s" % (cdb, str(err)))
            raise type(newerr)(newerr).with_traceback(exc_info[2])

        # reset the tempTables since we just lost them because of the (re)connect
        self.tempTables = sqllib.CaselessDict()
        self.closed = False
        return True

    def close_fork(self):
        if hasattr(self.dbh, 'close_fork'):
            self.dbh.close_fork()
            self.dbh = None
        self.close()

    def itercursor(self):
        assert (self.dbh)
        return self.iterCursorClass(self.dbh)

    def loadSchema(self):
        BaseDatabase.loadSchema(self)
        c = self.cursor()
        # get tables
        c.execute("""
        select tablename as name, schemaname as schema
        from pg_tables
        where schemaname not in ('pg_catalog', 'pg_toast', 'information_schema')
        and ( schemaname !~ '^pg_temp_' OR schemaname = (pg_catalog.current_schemas(true))[1])
        """)
        for table, schema in c.fetchall():
            if schema.startswith("pg_temp"):
                self.tempTables[table] = sqllib.Llist()
            else:
                self.tables[table] = sqllib.Llist()
        if not len(self.tables):
            return self.version
        # views
        c.execute("""
        select viewname as name
        from pg_views
        where schemaname not in ('pg_catalog', 'pg_toast', 'information_schema')
        """)
        for name, in c.fetchall():
            self.views[name] = True
        # indexes
        c.execute("""
        select indexname as name, tablename as table, schemaname as schema
        from pg_indexes
        where schemaname not in ('pg_catalog', 'pg_toast', 'information_schema')
        and ( schemaname !~ '^pg_temp_' OR schemaname = (pg_catalog.current_schemas(true))[1])
        """)
        for (name, table, schema) in c.fetchall():
            if schema.startswith("pg_temp"):
                self.tempTables.setdefault(table, sqllib.Llist()).append(name)
            else:
                self.tables.setdefault(table, sqllib.Llist()).append(name)
        # sequences. I wish there was a better way...
        c.execute("""
        SELECT c.relname as name
        FROM pg_catalog.pg_class c
        LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'S'
        AND n.nspname NOT IN ('pg_catalog', 'pg_toast', 'information_schema')
        AND pg_catalog.pg_table_is_visible(c.oid)
        """)
        for name, in c.fetchall():
            self.sequences[name] = True
        # triggers
        # AWKWARD: postgres 9.0 changed tgisconstraint to tgisinternal, so we
        # have to detect which it is to maintain compatibility :(
        #   -- gxti 2010-11-01
        c.execute("""
            SELECT a.attname
            FROM pg_catalog.pg_attribute a
            LEFT JOIN pg_catalog.pg_class c ON a.attrelid = c.oid
            LEFT JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid
            WHERE n.nspname = 'pg_catalog' AND c.relname = 'pg_trigger'
            AND a.attname in ('tgisconstraint', 'tgisinternal')
            """)
        colname, = c.fetchone()

        c.execute("""
        SELECT t.tgname, c.relname
        FROM pg_catalog.pg_trigger t, pg_class c, pg_namespace n
        WHERE t.tgrelid = c.oid AND c.relnamespace = n.oid
        AND NOT t.%(colname)s
        AND n.nspname NOT IN ('pg_catalog', 'pg_toast', 'information_schema')
        AND ( n.nspname !~ '^pg_temp_' OR n.nspname = (pg_catalog.current_schemas(true))[1])
        """ % dict(colname=colname))
        for (name, table) in c.fetchall():
            self.triggers[name] = table
        version = self.getVersion()
        return version

    # Transaction support
    def inTransaction(self, default=None):
        """
        Return C{True} if the connection currently has an active
        transaction.
        """
        return self.dbh.transaction in (pgsql.TRANS_INTRANS,
                pgsql.TRANS_INERROR, pgsql.TRANS_ACTIVE)

    def transaction(self, name = None):
        "start transaction [ named point ]"
        assert(self.dbh)
        c = self.cursor()
        if name:
            if not self.inTransaction():
                c.execute(self.basic_transaction)
            c.execute("SAVEPOINT " + name)
        else:
            c.execute(self.basic_transaction)
        return c

    def rollback(self, name=None):
        "rollback [ to transaction point ]"
        assert(self.dbh)
        if name:
            self.dbh.execute("ROLLBACK TO SAVEPOINT " + name)
        else:
            return self.dbh.rollback()

    # Postgresql's trigegr syntax kind of sucks because we have to
    # create a function first and then call that function from the
    # trigger
    def createTrigger(self, table, column, onAction, pinned=None):
        if pinned is not None:
            import warnings
            warnings.warn(
                'The "pinned" kwparam to createTrigger is deprecated and '
                'no longer has any affect on triggers',
                DeprecationWarning)
        onAction = onAction.lower()
        assert(onAction in ["insert", "update"])
        # first create the trigger function
        triggerName = "%s_%s" % (table, onAction)
        if triggerName in self.triggers:
            return False
        funcName = "%s_func" % triggerName
        cu = self.dbh.cursor()
        cu.execute("""
        CREATE OR REPLACE FUNCTION %s()
        RETURNS trigger
        AS $$
        BEGIN
            NEW.%s := TO_NUMBER(TO_CHAR(CURRENT_TIMESTAMP, 'YYYYMMDDHH24MISS'), '99999999999999') ;
            RETURN NEW;
        END ; $$ LANGUAGE 'plpgsql';
        """ % (funcName, column))
        # now create the trigger based on the above function
        cu.execute("""
        CREATE TRIGGER %s
        BEFORE %s ON %s
        FOR EACH ROW
        EXECUTE PROCEDURE %s()
        """ % (triggerName, onAction, table, funcName))
        self.triggers[triggerName] = table
        return True
    def dropTrigger(self, table, onAction):
        onAction = onAction.lower()
        triggerName = "%s_%s" % (table, onAction)
        if triggerName not in self.triggers:
            return False
        funcName = "%s_func" % triggerName
        cu = self.dbh.cursor()
        cu.execute("DROP TRIGGER %s ON %s" % (triggerName, table))
        cu.execute("DROP FUNCTION %s()" % funcName)
        del self.triggers[triggerName]
        return True

    def analyze(self, table=""):
        cu = self.cursor()
        assert (isinstance(table, str))
        cu.execute("ANALYZE %s" %table)

    def _bulkload(self, tableName, rows, columnNames, start_transaction = True):
        # first, make sure we do this in a transaction so we can roll it back
        if not self.inTransaction():
            self.transaction()
        # now it's safe to do the bulkload
        return self.dbh.bulkload(tableName, rows, columnNames)

    def truncate(self, *tables):
        cu = self.cursor()
        cu.execute("TRUNCATE TABLE " + ", ".join(tables))

    def lockTable(self, tableName):
        cu = self.cursor()
        # "This mode protects a table against concurrent data changes, and is
        # self-exclusive so that only one session can hold it at a time."
        cu.execute("LOCK TABLE %s IN SHARE ROW EXCLUSIVE MODE" % (tableName,))

    # resetting the auto increment values of primary keys
    def setAutoIncrement(self, table, column, value = None):
        cu = self.cursor()
        seqName = "%s_%s_seq" % (table, column)
        usedVal = True
        if value is None:
            cu.execute("select max(%s) from %s" % (column, table))
            value = cu.fetchall()[0][0]
            if value is None:
                usedVal = False
                value = 1
            else:
                values = int(value)
        cu.execute("select setval(?, ?, ?)", (seqName, value, usedVal))
        ret = cu.fetchall()
        assert (ret[0][0] == value)
        return True

    def use(self, dbName, **kwargs):
        self.close()
        self.database = "/".join([self.database.rsplit("/", 1)[0], dbName])
        return self.connect(**kwargs)
