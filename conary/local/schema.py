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

import errno
import fcntl
import os
import sys
import itertools
from conary import trove, deps, errors, files, streams
from conary.dbstore import idtable, migration, sqlerrors

# Stuff related to SQL schema maintenance and migration

TROVE_TROVES_BYDEFAULT = 1 << 0
TROVE_TROVES_WEAKREF   = 1 << 1

VERSION = 20

def resetTable(cu, name):
    try:
        cu.execute("DELETE FROM %s" % name, start_transaction = False)
        return True
    except Exception as e:
        return False

def _createVersions(db, cu = None):
    if "Versions" in db.tables:
        return
    if cu is None:
        cu = db.cursor()
    if idtable.createIdTable(db, "Versions", "versionId", "version"):
        cu.execute("INSERT INTO Versions (versionId, version) VALUES (0, NULL)")
        db.commit()
        db.loadSchema()

# Schema creation functions
def _createFlavors(db):
    if "Flavors" in db.tables:
        return
    cu = db.cursor()
    idtable.createIdTable(db, "Flavors", "flavorId", "flavor")
    cu.execute("SELECT FlavorID from Flavors")
    if cu.fetchone() == None:
        # reserve flavor 0 for "no flavor information"
        cu.execute("INSERT INTO Flavors VALUES (0, NULL)")
    idtable.createMappingTable(db, "DBFlavorMap", "instanceId", "flavorId")
    db.commit()
    db.loadSchema()

def createDBTroveFiles(db):
    if "DBTroveFiles" in db.tables:
        return
    cu = db.cursor()
    _createVersions(db, cu)
    cu.execute("""
    CREATE TABLE DBTroveFiles(
        streamId            %(PRIMARYKEY)s,
        pathId              BINARY(16),
        versionId           INTEGER,
        path                %(STRING)s,
        fileId              BINARY(20),
        instanceId          INTEGER,
        isPresent           INTEGER,
        stream              BLOB
    )""" % db.keywords)
    cu.execute("CREATE INDEX DBTroveFilesIdx ON DBTroveFiles(fileId)")
    cu.execute("CREATE INDEX DBTroveFilesInstanceIdx2 ON DBTroveFiles(instanceId, pathId)")
    cu.execute("CREATE INDEX DBTroveFilesPathIdx ON DBTroveFiles(path)")

    idtable.createIdTable(db, "Tags", "tagId", "tag")

    cu.execute("""
    CREATE TABLE DBFileTags(
        streamId            INTEGER,
        tagId               INTEGER
    )""")
    db.commit()
    db.loadSchema()

def createInstances(db):
    if "Instances" in db.tables:
        return
    cu = db.cursor()
    _createVersions(db, cu)
    cu.execute("""
    CREATE TABLE Instances(
        instanceId      %(PRIMARYKEY)s,
        troveName       %(STRING)s,
        versionId       INTEGER,
        flavorId        INTEGER,
        timeStamps      %(STRING)s,
        isPresent       INTEGER,
        pinned          BOOLEAN
    )""" % db.keywords)
    cu.execute("CREATE INDEX InstancesNameIdx ON Instances(troveName)")
    cu.execute("CREATE UNIQUE INDEX InstancesIdx ON "
               "Instances(troveName, versionId, flavorId)")
    db.commit()
    db.loadSchema()

def _createTroveTroves(db):
    if "TroveTroves" in db.tables:
        return
    cu = db.cursor()
    cu.execute("""
    CREATE TABLE TroveTroves(
        instanceId      INTEGER NOT NULL,
        includedId      INTEGER NOT NULL,
        flags           INTEGER,
        inPristine      BOOLEAN
    )""")
    # this index is so we can quickly tell what troves are needed by another trove
    cu.execute("CREATE INDEX TroveTrovesIncludedIdx ON TroveTroves(includedId)")
    # This index is used to enforce that TroveTroves only contains
    # unique TroveTrove (instanceId, includedId) pairs.
    cu.execute("CREATE UNIQUE INDEX TroveTrovesInstanceIncluded_uq ON "
               "TroveTroves(instanceId,includedId)")
    db.commit()
    db.loadSchema()

def createTroveInfo(db):
    if "TroveInfo" in db.tables:
        return
    cu = db.cursor()
    cu.execute("""
    CREATE TABLE TroveInfo(
        instanceId      INTEGER NOT NULL,
        infoType        INTEGER NOT NULL,
        data            %(MEDIUMBLOB)s
    )""" % db.keywords)
    cu.execute("CREATE INDEX TroveInfoIdx ON TroveInfo(instanceId)")
    cu.execute("CREATE INDEX TroveInfoTypeIdx ON TroveInfo(infoType, data)")
    cu.execute("CREATE INDEX TroveInfoInstTypeIdx ON TroveInfo(instanceId, infoType)")
    db.commit()
    db.loadSchema()

