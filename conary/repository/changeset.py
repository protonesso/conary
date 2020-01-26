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


import struct
import errno
import gzip
import itertools
import os
import sys

try:
    from io import StringIO as _StringIO
    StringIO = _StringIO
except ImportError:
    from io import StringIO

from conary import files, rpmhelper, streams, trove, versions
from conary.lib import base85, enum, log, patch, sha1helper, util, api
from conary.lib import cpiostream
from conary.lib import fixeddifflib
from conary.lib.ext import pack
from conary.repository import filecontainer, filecontents, errors

# cft is a string used by the EnumeratedType class; it's not a type itself!
#
# "refr" being the same length as "file" matters. it means a path to a file's
#    contents are stored, not the file itself. it's used for repository
#    side changesets to avoid storing contents repeatedly
# "ptr" is for duplicate file contents in a changeset (including hardlinks)
# "hldr" means there are no contents and the file should be skipped
#    (used for locally stored rollbacks when the original file contents can't
#     be ascertained)
# "diff" means the file is stored as a unified diff, not absolute contents
# "file" means the file contents are stored normally
ChangedFileTypes = enum.EnumeratedType("cft", "file", "diff", "ptr",
                                       "refr", "hldr")

_STREAM_CS_PRIMARY  = 1
_STREAM_CS_TROVES     = 2
_STREAM_CS_OLD_TROVES = 3
_STREAM_CS_FILES    = 4

_FILEINFO_OLDFILEID = 1
_FILEINFO_NEWFILEID = 2
_FILEINFO_CSINFO    = 3

SMALL = streams.SMALL
LARGE = streams.LARGE

def makeKey(pathId, fileId):
    return pathId + fileId

def parseKey(key):
    return key[0:16], key[16:]

class FileInfo(streams.StreamSet):

    streamDict = {
        _FILEINFO_OLDFILEID : (SMALL, streams.StringStream, "oldFileId"),
        _FILEINFO_NEWFILEID : (SMALL, streams.StringStream, "newFileId"),
        _FILEINFO_CSINFO    : (LARGE, streams.StringStream, "csInfo"   )
        }
    __slots__ = [ "oldFileId", "newFileId", "csInfo" ]

    def __init__(self, first, newFileId = None, csInfo = None):
        if newFileId is None:
            streams.StreamSet.__init__(self, first)
        else:
            streams.StreamSet.__init__(self)
            self.oldFileId.set(first)
            self.newFileId.set(newFileId)
            self.csInfo.set(csInfo)

class ChangeSetNewTroveList(dict, streams.InfoStream):

    def freeze(self, skipSet = None):
        l = [ x[1].freeze() for x in sorted(self.items()) ]
        return pack.pack("!" + "SI" * len(l), *l)

    def thaw(self, data):
        # this is only used to reset the list; thawFromFile is used for
        # every real thaw
        while self:
            self.clear()

        assert(not data)

    def thawFromFile(self, f, totalSize):
        while self:
            self.clear()

        while totalSize:
            s = f.read(4)
            totalSize -= 4

            size = struct.unpack("!I", s)[0]

            s = f.read(size)
            totalSize -= size

            trvCs = trove.ThawTroveChangeSet(s)
            self[(trvCs.getName(), trvCs.getNewVersion(),
                                          trvCs.getNewFlavor())] = trvCs

    def __init__(self, data = None):
        if data:
            self.thaw(data)

class ChangeSetFileDict(dict, streams.InfoStream):

    def freeze(self, skipSet = None):
        fileList = []
        for ((oldFileId, newFileId), (csInfo)) in sorted(self.items()):
            if not oldFileId:
                oldFileId = ""

            s = FileInfo(oldFileId, newFileId, csInfo).freeze()
            fileList.append(struct.pack("!I", len(s)) + s)

        return "".join(fileList)

    def __getitem__(self, item):
        if item[0] is None:
            item = item[1]
        return dict.__getitem__(self, item)

    def get(self, item, default):
        if item[0] is None:
            item = item[1]
        return dict.get(self, item, default)

    def __setitem__(self, item, val):
        if item[0] is None:
            item = item[1]
        return dict.__setitem__(self, item, val)

    def __hasitem__(self):
        if item[0] is None:
            item = item[1]
        return dict.__hasitem__(self, item)

    def iteritems(self):
        for item in dict.iteritems(self):
            if isinstance(item[0], tuple):
                yield item
            else:
                yield (None, item[0]), item[1]

    def items(self):
        return list(self.items())

    def thaw(self ,data):
        i = 0
        while i < len(data):
            i, ( frzFile, ) = pack.unpack("!SI", i, data)
            info = FileInfo(frzFile)

            newFileId = info.newFileId()
            oldFileId = info.oldFileId()
            if oldFileId == "":
                self[sys.intern(newFileId)] = info.csInfo()
            else:
                self[(sys.intern(oldFileId), sys.intern(newFileId))] = info.csInfo()

    def __init__(self, data = None):
        if data:
            self.thaw(data)


