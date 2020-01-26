
import conary._sqlite3 as _sqlite

import copy, new, sys, weakref
from types import *

import errno
import os
import time
import traceback

if _sqlite.sqlite_version_info() < (3,2,1):
    raise RuntimeError("sqlite too old")

_BEGIN = "BEGIN IMMEDIATE"

if sys.version_info[:2] >= (2,2):
    MyStopIteration = StopIteration
else:
    MyStopIteration = IndexError

class DBAPITypeObject:
    def __init__(self,*values):
        self.values = values

    def __cmp__(self,other):
        if other in self.values:
            return 0
        if other < self.values:
            return 1
        else:
            return -1

class Row:
    def __init__(self, data, description):
        self.description = description
        self.data = data
        self.col_names = {}
        for idx, col in enumerate(description):
            col_name = col[0]
            if not col_name:
                col_name = ''
            self.col_names[col_name.upper()] = idx

    def __getitem__(self, idx):
        if isinstance(idx, str):
            return self.__getattr__(idx)
        return self.data[idx]

    def __getattr__(self, attr):
        attr = attr.upper()
        if attr in self.col_names:
            return self.data[self.col_names[attr]]
        raise AttributeError(attr)

    def __len__(self):
        return len(self.data)

    def __contains__(self, key):
        return key in self

    def __getslice__(self, i, j):
        return Row(self.data[i:j], self.description[i:j])

    def __repr__(self):
        return repr(self.data)

    def __str__(self):
        return str(self.data)

    def __cmp__(self, other):
        return cmp(self.data, other)

    def description(self):
        return self.description

    def keys(self):
        return [ x[0] for x in self.description ]

    def values(self):
        return self.data[:]

    def items(self):
        l = []
        for i in range(len(self.data)):
            l.append((self.description[i][0], self.data[i]))

        return l

    def has_key(self, key):
        return key.upper() in self.col_names

    def get(self, key, defaultval=None):
        if key in self:
            return self[key]
        else:
            return defaultval