def createMetadata(db):
    commit = False
    cu = db.cursor()
    _createVersions(db, cu)
    if 'Metadata' not in db.tables:
        cu.execute("""
        CREATE TABLE Metadata(
            metadataId          %(PRIMARYKEY)s,
            itemId              INTEGER NOT NULL,
            versionId           INTEGER NOT NULL,
            branchId            INTEGER NOT NULL,
            timeStamp           NUMERIC(13,3) NOT NULL
        )""" % db.keywords)
        commit = True
    if 'MetadataItems' not in db.tables:
        cu.execute("""
        CREATE TABLE MetadataItems(
            metadataId      INTEGER NOT NULL,
            class           INTEGER NOT NULL,
            data            TEXT NOT NULL,
            language        VARCHAR(254) NOT NULL DEFAULT 'C'
        )""")
        cu.execute("CREATE INDEX MetadataItemsIdx ON MetadataItems(metadataId)")
        commit = True
    if commit:
        db.commit()
        db.loadSchema()

def createDataStore(db):
    if "DataStore" in db.tables:
        return
    cu = db.cursor()
    cu.execute("""
    CREATE TABLE DataStore(
        hash    BINARY(20) NOT NULL,
        count   INTEGER,
        data    BLOB
    )""")
    cu.execute("CREATE INDEX DataStoreIdx ON DataStore(hash)")
    db.commit()
    db.loadSchema()

def createDatabaseAttributes(db):
    if "DatabaseAttributes" in db.tables:
        return
    cu = db.cursor()
    cu.execute("""
    CREATE TABLE DatabaseAttributes(
        id      %(PRIMARYKEY)s,
        name    %(STRING)s,
        value   %(STRING)s
    )
    """ % db.keywords)
    cu.execute("CREATE UNIQUE INDEX DatabaseAttributesNameIdx "
               "ON DatabaseAttributes(name)")
    cu.execute("INSERT INTO DatabaseAttributes (name, value) "
               "VALUES ('transaction counter', '0')")
    db.commit()
    db.loadSchema()

def _createDepTable(db, cu, name, isTemp):
    d =  {"tmp" : "", "name" : name}
    startTrans = not isTemp
    if isTemp:
        if name in db.tempTables:
            resetTable(cu, name)
            return False
        d['tmp'] = 'TEMPORARY'

    cu.execute("""
    CREATE %(tmp)s TABLE %(name)s(
        depId           %%(PRIMARYKEY)s,
        class           INTEGER NOT NULL,
        name            VARCHAR(254) NOT NULL,
        flag            VARCHAR(254) NOT NULL
    ) %%(TABLEOPTS)s""" % d % db.keywords, start_transaction = (not isTemp))
    cu.execute("CREATE UNIQUE INDEX %sIdx ON %s(class, name, flag)" %
               (name, name), start_transaction = startTrans)
    if isTemp:
        db.tempTables[name] = True

def _createRequiresTable(db, cu, name, isTemp):
    d = { "tmp" : "",
          "name" : name,
          "constraint" : "",
          "tmpCol" : ""}
    startTrans = not isTemp

    if isTemp:
        if name in db.tempTables:
            resetTable(cu, name)
            return False
        d['tmp'] = 'TEMPORARY'
        d['tmpCol'] = ',satisfied INTEGER DEFAULT 0'
    else:
        d['constraint'] = """,
        CONSTRAINT %(name)s_instanceId_fk
            FOREIGN KEY (instanceId) REFERENCES Instances(instanceId)
            ON DELETE RESTRICT ON UPDATE CASCADE,
        CONSTRAINT %(name)s_depId_fk
            FOREIGN KEY (depId) REFERENCES Dependencies(depId)
            ON DELETE RESTRICT ON UPDATE CASCADE
        """ %d

    cu.execute("""
    CREATE %(tmp)s TABLE %(name)s(
        instanceId      INTEGER NOT NULL,
        depId           INTEGER NOT NULL,
        depNum          INTEGER,
        depCount        INTEGER %(constraint)s
        %(tmpCol)s
    ) %%(TABLEOPTS)s""" % d % db.keywords, start_transaction = startTrans)
    cu.execute("CREATE INDEX %(name)sIdx ON %(name)s(instanceId)" % d,
               start_transaction = startTrans)
    cu.execute("CREATE INDEX %(name)sIdx2 ON %(name)s(depId)" % d,
               start_transaction = startTrans)
    # XXX: do we really need this index?
    cu.execute("CREATE INDEX %(name)sIdx3 ON %(name)s(depNum)" % d,
               start_transaction = startTrans)
    if isTemp:
        db.tempTables[name] = True
    return True


