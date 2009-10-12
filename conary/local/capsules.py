#
# Copyright (c) 2009 rPath, Inc.
#
# This program is distributed under the terms of the Common Public License,
# version 1.0. A copy of this license should have been distributed with this
# source file in a file called LICENSE. If it is not present, the license
# is always available at http://www.rpath.com/permanent/licenses/CPL-1.0.
#
# This program is distributed in the hope that it will be useful, but
# without any warranty; without even the implied warranty of merchantability
# or fitness for a particular purpose. See the Common Public License for
# full details.
#

import os, tempfile, sys

from conary import trove
from conary.lib import util

class CapsuleOperation(object):

    def __init__(self, root, db, changeSet):
        self.root = root
        self.db = db
        self.changeSet = changeSet

    def apply(self, root):
        raise NotImplementedException

    def install(self, troveCs):
        raise NotImplementedException

    def remove(self, trove):
        raise NotImplementedException

class SingleCapsuleOperation(CapsuleOperation):

    def __init__(self, *args, **kwargs):
        CapsuleOperation.__init__(self, *args, **kwargs)
        self.installs = []
        self.removes = []

    def _filesNeeded(self):
        return [ x[1] for x in self.installs ]

    def apply(self):
        raise NotImplementedError

    def install(self, troveCs):
        if troveCs.getOldVersion():
            trv = self.db.getTrove(*troveCs.getOldNameVersionFlavor())
            self.remove(trv)
            trv = trv.copy()
            trv.applyChangeSet(troveCs)
        else:
            trv = trove.Trove(troveCs)

        for pathId, path, fileId, version in trv.iterFileList(capsules = True):
            # there should only be one...
            break

        assert(pathId == trove.CAPSULE_PATHID)

        self.installs.append((troveCs, (pathId, path, fileId)))

    def remove(self, trv):
        self.removes.append(trv)

class MetaCapsuleOperations(CapsuleOperation):

    availableClasses = { 'rpm' : ('conary.local.rpmcapsule',
                                  'RpmCapsuleOperation') }

    def __init__(self, *args, **kwargs):
        CapsuleOperation.__init__(self, *args, **kwargs)
        self.capsuleClasses = {}

    def apply(self):
        fileDict = {}
        for kind, obj in sorted(self.capsuleClasses.items()):
            fileDict.update(
                dict(((x[0], x[2]), x[1]) for x in obj._filesNeeded()))

        try:
            for ((pathId, fileId), path) in sorted(fileDict.items()):
                tmpfd, tmpname = tempfile.mkstemp(prefix = path,
                                                  suffix = '.conary')
                fObj = self.changeSet.getFileContents(pathId, fileId)[1].get()
                util.copyfileobj(fObj, os.fdopen(tmpfd, "w"))
                # tmpfd is closed when the file object created by os.fdopen
                # disappears
                fileDict[(pathId, fileId)] = tmpname

            for kind, obj in sorted(self.capsuleClasses.items()):
                obj.apply(fileDict)
        finally:
            for tmpPath in fileDict.values():
                try:
                    os.unlink(tmpPath)
                except:
                    pass

    def getCapsule(self, kind):
        if kind not in self.capsuleClasses:
            module, klass = self.availableClasses[kind]

            if module not in sys.modules:
                __import__(module)
            self.capsuleClasses[kind] = \
                getattr(sys.modules[module], klass)(self.root, self.db,
                                                    self.changeSet)

        return self.capsuleClasses[kind]

    def install(self, troveCs):
        absTroveInfo = troveCs.getFrozenTroveInfo()
        capsuleInfo = trove.TroveInfo.find(trove._TROVEINFO_TAG_CAPSULE,
                                             absTroveInfo)
        if not capsuleInfo or not capsuleInfo.type():
            return False

        capsule = self.getCapsule(capsuleInfo.type())
        capsule.install(troveCs)

        return True

    def remove(self, trove):
        cType = trove.troveInfo.capsule.type()
        if not cType:
            return False

        capsule = self.getCapsule(cType)
        capsule.remove(trove)
        return True
