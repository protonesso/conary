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


from conary.server import schema
from conary.repository import errors
from conary.repository.netrepos import instances, versionops
from conary.repository.netrepos import items
from conary.lib.tracelog import logMe

# - Entries in the RoleTroves table are processed and flattened
#   into the RoleAllTroves table
# - Entries in the Permissions table are processed and flattened into
#   the RoleAllPermissions table
# - Then, RoleAllTroves and RoleAllPermissions are summarized
#   in the RoleInstancesCache table

# base class for handling RAP and GAT
class RoleTable:
    def __init__(self, db):
        self.db = db

    def getWhereArgs(self, cond = "where", **kw):
        where = []
        for key, val in list(kw.items()):
            if val is None:
                continue
            where.append("%s = %d" % (key, val))
        if len(where):
            where = cond + " " + " and ".join(where)
        else:
            where = ""
        return where


# class and methods for handling RoleTroves operations
class RoleTroves(RoleTable):
    # given a list of (n,v,f) tuples, convert them to instanceIds in
    # the tmpInstanceId table
    def _findInstanceIds(self, troveList, checkMissing=True):
        cu = self.db.cursor()
        schema.resetTable(cu, "tmpNVF")
        schema.resetTable(cu, "tmpInstanceId")
        for (n,v,f) in troveList:
            cu.execute("insert into tmpNVF (name, version, flavor) "
                       "values (?,?,?)", (n,v,f), start_transaction=False)
        self.db.analyze("tmpNVF")
        cu.execute("""
        insert into tmpInstanceId (idx, instanceId)
        select tmpNVF.idx, Instances.instanceId
        from tmpNVF
        join Items on tmpNVF.name = Items.item
        join Versions on tmpNVF.version = Versions.version
        join Flavors on tmpNVF.flavor = Flavors.flavor
        join Instances on
            Instances.itemId = Items.itemId and
            Instances.versionId = Versions.versionId and
            Instances.flavorId = Flavors.flavorId
        where
            Instances.isPresent in (%d,%d)
        """ % (instances.INSTANCE_PRESENT_NORMAL,
               instances.INSTANCE_PRESENT_HIDDEN),
                   start_transaction=False)
        self.db.analyze("tmpInstances")
        # check if any troves specified are missing
        cu.execute("""
        select tmpNVF.idx, name, version, flavor
        from tmpNVF
        left join tmpInstanceId using(idx)
        where tmpInstanceId.instanceId is NULL
        """)
        if checkMissing:
            # granting permissions to a !present trove has a fuzzy meaning
            for i, n, v, f in cu.fetchall():
                raise errors.TroveMissing(n,v)
        return True

    # update the RoleAllTroves table
    def rebuild(self, cu = None, rtId = None, roleId = None):
        where = []
        args = {}
        if rtId is not None:
            where.append("ugtId = :rtId")
            args["rtId"] = rtId
        if roleId is not None:
            where.append("userGroupId = :roleId")
            args["roleId"] = roleId
        whereCond = ""
        andCond = ""
        if where:
            whereCond = "where " + " and ".join(where)
            andCond   = "and "   + " and ".join(where)
        if cu is None:
            cu = self.db.cursor()
        # update the UserGroupAllTroves table
        cu.execute("delete from UserGroupAllTroves %s" % (whereCond,), args)
        cu.execute("""
        insert into UserGroupAllTroves (ugtId, userGroupId, instanceId)
        select ugtId, userGroupId, instanceId from UserGroupTroves %s
        union
        select ugtId, userGroupId, TroveTroves.includedId
        from UserGroupTroves join TroveTroves using (instanceId)
        where UserGroupTroves.recursive = 1 %s
        """ %(whereCond, andCond), args)
        if rtId is None and roleId is None:
            # this was a full rebuild
            self.db.analyze("UserGroupAllTroves")
        return True

    # grant access on a troveList to role
    def add(self, roleId, troveList, recursive=True):
        """
        grant access on a troveList to a Role. If recursive = True,
        then access is also granted to all the children of the troves
        passed
        """
        self._findInstanceIds(troveList)
        recursive = int(bool(recursive))
        # we have now the list of instanceIds in the tmpInstanceId table.
        # avoid inserting duplicates
        cu = self.db.cursor()
        cu.execute("""
        select distinct tmpInstanceId.instanceId, ugt.userGroupId, ugt.ugtId, ugt.recursive
        from tmpInstanceId
        left join UserGroupTroves as ugt using(instanceId)
        """)
        # record the new permissions
        ugtList = []
        results = cu.fetchall()
        instanceIds = set([ x[0] for x in results ])
        for instanceId in instanceIds:
            # if there's more than one result here, a DB constraint has been broken
            permissions = [ x[2:] for x in results if x[0] == instanceId and x[1] == roleId ]
            if len(permissions) == 0:
                # This is a new role for this instanceId
                cu.execute("insert into UserGroupTroves(userGroupId, instanceId, recursive) "
                           "values (?,?,?)", (roleId, instanceId, recursive))
                ugtId = cu.lastrowid
            else:
                ugtId, recflag = permissions[0]
                if recursive and not recflag:
                    # granting recursive access to something that wasn't recursive before
                    cu.execute("update UserGroupTroves set recursive = ? where ugtId = ?",
                           (recursive, ugtId))
                else:
                    ugtId = None
            if ugtId:
                self.rebuild(cu, ugtId, roleId)
                ugtList.append(ugtId)
        return ugtList      

    # remove trove access grants
    def delete(self, roleId, troveList):
        """remove group access to troves passed in the (n,v,f) troveList"""
        self._findInstanceIds(troveList, checkMissing=False)
        cu = self.db.cursor()
        schema.resetTable(cu, "tmpId")
        cu.execute("""
        insert into tmpId(id)
        select ugtId from UserGroupTroves
        where userGroupId = ?
        and instanceId in (select instanceId from tmpInstanceId)
        """, roleId, start_transaction=False)
        self.db.analyze("tmpId")
        # save what instanceIds will be affected by this delete
        schema.resetTable(cu, "tmpInstances")
        cu.execute("""
        insert into tmpInstances(instanceId)
        select distinct ugat.instanceId
        from UserGroupAllTroves as ugat
        where ugat.userGroupId = ?
          and ugat.ugtId in (select id from tmpId)
        """, roleId, start_transaction = False)
        cu.execute("delete from UserGroupAllTroves where ugtId in (select id from tmpId)")
        cu.execute("delete from UserGroupTroves where ugtId in (select id from tmpId)")
        # filter out the ones that are still allowed based on other permissions
        cu.execute("""
        delete from tmpInstances
        where exists (
            select 1 from UserGroupAllTroves as ugat
            where userGroupId = ?
              and ugat.instanceId = tmpInstances.instanceId )
        """, roleId, start_transaction=False)
        return True

    # list what we have in the repository for a roleId
    def list(self, roleId):
        """return a list of the troves this usergroup is granted special access"""
        cu = self.db.cursor()
        cu.execute("""
        select Items.item, Versions.version, Flavors.flavor, ugt.recursive
        from UserGroupTroves as ugt
        join Instances using(instanceId)
        join Items on Instances.itemId = Items.itemId
        join Versions on Instances.versionId = Versions.versionId
        join Flavors on Instances.flavorId = Flavors.flavorId
        where ugt.userGroupId = ? """, roleId)
        return [ ((n,v,f),r) for n,v,f,r in cu.fetchall()]