def _createProvidesTable(db, cu, name, isTemp):
    d = { "tmp" : "",
          "name" : name,
          "constraint" : "" }
    startTrans = not isTemp

    if isTemp:
        if name in db.tempTables:
            resetTable(cu, name)
            return False
        d['tmp'] = 'TEMPORARY'
    else:
        d['constraint'] = """,
        CONSTRAINT %(name)s_instanceId_fk
            FOREIGN KEY (instanceId) REFERENCES Instances(instanceId)
            ON DELETE RESTRICT ON UPDATE CASCADE,
        CONSTRAINT %(name)s_depId_fk
            FOREIGN KEY (depId) REFERENCES Dependencies(depId)
            ON DELETE RESTRICT ON UPDATE CASCADE
        """ %d
    cu.execute("""
    CREATE %(tmp)s TABLE %(name)s(
        instanceId          INTEGER NOT NULL,
        depId               INTEGER NOT NULL %(constraint)s
    ) %%(TABLEOPTS)s""" % d % db.keywords, start_transaction = startTrans)
    cu.execute("CREATE INDEX %(name)sIdx ON %(name)s(instanceId)" % d,
               start_transaction = startTrans)
    cu.execute("CREATE INDEX %(name)sIdx2 ON %(name)s(depId)" % d,
               start_transaction = startTrans)
    if isTemp:
        db.tempTables[name] = True


def _createDepWorkTable(db, cu, name):
    if name in db.tempTables:
        return False
    cu.execute("""
    CREATE TEMPORARY TABLE %s(
        troveId         INTEGER,
        depNum          INTEGER,
        flagCount       INTEGER,
        isProvides      INTEGER,
        class           INTEGER,
        name            VARCHAR(254),
        flag            VARCHAR(254),
        merged          INTEGER
    ) %%(TABLEOPTS)s""" % name % db.keywords, start_transaction = False)

    cu.execute("""
    CREATE INDEX %sIdx ON %s(troveId, class, name, flag)
    """ % (name, name), start_transaction = False)
    db.tempTables[name] = True

# This should be called only once per establishing a db connection
def setupTempDepTables(db, cu=None):
    if cu is None:
        cu = db.cursor()
    _createRequiresTable(db, cu, "TmpRequires", isTemp=True)
    _createProvidesTable(db, cu, "TmpProvides", isTemp=True)
    _createDepTable(db, cu, 'TmpDependencies', isTemp=True)
    _createDepWorkTable(db, cu, "DepCheck")

    if "suspectDepsOrig" not in db.tempTables:
        cu.execute("CREATE TEMPORARY TABLE suspectDepsOrig(depId integer)",
                   start_transaction=False)
        db.tempTables["suspectDepsOrig"] = True
    if "suspectDeps" not in db.tempTables:
        cu.execute("CREATE TEMPORARY TABLE suspectDeps(depId integer)",
                   start_transaction=False)
        db.tempTables["suspectDeps"] = True
    if "BrokenDeps" not in db.tempTables:
        cu.execute("CREATE TEMPORARY TABLE BrokenDeps(depNum INTEGER)",
                   start_transaction=False)
        db.tempTables["BrokenDeps"] = True
    if "RemovedTroveIds" not in db.tempTables:
        cu.execute("""
            CREATE TEMPORARY TABLE RemovedTroveIds(
                rowId %(PRIMARYKEY)s,
                troveId INTEGER,
                nodeId INTEGER
            )""" % db.keywords, start_transaction=False)
        cu.execute("CREATE INDEX RemovedTroveIdsIdx ON RemovedTroveIds(troveId)",
                   start_transaction=False)
        db.tempTables["RemovedTroveIds"] = True
    if "RemovedTroves" not in db.tempTables:
        cu.execute("""
            CREATE TEMPORARY TABLE RemovedTroves(
                name        VARCHAR(254),
                version     %(STRING)s,
                flavor      %(STRING)s,
                nodeId      INTEGER
            )""" % db.keywords, start_transaction = False)
        db.tempTables["RemovedTroves"] = True
    db.commit()