class ChangeSet(streams.StreamSet):

    streamDict = {
        _STREAM_CS_PRIMARY:
        (LARGE, streams.ReferencedTroveList, "primaryTroveList"),
        _STREAM_CS_TROVES:
        (LARGE, ChangeSetNewTroveList,       "newTroves"       ),
        _STREAM_CS_OLD_TROVES:
        (LARGE, streams.ReferencedTroveList, "oldTroves"       ),
        _STREAM_CS_FILES:
           (LARGE, ChangeSetFileDict,        "files"           ),
    }
    ignoreUnknown = True

    def _resetTroveLists(self):
        # XXX hack
        self.newTroves = ChangeSetNewTroveList()
        self.oldTroves = streams.ReferencedTroveList()

    def isEmpty(self):
        return not bool(self.newTroves) and not bool(self.oldTroves)

    def isAbsolute(self):
        return self.absolute

    def isLocal(self):
        return self.local

    def addPrimaryTrove(self, name, version, flavor):
        assert(flavor is not None)
        self.primaryTroveList.append((name, version, flavor))

    def setPrimaryTroveList(self, l):
        del self.primaryTroveList[:]
        self.primaryTroveList.extend(l)

    @api.publicApi
    def getPrimaryTroveList(self):
        return self.primaryTroveList

    def getPrimaryPackageList(self):
        import warnings
        warnings.warn("getPrimaryPackage is deprecated, use "
                      "getPrimaryTroveList", DeprecationWarning)
        return self.primaryTroveList

    def newTrove(self, csTrove):
        old = csTrove.getOldVersion()
        new = csTrove.getNewVersion()
        assert(not old or min(old.timeStamps()) > 0)
        assert(min(new.timeStamps()) > 0)

        self.newTroves[(csTrove.getName(), new,
                        csTrove.getNewFlavor())] = csTrove

        if csTrove.isAbsolute():
            self.absolute = True
        if (old and old.onLocalLabel()) or new.onLocalLabel():
            self.local = 1

    def newPackage(self, csTrove):
        import warnings
        warnings.warn("newPackage is deprecated, use newTrove",
                      DeprecationWarning)
        return self.newTrove(csTrove)

    def delNewTrove(self, name, version, flavor):
        del self.newTroves[(name, version, flavor)]
        if (name, version, flavor) in self.primaryTroveList:
            self.primaryTroveList.remove((name, version, flavor))

    def oldTrove(self, name, version, flavor):
        assert(min(version.timeStamps()) > 0)
        self.oldTroves.append((name, version, flavor))

    def hasOldTrove(self, name, version, flavor):
        return (name, version, flavor) in self.oldTroves

    def delOldTrove(self, name, version, flavor):
        self.oldTroves.remove((name, version, flavor))

    @api.publicApi
    def iterNewTroveList(self):
        """
        @return: dictionary-valueiterator object
        """
        return iter(self.newTroves.values())

    def iterNewPackageList(self):
        import warnings
        warnings.warn("iterNewPackageList is deprecated", DeprecationWarning)
        return iter(self.newTroves.values())

    @api.publicApi
    def getNewTroveVersion(self, name, version, flavor):
        return self.newTroves[(name, version, flavor)]

    def hasNewTrove(self, name, version, flavor):
        return (name, version, flavor) in self.newTroves

    def getOldTroveList(self):
        return self.oldTroves

    def configFileIsDiff(self, pathId, fileId):
        key = makeKey(pathId, fileId)
        (tag, cont, compressed) = self.configCache.get(key, (None, None, None))
        if tag is None:
            (tag, cont, compressed) = self.configCache.get(pathId,
                                                           (None, None, None))
        return tag == ChangedFileTypes.diff

    def addFileContents(self, pathId, fileId, contType, contents, cfgFile,
                        compressed = False):

        key = makeKey(pathId, fileId)
        if cfgFile:
            cache = self.configCache
            if compressed:
                s = util.decompressString(contents.get().read())
                contents = filecontents.FromString(s)
                compressed = False
        else:
            cache = self.fileContents

        otherContType, otherContents, _ = cache.get(key,(None,None,None))

        if otherContents and otherContType == ChangedFileTypes.diff:
            if contType == ChangedFileTypes.diff and \
                contents.str != otherContents.str:
                # two different diffs is an error
                raise ChangeSetKeyConflictError(key)
        else:
            cache[key] = (contType, contents, compressed)

    def getFileContents(self, pathId, fileId, compressed = False):
        key = makeKey(pathId, fileId)
        if key in self.fileContents:
            (tag, contentObj, isCompressed) = self.fileContents[key]
        else:
            (tag, contentObj, isCompressed) = self.configCache[key]

        if compressed and isCompressed:
            # we have compressed contents, and we've been asked for compressed
            # contents
            pass
        elif not compressed and not isCompressed:
            # we have uncompressed contents, and we've asked for uncompressed
            # contents
            pass
        elif compressed and not isCompressed:
            # we have uncompressed contents, but have been asked for compressed
            # contents
            f = util.BoundedStringIO()
            compressor = util.DeterministicGzipFile(None, "w", fileobj = f)
            util.copyfileobj(contentObj.get(), compressor)
            compressor.close()
            f.seek(0)
            contentObj = filecontents.FromFile(f, compressed = True)
        else:
            # we have compressed contents, but have been asked for uncompressed
            assert(0)
            uncompressor = gzip.GzipFile(None, "r", fileobj = contentObj.get())
            contentObj = filecontents.FromFile(uncompressed)

        return (tag, contentObj)

    def addFile(self, oldFileId, newFileId, csInfo):
        self.files[(oldFileId, newFileId)] = csInfo

    def formatToFile(self, cfg, f):
        f.write("primary troves:\n")
        for (troveName, version, flavor) in self.primaryTroveList:
            if flavor.isEmpty():
                f.write("\t%s %s\n" % (troveName, version.asString()))
            else:
                f.write("\t%s %s %s\n" % (
                    troveName, version.asString(), flavor.freeze()))
        f.write("\n")

        for trv in self.newTroves.values():
            trv.formatToFile(self, f)
        for (troveName, version, flavor) in self.oldTroves:
            f.write("remove %s %s\n" % (troveName, version.asString()))

    def getFileChange(self, oldFileId, newFileId):
        return self.files.get((oldFileId, newFileId), None)

    def _findFileChange(self, fileId):
        # XXX this is a linear search - do not use this method!
        # this only exists for AbstractTroveChangeSet.formatToFile()
        for oldFileId, newFileId in self.files.keys():
            if newFileId == fileId:
                return oldFileId, self.files[(oldFileId, newFileId)]

    def writeContents(self, csf, contents, early, withReferences):
        # these are kept sorted so we know which one comes next
        idList = list(contents.keys())
        idList.sort()

        sizeCorrection = 0

        if early:
            tag = "1 "
        else:
            tag = "0 "

        # diffs come first, followed by plain files

        for hash in idList:
            (contType, f, compressed) = contents[hash]
            if contType == ChangedFileTypes.diff:
                csf.addFile(hash, f, tag + contType[4:],
                            precompressed = compressed)

        for hash in idList:
            (contType, f, compressed) = contents[hash]
            if contType != ChangedFileTypes.diff:
                if withReferences and \
                        isinstance(f, filecontents.CompressedFromDataStore):
                    sha1 = sha1helper.sha1ToString(f.getSha1())
                    realSize = os.stat(f.path()).st_size
                    nameEntry = sha1 + ' ' + str(realSize)
                    sizeCorrection += (realSize - len(nameEntry))
                    if realSize >= 0x100000000:
                        # add 4 bytes to store a 64-bit size
                        sizeCorrection += 4
                    csf.addFile(hash,
                                filecontents.FromString(nameEntry,
                                                        compressed = True),
                                tag + ChangedFileTypes.refr[4:],
                                precompressed = True)
                else:
                    csf.addFile(hash, f, tag + contType[4:],
                                precompressed = compressed)

        return sizeCorrection

    def writeAllContents(self, csf, withReferences):
        one = self.writeContents(csf, self.configCache, True, withReferences)
        two = self.writeContents(csf, self.fileContents, False, withReferences)

        return one + two

    def appendToFile(self, outFile, withReferences = False,
                     versionOverride = None):
        start = outFile.tell()

        csf = filecontainer.FileContainer(outFile,
                                          version = versionOverride,
                                          append = True)

        str = self.freeze()
        csf.addFile("CONARYCHANGESET", filecontents.FromString(str), "")
        correction = self.writeAllContents(csf,
                                           withReferences = withReferences)
        return (outFile.tell() - start) + correction

    def writeToFile(self, outFileName, withReferences = False, mode = 0o666,
                    versionOverride = None):
        # 0666 is right for mode because of umask
        try:
            outFileFd = os.open(outFileName,
                                os.O_RDWR | os.O_CREAT | os.O_TRUNC, mode)

            outFile = os.fdopen(outFileFd, "w+")

            size = self.appendToFile(outFile, withReferences = withReferences,
                                     versionOverride = versionOverride)
            outFile.close()
            return size
        except:
            os.unlink(outFileName)
            raise

    def makeRollback(self, db, redirectionRollbacks = True, repos = None,
                     clearCapsule = False):
        # clearCapsule is a hack to let repair work on capsules (where
        # we neither have nor need the capsule to perform the repair)
        assert(not self.absolute)

        rollback = ChangeSet()

        # if we need old contents for a file we can get them from the
        # filesystem or the local database (for config files). if the
        # original contents aren't available, we make a note of that
        # in this list and handle it later on
        hldrContents = []

        for troveCs in self.iterNewTroveList():
            if not troveCs.getOldVersion():
                # This was a new trove, and the inverse of a new trove is an
                # old trove, unless it's a phantom trove in which case don't
                # roll it back since we didn't install the underlying capsule
                # in the first place.
                if not troveCs.getNewVersion().onPhantomLabel():
                    rollback.oldTrove(troveCs.getName(),
                            troveCs.getNewVersion(), troveCs.getNewFlavor())
                continue

            if troveCs.getOldVersion().onPhantomLabel():
                # Also don't roll back updates from a phantom trove since we
                # have no way to put the original capsule back. Instead we'll
                # just leave the managed capsule trove alone and hope it's
                # close enough.
                continue

            # if redirectionRollbacks are requested, create one for troves
            # which are not on the local branch (ones which exist in the
            # repository)
            if not troveCs.getOldVersion().isOnLocalHost() and \
               not troveCs.getNewVersion().isOnLocalHost() and \
               redirectionRollbacks:
                newTrove = trove.Trove(troveCs.getName(),
                                       troveCs.getNewVersion(),
                                       troveCs.getNewFlavor(), None)
                oldTrove = trove.Trove(troveCs.getName(),
                                       troveCs.getOldVersion(),
                                       troveCs.getOldFlavor(), None,
                                       type = trove.TROVE_TYPE_REDIRECT)
                rollback.newTrove(oldTrove.diff(newTrove)[0])
                continue

            trv = db.getTrove(troveCs.getName(), troveCs.getOldVersion(),
                                troveCs.getOldFlavor())

            # make a copy because we modify it locally to clear capsules
            invertedTroveInfo = trove.TroveInfo(trv.getTroveInfo().freeze())
            if clearCapsule:
                invertedTroveInfo.capsule.reset()

            newTroveInfo = troveCs.getTroveInfo()
            if newTroveInfo is None:
                newTroveInfo = trove.TroveInfo(trv.getTroveInfo().freeze())
                newTroveInfo.twm(troveCs.getTroveInfoDiff(), newTroveInfo)
            newTroveInfoDiff = invertedTroveInfo.diff(newTroveInfo)

            # this is a modified trove and needs to be inverted

            invertedTrove = trove.TroveChangeSet(troveCs.getName(),
                                                 trv.getChangeLog(),
                                                 troveCs.getNewVersion(),
                                                 troveCs.getOldVersion(),
                                                 troveCs.getNewFlavor(),
                                                 troveCs.getOldFlavor(),
                                                 troveCs.getNewSigs(),
                                                 troveCs.getOldSigs(),
                                                 troveInfoDiff = newTroveInfoDiff)

            invertedTrove.setRequires(trv.getRequires())
            invertedTrove.setProvides(trv.getProvides())
            invertedTrove.setTroveInfo(invertedTroveInfo)

            for weak in (True, False):
                for (name, list) in troveCs.iterChangedTroves(
                                strongRefs = not weak, weakRefs = weak):
                    for (oper, version, flavor, byDef) in list:
                        if oper == '+':
                            invertedTrove.oldTroveVersion(name, version, flavor,
                                                          weakRef = weak)
                        elif oper == "-":
                            invertedTrove.newTroveVersion(name, version, flavor,
                               trv.includeTroveByDefault(name, version, flavor),
                               weakRef = weak)
                        elif oper == "~":
                            # invert byDefault flag
                            invertedTrove.changedTrove(name, version, flavor, not byDef,
                                                       weakRef = weak)

            for (pathId, path, origFileId, version) in troveCs.getNewFileList():
                invertedTrove.oldFile(pathId)

            for pathId in troveCs.getOldFileList():
                if not trv.hasFile(pathId):
                    # this file was removed using 'conary remove /path'
                    # so it does not go in the rollback
                    continue

                (path, origFileId, version) = trv.getFile(pathId)
                invertedTrove.newFile(pathId, path, origFileId, version)

                origFile = db.getFileVersion(pathId, origFileId, version)
                rollback.addFile(None, origFileId, origFile.freeze())

                if not origFile.hasContents:
                    continue

                # We only have the contents of config files available
                # from the db. Files which aren't in the db
                # we'll gather from the filesystem *as long as they have
                # not changed*. If they have changed, they'll show up as
                # members of the local branch, and their contents will be
                # saved as part of that change set. we don't rely on
                # the contents staying in the datastore; we cache them
                # instead
                if origFile.flags.isConfig():
                    cont = filecontents.FromDataStore(db.contentsStore,
                                                      origFile.contents.sha1())
                    rollback.addFileContents(pathId, origFileId,
                                             ChangedFileTypes.file,
                                             filecontents.FromString(
                                                cont.get().read()),
                                             1)
                else:
                    fullPath = db.root + path

                    try:
                        fsFile = files.FileFromFilesystem(fullPath, pathId,
                                    possibleMatch = origFile)
                    except OSError as e:
                        if e.errno != errno.ENOENT:
                            raise
                        fsFile = None

                    if fsFile and fsFile.contents == origFile.contents:
                        rollback.addFileContents(pathId, origFileId,
                                 ChangedFileTypes.file,
                                 filecontents.FromFilesystem(fullPath), 0)
                    elif origFile.contents.size() == 0:
                        rollback.addFileContents(pathId, origFileId,
                                 ChangedFileTypes.file,
                                 filecontents.FromString(''), 0)
                    else:
                        hldrContents.append((trv, pathId, origFileId,
                                             version, 0))


            for (pathId, newPath, newFileId, newVersion) in troveCs.getChangedFileList():
                if not trv.hasFile(pathId):
                    # the file has been removed from the local system; we
                    # don't need to restore it on a rollback
                    continue
                (curPath, curFileId, curVersion) = trv.getFile(pathId)

                if newPath:
                    invertedTrove.changedFile(pathId, curPath, curFileId, curVersion)
                else:
                    invertedTrove.changedFile(pathId, None, curFileId, curVersion)

                if curFileId == newFileId:
                    continue

                try:
                    csInfo = self.files[(curFileId, newFileId)]
                except KeyError:
                    log.error('File objects stored in your database do '
                              'not match the same version of those file '
                              'objects in the repository. The best thing '
                              'to do is erase the version on your system '
                              'by using "conary erase --just-db --no-deps" '
                              'and then run the update again by using '
                              '"conary update --replace-files"')
                    continue

                origFile = db.getFileVersion(pathId, curFileId, curVersion)

                if files.fileStreamIsDiff(csInfo):
                    # this is a diff, not an absolute change
                    newFile = origFile.copy()
                    newFile.twm(csInfo, origFile)
                else:
                    newFile = files.ThawFile(csInfo, pathId)

                rollback.addFile(newFileId, curFileId, origFile.diff(newFile))

                if not isinstance(origFile, files.RegularFile):
                    continue

                # If a config file has changed between versions, save
                # it; if it hasn't changed the unmodified version will
                # still be available from the database when the rollback
                # gets applied. We may be able to get away with just reversing
                # a diff rather then saving the full contents. capsule pathids
                # aren't expected to be available; we mark those as special
                if pathId == trove.CAPSULE_PATHID:
                    rollback.addFileContents(pathId, curFileId,
                                     ChangedFileTypes.hldr,
                                     filecontents.FromString(""), False);
                elif (origFile.flags.isConfig() and newFile.flags.isConfig() and
                        origFile.hasContents and newFile.hasContents and
                        (origFile.contents.sha1() != newFile.contents.sha1())):
                    if self.configFileIsDiff(newFile.pathId(), newFileId):
                        (contType, cont) = self.getFileContents(
                                    newFile.pathId(), newFileId)
                        f = cont.get()
                        diff = "".join(patch.reverse(f.readlines()))
                        f.seek(0)
                        cont = filecontents.FromString(diff)
                        rollback.addFileContents(pathId, curFileId,
                                                 ChangedFileTypes.diff, cont, 1)
                    else:
                        cont = filecontents.FromDataStore(db.contentsStore,
                                    origFile.contents.sha1())
                        rollback.addFileContents(pathId, curFileId,
                                                 ChangedFileTypes.file, cont,
                                                 newFile.flags.isConfig())
                elif ((origFile.hasContents != newFile.hasContents) or
                      (origFile.hasContents and newFile.hasContents and
                         origFile.contents.sha1() != newFile.contents.sha1())):
                    # this file changed, so we need the contents
                    fullPath = db.root + curPath
                    try:
                        fsFile = files.FileFromFilesystem(fullPath, pathId,
                                    possibleMatch = origFile)
                    except OSError as err:
                        if err.errno == errno.ENOENT:
                            # the file doesn't exist - the user removed
                            # it manually.  This will make us store
                            # just an empty string as contents
                            fsFile = None
                        else:
                            raise

                    isConfig = (origFile.flags.isConfig() or
                                newFile.flags.isConfig())

                    if (isinstance(fsFile, files.RegularFile) and
                        fsFile.contents.sha1() == origFile.contents.sha1()):
                        # the contents in the file system are right
                        rollback.addFileContents(pathId, curFileId,
                                         ChangedFileTypes.file,
                                         filecontents.FromFilesystem(fullPath),
                                         isConfig)
                    elif origFile.contents.size() == 0:
                        # contents are wrong but the file is empty anyway
                        rollback.addFileContents(pathId, curFileId,
                                 ChangedFileTypes.file,
                                 filecontents.FromString(''), 0)
                    else:
                        # the contents in the file system are wrong; add
                        # it to the list of things to deal with a bit later
                        hldrContents.append((trv, pathId, curFileId, curVersion,
                                             isConfig))

            rollback.newTrove(invertedTrove)

        for (name, version, flavor) in self.getOldTroveList():
            if version.onPhantomLabel():
                # Can't roll back erase of a phantom trove because we never had
                # the underlying capsule, so just skip it.
                continue
            if not version.isOnLocalHost() and redirectionRollbacks:
                oldTrove = trove.Trove(name, version, flavor, None,
                                       type = trove.TROVE_TYPE_REDIRECT)
                rollback.newTrove(oldTrove.diff(None)[0])
                continue

            trv = db.getTrove(name, version, flavor)
            troveDiff = trv.diff(None)[0]
            rollback.newTrove(troveDiff)

            # everything in the rollback is considered primary
            rollback.addPrimaryTrove(name, version, flavor)

            for (pathId, path, fileId, fileVersion) in trv.iterFileList(
                    members=True, capsules=True):
                fileObj = db.getFileVersion(pathId, fileId, fileVersion)
                rollback.addFile(None, fileId, fileObj.freeze())
                if fileObj.hasContents:
                    fullPath = db.root + path

                    if fileObj.flags.isConfig():
                        cont = filecontents.FromDataStore(db.contentsStore,
                                    fileObj.contents.sha1())
                        # make a copy of the contents in memory in case
                        # the database gets changed
                        cont = filecontents.FromString(cont.get().read())
                        rollback.addFileContents(pathId, fileId,
                                                 ChangedFileTypes.file, cont,
                                                 fileObj.flags.isConfig())
                        continue

                    if os.path.exists(fullPath):
                        fsFile = files.FileFromFilesystem(fullPath, pathId,
                                    possibleMatch = fileObj)
                    else:
                        fsFile = None

                    if fsFile and fsFile.hasContents and \
                            fsFile.contents.sha1() == fileObj.contents.sha1():
                        # the contents in the file system are right
                        contType = ChangedFileTypes.file
                        rollback.addFileContents(pathId, fileId,
                                        ChangedFileTypes.file,
                                        filecontents.FromFilesystem(fullPath),
                                        fileObj.flags.isConfig())
                    elif fileObj.contents.size() == 0:
                        # contents are wrong but the file is empty anyway
                        rollback.addFileContents(pathId, fileId,
                                 ChangedFileTypes.file,
                                 filecontents.FromString(''), 0)
                    else:
                        # the contents in the file system are wrong; we'll
                        # deal with this a bit later
                        hldrContents.append((trv, pathId, fileId, fileVersion,
                                             fileObj.flags.isConfig()))

        if not repos:
            # we don't have a repository object, so we have to handle missing
            # file contents the best we can (which is through a hldr content
            # type stub)
            cont = filecontents.FromString("")
            for trv, pathId, fileId, version, isConfig in hldrContents:
                rollback.addFileContents(pathId, fileId, ChangedFileTypes.hldr,
                                         cont, isConfig)
        else:
            # we have a repository, so we can get the contents for missing
            # contents

            # start off by looking for things which have capsules
            contentsNeeded = []
            capsContentsNeeded = {}
            for (trv, pathId, fileId, version, isConfig) in hldrContents:
                if (trv.troveInfo.capsule and
                      trv.troveInfo.capsule.type() ==
                            trove._TROVECAPSULE_TYPE_RPM):
                    caps = trv.iterFileList(members = False, capsules = True)
                    for capsPathId, capsPath, capsFileId, capsVersion in caps:
                        if (capsFileId, capsVersion) not in capsContentsNeeded:
                            t = (capsFileId, capsVersion)
                            l = capsContentsNeeded.get(t, None)
                            if l is None:
                                l = []
                                capsContentsNeeded[t] = l

                            path = trv.getFile(pathId)[0]
                            l.append((path, pathId, fileId, isConfig))
                else:
                    contentsNeeded.append((fileId, version))

            allFileContents = repos.getFileContents(
                    contentsNeeded + sorted(capsContentsNeeded.keys()))
            for (trv, pathId, fileId, version, isConfig), fileContents in \
                            zip(hldrContents, allFileContents):
                rollback.addFileContents(pathId, fileId, ChangedFileTypes.file,
                                         fileContents, isConfig)

            for ((fileId, version), l), fileContents in zip(
                        sorted(capsContentsNeeded.items()),
                        allFileContents[len(contentsNeeded):]):
                payload = rpmhelper.UncompressedRpmPayload(fileContents.get())
                filePaths = [ x[0] for x in l ]
                fileObjs = rpmhelper.extractFilesFromCpio(payload, filePaths)
                for (path, pathId, fileId, isConfig), f in \
                        zip(l, fileObjs):
                    rollback.addFileContents(pathId, fileId,
                                             ChangedFileTypes.file,
                                             filecontents.FromFile(f),
                                             isConfig)

        return rollback

    def setTargetShadow(self, repos, targetShadowLabel):
        """
        Retargets this changeset to create troves and files on
        shadow targetLabel off of the parent of the source node. Version
        calculations aren't quite right for source troves
        (s/incrementBuildCount).

        @param repos: repository which will be committed to
        @type repos: repository.Repository
        @param targetShadowLabel: label of the branch to commit to
        @type targetShadowLabel: versions.Label
        """
        assert(not targetShadowLabel == versions.LocalLabel())
        # if it's local, Version.parentVersion() has to work everywhere
        assert(self.isLocal())
        assert(not self.isAbsolute())

        troveVersions = {}

        troveCsList = [ (x.getName(), x) for x in self.iterNewTroveList() ]
        troveCsList.sort()
        troveCsList.reverse()
        origTroveList = repos.getTroves([ (x[1].getName(), x[1].getOldVersion(),
                                           x[1].getOldFlavor())
                                          for x in troveCsList ])

        # this loop needs to handle components before packages; reverse
        # sorting by name ensures that
        #
        # XXX this is busted for groups

        for (name, troveCs), oldTrv in \
                                zip(troveCsList, origTroveList):
            origVer = troveCs.getNewVersion()

            oldVer = troveCs.getOldVersion()
            assert(oldVer is not None)
            newVer = oldVer.createShadow(targetShadowLabel)
            newVer.incrementBuildCount()

            if repos.hasTrove(name, newVer, troveCs.getNewFlavor()):
                newVer = repos.getTroveLatestVersion(name, newVer.branch()).copy()
                newVer.incrementBuildCount()

            newTrv = oldTrv.copy()
            newTrv.applyChangeSet(troveCs)

            newTrv.changeVersion(newVer)
            newTrv.invalidateDigests()
            newTrv.computeDigests()

            assert(name not in troveVersions)
            troveVersions[(name, troveCs.getNewFlavor())] = \
                                [ (origVer, newVer) ]

            fileList = [ x for x in newTrv.iterFileList() ]
            for (pathId, path, fileId, fileVersion) in fileList:
                if not fileVersion.onLocalLabel(): continue
                newTrv.updateFile(pathId, path, newVer, fileId)

            subTroves = [ x for x in newTrv.iterTroveListInfo() ]
            for (name, subVersion, flavor), byDefault, isStrong in subTroves:
                if (name, flavor) not in troveVersions: continue

                newTrv.delTrove(name, subVersion, flavor, missingOkay = False)
                newTrv.addTrove(name, newVer, flavor, byDefault = byDefault,
                                weakRef = (not isStrong))

            # throw away sigs and recompute the hash
            newTrv.invalidateDigests()
            newTrv.computeDigests()

            self.delNewTrove(troveCs.getName(), troveCs.getNewVersion(),
                             troveCs.getNewFlavor())
            troveCs = newTrv.diff(oldTrv)[0]
            self.newTrove(troveCs)

        # this has to be true, I think...
        self.local = 0

    def getJobSet(self, primaries = False):
        """
        Regenerates the primary change set job (passed to change set creation)
        for this change set.
        """
        jobSet = set()

        for trvCs in list(self.newTroves.values()):
            if trvCs.getOldVersion():
                job = (trvCs.getName(),
                       (trvCs.getOldVersion(), trvCs.getOldFlavor()),
                       (trvCs.getNewVersion(), trvCs.getNewFlavor()),
                       trvCs.isAbsolute())
            else:
                job = (trvCs.getName(), (None, None),
                       (trvCs.getNewVersion(), trvCs.getNewFlavor()),
                       trvCs.isAbsolute())

            if not primaries or \
                    (job[0], job[2][0], job[2][1]) in self.primaryTroveList:
                jobSet.add(job)

        for item in self.oldTroves:
            if not primaries or item in self.primaryTroveList:
                jobSet.add((item[0], (item[1], item[2]),
                                (None, None), False))

        return jobSet

    def clearTroves(self):
        """
        Reset the newTroves and oldTroves list for this changeset. File
        information is preserved.
        """
        self.primaryTroveList.thaw("")
        self.newTroves.thaw("")
        self.oldTroves.thaw("")

    def removeCommitted(self, repos):
        """
        Walk the changeset and removes any items which are already in the
        repositories. Returns a changeset which will commit without causing
        duplicate trove errors. If everything in the changeset has already
        been committed, return False. If there are items left for commit,
        return True.

        @param repos: repository to check for duplicates
        @type repos: repository.netclient.NetworkRepositoryClient
        @rtype: repository.changeset.ChangeSet or None
        """
        newTroveInfoList = [ x.getNewNameVersionFlavor() for x in
                                self.iterNewTroveList() if x.getNewVersion()
                                is not None ]
        present = repos.hasTroves(newTroveInfoList)

        for (newTroveInfo, isPresent) in present.items():
            if isPresent:
                self.delNewTrove(*newTroveInfo)

        if self.newTroves:
            return True

        return False

    def _makeFileGitDiffCapsule(self, troveSource, pathId, xxx_todo_changeme, xxx_todo_changeme1, diffBinaries):
        (oldPath, oldFileId, oldFileVersion, oldFileObj) = xxx_todo_changeme
        (newPath, newFileId, newFileObj) = xxx_todo_changeme1
        if pathId == trove.CAPSULE_PATHID:
            return

        if oldFileId == newFileId:
            return

        if not oldFileObj:
            yield "diff --git a%s b%s\n" % (newPath, newPath)
            yield "new user %s\n" % newFileObj.inode.owner()
            yield "new group %s\n" % newFileObj.inode.group()
            yield "new mode %o\n" % (newFileObj.statType |
                                     newFileObj.inode.perms())
        else:
            yield "diff --git a%s b%s\n" % (oldPath, newPath)
            if oldFileObj.inode.perms() != newFileObj.inode.perms():
                yield "old mode %o\n" % (oldFileObj.statType |
                                         oldFileObj.inode.perms())
                yield "new mode %o\n" % (newFileObj.statType |
                                         newFileObj.inode.perms())
            if oldFileObj.inode.owner() != newFileObj.inode.owner():
                yield "old user %s\n" % oldFileObj.inode.owner()
                yield "new user %s\n" % newFileObj.inode.owner()

            if oldFileObj.inode.group() != newFileObj.inode.group():
                yield "old group %s\n" % oldFileObj.inode.group()
                yield "new group %s\n" % newFileObj.inode.group()

        if not newFileObj.hasContents:
            return
        elif (oldFileObj and oldFileObj.hasContents and
              oldFileObj.contents.sha1() == newFileObj.contents.sha1()):
            return
        yield "Encapsulated files differ\n"

    def _makeFileGitDiff(self, troveSource, pathId, xxx_todo_changeme2, xxx_todo_changeme3,
                         diffBinaries):
        (oldPath, oldFileId, oldFileVersion, oldFileObj) = xxx_todo_changeme2
        (newPath, newFileId, newFileObj) = xxx_todo_changeme3
        if oldFileId == newFileId:
            return

        if not oldFileObj:
            yield "diff --git a%s b%s\n" % (newPath, newPath)
            yield "new user %s\n" % newFileObj.inode.owner()
            yield "new group %s\n" % newFileObj.inode.group()
            yield "new mode %o\n" % (newFileObj.statType |
                                     newFileObj.inode.perms())
        else:
            yield "diff --git a%s b%s\n" % (oldPath, newPath)
            if oldFileObj.inode.perms() != newFileObj.inode.perms():
                yield "old mode %o\n" % (oldFileObj.statType |
                                         oldFileObj.inode.perms())
                yield "new mode %o\n" % (newFileObj.statType |
                                         newFileObj.inode.perms())
            if oldFileObj.inode.owner() != newFileObj.inode.owner():
                yield "old user %s\n" % oldFileObj.inode.owner()
                yield "new user %s\n" % newFileObj.inode.owner()

            if oldFileObj.inode.group() != newFileObj.inode.group():
                yield "old group %s\n" % oldFileObj.inode.group()
                yield "new group %s\n" % newFileObj.inode.group()

        if not newFileObj.hasContents:
            return
        elif (oldFileObj and oldFileObj.hasContents and
              oldFileObj.contents.sha1() == newFileObj.contents.sha1()):
            return

        newContentsType, newContents = self.getFileContents(pathId,
                                                            newFileId)

        if oldPath is None:
            oldPath = '/dev/null'

        if newContentsType == ChangedFileTypes.diff:
            yield "--- a%s\n" % oldPath
            yield "+++ b%s\n" % newPath
            for x in newContents.get().readlines():
                yield x
        else:
            # is the new content a text file?
            isConfig = False
            if newFileObj.flags.isConfig():
                isConfig = True
            elif newFileObj.contents.size() < 1028 * 20:
                contents = newContents.get().read()
                try:
                    contents.decode('utf-8')
                    isConfig = True
                except:
                    isConfig = False

            if isConfig and oldFileId and oldFileObj.hasContents:
                oldContents = troveSource.getFileContents(
                        [ (oldFileId, oldFileVersion) ])[0]
                if oldContents is None:
                    # contents are unavailable. assume it's binary
                    # rather than making repository calls
                    isConfig = False
                else:
                    contents = oldContents.get().read()
                    try:
                        contents.decode('utf-8')
                    except:
                        isConfig = False
            elif isConfig:
                oldContents = None

            if isConfig:
                yield "--- a%s\n" % oldPath
                yield "+++ b%s\n" % newPath
                if oldFileId and oldContents:
                    unified = fixeddifflib.unified_diff(
                                 oldContents.get().readlines(),
                                 newContents.get().readlines())
                else:
                    unified = fixeddifflib.unified_diff([],
                                 newContents.get().readlines())
                # skip ---/+++ lines
                next(unified)
                next(unified)
                for x in unified:
                    yield x
            else:
                if diffBinaries:
                    yield "GIT binary patch\n"
                    yield "literal %d\n" % newFileObj.contents.size()
                    for x in base85.iterencode(newContents.get(),
                                               compress = True):
                        yield x
                    yield '\n'
                else:
                    yield "Binary files differ\n"

    def gitDiff(self, troveSource, diffBinaries=True):
        """
        Represent the file changes as a GIT diff. Normal files and symlinks
        are represented; other file types (including directories) are
        excluded. Config files are encoded as normal diffs, other files
        are encoded using standard base85 gzipped binary encoding. No trove
        information is included, though removed files are represented properly.

        @param diffBinaries: Include base64 differences of binary files
        @type diffBinaries: bool
        """
        jobs = list(self.getJobSet())
        oldTroves = troveSource.getTroves(
            [ (x[0], x[1][0], x[1][1]) for x in jobs if x[1][0] is not None ])

        # get the old file objects we need
        filesNeeded = []
        for job in jobs:
            if job[1][0] is not None:
                oldTrv = oldTroves.pop(0)
            else:
                oldTrv = None

            if self.hasNewTrove(job[0], job[2][0], job[2][1]):
                trvCs = self.getNewTroveVersion(job[0], job[2][0], job[2][1])

                # look at the changed files and get a list of file objects
                # we need to have available
                for (pathId, path, fileId, fileVersion) in \
                                            trvCs.getChangedFileList():
                    oldPath = oldTrv.getFile(pathId)[0]
                    if fileVersion:
                        filesNeeded.append(
                            (pathId, ) + oldTrv.getFile(pathId)[1:3] + (oldPath, ))

                for pathId in trvCs.getOldFileList():
                    oldPath = oldTrv.getFile(pathId)[0]
                    filesNeeded.append((pathId, ) +
                                    oldTrv.getFile(pathId)[1:3] +
                                    (oldPath, ))
            else:
                filesNeeded.extend((pathId, fileId, version, path)
                    for pathId, path, fileId, version in oldTrv.iterFileList())

        fileObjects = troveSource.getFileVersions(
                            [ x[0:3] for x in filesNeeded ])

        # now look at all of the files, new and old, to order the diff right
        # so we don't have to go seeking all over the changeset
        configList = []
        normalList = []
        removeList = []
        encapsulatedList = []
        for job in jobs:
            if self.hasNewTrove(job[0], job[2][0], job[2][1]):
                trvCs = self.getNewTroveVersion(job[0], job[2][0], job[2][1])
                for (pathId, path, fileId, fileVersion) in \
                                            trvCs.getNewFileList():
                    fileStream = self.getFileChange(None, fileId)
                    if trvCs.hasCapsule():
                        encapsulatedList.append((pathId, fileId,
                            (None, None, None, None),
                            (path, fileId, fileStream)))
                    elif files.frozenFileFlags(fileStream).isConfig():
                        configList.append((pathId, fileId,
                                        (None, None, None, None),
                                        (path, fileId, fileStream)))
                    else:
                        normalList.append((pathId, fileId,
                                        (None, None, None, None),
                                        (path, fileId, fileStream)))

                for (pathId, path, fileId, fileVersion) in \
                                            trvCs.getChangedFileList():
                    oldFileObj = fileObjects.pop(0)
                    fileObj = oldFileObj.copy()
                    oldFileId, oldFileVersion, oldPath = filesNeeded.pop(0)[1:4]
                    diff = self.getFileChange(oldFileId, fileId)
                    # check if new and old files are of the same type
                    if fileObj.lsTag == diff[1]:
                        fileObj.twm(diff, fileObj)
                    else:
                        fileObj = troveSource.getFileVersion(
                            pathId, fileId, fileVersion)

                    if path is None:
                        path = oldPath

                    if trvCs.hasCapsule():
                        encapsulatedList.append((pathId, fileId,
                            (oldPath, oldFileId, oldFileVersion, oldFileObj),
                            (path, fileId, fileObj.freeze())))
                    elif fileObj.flags.isConfig():
                        configList.append((pathId, fileId,
                                        (oldPath, oldFileId, oldFileVersion,
                                        oldFileObj),
                                        (path, fileId, fileObj.freeze())))
                    else:
                        normalList.append((pathId, fileId,
                                        (oldPath, oldFileId, oldFileVersion,
                                        oldFileObj),
                                        (path, fileId, fileObj.freeze())))

                for pathId in trvCs.getOldFileList():
                    oldFileObj = fileObjects.pop(0)
                    oldFileId, oldFileVersion, oldPath = filesNeeded.pop(0)[1:4]
                    removeList.append((oldPath, oldFileObj))
            else:
                for (pathId, fileId, version, path), fileObj in \
                        zip(filesNeeded, fileObjects):
                    removeList.append((path, fileObj))

        for path, fileObj in removeList:
            yield "diff --git a%s b%s\n" % (path, path)
            yield "deleted file mode %o\n" % (fileObj.statType |
                                              fileObj.inode.perms())
            yield "Binary files %s and /dev/null differ\n" % path

        configList.sort()
        normalList.sort()
        encapsulatedList.sort()

        for (pathId, fileId, oldInfo, newInfo) in \
                itertools.chain(configList, normalList):
            newInfo = newInfo[0:2] + (files.ThawFile(newInfo[2], pathId),)
            for x in self._makeFileGitDiff(troveSource, pathId,
                        oldInfo, newInfo, diffBinaries):
                yield x

        for (pathId, fileId, oldInfo, newInfo) in encapsulatedList:
            newInfo = newInfo[0:2] + (files.ThawFile(newInfo[2], pathId),)
            for x in self._makeFileGitDiffCapsule(troveSource, pathId,
                    oldInfo, newInfo, diffBinaries):
                yield x


    def __init__(self, data = None):
        streams.StreamSet.__init__(self, data)
        self.configCache = {}
        self.fileContents = {}
        self.absolute = False
        self.local = 0