class Cursor:
    """Abstract cursor class implementing what all cursor classes have in
    common."""

    def __init__(self, conn):
        self.arraysize = 1

        # Add ourselves to the list of cursors for our owning connection.
        self.con = weakref.proxy(conn)
        self.con.cursors[id(self)] = self

        self.stmt = None
        self._reset()
        self.rowcount = -1
        self.rownumber = 0

    def _reset(self):
        # closed is a trinary variable:
        #     == None => Cursor has not been opened.
        #     ==    0 => Cursor is open.
        #     ==    1 => Cursor is closed.
        self.closed = None
        self.description = None
        self.current_row = None
        if self.stmt is not None:
            try:
                self.stmt.reset()
            # XXX sqlite3_stmt_reset() returns a return code that
            # reflects the last error for the vdbm.  We should ignore
            # it since we're destroying this statement anyway
            except _sqlite.DatabaseError:
                pass
            self.stmt = None

    def _checkNotClosed(self, methodname=None):
        if self.closed:
            raise _sqlite.ProgrammingError("%s failed - the cursor is closed." % (methodname or ""))

    def compile(self, SQL):
        return self.con.db.prepare(SQL)

    def execstmt(self, stmt, *parms):
        stmt.reset()
        for i, parm in enumerate(parms):
            stmt.bind(i + 1, parm)
        self.current_row = stmt.step()

    def _preExecute(self, funcName, SQL, kwargs):
        start_transaction = kwargs.pop('start_transaction', True)
        self._checkNotClosed(funcName)
        startingTransaction = False
        SQL = SQL.strip()

        if self.con is None:
            raise _sqlite.ProgrammingError("connection is closed.")

        if self.con.autocommit:
            pass
        elif start_transaction:
            if not self.con.inTransaction:
                if len(SQL) >= 5 and SQL[:5].upper() == "BEGIN":
                    startingTransaction = True
                elif (len(SQL) >= 6 and SQL[:6].upper()
                      not in ("SELECT", "VACUUM", "DETACH")):
                    self.con._begin()
        return startingTransaction, SQL

    def execute(self, SQL, *parms, **kwargs):
        startingTransaction, SQL = self._preExecute("execute", SQL, kwargs)

        # prepare the statement
        self.stmt = self.con.db.prepare(SQL)
        # first dereference the list/tuple if it is encapsulated
        if len(parms) == 1:
            if isinstance(parms[0], tuple) or \
                   isinstance(parms[0], list) or \
                   isinstance(parms[0], dict) or \
                   isinstance(parms[0], Row):
                parms = parms[0]
        # now bind the arguments. lists/tuples are positionals
        if isinstance(parms, tuple) or isinstance(parms, list) \
               or isinstance(params, Row):
            for i, parm in enumerate(parms):
                self.stmt.bind(i + 1, parm)
        # hashes are named parameters
        elif isinstance(parms, dict):
            for pkey, pval in parms.items():
                if pkey[0] == ":":
                    pkey = pkey[1:]
                kwargs[pkey] = pval
        else:
            raise _sqlite.ProgrammingError("Don't know how to bind these parameters")
        # the sqlite C bindings require us to reference these bind parameters as :name
        for pkey, pval in list(kwargs.items()):
            # the sqlite bindings don't like 'binding' unkown named arguments
            try:
                self.stmt.bind(":" + pkey, pval)
            except _sqlite.ProgrammingError as e:
                if e.args[0] == "Bind parameter name unknown to the query":
                    continue
                raise
        self.current_row = self.stmt.step()
        if startingTransaction:
            self.con.inTransaction = True
        self.description = self.stmt.get_description()
        self.closed = 0
        # the PEP 249 leaves the return value undefined.  This allows
        # you to do "for row in cu.execute(...)"
        return self

    def executemany(self, query, parm_sequence, **kw):
        startingTransaction, SQL = self._preExecute("executemany", query, kw)
        # prepare the statement
        self.stmt = self.con.db.prepare(SQL)
        for parms in parm_sequence:
            self.stmt.reset()
            if isinstance(parms,tuple) or isinstance(parms, list) \
                   or isinstance(parms, Row):
                # bind as positional arguments
                for i, parm in enumerate(parms):
                    self.stmt.bind(i+1, parm)
            elif isinstance(parms, dict):
                # bind as named arguments
                for pkey, pval in parms.items():
                    if pkey[0] != ":": pkey = ":" + pkey
                    self.stmt.bind(pkey, pval)
            else:
                # this better be an int or string or we'll fail here
                self.stmt.bind(1, parms)
            self.current_row = self.stmt.step()
            if startingTransaction:
                self.con.inTransaction = True
            self.closed = 0
        return self

    def close(self):
        if self.con and self.con.closed:
            raise _sqlite.ProgrammingError("This cursor's connection is already closed.")
        if self.closed:
            raise _sqlite.ProgrammingError("This cursor is already closed.")

        self._reset()
        self.closed = 1

        # Disassociate ourselves from our connection.
        try:
            cursors = self.con.cursors
            del cursors.data[id(self)]
        except:
            pass

    def __del__(self):
        # Disassociate ourselves from our connection.
        try:
            cursors = self.con.cursors
            del cursors.data[id(self)]
        except:
            pass

    def setinputsizes(self, sizes):
        """Does nothing, required by DB API."""
        self._checkNotClosed("setinputsize")

    def setoutputsize(self, size, column=None):
        """Does nothing, required by DB API."""
        self._checkNotClosed("setinputsize")

    #
    # DB-API methods:
    #

    def fetchone(self):
        self._checkNotClosed("fetchone")
        data = self.current_row
        if data is None:
            return None
        self.rownumber += 1
        self.current_row = self.stmt.step()
        return Row(data, self.description)

    def fetchmany(self, howmany=None):
        self._checkNotClosed("fetchmany")
        if howmany is None:
            howmany = self.arraysize

        l = []
        for i in range(howmany):
            row = self.fetchone()
            if row is None:
                break
            l.append(row)

        return l

    def fetchall(self):
        self._checkNotClosed("fetchall")

        l = []
        while True:
            row = self.fetchone()
            if row is None:
                break
            l.append(row)

        return l

    #
    # Optional DB-API extensions from PEP 0249:
    #

    def __iter__(self):
        return self

    def __next__(self):
        item = self.fetchone()
        if item is None:
            raise MyStopIteration
        else:
            return item

    def scroll(self, value, mode="relative"):
        if mode == "absolute":
            value = value - self.rownumber
        if value > 0:
            for i in range(value):
                row = self.fetchone()
                if row is None:
                    raise IndexError
            return
        raise _sqlite.NotSupportedError("cannot scroll backward")

    def __getattr__(self, key):
        if key in self.__dict__:
            return self.__dict__[key]
        elif key == "sql":
            # The sql attribute is a PySQLite extension.
            return self.con.db.sql
        elif key == "lastrowid":
            return self.con.db.sqlite_last_insert_rowid()
        elif key == "connection":
            return self.con
        else:
            raise AttributeError(key)