def createDependencies(db, skipCommit=False):
    commit = False
    cu = db.cursor()

    if "Dependencies" not in db.tables:
        _createDepTable(db, cu, "Dependencies", isTemp=False)
        commit = True
    if "Requires" not in db.tables:
        _createRequiresTable(db, cu, "Requires", isTemp=False)
        commit = True
    if "Provides" not in db.tables:
        _createProvidesTable(db, cu, "Provides", isTemp=False)
        commit = True
    if commit:
        if not skipCommit:
            db.commit()
        db.loadSchema()

def setupTempTables(db, cu=None, skipCommit=False):
    if cu is None:
        cu = db.cursor()

    if "getFilesTbl" not in db.tempTables:
        cu.execute("""
        CREATE TEMPORARY TABLE getFilesTbl(
            row %(PRIMARYKEY)s,
            fileId BINARY
        ) %(TABLEOPTS)s""" % db.keywords, start_transaction=False)
        db.tempTables["getFilesTbl"] = True

    if not skipCommit:
        db.commit()

def createSchema(db):
    _createVersions(db)
    createInstances(db)
    _createTroveTroves(db)
    createDBTroveFiles(db)
    _createFlavors(db)
    createDependencies(db)
    createTroveInfo(db)
    createDataStore(db)
    createDatabaseAttributes(db)

# SCHEMA Migration

# redefine to enable stdout messaging for the migration process
class SchemaMigration(migration.SchemaMigration):
    def message(self, msg = None):
        if msg is None:
            msg = self.msg
        print("\r%s\r" %(' '*len(self.msg)), end=' ')
        self.msg = msg
        sys.stdout.write(msg)
        sys.stdout.flush()

class MigrateTo_5(SchemaMigration):
    Version = 5
    def canUpgrade(self):
        return self.version in [2,3,4]

    def migrate(self):
        from conary.local import deptable
        class FakeTrove:
            def setRequires(self, req):
                self.r = req
            def setProvides(self, prov):
                self.p = prov
            def getRequires(self):
                return self.r
            def getProvides(self):
                return self.p
            def __init__(self):
                self.r = deps.deps.DependencySet()
                self.p = deps.deps.DependencySet()

        if self.version == 2:
            self.cu.execute(
                "ALTER TABLE DBInstances ADD COLUMN pinned BOOLEAN")

        instances = [ x[0] for x in
                      self.cu.execute("select instanceId from DBInstances") ]
        dtbl = deptable.DependencyTables(self.db)
        setupTempDepTables(self.db)
        troves = []

        for instanceId in instances:
            trv = FakeTrove()
            dtbl.get(self.cu, trv, instanceId)
            troves.append(trv)

        self.cu.execute("delete from dependencies")
        self.cu.execute("delete from requires")
        self.cu.execute("delete from provides")
        for instanceId, trv in zip(instances, troves):
            dtbl.add(self.cu, trv, instanceId)
        return self.Version

class MigrateTo_6(SchemaMigration):
    Version = 6
    def migrate(self):
        self.cu.execute(
            "ALTER TABLE TroveTroves ADD COLUMN inPristine INTEGER")
        self.cu.execute("UPDATE TroveTroves SET inPristine=?", True)
        # erase unused versions
        self.message("Removing unused version strings...")
        self.cu.execute("""
        DELETE FROM Versions WHERE versionId IN
            ( SELECT versions.versionid
              FROM versions LEFT OUTER JOIN
              ( SELECT versionid AS usedversions FROM dbinstances
                UNION
                SELECT versionid AS usedversions FROM dbtrovefiles )
              ON usedversions = versions.versionid
              WHERE usedversions IS NULL )
         """)
        return self.Version

class MigrateTo_7(SchemaMigration):
    Version = 7
    def migrate(self):
        self.cu.execute("""
        DELETE FROM TroveTroves
        WHERE TroveTroves.ROWID in (
            SELECT Second.ROWID
            FROM TroveTroves AS First
            JOIN TroveTroves AS Second USING(instanceId, includedId)
            WHERE First.ROWID < Second.ROWID
            )""")
        self.cu.execute("CREATE UNIQUE INDEX TroveTrovesInstIncIdx ON "
                        "TroveTroves(instanceId,includedId)")
        return self.Version

class MigrateTo_8(SchemaMigration):
    Version = 8
    def migrate(self):
        # we don't alter here because lots of indices have changed
        # names; this is just easier
        self.cu.execute('DROP INDEX InstancesNameIdx')
        self.cu.execute('DROP INDEX InstancesIdx')
        createInstances(self.db)
        self.cu.execute("""INSERT INTO Instances
                            (instanceId, troveName, versionId, flavorId,
                             timeStamps, isPresent, pinned)
                           SELECT instanceId, troveName, versionId, flavorId,
                                  timeStamps, isPresent, 0 FROM DBInstances
                        """)
        _createFlavors(self.db)
        self.cu.execute('INSERT INTO Flavors SELECT * FROM DBFlavors '
                        'WHERE flavor IS NOT NULL')
        self.cu.execute('DROP TABLE DBFlavors')
        return self.Version