class ChangeSetFromAbsoluteChangeSet(ChangeSet):

    #streamDict = ChangeSet.streamDict

    def __init__(self, absCS):
        self.absCS = absCS
        ChangeSet.__init__(self)

class ChangeSetKeyConflictError(Exception):

    name = "ChangeSetKeyConflictError"

    def __init__(self, key, trove1=None, file1=None, trove2=None, file2=None):
        if len(key) == 16:
            self.pathId = key
            self.fileId = None
        else:
            self.pathId, self.fileId = parseKey(key)

        self.trove1 = trove1
        self.file1 = file1
        self.trove2 = trove2
        self.file2 = file2

    def getKey(self):
        if self.fileId:
            return self.pathId + self.fileId
        else:
            return self.pathId

    def getPathId(self):
        return self.pathId

    def getConflicts(self):
        return (self.trove1, self.file1), (self.trove2, self.file2)

    def getTroves(self):
        return self.trove1, self.trove2

    def getPaths(self):
        return self.file1[1], self.file2[1]

    def __str__(self):
        if self.trove1 is None:
            return '%s: %s,%s' % (self.name,
                                  sha1helper.md5ToString(self.pathId),
                                  sha1helper.sha1ToString(self.fileId))
        else:
            path1, path2 = self.getPaths()
            trove1, trove2 = self.getTroves()
            v1 = trove1.getNewVersion().trailingRevision()
            v2 = trove2.getNewVersion().trailingRevision()
            trove1Info = '(%s %s)' % (trove1.getName(), v1)
            trove2Info = '(%s %s)' % (trove2.getName(), v2)
            if path1:
                trove1Info = path1 + ' ' + trove1Info
            if path2:
                trove2Info = path2 + ' ' + trove2Info

            return (('%s:\n'
                     '  %s\n'
                     '     conflicts with\n'
                     '  %s') % (self.name, trove1Info, trove2Info))

