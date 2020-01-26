#!/usr/bin/env python
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


# Cristian Gafton, <gafton@rpath.com>
# Migrates a conary repository DB stored in a abacked supported by
# dbstore to another db stored in a backend supported by dbstore

import sys
import os
if 'CONARY_PATH' in os.environ:
    sys.path.insert(0, os.environ['CONARY_PATH'])
    sys.path.insert(0, os.environ['CONARY_PATH']+"/conary/scripts")
    
import time
import itertools
import optparse

from conary.server.schema import VERSION

import tablelist
from database import getdb

class Callback:
    def __init__(self, table, count, tag = ""):
        self.table = table
        self.count = count
        self.tag = tag
        self.start = time.time()
        self.counter = 0
    def display(self, counter, pre = "", post = ""):
        global options
        if not options.verbose:
            return
        sys.stdout.write("\r%s %s: %s %s" % (
            pre, self.table, self.timings(counter), post))
        sys.stdout.flush()
    def increment(self, counter = 1):
        self.counter += counter
        if self.counter % 1000 == 0:
            self.display(self.counter, pre = self.tag)
    def last(self):
        self.display(self.count, post = " " * (len(self.tag)+1))
        if options.verbose:
            sys.stdout.write("\n")
        sys.stdout.flush()
    def timings(self, current):
        tnow = time.time()
        tpassed = max(tnow-self.start,1)
        speed = max(current/tpassed,1)
        tremaining = (self.count-current)/speed
        return "%d/%d %02d%% (%d rec/sec, %d:%02d passed, %d:%02d remaining)" % (
            current, self.count, (current*100)/max(self.count,1),
            speed,
            tpassed/60, tpassed % 60,
            tremaining/60, tremaining % 60)

def migrate_table(src, dst, t, batch=5000):
    count = src.getCount(t)
    fields = src.getFields(t)
    dstCu = dst.prepareInsert(t, fields)
    callback = Callback(t, count, "Copying")
    rowCounter = 0
    commitCounter = 0
    srcCu = src.iterRows(t)
    while rowCounter <= count:
        rows = srcCu.fetchmany(batch)
        if len(rows) == 0:
            break
        ret = dstCu.insertRows(rows, callback)
        rowCounter += ret
        commitCounter += ret
        if commitCounter > 10000:
            dst.commit()
            commitCounter = 0
    callback.last()
    dst.commit()
    # test out that we did a good insert
    dstCount = dst.getCount(t)
    assert (count == dstCount), "Source Rows count %d != target rows count %d for table %s" % (
        count, dstCount, t)
    return count

def verify_table(src, dst, table, quick=False):
    srcCount = src.getCount(table)
    dstCount = dst.getCount(table)
    assert(srcCount == dstCount), "not all records were copied: src=%d, dst=%d" %(
        srcCount, dstCount)
    srcFields = src.getFields(table)
    dstFields = dst.getFields(table)
    assert ( set(srcFields) == set(dstFields) ), "columns are different: src=%d, dst=%d" % (
        srcFields, dstFields)
    if quick:
        return True
    fields = ",".join(srcFields)
    srcCu = src.iterRows(table, fields)
    dstCu = dst.iterRows(table, fields)
    callback = Callback(table, srcCount, "Verify")
    for row1, row2 in zip(srcCu, dstCu):
        for a,b in zip(row1, row2):
            assert (a==b), "\nrow differences in table %s:\nsrc: %s\ndst: %s\n" %(
                table, row1, row2)
        callback.increment()
    callback.last()
    return True

#
# MAIN PROGRAM
#
if __name__ == '__main__':
    def store_db(option, opt_str, value, parser):
        if parser.values.db is None:
            parser.values.db = []
        parser.values.db.append((opt_str[2:], value))
        if len(parser.values.db) > 2:
            raise optparse.OptionValueError("Can only specify one source and one target database")
    parser = optparse.OptionParser(usage = "usage: %prog [options] srcopt=DB dstopt=DB")
    for db in ["sqlite", "mysql", "postgresql"]:
        parser.add_option("--" + db, action = "callback", callback = store_db, type="string",
                      dest = "db", help = "specify a %s database" % db, metavar = db.upper())
    parser.add_option("--verify", "-V", action = "store_true", dest = "verify", default = False,
                      help = "Verify each table after copy")
    parser.add_option("--batch", "-b", action = "store", dest = "batch", metavar="N", type = int,
                      default = 5000, help = "batch size in (row count) for each copy operation")
    parser.add_option("--verbose", "-v", action = "store_true", dest = "verbose",
                      default = False, help = "verbose output")
    parser.add_option("--add-table", "-t", action = "append", type = "string", metavar = "T",
                      dest = "tables", help = "add table T to the list of tables to transfer")
    (options, args) = parser.parse_args()
    if options.db is None or len(options.db) != 2:
        parser.print_help()
        sys.exit(-1)
    if options.tables:
        for t in options.tables:
            tablelist.TableList.append(t)
    src = getdb(*options.db[0])
    dst = getdb(*options.db[1])
    dst.verbose = options.verbose

    # Sanity checks
    src.checkTablesList()
    dst.createSchema()
    dst.checkTablesList(isSrc=False)
    # check that the source and target match schemas
    diff = set(src.getTables()).difference(set(dst.getTables()))
    if diff:
        print(("WARNING: Only in Source (%s): %s" % (src.driver, diff)))
    diff = set(dst.getTables()).difference(set(src.getTables()))
    if diff:
        print(("WARNING: Only in Target (%s): %s" % (dst.driver, diff)))
    # compare each table's schema between the source and target
    for table in tablelist.TableList:
        srcFields = src.getFields(table)
        dstFields = dst.getFields(table)
        if set(srcFields) != set(dstFields):
            raise RuntimeError("""\
            Schema definitions are different between databases:
            Table: %s
            %s: %s
            %s: %s""" % (table, src.driver, srcFields, dst.driver, dstFields))

    # now migrate all tables
    for table in tablelist.TableList:
        migrate_table(src, dst, table, options.batch)
        if options.verify:
            verify_table(src, dst, table)
        sys.stdout.flush()

    # create the indexes to close the loop
    dst.createIndexes()
    dst.finalize(VERSION)

    src.close()
    dst.close()
    if options.verbose:
        print("Done")