class MigrateTo_9(SchemaMigration):
    Version = 9
    def migrate(self):
        for klass, infoType in [
            (trove.BuildDependencies, trove._TROVEINFO_TAG_BUILDDEPS),
            (trove.LoadedTroves,      trove._TROVEINFO_TAG_LOADEDTROVES) ]:
            for instanceId, data in \
                    [ x for x in self.cu.execute(
                        "select instanceId, data from TroveInfo WHERE "
                        "infoType=?", infoType) ]:
                obj = klass(data)
                f = obj.freeze()
                if f != data:
                    self.cu.execute("update troveinfo set data=? where "
                                    "instanceId=? and infoType=?", f,
                                    instanceId, infoType)
                    self.cu.execute("delete from troveinfo where "
                                    "instanceId=? and infoType=?",
                                    instanceId, trove._TROVEINFO_TAG_SIGS)
        return self.Version

class MigrateTo_10(SchemaMigration):
    Version = 10
    def migrate(self):
        self.cu.execute("SELECT COUNT(*) FROM DBTroveFiles")
        total = self.cu.fetchone()[0]

        self.cu.execute("SELECT instanceId, fileId, stream FROM DBTroveFiles")
        changes = []
        changedTroves = set()
        for i, (instanceId, fileId, stream) in enumerate(self.cu):
            i += 1
            if i % 1000 == 0 or (i == total):
                self.message("Reordering streams and recalculating "
                             "fileIds... %d/%d" %(i, total))
            f = files.ThawFile(stream, fileId)
            if not f.provides() and not f.requires():
                # if there are no deps, skip
                continue
            newStream = f.freeze()
            newFileId = f.fileId()
            if newStream == stream and newFileId == fileId:
                # if the stream didn't change, skip
                continue
            changes.append((newFileId, newStream, fileId))
            changedTroves.add(instanceId)

        # make the changes
        for newFileId, newStream, fileId in changes:
            self.cu.execute(
                "UPDATE DBTroveFiles SET fileId=?, stream=? WHERE fileId=?",
                (newFileId, newStream, fileId))

        # delete signatures for the instances we changed
        for instanceId in changedTroves:
            self.cu.execute(
                "DELETE FROM troveinfo WHERE instanceId=? AND infoType=?",
                (instanceId, trove._TROVEINFO_TAG_SIGS))

        return self.Version


# convert contrib.rpath.com -> contrib.rpath.org
class MigrateTo_11(SchemaMigration):
    Version = 11
    def migrate(self):
        self.cu.execute('select count(*) from versions')
        total = self.cu.fetchone()[0]

        updates = []
        self.cu.execute("select versionid, version from versions")
        for i, (versionId, version) in enumerate(self.cu):
            self.message("Renaming contrib.rpath.com to contrib.rpath.org... "
                         "%d/%d" %(i+1, total))
            if not versionId:
                continue
            new = version.replace('contrib.rpath.com', 'contrib.rpath.org')
            if version != new:
                updates.append((versionId, new))

        for versionId, version in updates:
            self.cu.execute("update versions set version=? where versionid=?",
                            (version, versionId))
            # erase signature troveinfo since the version changed
            self.cu.execute("""
            delete from TroveInfo
            where infotype = 9
            and instanceid in (
              select instanceid
              from instances
              where instances.versionid = ? )""",
                       (versionId,))
        return self.Version

# calculate path hashes for every trove
class MigrateTo_12(SchemaMigration):
    Version = 12
    def migrate(self):
        instanceIds = [ x[0] for x in self.cu.execute(
            "select instanceId from instances") ]
        for i, instanceId in enumerate(instanceIds):
            if i % 20 == 0:
                self.message("Updating trove %d of %d" %(
                    i, len(instanceIds)))
            ph = trove.PathHashes()
            for path, in self.cu.execute(
                "select path from dbtrovefiles where instanceid=?",
                instanceId):
                ph.addPath(path)

            self.cu.execute("""
                insert into troveinfo(instanceId, infoType, data)
                    values(?, ?, ?)""", instanceId,
                    trove._TROVEINFO_TAG_PATH_HASHES, ph.freeze())
        return self.Version

