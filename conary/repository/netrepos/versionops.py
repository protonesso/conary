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


from conary import versions
from conary.dbstore import idtable
from conary.dbstore import sqlerrors
from conary.repository import trovesource
from conary.repository.errors import DuplicateBranch, InvalidSourceNameError
from conary.repository.netrepos import items

LATEST_TYPE_ANY     = trovesource.TROVE_QUERY_ALL     # redirects, removed, and normal
LATEST_TYPE_PRESENT = trovesource.TROVE_QUERY_PRESENT # redirects and normal
LATEST_TYPE_NORMAL  = trovesource.TROVE_QUERY_NORMAL  # hide branches which end in redirects

class BranchTable(idtable.IdTable):
    def __init__(self, db):
        idtable.IdTable.__init__(self, db, "Branches", "branchId", "branch")

    def addId(self, branch):
        assert(isinstance(branch, versions.Branch))
        cu = self.db.cursor()
        cu.execute("INSERT INTO Branches (branch) VALUES (?)",
                   branch.asString())
        return cu.lastrowid

    def getId(self, theId):
        return versions.VersionFromString(idtable.IdTable.getId(self, theId))

    def __getitem__(self, branch):
        assert(isinstance(branch, versions.Branch))
        return idtable.IdTable.__getitem__(self, branch.asString())

    def get(self, branch, defValue):
        assert(isinstance(branch, versions.Branch))
        return idtable.IdTable.get(self, branch.asString(), defValue)

    def __delitem__(self, branch):
        assert(isinstance(branch, versions.Branch))
        idtable.IdTable.__delitem__(self, branch.asString())

    def has_key(self, branch):
        assert(isinstance(branch, versions.Branch))
        return idtable.IdTable.has_key(self, branch.asString())

    def iterkeys(self):
        raise NotImplementedError

    def iteritems(self):
        raise NotImplementedError

class LabelTable(idtable.IdTable):
    def __init__(self, db):
        idtable.IdTable.__init__(self, db, 'Labels', 'labelId', 'label')

    def addId(self, label):
        idtable.IdTable.addId(self, label.asString())

    def __getitem__(self, label):
        return idtable.IdTable.__getitem__(self, label.asString())

    def get(self, label, defValue):
        return idtable.IdTable.get(self, label.asString(), defValue)

    def __delitem__(self, label):
        idtable.IdTable.__delitem__(self, label.asString())

    def has_key(self, label):
        return idtable.IdTable.has_key(self, label.asString())

    def iterkeys(self):
        raise NotImplementedError

    def iteritems(self):
        raise NotImplementedError