class PathIdsConflictError(ChangeSetKeyConflictError):

    name = "PathIdsConflictError"

    def __str__(self):
        if self.trove1 is None:
            return '%s: %s' % (self.name, sha1helper.md5ToString(self.pathId))
        else:
            return ChangeSetKeyConflictError.__str__(self)

class ReadOnlyChangeSet(ChangeSet):

    def thawFromFile(self, f):
        while True:
            s = f.read(5)
            if not s:
                break

            tag, size = struct.unpack("!BI", s)
            size &= ~(1 << 31)
            if tag not in self.streamDict:
                # this implements ignoreUnknown = True
                f.read(size)
                continue

            obj = getattr(self, self.streamDict[tag][2])

            if tag == _STREAM_CS_TROVES:
                obj.thawFromFile(f, size)
            else:
                s = f.read(size)
                obj.thaw(s)

    def addFileContents(self, *args, **kw):
        raise NotImplementedError

    def fileQueueCmp(a, b):
        if a[1][0] == "1" and b[1][0] == "0":
            return -1
        elif a[1][0] == "0" and b[1][0] == "1":
            return 1

        if a[0] < b[0]:
            return -1
        elif a[0] == b[0]:
            if len(a[0]) == 16:
                raise PathIdsConflictError(a[0])
            else:
                # this is an actual conflict if one of the files is a diff
                # (other file types conflicts are okay; replacing contents
                # with a ptr is okay, as is the opposite)
                if (a[2:] == ChangedFileTypes.diff[4:] or
                    b[2:] == ChangedFileTypes.diff[4:]):
                    raise ChangeSetKeyConflictError(a[0])
        else:
            return 1

    fileQueueCmp = staticmethod(fileQueueCmp)

    def _nextFile(self):
        if self.lastCsf:
            next = self.lastCsf.getNextFile()
            if next:
                util.tupleListBsearchInsert(self.fileQueue,
                                            next + (self.lastCsf,),
                                            self.fileQueueCmp)
            self.lastCsf = None

        if not self.fileQueue:
            return None

        rc = self.fileQueue[0]
        self.lastCsf = rc[3]
        del self.fileQueue[0]

        return rc

    def getFileContents(self, pathId, fileId, compressed = False):
        name = None
        key = makeKey(pathId, fileId)
        if pathId in self.configCache:
            assert(not compressed)
            name = pathId
            (tag, contents, alreadyCompressed) = self.configCache[pathId]
            cont = contents
        elif key in self.configCache:
            name = key
            (tag, contents, alreadyCompressed) = self.configCache[key]

            cont = contents

            if compressed:
                f = util.BoundedStringIO()
                compressor = util.DeterministicGzipFile(None, "w", fileobj = f)
                util.copyfileobj(cont.get(), compressor)
                compressor.close()
                f.seek(0)
                cont = filecontents.FromFile(f, compressed = True)
        else:
            self.filesRead = True

            rc = self._nextFile()
            while rc:
                name, tagInfo, f, csf = rc
                if not compressed:
                    f = gzip.GzipFile(None, "r", fileobj = f)

                # if we found the key we're looking for, or the pathId
                # we got is a config file, cache or break out of the loop
                # accordingly
                #
                # we check for both the key and the pathId here for backwards
                # compatibility reading old change set formats
                if name == key or name == pathId or tagInfo[0] == '1':
                    tag = 'cft-' + tagInfo.split()[1]
                    cont = filecontents.FromFile(f, compressed = compressed)

                    # we found the one we're looking for, break out
                    if name == key or name == pathId:
                        self.lastCsf = csf
                        break

                rc = self._nextFile()

        if name != key and name != pathId:
            if len(pathId) == 16:
                pathId = sha1helper.md5ToString(pathId)
            raise KeyError('pathId %s is not in the changeset' % pathId)
        else:
            return (tag, cont)

    def makeAbsolute(self, repos):
        """
        Converts this (relative) change set to an abstract change set.  File
        streams and contents are omitted unless the file changed. This is fine
        for changesets being committed, not so hot for changesets which are
        being applied directly to a system. The absolute changeset is returned
        as a new changeset; self is left unchanged.
        """
        assert(not self.absolute)

        absCs = ChangeSet()
        absCs.setPrimaryTroveList(self.getPrimaryTroveList())
        neededFiles = []

        oldTroveList = [ (x.getName(), x.getOldVersion(),
                          x.getOldFlavor()) for x in list(self.newTroves.values()) ]
        oldTroves = repos.getTroves(oldTroveList)

        # for each file find the old fileId for it so we can assemble the
        # proper stream and contents
        for trv, troveCs in zip(oldTroves,
                                           iter(self.newTroves.values())):
            if trv.troveInfo.incomplete():
                raise errors.TroveError('''\
Cannot apply a relative changeset to an incomplete trove.  Please upgrade conary and/or reinstall %s=%s[%s].''' % (trv.getName(), trv.getVersion(),
                                   trv.getFlavor()))
            troveName = troveCs.getName()
            newVersion = troveCs.getNewVersion()
            newFlavor = troveCs.getNewFlavor()
            assert(troveCs.getOldVersion() == trv.getVersion())
            assert(trv.getName() == troveName)

            # XXX this is broken.  makeAbsolute() is only used for
            # committing local changesets, and they can't have new
            # files, so we're OK at the moment.
            for (pathId, path, fileId, version) in troveCs.getNewFileList():
                filecs = self.files[(None, fileId)]
                newFiles.append((None, fileId, filecs))

            for (pathId, path, fileId, version) in troveCs.getChangedFileList():
                (oldPath, oldFileId, oldVersion) = trv.getFile(pathId)
                filecs = self.files[(oldFileId, fileId)]
                neededFiles.append((pathId, oldFileId, fileId, oldVersion,
                                    version, filecs))

            # we've mucked around with this troveCs, it won't pass
            # integrity checks
            trv.applyChangeSet(troveCs, skipIntegrityChecks = True)
            newCs = trv.diff(None, absolute = True)[0]
            absCs.newTrove(newCs)

        fileList = [ (x[0], x[1], x[3]) for x in neededFiles ]
        fileObjs = repos.getFileVersions(fileList)

        # XXX this would be markedly more efficient if we batched up getting
        # file contents
        for ((pathId, oldFileId, newFileId, oldVersion, newVersion, filecs),
                        fileObj) in zip(neededFiles, fileObjs):
            fileObj.twm(filecs, fileObj)
            (absFileCs, hash) = fileChangeSet(pathId, None, fileObj)
            absCs.addFile(None, newFileId, absFileCs)

            if newVersion != oldVersion and fileObj.hasContents:
                # we need the contents as well
                if files.contentsChanged(filecs):
                    if fileObj.flags.isConfig():
                        # config files aren't available compressed
                        (contType, cont) = self.getFileContents(
                                     pathId, newFileId)
                        if contType == ChangedFileTypes.diff:
                            origCont = repos.getFileContents([(oldFileId,
                                                               oldVersion)])[0]
                            diff = cont.get().readlines()
                            oldLines = origCont.get().readlines()
                            (newLines, failures) = patch.patch(oldLines, diff)
                            assert(not failures)
                            fileContents = filecontents.FromString(
                                                            "".join(newLines))
                            absCs.addFileContents(pathId, newFileId,
                                                  ChangedFileTypes.file,
                                                  fileContents, True)
                        else:
                            absCs.addFileContents(pathId, newFileId,
                                                  ChangedFileTypes.file,
                                                  cont, True)
                    else:
                        (contType, cont) = self.getFileContents(pathId,
                                                newFileId, compressed = True)
                        assert(contType == ChangedFileTypes.file)
                        absCs.addFileContents(pathId, newFileId,
                                              ChangedFileTypes.file,
                                              cont, False, compressed = True)
                else:
                    # include the old contents; we might need them for
                    # a distributed branch
                    cont = repos.getFileContents([(oldFileId, oldVersion)])[0]
                    absCs.addFileContents(pathId, newFileId,
                                          ChangedFileTypes.file, cont,
                                          fileObj.flags.isConfig())

        return absCs

    def rootChangeSet(self, db, troveMap):
        """
        Converts this (absolute) change set to a relative change
        set. The second parameter, troveMap, specifies the old trove
        for each trove listed in this change set. It is a dictionary
        mapping (troveName, newVersion, newFlavor) tuples to
        (oldVersion, oldFlavor) pairs. The troveMap may be (None, None)
        if a new install is desired (the trove is switched from absolute
        to relative to nothing in this case). If an entry is missing for
        a trove, that trove is left absolute.

        Rooting can happen multiple times (only once per trove though). To
        allow this, the absolute file streams remain available from this
        changeset for all time; rooting does not remove them.
        """
        # this has an empty source path template, which is only used to
        # construct the eraseFiles list anyway

        # absolute change sets cannot have eraseLists
        #assert(not eraseFiles)

        newFiles = []
        newTroves = []

        for (key, troveCs) in list(self.newTroves.items()):
            troveName = troveCs.getName()
            newVersion = troveCs.getNewVersion()
            newFlavor = troveCs.getNewFlavor()

            if key not in troveMap:
                continue

            assert(not troveCs.getOldVersion())
            assert(troveCs.isAbsolute())

            (oldVersion, oldFlavor) = troveMap[key]

            if not oldVersion:
                # new trove; the Trove.diff() right after this never
                # sets the absolute flag, so the right thing happens
                old = None
            else:
                old = db.getTrove(troveName, oldVersion, oldFlavor,
                                             pristine = True)
            newTrove = trove.Trove(troveCs)

            # we ignore trovesNeeded; it doesn't mean much in this case
            (troveChgSet, filesNeeded, trovesNeeded) = \
                          newTrove.diff(old, absolute = 0)
            newTroves.append(troveChgSet)
            filesNeeded.sort()

            for x in filesNeeded:
                (pathId, oldFileId, oldVersion, newFileId, newVersion) = x
                filecs = self.getFileChange(None, newFileId)

                if not oldVersion:
                    newFiles.append((oldFileId, newFileId, filecs))
                    continue

                fileObj = files.ThawFile(filecs, pathId)
                (oldFile, oldCont) = db.getFileVersion(pathId,
                                oldFileId, oldVersion, withContents = 1)
                (filecs, hash) = fileChangeSet(pathId, oldFile, fileObj)

                newFiles.append((oldFileId, newFileId, filecs))

        # leave the old files in place; we my need those diffs for a
        # trvCs which hasn't been rooted yet
        for tup in newFiles:
            self.addFile(*tup)

        for troveCs in newTroves:
            self.newTrove(troveCs)

        self.absolute = False

    def writeAllContents(self, csf, withReferences = False):
        # diffs go out, then config files, then we whatever contents are left
        assert(not self.filesRead)
        assert(not withReferences)
        self.filesRead = True

        keyList = list(self.configCache.keys())
        keyList.sort()

        # write out the diffs. these are always in the cache
        for key in keyList:
            (tag, contents, compressed) = self.configCache[key]
            if isinstance(contents, str):
                contents = filecontents.FromString(contents)

            if tag == ChangedFileTypes.diff:
                csf.addFile(key, contents, "1 " + tag[4:])

        # Absolute change sets will have other contents which may or may
        # not be cached. For the ones which are cached, turn them into a
        # filecontainer-ish object (using DictAsCsf) which we will step
        # through along with the rest of the file contents. It beats sucking
        # all of this into RAM. We don't bother cleaning up the mess we
        # make in self.fileQueue since you can't write a changeset multiple
        # times anyway.
        allContents = {}
        for key in keyList:
            (tag, contents, compressed) = self.configCache[key]
            if tag != ChangedFileTypes.diff:
                allContents[key] = (tag, contents, False)

        wrapper = DictAsCsf({})
        wrapper.addConfigs(allContents)

        entry = wrapper.getNextFile()
        if entry:
            util.tupleListBsearchInsert(self.fileQueue,
                                        entry + (wrapper,),
                                        self.fileQueueCmp)

        next = self._nextFile()
        correction = 0
        last = None
        while next:
            name, tagInfo, f, otherCsf = next
            if last == name:
                next = self._nextFile()
                continue
            last = name

            if tagInfo[2:] == ChangedFileTypes.refr[4:]:
                entry = f.read()
                sha1, realSize = entry.split(' ')
                realSize = int(realSize)
                correction += realSize - len(entry)
                if realSize >= 0x100000000:
                    # add 4 bytes to store a 64-bit size
                    correction += 4
                f.seek(0)
                contents = filecontents.FromString(entry)
            else:
                contents = filecontents.FromFile(f)

            csf.addFile(name, contents, tagInfo, precompressed = True)
            next = self._nextFile()

        return correction

    def _mergeConfigs(self, otherCs):
        for key, f in otherCs.configCache.items():
            if key not in self.configCache:
                self.configCache[key] = f
            elif len(key) == 16:
                raise PathIdsConflictError(key)
            elif (self.configCache[key][0] == ChangedFileTypes.diff and
                  f[0] == ChangedFileTypes.file):
                # happily replace a diff with proper file contents; we can
                # deal with that everywhere. no reason to conflict.
                self.configCache[key] = f
            elif (self.configCache[key][0] == ChangedFileTypes.file and
                  f[0] == ChangedFileTypes.diff):
                # happily let a file we already found override a diff, same
                # as above
                pass
            elif (self.configCache[key][0] == f[0] and
                     self.configCache[key][1].get().read() ==
                     f[1].get().read()):
                # they're the same anyway; doesn't much matter which we pick
                pass
            elif (self.configCache[key][0] == ChangedFileTypes.diff or
                  f[0] == ChangedFileTypes.diff):
                raise ChangeSetKeyConflictError(key)

    def _mergeReadOnly(self, otherCs):
        assert(not self.lastCsf)
        assert(not otherCs.lastCsf)

        self._mergeConfigs(otherCs)
        self.fileContainers += otherCs.fileContainers
        self.csfWrappers += otherCs.csfWrappers
        for entry in otherCs.fileQueue:
            util.tupleListBsearchInsert(self.fileQueue, entry,
                                        self.fileQueueCmp)

    def _mergeCs(self, otherCs):
        assert(otherCs.__class__ ==  ChangeSet)

        self._mergeConfigs(otherCs)
        wrapper = DictAsCsf(otherCs.fileContents)
        self.csfWrappers.append(wrapper)
        entry = wrapper.getNextFile()
        if entry:
            util.tupleListBsearchInsert(self.fileQueue,
                                        entry + (wrapper,),
                                        self.fileQueueCmp)
    def merge(self, otherCs):
        self.files.update(otherCs.files)

        self.primaryTroveList += otherCs.primaryTroveList
        self.absolute = self.absolute and otherCs.absolute
        self.newTroves.update(otherCs.newTroves)

        # keep the old trove lists unique on merge.  we erase all the
        # entries and extend the existing oldTroves object because it
        # is a streams.ReferencedTroveList, not a regular list
        if otherCs.oldTroves:
            l = list(dict.fromkeys(self.oldTroves + otherCs.oldTroves).keys())
            del self.oldTroves[:]
            self.oldTroves.extend(l)

        err = None
        try:
            if isinstance(otherCs, ReadOnlyChangeSet):
                self._mergeReadOnly(otherCs)
            else:
                self._mergeCs(otherCs)
        except ChangeSetKeyConflictError as err:
            pathId = err.pathId

            # look up the trove and file that caused the pathId
            # conflict.
            troves = set(itertools.chain(self.iterNewTroveList(),
                                         otherCs.iterNewTroveList()))
            conflicts = []
            for myTrove in sorted(troves):
                files = (myTrove.getNewFileList()
                         + myTrove.getChangedFileList())
                conflicts.extend((myTrove, x) for x in files if x[0] == pathId)

            if len(conflicts) >= 2:
                raise err.__class__(err.getKey(),
                                    conflicts[0][0], conflicts[0][1],
                                    conflicts[1][0], conflicts[1][1])
            else:
                raise

    def reset(self):
        for csf in self.fileContainers:
            csf.reset()
            # skip the CONARYCHANGESET
            (name, tagInfo, control) = csf.getNextFile()
            assert(name == "CONARYCHANGESET")

        for csf in self.csfWrappers:
            csf.reset()

        self.fileQueue = []
        for csf in itertools.chain(self.fileContainers, self.csfWrappers):
            # find the first non-config file
            entry = csf.getNextFile()
            while entry:
                if entry[1][0] == '0':
                    break

                entry = csf.getNextFile()

            if entry:
                util.tupleListBsearchInsert(self.fileQueue, entry + (csf,),
                                            self.fileQueueCmp)

        self.filesRead = False

    def iterRegularFileContents(self):
        """
        Yields (sha1, fobj) tuples for each non-config, non-diff, non-pointer
        file content item in the changeset.
        """
        unpack = {}
        for (oldFileId, newFileId), stream in self.files.items():
            if not files.frozenFileHasContents(stream):
                continue
            if files.frozenFileFlags(stream).isEncapsulatedContent():
                continue
            cont = files.frozenFileContentInfo(stream)
            unpack[newFileId] = cont.sha1()

        want_tag = '0 ' + ChangedFileTypes.file[4:]
        while True:
            f = self._nextFile()
            if not f:
                break
            name, tag, fobj, csf = f
            if len(name) != 36 or tag != want_tag:
                continue
            fileId = name[16:]
            sha1 = unpack.get(fileId)
            if not sha1:
                continue
            yield sha1, fobj

    def __init__(self, data = None):
        ChangeSet.__init__(self, data = data)
        self.filesRead = False
        self.csfWrappers = []
        self.fileContainers = []

        self.lastCsf = None
        self.fileQueue = []