class MigrateTo_13(SchemaMigration):
    Version = 13
    def migrate(self):
        self.cu.execute("DELETE FROM TroveInfo WHERE infoType=?",
                        trove._TROVEINFO_TAG_SIGS)
        self.cu.execute("DELETE FROM TroveInfo WHERE infoType=?",
                        trove._TROVEINFO_TAG_FLAGS)
        self.cu.execute("DELETE FROM TroveInfo WHERE infoType=?",
                        trove._TROVEINFO_TAG_INSTALLBUCKET)

        flags = trove.TroveFlagsStream()
        flags.isCollection(set = True)
        collectionStream = flags.freeze()
        flags.isCollection(set = False)
        notCollectionStream = flags.freeze()

        self.cu.execute("""
        INSERT INTO TroveInfo
            SELECT instanceId, ?, ? FROM Instances
            WHERE NOT (trovename LIKE '%:%' OR trovename LIKE 'fileset-%')
        """, trove._TROVEINFO_TAG_FLAGS, collectionStream)

        self.cu.execute("""
        INSERT INTO TroveInfo
            SELECT instanceId, ?, ? FROM Instances
            WHERE     (trovename LIKE '%:%' OR trovename LIKE 'fileset-%')
            """, trove._TROVEINFO_TAG_FLAGS, notCollectionStream)
        return self.Version

class MigrateTo_14(SchemaMigration):
    Version = 14
    def migrate(self):
        # we need to rerun the MigrateTo_10 migration since we missed
        # some trovefiles the first time around
        class M10(MigrateTo_10):
            # override sanity checks to force the migration to run
            # out of order
            def canUpgrade(self):
                return self.version == 13
        m10 = M10(self.db)
        m10.migrate()
        # We need to make sure that loadedTroves and buildDeps troveinfo
        # isn't included in any commponent's trove.
        self.cu.execute("""
        DELETE FROM TroveInfo
        WHERE
           infotype IN (4, 5)
        AND instanceid IN (SELECT instanceid
                           FROM Instances
                           WHERE trovename LIKE '%:%')""")
        return self.Version

class MigrateTo_15(SchemaMigration):
    Version = 15
    def migrate(self):
        # some indexes have changed - we need to update the local schema
        if "TroveInfoIdx2" in self.db.tables["TroveInfo"]:
            self.cu.execute("DROP INDEX TroveInfoIdx2")
        self.cu.execute("CREATE INDEX TroveInfoTypeIdx ON TroveInfo(infoType, instanceId)")
        if "TroveTrovesInstanceIdx" in self.db.tables["TroveTroves"]:
            self.cu.execute("DROP INDEX TroveTrovesInstanceIdx")
        if "TroveTrovesInstIncIdx" in self.db.tables["TroveTroves"]:
            self.cu.execute("DROP INDEX TroveTrovesInstIncIdx")
        if "TroveTrovesInstanceIncluded_uq" not in self.db.tables["TroveTroves"]:
            self.cu.execute(
                       "CREATE UNIQUE INDEX TroveTrovesInstanceIncluded_uq ON "
                       "TroveTroves(instanceId,includedId)")
        self.db.commit()
        self.db.loadSchema()
        return self.Version

class MigrateTo_16(SchemaMigration):
    Version = 16
    def migrate(self):
        cu = self.cu
        cu.execute("""
        CREATE TABLE TroveTroves2(
            instanceId      INTEGER,
            includedId      INTEGER,
            flags           INTEGER,
            inPristine      BOOLEAN
        )""")
        cu.execute('''
        INSERT INTO TroveTroves2
            SELECT instanceId, includedId,
                   CASE WHEN byDefault THEN %d ELSE 0 END,
                   inPristine
            FROM TroveTroves''' % TROVE_TROVES_BYDEFAULT)

        cu.execute('DROP TABLE TroveTroves')
        cu.execute('ALTER TABLE TroveTroves2 RENAME TO TroveTroves')

        cu.execute("CREATE INDEX TroveTrovesIncludedIdx ON TroveTroves(includedId)")
        # This index is used to enforce that TroveTroves only contains
        # unique TroveTrove (instanceId, includedId) pairs.

        cu.execute("CREATE UNIQUE INDEX TroveTrovesInstanceIncluded_uq ON "
                   "TroveTroves(instanceId,includedId)")

        self.db.commit()
        self.db.loadSchema()
        return self.Version