# class and methods for handling RoleAllPermissions operations
class RolePermissions(RoleTable):

    def _filterItems(self, cu, sql, permissionId=None):
        """
        Apply regexp permission checks, returning a SQL clause that filters a
        join between instances and permissions.

        The supplied query string should select relevant rows from the
        instances table.
        """
        cu.execute("SELECT DISTINCT itemId, item FROM Items"
                " JOIN Permissions USING ( itemId )"
                + self.getWhereArgs("WHERE", permissionId=permissionId))
        patterns = dict(cu)

        # The supplied query yields items that need to be checked against
        # patterns.
        cu.execute(sql)
        where = set()
        for itemId, item in cu:
            for patternId, pattern in patterns.items():
                if pattern == 'ALL':
                    # This is by far the most common case, so instead of
                    # enumerating every trove with its own filter, just select
                    # on whether the ALL permission is applicable.
                    where.add('p.itemId = %d' % patternId)
                elif items.checkTrove(pattern, item):
                    where.add("(p.itemId = %d AND i.itemId = %d)"
                            % (patternId, itemId))
        if where:
            return "( %s )" % (" OR ".join(where))
        else:
            return None

    def addId(self, cu = None, permissionId = None, roleId = None, instanceId = None):
        """
        Adds into the RoleAllPermissions table new entries triggered by one or
        more recordIds.
        """
        if cu is None:
            cu = self.db.cursor()
        itemClause = self._filterItems(cu, "SELECT DISTINCT itemId, item "
                "FROM Instances JOIN Items USING (itemId)"
                + self.getWhereArgs("WHERE", instanceId=instanceId),
                permissionId=permissionId)
        if not itemClause:
            # No items matched any permission
            return
        cu.execute(
        """INSERT INTO UserGroupAllPermissions
                (permissionId, userGroupId, instanceId, canWrite)
            SELECT p.permissionId, p.userGroupId, i.instanceId, p.canWrite
            FROM Instances i
            JOIN Nodes USING (itemId, versionId)
            JOIN LabelMap USING (itemId, branchId)
            JOIN Permissions p ON p.labelId = 0 OR p.labelId = LabelMap.labelId
            WHERE %s%s""" % (
                itemClause,
                self.getWhereArgs(" AND",
                    permissionId=permissionId,
                    roleId=roleId,
                    instanceId=instanceId,
                    )))

    def addInstanceSet(self, cu, table, column):
        itemClause = self._filterItems(cu,
            """SELECT DISTINCT Items.itemId, Items.item FROM %s
            JOIN Instances USING (%s)
            JOIN Items ON Items.itemId = Instances.itemId
            """ % (table, column))
        if not itemClause:
            # No items matched any permission
            return
        cu.execute(
        """INSERT INTO UserGroupAllPermissions
                (permissionId, userGroupId, instanceId, canWrite)
            SELECT p.permissionId, p.userGroupId, i.instanceId, p.canWrite
            FROM %s
            JOIN Instances i USING (%s)
            JOIN Nodes n ON i.itemId = n.itemId AND i.versionId = n.versionId
            JOIN LabelMap m ON n.itemId = m.itemId AND n.branchId = m.branchId
            JOIN Permissions p ON p.labelId = 0 OR p.labelId = m.labelId
            WHERE %s
            """ % (table, column, itemClause))

    def deleteId(self, cu = None, permissionId = None, roleId = None,
                 instanceId = None):
        where = self.getWhereArgs("where", permissionId=permissionId,
            userGroupId=roleId, instanceId=instanceId)
        if cu is None:
            cu = self.db.cursor()
        cu.execute("delete from UserGroupAllPermissions %s" % (where,))
        return True

    def rebuild(self, cu = None, permissionId = None, roleId = None,
                instanceId = None):
        if cu is None:
            cu = self.db.cursor()
        self.deleteId(cu, permissionId, roleId, instanceId)
        self.addId(cu, permissionId, roleId, instanceId)
        if permissionId is None and roleId is None and instanceId is None:
            # this was a full rebuild
            self.db.analyze("UserGroupAllPermissions")
        return True