class ChangeSetFromFile(ReadOnlyChangeSet):
    @api.publicApi
    def __init__(self, fileName, skipValidate = 1):
        self.fileName = None
        try:
            if type(fileName) is str:
                try:
                    f = util.ExtendedFile(fileName, "r", buffering = False)
                except IOError as err:
                    raise errors.ConaryError(
                                "Error opening changeset '%s': %s" %
                                    (fileName, err.strerror))
                try:
                    csf = filecontainer.FileContainer(f)
                except IOError as err:
                    raise filecontainer.BadContainer(
                                "File %s is not a valid conary changeset: %s" % (fileName, err))
                self.fileName = fileName
            else:
                csf = filecontainer.FileContainer(fileName)
                if hasattr(fileName, 'path'):
                    self.fileName = fileName.path

            (name, tagInfo, control) = csf.getNextFile()
            assert(name == "CONARYCHANGESET")
        except filecontainer.BadContainer:
            raise filecontainer.BadContainer(
                        "File %s is not a valid conary changeset." % fileName)

        control.file.seek(control.start, 0)
        ReadOnlyChangeSet.__init__(self)
        start = gzip.GzipFile(None, 'r', fileobj = control)
        self.thawFromFile(start)

        self.absolute = True
        empty = True
        self.fileContainers = [ csf ]

        for trvCs in self.newTroves.values():
            if not trvCs.isAbsolute():
                self.absolute = False
            empty = False

            old = trvCs.getOldVersion()
            new = trvCs.getNewVersion()

            if (old and old.onLocalLabel()) or new.onLocalLabel():
                self.local = 1

        if empty:
            self.absolute = False

        # load the diff cache
        nextFile = csf.getNextFile()
        while nextFile:
            key, tagInfo, f = nextFile

            (isConfig, tag) = tagInfo.split()
            tag = 'cft-' + tag
            isConfig = isConfig == "1"

            # cache all config files because:
            #   1. diffs are needed both to precompute a job and to store
            #      the new config contents in the database
            #   2. full contents are needed if the config file moves components
            #      and we need to generate a diff and then store that config
            #      file in the database
            # (there are other cases as well)
            if not isConfig:
                break

            cont = filecontents.FromFile(gzip.GzipFile(None, 'r', fileobj = f))
            self.configCache[key] = (tag, cont, False)

            nextFile = csf.getNextFile()

        if nextFile:
            self.fileQueue.append(nextFile + (csf,))