class MigrateTo_17(SchemaMigration):
    Version = 17
    def migrate(self):
        # whoops, path hashes weren't sorted, sigs are invalid.
        rows = self.cu.execute("""
                    SELECT instanceId,data from TroveInfo WHERE infoType=?
                   """, trove._TROVEINFO_TAG_PATH_HASHES)
        neededChanges = []
        PathHashes = trove.PathHashes
        for instanceId, data in rows:
            frzn = PathHashes(data).freeze()
            if frzn != data:
                neededChanges.append((instanceId, frzn))

        cu = self.cu
        for instanceId, frzn in neededChanges:
            cu.execute('''DELETE FROM TroveInfo
                          WHERE instanceId=? AND infoType=?''', instanceId,
                        trove._TROVEINFO_TAG_SIGS)
            cu.execute('''UPDATE TroveInfo SET data=?
                          WHERE instanceId=? AND infoType=?
                       ''', frzn, instanceId, trove._TROVEINFO_TAG_PATH_HASHES)
        return self.Version

class MigrateTo_18(SchemaMigration):
    Version = 18
    def migrate(self):
        cu = self.cu
        cu.execute("""
    CREATE TABLE NewInstances(
        instanceId      %(PRIMARYKEY)s,
        troveName       %(STRING)s,
        versionId       INTEGER,
        flavorId        INTEGER,
        timeStamps      %(STRING)s,
        isPresent       INTEGER,
        pinned          BOOLEAN
    )""" % self.db.keywords)
        cu.execute('INSERT INTO NewInstances SELECT * FROM Instances')
        cu.execute('DROP TABLE Instances')
        cu.execute('ALTER TABLE NewInstances RENAME TO Instances')
        # recreate indexes
        cu.execute("CREATE INDEX InstancesNameIdx ON Instances(troveName)")
        cu.execute("CREATE UNIQUE INDEX InstancesIdx ON "
                   "Instances(troveName, versionId, flavorId)")

        cu.execute('''DELETE FROM TroveInfo WHERE instanceId
                      NOT IN (SELECT instanceId FROM Instances)''')

        # delete BuildDeps, Loaded troves, label path, and policy tups
        # from components (they shouldn't have had them in the first place)
        cu.execute('''DELETE FROM TroveInfo
                        WHERE infoType in (4,5,11,12) AND
                               instanceId IN (
                                SELECT instanceId FROM Instances
                                    WHERE troveName LIKE '%:%')''')
        return self.Version

class MigrateTo_19(SchemaMigration):
    Version = 19
    def migrate(self):
        cu = self.cu

        versionStream = streams.IntStream()
        versionStream.set(0)

        incompleteStream = streams.ByteStream()
        incompleteStream.set(1)

        for tag, data in [
              (trove._TROVEINFO_TAG_TROVEVERSION, versionStream.freeze()),
              (trove._TROVEINFO_TAG_INCOMPLETE,   incompleteStream.freeze()) ]:
            cu.execute("""
                INSERT INTO TroveInfo
                    SELECT instanceId, ?, ? FROM Instances
                """, (tag, data))

        return self.Version

class MigrateTo_20(SchemaMigration):
    Version = 20

    def migrate(self):
        import tempfile
        import os
        from conary import dbstore

        # figure out where the database lives currently
        assert(self.db.driver == 'sqlite')
        dbPath = self.db.database
        assert(isinstance(dbPath, str))
        # make a new database file
        fd, fn = tempfile.mkstemp(prefix=os.path.basename(dbPath) + '-new-',
                                  dir=os.path.dirname(dbPath))
        os.close(fd)
        newdb = dbstore.connect(fn, driver='sqlite')
        # create the schema in the new db
        newdb.loadSchema()
        createSchema(newdb)
        # make sure we have a good view of the new schema
        newdb.commit()
        newdb.loadSchema()

        cu = self.cu
        # have to commit in order to attach
        self.db.commit()
        cu.execute("ATTACH '%s' AS newdb" %fn, start_transaction=False)

        for t in list(newdb.tables.keys()):
            self.message('Converting database schema to version 20 '
                         '- current table: %s' %t)
            cu.execute('INSERT OR REPLACE INTO newdb.%s '
                       'SELECT * FROM %s' % (t, t))

        # fix up some potentially bad entries we know about
        cu.execute("""UPDATE newdb.TroveInfo
                      SET data='1.0'
                      WHERE hex(data)='31' AND infotype=3""")
        cu.execute("""UPDATE newdb.Dependencies
                      SET flag='1.0'
                      WHERE name LIKE 'conary:%' AND flag='1'""");

        self.message('Converting database schema to version 20 '
                     '- committing')
        self.db.commit()
        self.message('')
        newdb.close()
        os.chmod(fn, 0o644)
        os.rename(dbPath, dbPath + '-pre-schema-update')
        os.rename(fn, dbPath)
        self.db.reopen()
        self.db.loadSchema()
        return self.Version