# class and methods for handling LatestCache operations
class LatestTable:
    def __init__(self, db):
        self.db = db
    def rebuild(self, cu = None):
        if cu is None:
            cu = self.db.cursor()
        # prepare for rebuild
        self.db.truncate("LatestCache")
        # populate the LatestCache table. We need to split the inserts
        # into chunks to make sure the backend can handle all the data
        # we're inserting.
        def _insertView(cu, latestType):
            latest = None
            if latestType == LATEST_TYPE_ANY:
                latest = "LatestViewAny"
            elif latestType == LATEST_TYPE_PRESENT:
                latest = "LatestViewPresent"
            elif latestType == LATEST_TYPE_NORMAL:
                latest = "LatestViewNormal"
            else:
                raise RuntimeError("Invalid Latest type requested in rebuild %s" %(
                    latestType))
            cu.execute("""
            insert into LatestCache
                (latestType, userGroupId, itemId, branchId, flavorId, versionId)
            select
                %d, userGroupId, itemId, branchId, flavorId, versionId
            from %s
            """ % (latestType, latest))
        _insertView(cu, LATEST_TYPE_ANY)
        _insertView(cu, LATEST_TYPE_PRESENT)
        _insertView(cu, LATEST_TYPE_NORMAL)
        self.db.analyze("LatestCache")
        return

    def update(self, cu, itemId, branchId, flavorId, roleId = None):
        cond = ""
        args = [itemId, branchId, flavorId]
        if roleId is not None:
            cond = "and userGroupId = ?"
            args.append(roleId)
        cu.execute("""
        delete from LatestCache
        where itemId = ? and branchId = ? and flavorId = ? %s""" % (cond,),
                   args)
        cu.execute("""
        insert into LatestCache
            (latestType, userGroupId, itemId, branchId, flavorId, versionId)
        select
            latestType, userGroupId, itemId, branchId, flavorId, versionId
        from LatestView
        where itemId = ? and branchId = ? and flavorId = ? %s""" % (cond,),
                   args)

    def updateInstanceId(self, cu, instanceId):
        cu.execute("""
        select itemId, flavorId, branchId
        from Instances join Nodes using(itemId, versionId)
        where instanceId = ?""", instanceId)
        for itemId, flavorId, branchId in cu.fetchall():
            self.update(cu, itemId, branchId, flavorId)

    def updateRoleId(self, cu, roleId, tmpInstances=False):
        if tmpInstances:
            # heuristics - we need to determine if it is easier to
            # recompute the entire latest cache for this roleId or
            # we should do discrete operations for each (itemId, flavorId) pair
            cu.execute(""" select count(*) from (
            select distinct itemId, flavorId from tmpInstances
            join Instances using(instanceId) ) as q""")
            tmpCount = cu.fetchone()[0]
            cu.execute(""" select count(*) from (
            select distinct itemId, flavorId from UserGroupInstancesCache as ugi
            join Instances using(instanceId) where ugi.userGroupId = ? ) as q""",
                       roleId)
            ugiCount = cu.fetchone()[0]
            # looping over tmpInstances tuples is only effective if we
            # process fewer than 1/4 (roughly) of the already existing
            # entries. Also, 500 entries will result in at least 2000
            # queries on the backend, which will also take time.
            if tmpCount > min(1000, ugiCount / 4):
                # do them all, it is more effective
                tmpInstances = False
        if not tmpInstances:
            cu.execute("delete from LatestCache where userGroupId = ?", roleId)
            cu.execute("""
            insert into LatestCache
                (latestType, userGroupId, itemId, branchId, flavorId, versionId)
            select
                latestType, userGroupId, itemId, branchId, flavorId, versionId
                from LatestView where userGroupId = ? """, roleId)
            return
        # we need to be more discriminate since we know what
        # instanceIds are new (they are provided in tmpInstances table)
        cu.execute("""
        select distinct itemId, flavorId, branchId
        from tmpInstances join Instances using(instanceId)
        join Nodes using(itemId, versionId) """)
        for itemId, flavorId, branchId in cu.fetchall():
            self.update(cu, itemId, branchId, flavorId, roleId)

    def updateFromNewTroves(self, table='tmpNewTroves'):
        """
        Update latest entries for a list of (itemId, branchId, flavorId) slots.
        """
        # This used to use the temp table but with very large databases the
        # query planner would make some awful decisions and end up taking 5
        # minutes to insert a single new trove. This way has great performance
        # for single troves and scales suitably well into the thousands.
        #
        # TODO: it's likely that the rest of the commit path would be better
        # off using a list in software instead of a temporary table.
        # Investigate that.
        cu = self.db.cursor()
        cu.execute("SELECT itemId, branchId, flavorId FROM %s" % table)
        pieces = ['(itemId = %d AND branchId = %d AND flavorId = %d)'
                % tuple(x) for x in cu]
        count = 1000
        while pieces:
            query = ' OR '.join(pieces[-count:])
            del pieces[-count:]
            cu.execute("DELETE FROM LatestCache WHERE " + query)
            cu.execute("""
                INSERT INTO LatestCache (latestType, userGroupId, itemId, branchId,
                        flavorId, versionId)
                SELECT DISTINCT v.latestType, v.userGroupId, v.itemId, v.branchId,
                        v.flavorId, v.versionId
                FROM LatestView v WHERE """ + query)


class LabelMap(idtable.IdPairSet):
    def __init__(self, db):
        idtable.IdPairMapping.__init__(self, db, 'LabelMap', 'itemId', 'labelId', 'branchId')

    def branchesByItem(self, itemId):
        return self.getByFirst(itemId)

class Nodes:
    def __init__(self, db):
        self.db = db

    def addRow(self, itemId, branchId, versionId, sourceItemId, timeStamps):
        cu = self.db.cursor()
        cu.execute("""
        INSERT INTO Nodes
        (itemId, branchId, versionId, sourceItemId, timeStamps, finalTimeStamp)
        VALUES (?, ?, ?, ?, ?, ?)""",
                   itemId, branchId, versionId, sourceItemId,
                   ":".join(["%.3f" % x for x in timeStamps]),
                   '%.3f' %timeStamps[-1])
        return cu.lastrowid

    def hasItemId(self, itemId):
        cu = self.db.cursor()
        cu.execute("SELECT itemId FROM Nodes WHERE itemId=?",
                   itemId)
        return not(cu.fetchone() == None)

    def hasRow(self, itemId, versionId):
        cu = self.db.cursor()
        cu.execute("SELECT itemId FROM Nodes "
                        "WHERE itemId=? AND versionId=?", itemId, versionId)
        return not(cu.fetchone() == None)

    def getRow(self, itemId, versionId, default):
        cu = self.db.cursor()
        cu.execute("SELECT nodeId FROM Nodes "
                        "WHERE itemId=? AND versionId=?", itemId, versionId)
        nodeId = cu.fetchone()
        if nodeId is None:
            return default
        return nodeId[0]

    def updateSourceItemId(self, nodeId, sourceItemId, mirrorMode=False):
        # mirrorMode is allowed to "steal" the sourceItemId of an
        # existing package this is done in order to protect old
        # mirrors that have already copied over busted content at
        # least partially
        cu = self.db.cursor()
        cu.execute("select sourceItemId from Nodes where nodeId = ?", nodeId)
        oldItemId = cu.fetchall()[0][0]
        if oldItemId is None or (oldItemId != sourceItemId and mirrorMode):
            cu.execute("update Nodes set sourceItemId = ? where nodeId = ?",
                       (sourceItemId, nodeId))
            return True
        if oldItemId != sourceItemId:
            # need to hit the database again to generate a nice(er) exception
            cu.execute("select Items.item, Versions.version, OldItems.item "
                       "from Nodes join Items on Nodes.itemId = Items.itemId "
                       "join Versions on Nodes.versionId = Versions.versionId "
                       "join Items as OldItems on Nodes.sourceItemId = OldItems.itemId "
                       "where Nodes.nodeId = ?", nodeId)
            ntup = tuple(cu.fetchall()[0])
            cu.execute("select item from Items where itemId = ?", sourceItemId)
            ntup = ntup + (cu.fetchall()[0][0],)
            raise InvalidSourceNameError(*ntup)
        return False # noop