# old may be None
def fileChangeSet(pathId, old, new):
    contentsHash = None

    diff = new.diff(old)

    if old and old.__class__ == new.__class__:
        if isinstance(new, files.RegularFile) and      \
                  isinstance(old, files.RegularFile)   \
                  and ((new.contents.sha1() != old.contents.sha1()) or
                       (not old.flags.isConfig() and new.flags.isConfig())):
            contentsHash = new.contents.sha1()
    elif isinstance(new, files.RegularFile):
            contentsHash = new.contents.sha1()

    return (diff, contentsHash)

def fileContentsUseDiff(oldFile, newFile, mirrorMode = False):
    # Don't use diff's for config files when the autosource flag changes
    # because the client may not have anything around it can apply the diff
    # to.
    return ((not mirrorMode) and
                oldFile and oldFile.flags.isConfig() and
                newFile.flags.isConfig() and
                (oldFile.flags.isAutoSource() == newFile.flags.isAutoSource()) )

def fileContentsDiff(oldFile, oldCont, newFile, newCont, mirrorMode = False):
    if fileContentsUseDiff(oldFile, newFile, mirrorMode = mirrorMode):
        first = oldCont.get().readlines()
        second = newCont.get().readlines()

        if first or second:
            diff = patch.unifiedDiff(first, second, "old", "new")
            next(diff)
            next(diff)
            cont = filecontents.FromString("".join(diff))
            contType = ChangedFileTypes.diff
        else:
            cont = filecontents.FromString("".join(second))
            contType = ChangedFileTypes.file
    else:
        cont = newCont
        contType = ChangedFileTypes.file

    return (contType, cont)