# this class takes care of the RoleInstancesCache table, which is
# a summary of rows present in RoleAllTroves and RoleAllPermissions tables
class RoleInstances(RoleTable):
    def __init__(self, db):
        RoleTable.__init__(self, db)
        self.rt = RoleTroves(db)
        self.rp = RolePermissions(db)
        self.latest = versionops.LatestTable(db)

    def _getRoleId(self, role):
        cu = self.db.cursor()
        cu.execute("SELECT userGroupId FROM UserGroups WHERE userGroup=?",
                   role)
        ret = cu.fetchall()
        if len(ret):
            return ret[0][0]
        raise errors.RoleNotFound

    def addTroveAccess(self, role, troveList, recursive=True):
        roleId = self._getRoleId(role)
        rtList = self.rt.add(roleId, troveList, recursive)
        # we now know the ids of the new acls added. They're useful in
        # updating the UGIC table
        cu = self.db.cursor()
        # grab the list of instanceIds we are adding to the UGIC table;
        # we need those for a faster recomputation of the LatestCache table
        schema.resetTable(cu, "tmpInstances")
        cu.execute("""
        insert into tmpInstances(instanceId)
        select distinct ugat.instanceId
        from UserGroupAllTroves as ugat
        where ugat.ugtId in (%s)
          and ugat.userGroupId = ?
          and not exists (
              select 1 from UserGroupInstancesCache as ugi
              where ugi.userGroupId = ?
                and ugi.instanceId = ugat.instanceId )
        """ % (",".join("%d" % x for x in rtList),),
                   (roleId, roleId), start_transaction=False)
        # insert into UGIC and recompute the latest table
        cu.execute("""
        insert into UserGroupInstancesCache (userGroupId, instanceId)
        select %d, instanceId from tmpInstances """ %(roleId,))
        # tmpInstances has instanceIds for which Latest needs to be recomputed
        self.db.analyze("tmpInstances")
        self.latest.updateRoleId(cu, roleId, tmpInstances=True)

    def deleteTroveAccess(self, role, troveList):
        roleId = self._getRoleId(role)
        # remove the RoleTrove access
        cu = self.db.cursor()
        self.rt.delete(roleId, troveList)
        # instanceIds that were removed from RAT are in tmpInstances now
        # RAP might still grant permissions to some, so we filter those out
        cu.execute("""
        delete from tmpInstances
        where exists (
            select 1 from UserGroupAllPermissions as ugap
            where ugap.userGroupId = ?
              and ugap.instanceId = tmpInstances.instanceId )
        """, roleId, start_transaction = False)
        self.db.analyze("tmpInstances")
        # now we should have in tmpInstances the instanceIds of the
        # troves this user can no longer access.
        cu.execute("""
        delete from UserGroupInstancesCache
        where userGroupId = ?
          and instanceId in (select instanceId from tmpInstances)
        """, roleId)
        # tmpInstances has instanceIds for which Latest needs to be recomputed
        self.latest.updateRoleId(cu, roleId, tmpInstances=True)

    def listTroveAccess(self, role):
        roleId = self._getRoleId(role)
        return self.rt.list(roleId)

    # changes in the Permissions table
    def addPermissionId(self, permissionId, roleId):
        cu = self.db.cursor()
        self.rp.addId(cu, permissionId = permissionId)
        # figure out newly accessible troves. We keep track separately
        # to speed up the Latest update
        schema.resetTable(cu, "tmpInstances")
        cu.execute("""
        insert into tmpInstances(instanceId)
        select instanceId from UserGroupAllPermissions as ugap
        where permissionId = ?
          and not exists (
              select instanceId from UserGroupInstancesCache as ugi
              where ugi.userGroupId = ?
              and ugi.instanceId = ugap.instanceId ) """,
                   (permissionId, roleId),
                   start_transaction = False)
        # update UsergroupInstancesCache
        cu.execute("""
        insert into UserGroupInstancesCache (userGroupId, instanceId, canWrite)
        select userGroupId, instanceId,
               case when sum(canWrite) = 0 then 0 else 1 end as canWrite
        from UserGroupAllPermissions
        where permissionId = ?
          and instanceId in (select instanceId from tmpInstances)
        group by userGroupId, instanceId
        """, permissionId)
        # update Latest
        self.latest.updateRoleId(cu, roleId, tmpInstances=True)

    def updatePermissionId(self, permissionId, roleId):
        cu = self.db.cursor()
        schema.resetTable(cu, "tmpInstances")
        # figure out how the access is changing
        cu.execute("""
        insert into tmpInstances(instanceId)
        select instanceId from UserGroupAllPermissions
        where permissionId = ? """, permissionId, start_transaction=False)
        # re-add
        self.rp.deleteId(cu, permissionId = permissionId)
        self.rp.addId(cu, permissionId = permissionId)
        # remove from consideration troves for which we still have access
        cu.execute("""
        delete from tmpInstances
        where exists (
            select 1 from UserGroupAllPermissions as ugap
            where ugap.userGroupId = :roleId
              and ugap.instanceId = tmpInstances.instanceId )
        or exists (
            select 1 from UserGroupAllTroves as ugat
            where ugat.userGroupId = :roleId
              and ugat.instanceId = tmpInstances.instanceId )
        """, roleId=roleId, start_transaction=False)
        self.db.analyze("tmpInstances")
        # remove trove access from troves that are left
        cu.execute("""
        delete from UserGroupInstancesCache
        where userGroupId = :roleId
          and instanceId in (select instanceId from tmpInstances)
          and not exists (
              select 1 from UserGroupAllTroves as ugat
              where ugat.userGroupId = :roleId
                and ugat.instanceId = UserGroupInstancesCache.instanceId )
        """, roleId=roleId)
        # add the new troves now
        cu.execute("""
        insert into UserGroupInstancesCache(userGroupId, instanceId, canWrite)
        select userGroupId, instanceId,
               case when sum(canWrite) = 0 then 0 else 1 end as canWrite
        from UserGroupAllPermissions as ugap
        where ugap.permissionId = ?
          and not exists (
              select 1 from UserGroupInstancesCache as ugi
              where ugi.instanceId = ugap.instanceId
                and ugi.userGroupId = ugap.userGroupId )
        group by userGroupId, instanceId
        """, permissionId)
        self.latest.updateRoleId(cu, roleId)
        return True

    # updates the canWrite flag for an acl change
    def updateCanWrite(self, permissionId, roleId):
        cu = self.db.cursor()
        # update the flattened table first
        cu.execute("""
        update UserGroupAllPermissions set canWrite = (
            select canWrite from Permissions where permissionId = ? )
        where permissionId = ? """, (permissionId, permissionId))
        # update the UserGroupInstancesCache now. hopefully we won't
        # do too many of these...
        cu.execute("""
        update UserGroupInstancesCache set canWrite = (
            select case when sum(canWrite) = 0 then 0 else 1 end
            from UserGroupAllPermissions as ugap
            where ugap.userGroupId = UserGroupInstancesCache.userGroupId
              and ugap.instanceId = UserGroupInstancesCache.instanceId )
        where userGroupId = ? and instanceId in (
            select instanceId from UserGroupAllPermissions as ugap2
            where ugap2.permissionId = ? )
        """, (roleId, permissionId))
        return True

    def deletePermissionId(self, permissionId, roleId):
        cu = self.db.cursor()
        # compute the list of troves for which no other RAP/RAT access exists
        schema.resetTable(cu, "tmpInstances")
        cu.execute("""
        insert into tmpInstances(instanceId)
        select ugi.instanceId from UserGroupInstancesCache as ugi
        where ugi.userGroupId = ?
          and not exists (
              select 1 from UserGroupAllPermissions as ugap
              where ugap.userGroupId = ?
                and ugap.instanceId = ugi.instanceId
                and ugap.permissionId != ? )
          and not exists (
              select 1 from UserGroupAllTroves as ugat
              where ugat.instanceId = ugi.instanceId
                and ugat.userGroupId = ? )""",
                   (roleId, roleId, permissionId, roleId),
                   start_transaction = False)
        # clean up the flattened table
        cu.execute("delete from UserGroupAllPermissions where permissionId = ?",
                   permissionId)
        # now we have only the troves which need to be erased out of UGIC
        self.db.analyze("tmpInstances")
        cu.execute("""
        delete from UserGroupInstancesCache
        where userGroupId = ?
        and instanceId in (select instanceId from tmpInstances)""", roleId)
        # update Latest
        self.latest.updateRoleId(cu, roleId, tmpInstances=True)

    # a new trove has been comitted to the system
    def addInstanceId(self, instanceId):
        cu = self.db.cursor()
        self.rp.addId(cu, instanceId = instanceId)
        cu.execute("""
        insert into UserGroupInstancesCache(userGroupId, instanceId, canWrite)
        select userGroupId, instanceId,
            case when sum(canWrite) = 0 then 0 else 1 end as canWrite
        from UserGroupAllPermissions as ugap
        where ugap.instanceId = ?
          and not exists (
              select 1 from UserGroupInstancesCache as ugi
              where ugi.instanceId = ugap.instanceId
                and ugi.userGroupId = ugap.userGroupId )
        group by userGroupId, instanceId
        """, instanceId)
        self.latest.updateInstanceId(cu, instanceId)

    def addInstanceIdSet(self, table, column):
        cu = self.db.cursor()
        self.rp.addInstanceSet(cu, table, column)
        cu.execute("""
        insert into UserGroupInstancesCache(userGroupId, instanceId, canWrite)
        select userGroupId, instanceId,
            case when sum(canWrite) = 0 then 0 else 1 end as canWrite
        from %s join
        UserGroupAllPermissions as ugap using (%s)
        where not exists (
              select 1 from UserGroupInstancesCache as ugi
              where ugi.instanceId = ugap.instanceId
                and ugi.userGroupId = ugap.userGroupId )
        group by userGroupId, instanceId
        """ % (table, column))

    def _updateLatest(self, table, column):
        cu = self.db.cursor()
        cu.execute("select %s from %s" % (column, table))
        for instanceId in [ x[0] for x in cu ]:
            self.latest.updateInstanceId(cu, instanceId)

    # these used used primarily by the markRemoved code
    def deleteInstanceId(self, instanceId):
        cu = self.db.cursor()
        for t in [ "UserGroupInstancesCache", "UserGroupAllTroves",
                   "UserGroupAllPermissions", "UserGroupTroves"]:
            cu.execute("delete from %s where instanceId = ?" % (t,),
                       instanceId)
        self.latest.updateInstanceId(cu, instanceId)

    def deleteInstanceIds(self, idTableName):
        cu = self.db.cursor()
        for t in [ "UserGroupInstancesCache", "UserGroupAllTroves",
                   "UserGroupAllPermissions", "UserGroupTroves" ]:
            cu.execute("delete from %s where instanceId in (select instanceId from %s)"%(
                t, idTableName))
        # this case usually does not require recomputing the
        # LatestCache since we only remove !present troves in bulk
        return True

    # rebuild the UGIC table entries
    def rebuild(self, roleId = None, cu = None):
        if cu is None:
            cu = self.db.cursor()
        where = self.getWhereArgs("where", userGroupId = roleId)
        cu.execute("delete from UserGroupInstancesCache %s" % (where,))
        # first, rebuild the flattened tables
        logMe(3, "rebuilding UserGroupAllTroves", "roleId=%s" % roleId)
        self.rt.rebuild(cu, roleId = roleId)
        logMe(3, "rebuilding UserGroupAllPermissions", "roleId=%s" % roleId)
        self.rp.rebuild(cu, roleId = roleId)
        # and now sum it up
        logMe(3, "updating UserGroupInstancesCache from UserGroupAllPermissions")
        cu.execute("""
        insert into UserGroupInstancesCache(userGroupId, instanceId, canWrite)
        select userGroupId, instanceId, case when sum(canWrite) = 0 then 0 else 1 end
        from UserGroupAllPermissions %s
        group by userGroupId, instanceId
        """ % (where,))
        cond = self.getWhereArgs("and", userGroupId = roleId)
        logMe(3, "updating UserGroupInstancesCache from UserGroupAllTroves")
        cu.execute("""
        insert into UserGroupInstancesCache(userGroupId, instanceId, canWrite)
        select distinct userGroupId, instanceId, 0 as canWrite
        from UserGroupAllTroves as ugat
        where not exists (
            select 1 from UserGroupInstancesCache as ugi
            where ugat.instanceId = ugi.instanceId
              and ugat.userGroupId = ugi.userGroupId )
        %s """ % (cond,))
        self.db.analyze("UserGroupInstancesCache")
        # need to rebuild the latest as well
        logMe(3, "rebuilding the LatestCache rows", "roleId=%s"%roleId)
        if roleId is not None:
            self.latest.updateRoleId(cu, roleId)
        else: # this is a full rebuild
            self.latest.rebuild()
        return True