def _lockedSql(db, func, *args):
    """
    Ensure write lock on database, otherwise concurrent access can result in
    "schema has changed" errors.
    """
    if not db.inTransaction():
        db.cursor().execute('BEGIN IMMEDIATE')
    return func(*args)


# silent update while we're at schema 20. We only need to create a
# index, so there is no need to do a full blown migration and stop
# conary from working until a schema migration is done
def optSchemaUpdate(db):
    # drop any ANALYZE information, because it makes sqlite go
    # very slowly.
    cu = db.cursor()
    cu.execute("select count(*) from sqlite_master where name='sqlite_stat1'")
    count = cu.fetchall()[0][0]
    if count != 0:
        cu.execute('select count(*) from sqlite_stat1')
        count = cu.fetchall()[0][0]
        if count != 0:
            _lockedSql(db, cu.execute, "DELETE FROM sqlite_stat1")

    # Create DatabaseAttributes (if it doesn't exist yet)
    if 'DatabaseAttributes' not in db.tables:
        _lockedSql(db, createDatabaseAttributes, db)

    #do we have the index we need?
    if "TroveInfoInstTypeIdx" not in db.tables["TroveInfo"]:
        _lockedSql(db, db.createIndex, "TroveInfo", "TroveInfoInstTypeIdx", "infoType,instanceId")
    if 'DBTroveFilesInstanceIdx' in db.tables['DBTroveFiles']:
        _lockedSql(db, db.dropIndex, 'DBTroveFiles', 'DBTroveFilesInstanceIdx')
    if 'DBTroveFilesInstanceIdx2' not in db.tables['DBTroveFiles']:
        _lockedSql(db, db.createIndex, 'DBTroveFiles',
                'DBTroveFilesInstanceIdx2', 'instanceId, pathId')


def _shareLock(db):
    """
    Take a share lock on the database syslock when an optional migration might
    run. If it conflicts due to an ongoing update then bail out.
    """
    if db.database == ':memory:':
        # Nothing to lock
        return None, True
    lockPath = os.path.join(os.path.dirname(db.database), 'syslock')
    try:
        lockFile = open(lockPath, 'r+')
        fcntl.lockf(lockFile.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    except IOError as err:
        if err.args[0] in (errno.EAGAIN, errno.EACCES):
            # Busy or no write access; skip optional migrations
            return None, False
        elif err.args[0] == errno.ENOENT:
            # Database has never been locked. Probably running in a testsuite,
            # so proceed anyway.
            return None, True
        raise
    return lockFile, True


def checkVersion(db):
    global VERSION
    version = db.getVersion()
    if version == VERSION:
        # the actions performed by this function should be integrated
        # in the next schema update, when we have a reason to block
        # conary functionality...  These schema changes *MUST* not be
        # required for Read Only functionality
        lockFile = None
        try:
            try:
                lockFile, locked = _shareLock(db)
                if locked:
                    optSchemaUpdate(db)
            except (sqlerrors.ReadOnlyDatabase, sqlerrors.DatabaseLocked):
                pass
        finally:
            if lockFile:
                lockFile.close()
        return version
    if version > VERSION:
        raise NewDatabaseSchema
    if version == 0:
        # assume we're setting up a new environment
        if "DatabaseVersion" not in db.tables:
            # if DatabaseVersion does not exist, but any other tables do exist,
            # then the database version is too old to deal with it
            if len(db.tables) > 0:
                raise OldDatabaseSchema
        version = db.setVersion(VERSION)

    if version in (2, 3, 4):
        version = MigrateTo_5(db)()

    # instantiate and call appropriate migration objects in succession.
    while version and version < VERSION:
        fname = 'MigrateTo_' + str(version.major + 1)
        migr = sys.modules[__name__].__dict__[fname](db)
        version = migr()
    return version

class OldDatabaseSchema(errors.DatabaseError):
    def __str__(self):
        return self.msg

    def __init__(self, msg = None):
        if msg:
            self.msg = msg
        else:
            self.msg = "The Conary database on this system is too old. "    \
                       "For information on how to\nconvert this database, " \
                       "please visit http://wiki.rpath.com/ConaryConversion."

class NewDatabaseSchema(errors.DatabaseError):
    msg = """The conary database on this system is too new.  You may have multiple versions of conary installed and be running the wrong one, or your conary may have been downgraded.  Please visit http://wiki.rpath.com for information on how to get support."""

    def __init__(self):
        errors.DatabaseError.__init__(self, self.msg)