# this creates an absolute changeset
#
# expects a list of (trove, fileMap) tuples
#
def CreateFromFilesystem(troveList):
    cs = ChangeSet()

    for (oldTrv, trv, fileMap) in troveList:
        (troveChgSet, filesNeeded, trovesNeeded) = trv.diff(oldTrv,
                                                            absolute = 1)
        cs.newTrove(troveChgSet)

        for (pathId, oldFileId, oldVersion, newFileId, newVersion) in filesNeeded:
            (file, realPath, filePath) = fileMap[pathId]
            (filecs, hash) = fileChangeSet(pathId, None, file)
            cs.addFile(oldFileId, newFileId, filecs)

            if hash and not file.flags.isEncapsulatedContent():
                cs.addFileContents(pathId, newFileId, ChangedFileTypes.file,
                          filecontents.FromFilesystem(realPath),
                          file.flags.isConfig())

    return cs

class DictAsCsf:
    maxMemSize = 16384

    def getNextFile(self):
        if self.__next__ >= len(self.items):
            return None

        (name, contType, contObj, compressed) = self.items[self.__next__]
        self.next += 1

        if compressed:
            compressedFile = contObj.get()
        else:
            f = contObj.get()
            compressedFile = util.BoundedStringIO(maxMemorySize =
                                                            self.maxMemSize)
            bufSize = 16384

            gzf = util.DeterministicGzipFile('', "wb", fileobj = compressedFile)
            while 1:
                buf = f.read(bufSize)
                if not buf:
                    break
                gzf.write(buf)
            gzf.close()

            compressedFile.seek(0)

        return (name, contType, compressedFile)

    def addConfigs(self, contents):
        # this is like __init__, but it knows things are config files so
        # it tags them with a "1" and puts them at the front
        l = [ (x[0], "1 " + x[1][0][4:], x[1][1], x[1][2])
                        for x in contents.items() ]
        l.sort()
        self.items = l + self.items

    def reset(self):
        self.next = 0

    def __init__(self, contents):
        # convert the dict (which is a changeSet.fileContents object) to a
        # (name, contTag, contObj, compressed) list, where contTag is the same
        # kind of tag we use in csf files "[0|1] [file|diff]"
        self.items = [ (x[0], "0 " + x[1][0][4:], x[1][1], x[1][2]) for x in
                            contents.items() ]
        self.items.sort()
        self.next = 0