class SqlVersioning:
    def __init__(self, db, versionTable, branchTable):
        self.items = items.Items(db)
        self.labels = LabelTable(db)
        self.labelMap = LabelMap(db)
        self.versionTable = versionTable
        self.branchTable = branchTable
        self.needsCleanup = False
        self.nodes = Nodes(db)
        self.db = db

    def versionsOnBranch(self, itemId, branchId):
        cu = self.db.cursor()
        cu.execute("""
            SELECT versionId FROM Nodes WHERE
                itemId=? AND branchId=? ORDER BY finalTimeStamp DESC
        """, itemId, branchId)

        for (versionId,) in cu:
            yield versionId

    def branchesOfLabel(self, itemId, label):
        labelId = self.labels[label]
        return self.labelMap[(itemId, labelId)]

    def versionsOfItem(self, itemId):
        for branchId in self.labelMap.branchesByItem(itemId):
            for versionId in self.versionsOnBranch(itemId, branchId):
                yield versionId

    def branchesOfItem(self, itemId):
        return self.labelMap.branchesByItem(itemId)

    def hasVersion(self, itemId, versionId):
        return self.nodes.hasItemId(itemId)

    def createVersion(self, itemId, version, flavorId, sourceName):
        """
        Creates a new versionId for itemId. The branch must already exist
        for the given itemId.
        """
        # make sure the branch exists; we need the branchId in case we
        # need to make this the latest version on the branch
        branch = version.branch()
        label = branch.label()
        branchId = self.branchTable.get(branch, None)
        if not branchId:
            # should we implicitly create these? it's certainly easier...
            #raise MissingBranchError(itemId, branch)
            branchId = self.createBranch(itemId, branch)
        else:
            # make sure the branch exists for this itemId; there are cases
            # where the branch can exist but not the label (most notably
            # if the branch was part of a redirect target)
            labelId = self.labels.get(label, None)
            if labelId is None:
                self.labels.addId(label)
                labelId = self.labels[label]

            existingBranchId = None
            for existingBranchId in self.labelMap.get((itemId, labelId), []):
                if existingBranchId == branchId: break

            if existingBranchId != branchId:
                self.createBranch(itemId, branch)

        versionId = self.versionTable.get(version, None)
        if versionId == None:
            try:
                self.versionTable.addId(version)
            except sqlerrors.ColumnNotUnique:
                import sys
                print('ERROR: tried to add', version.asString(), 'to version table but it seems to already be there', versionId, file=sys.stderr)
                raise
            versionId = self.versionTable.get(version, None)

        if self.nodes.hasRow(itemId, versionId):
            raise DuplicateVersionError(itemId, version)

        sourceItemId = None
        if sourceName:
            sourceItemId = self.items.getOrAddId(sourceName)

        nodeId = self.nodes.addRow(itemId, branchId, versionId, sourceItemId,
                                   version.timeStamps())

        return (nodeId, versionId)

    def createBranch(self, itemId, branch):
        """
        Creates a new branch for the given node.
        """
        label = branch.label()
        branchId = self.branchTable.get(branch, None)
        if not branchId:
            branchId = self.branchTable.addId(branch)
            if label not in self.labels:
                self.labels.addId(label)

        labelId = self.labels[label]

        if (itemId, labelId) in self.labelMap and \
           branchId in self.labelMap[(itemId, labelId)]:
            raise DuplicateBranch
        self.labelMap.addItem((itemId, labelId), branchId)

        return branchId

class SqlVersionsError(Exception):
    pass

class MissingBranchError(SqlVersionsError):
    def __str__(self):
        return "node %d does not contain branch %s" % (
            self.itemId, self.branch.asString())
    def __init__(self, itemId, branch):
        SqlVersionsError.__init__(self)
        self.branch = branch
        self.itemId = itemId

class DuplicateVersionError(SqlVersionsError):
    def __str__(self):
        return "node %d already contains version %s" % (
            self.itemId, self.version.asString())
    def __init__(self, itemId, version):
        SqlVersionsError.__init__(self)
        self.version = version
        self.itemId = itemId