class Connection:
    def __init__(self, database=None, converters={}, autocommit=0, encoding=None, timeout=None, command_logfile=None, lockJournal=None, *arg, **kwargs):
        # Old parameter names, for backwards compatibility
        database = database or kwargs.get("db")
        encoding = encoding or kwargs.get("client_encoding")
        if lockJournal:
            if lockJournal is True:
                lockJournal = database + '.lockjournal'

            self.lockJournal = LockJournal(lockJournal)
        else:
            self.lockJournal = None

        self.db = _sqlite.connect(database)

        if type(encoding) not in (TupleType, ListType):
            self.encoding = (encoding or sys.getdefaultencoding(),)
        else:
            self.encoding = encoding

        self.autocommit = autocommit

        self.closed = 0
        self.inTransaction = 0

        self.cursors = weakref.WeakValueDictionary()

        if timeout is not None:
            self.db.sqlite_busy_timeout(timeout)

        self.db.set_command_logfile(command_logfile)

    def __del__(self):
        if not self.closed:
            self.close()

    def _checkNotClosed(self, methodname):
        if self.closed:
            raise _sqlite.ProgrammingError("%s failed - Connection is closed." % methodname)

    def __anyCursorsLeft(self):
        return len(list(self.cursors.data.keys())) > 0

    def __closeCursors(self, doclose=0):
        """__closeCursors() - closes all cursors associated with this connection"""
        if self.__anyCursorsLeft():
            cursors = [x() for x in list(self.cursors.data.values())]

            for cursor in cursors:
                try:
                    if doclose:
                        cursor.close()
                    else:
                        cursor._reset()
                except weakref.ReferenceError:
                    pass

    def _execute(self, sql):
        c = self.cursor()
        c.execute(sql)

    def _begin(self):
        self._execute(_BEGIN)
        self.inTransaction = 1

        if self.lockJournal:
            self.lockJournal.start()

    #
    # PySQLite extensions:
    #

    def create_function(self, name, nargs, func):
        self.db.create_function(name, nargs, func)

    def create_aggregate(self, name, nargs, agg_class):
        self.db.create_aggregate(name, nargs, agg_class)

    #
    # DB-API methods:
    #

    def commit(self):
        self._checkNotClosed("commit")
        if self.autocommit:
            # Ignore .commit(), according to the DB-API spec.
            return

        if self.inTransaction:
            # shut down any pending sql statements
            self.__closeCursors(0)
            if self.lockJournal:
                self.lockJournal.stop()
            self._execute("COMMIT")
            self.inTransaction = 0

    def rollback(self):
        self._checkNotClosed("rollback")
        if self.autocommit:
            raise _sqlite.ProgrammingError("Rollback failed - autocommit is on.")

        if self.inTransaction:
            # shut down any pending sql statements
            self.__closeCursors(0)
            self._execute("ROLLBACK")
            self.inTransaction = 0

    def close(self):
        self._checkNotClosed("close")

        self.__closeCursors(1)

        if self.inTransaction:
            self.rollback()

        self.db.close()
        self.closed = 1

    def cursor(self):
        self._checkNotClosed("cursor")
        return Cursor(self)

    #
    # Optional DB-API extensions from PEP 0249:
    #

    def __getattr__(self, key):
        if key in list(self.__dict__.keys()):
            return self.__dict__[key]
        elif key in ('IntegrityError', 'InterfaceError', 'InternalError',
                     'NotSupportedError', 'OperationalError',
                     'ProgrammingError', 'Warning'):
            return getattr(_sqlite, key)
        else:
            raise AttributeError(key)

class LockJournal(object):
    def __init__(self, path):
        self.path = path

    def start(self):
        lj = open(self.path, 'w')
        lj.write('pid: %s\n' % os.getpid())
        lj.write('time: %s\n' % time.strftime('%X %x'))

        f = sys._getframe(2)
        while f != None:
            f = f.f_back
            if f.f_code.co_filename != __file__:
                break
        lj.write('Stack:\n\n')
        if f is not None:
            traceback.print_stack(f, file=lj)


    def stop(self):
        try:
            os.remove(self.path)
        except OSError as err:
            if err.errno != errno.ENOENT:
                raise