def _convertChangeSetV2V1(inFc, outPath):
    assert(inFc.version == filecontainer.FILE_CONTAINER_VERSION_FILEID_IDX)
    outFcObj = util.ExtendedFile(outPath, "w+", buffering = False)
    outFc = filecontainer.FileContainer(outFcObj,
            version = filecontainer.FILE_CONTAINER_VERSION_WITH_REMOVES)

    info = inFc.getNextFile()
    lastPathId = None
    size = 0
    while info:
        key, tag, f = info
        if len(key) == 36:
            # snip off the fileId
            key = key[0:16]

            if key == lastPathId:
                raise PathIdsConflictError(key)

            size -= 20

        if 'ptr' in tag:
            # I'm not worried about this pointing to the wrong file; that
            # can only happen if there are multiple files with the same
            # PathId, which would cause the conflict we test for above
            oldCompressed = f.read()
            old = gzip.GzipFile(None, "r",
                                fileobj = StringIO(oldCompressed)).read()
            new = old[0:16]
            newCompressedF = StringIO()
            util.DeterministicGzipFile(None, "w", fileobj = newCompressedF).write(new)
            newCompressed = newCompressedF.getvalue()
            fc = filecontents.FromString(newCompressed, compressed = True)
            size -= len(oldCompressed) - len(newCompressed)
        else:
            fc = filecontents.FromFile(f)

        outFc.addFile(key, fc, tag, precompressed = True)
        info = inFc.getNextFile()

    outFcObj.close()

    return size

def getNativeChangesetVersion(protocolVersion):
    """Return the native changeset version supported by a client speaking the
    supplied protocol version

    @param protocolVersion: Protocol version that the client negotiated with
    the server
    @type protocolVersion: int
    @rtype: int
    @return: native changeset version for a client speaking the protocol
    version
    """
    # Add more versions as necessary, but do remember to add them to
    # netclient's FILE_CONTAINER_* constants
    if protocolVersion < 38:
        return filecontainer.FILE_CONTAINER_VERSION_NO_REMOVES
    elif protocolVersion < 43:
        return filecontainer.FILE_CONTAINER_VERSION_WITH_REMOVES
    # Add more changeset versions here as the currently newest client is
    # replaced by a newer one
    return filecontainer.FILE_CONTAINER_VERSION_FILEID_IDX

class AbstractChangesetExploder:

    def __init__(self, cs):
        ptrMap = {}

        fileList = []
        linkGroups = {}
        linkGroupFirstPath = {}
        rpmCapsules = []
        self.rpmFileObj = {}

        # sort the files by pathId,fileId
        for trvCs in cs.iterNewTroveList():
            assert(not trvCs.getOldVersion())
            trv = trove.Trove(trvCs)
            self.installingTrove(trv)
            for pathId, path, fileId, version in trv.iterFileList(
                                capsules = True, members = True):
                if pathId != trove.CAPSULE_PATHID:
                    fileList.append((pathId, fileId, path, trv))
                elif (trv.troveInfo.capsule.type() ==
                            trove._TROVECAPSULE_TYPE_RPM):
                    rpmCapsules.append(fileId)
                else:
                    raise KeyError('cannot expand capsule type %s' %
                                        trv.troveInfo.capsule.type())

        fileList.sort()
        restoreList = []
        self.fileObjMap = {}
        for pathId, fileId, path, trv in fileList:
            fileCs = cs.getFileChange(None, fileId)
            if fileCs is None:
                self.fileMissing(trv, pathId, fileId, path)
                continue

            hasCapsule = trv.troveInfo.capsule.type() and True

            fileObj = files.ThawFile(fileCs, pathId)
            self.fileObjMap[path] = fileObj

            # installFile can control installation of nonderived contents
            # only
            if not trove.conaryContents(hasCapsule, pathId, fileObj):
                continue

            destDir = self.installFile(trv, path, fileObj)
            if not destDir:
                continue

            if fileObj.hasContents:
                restoreList.append((pathId, fileId, fileObj, destDir, path,
                                    trv))
            else:
                self.restoreFile(trv, fileObj, None, destDir, path)

        rpmCapsules.sort()
        for fileId in rpmCapsules:
            (contentType, contents) = cs.getFileContents(trove.CAPSULE_PATHID,
                                                         fileId)
            rpmFileObj = contents.get()
            self.rpmFileObj[fileId] = rpmFileObj
            cpioFileObj = rpmhelper.UncompressedRpmPayload(rpmFileObj)
            exploder = cpiostream.CpioExploder(cpioFileObj)
            exploder.explode(self.destDir)

        delayedRestores = {}
        for pathId, fileId, fileObj, destDir, destPath, trv in restoreList:
            (contentType, contents) = cs.getFileContents(pathId, fileId,
                                                         compressed = True)
            if contentType == ChangedFileTypes.ptr:
                targetPtrId = contents.get().read()
                targetPtrId = util.decompressString(targetPtrId)
                l = delayedRestores.setdefault(targetPtrId, [])
                l.append((fileObj, destDir, destPath))
                continue

            assert(contentType == ChangedFileTypes.file)

            ptrId = pathId + fileId
            if pathId in delayedRestores:
                ptrMap[pathId] = destPath
            elif ptrId in delayedRestores:
                ptrMap[ptrId] = destPath

            self.restoreFile(trv, fileObj, contents, destDir, destPath)

            linkGroup = fileObj.linkGroup()
            if linkGroup:
                linkGroups[linkGroup] = destPath

            for fileObj, targetDestDir, targetPath in \
                                            delayedRestores.get(ptrId, []):
                linkGroup = fileObj.linkGroup()
                if linkGroup in linkGroups:
                    self.restoreLink(trv, fileObj, targetDestDir,
                                     linkGroups[linkGroup], targetPath)
                else:
                    self.restoreFile(trv, fileObj, contents, targetDestDir,
                                     targetPath)

                    if linkGroup:
                        linkGroups[linkGroup] = targetPath

    def installingTrove(self, trv):
        pass

    def restoreFile(self, trv, fileObj, contents, destdir, path):
        fileObj.restore(contents, destdir, destdir + path)

    def restoreLink(self, trv, fileObj, destdir, sourcePath, targetPath):
        util.createLink(destdir + sourcePath, destdir + targetPath)

    def installFile(self, trv, path, fileObj):
        raise NotImplementedException

    def fileMissing(self, trv, pathId, fileId, path):
        raise KeyError(pathId + fileId)

class ChangesetExploder(AbstractChangesetExploder):

    def __init__(self, cs, destDir):
        self.destDir = destDir
        AbstractChangesetExploder.__init__(self, cs)

    def installFile(self, trv, path, fileObj):
        return self.destDir
