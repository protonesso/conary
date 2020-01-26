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


"""
CONARY state files - stores directory-specific context and source trove info
for a particular directory
"""
import copy
import itertools
import os

from conary import errors, trove
from conary.deps import deps
from conary.lib import sha1helper
from conary import versions

class FileInfo(object):

    __slots__ = ( 'isConfig', 'refresh', 'isAutoSource' )

    # container for the extra information we keep on files for SourceStates
    # this has no access methods; it is meant to be accessed directly

    def __str__(self):
        def one(name, val):
            if val:
                return name

            return None

        l = [ x for x in [ one('config', self.isConfig),
                            one('refresh', self.refresh),
                            one('autosource', self.isAutoSource) ]
              if x is not None ]

        if l:
            return '/'.join(l)
        else:
            return '_'

    def __init__(self, isConfig = False, isAutoSource = False,
                 refresh = False, str = ""):
        self.isConfig = isConfig
        self.isAutoSource = isAutoSource
        self.refresh = refresh

        if not str or str == '_':
            return

        l = str.split("/")
        for item in l:
            if item == 'config':
                self.isConfig = True
            elif item == 'refresh':
                self.refresh = True
            elif item == 'autosource':
                self.isAutoSource = True

class ConaryState:

    stateVersion = 2
    __developer_api__ = True

    def __init__(self, context=None, source=None):
        self.context = context
        self.source = source

    def write(self, filename):
        f = open(filename, "w")
        self._write(f)
        if self.hasSourceState():
            self.source._write(f)

    def _write(self, f):
        f.write("stateversion %d\n" % self.stateVersion)
        if self.getContext():
            f.write("context %s\n" % self.getContext())

    def hasContext(self):
        return bool(self.context )

    def getContext(self):
        return self.context

    def setContext(self, name):
        self.context = name

    def getSourceState(self):
        if not self.source:
            raise ConaryStateError('No source state defined in CONARY')
        return self.source

    def setSourceState(self, sourceState):
        self.source = sourceState

    def hasSourceState(self):
        return bool(self.source)

    def copy(self):
        if self.hasSourceState():
            sourceState = self.getSourceState().copy()
        else:
            sourceState = None
        return ConaryState(self.context, sourceState)

class SourceState(trove.Trove):

    __slots__ = [ "branch", "pathMap", "lastMerged", "fileInfo" ]
    __developer_api__ = True

    def setPathMap(self, map):
        self.pathMap = map

    def removeFile(self, pathId):
        trove.Trove.removeFile(self, pathId)
        del self.fileInfo[pathId]

    def addFile(self, pathId, path, version, fileId, isConfig,
                isAutoSource):
        trove.Trove.addFile(self, pathId, path, version, fileId)
        self.fileInfo[pathId] = FileInfo(isConfig = isConfig,
                                         isAutoSource = isAutoSource)

    def removeFilePath(self, file):
        for (pathId, path, fileId, version) in self.iterFileList():
            if path == file:
                self.removeFile(pathId)
                return True

        return False

    def _write(self, f):
        """
        Returns a string representing file information for this trove
        trove, which can later be read by the read() method. This is
        only used to create the Conary control file when dealing with
        :source component checkins, so things like trove dependency
        information is not needed.  The format of the string is:

        name <name>
        version <version>
        branch <branch>
        (lastmerged <version>)?
        (factory <name>)?
        <file count>
        PATHID1 PATH1 FILEID1 ISCONFIG1 REFRESH1 VERSION1
        PATHID2 PATH2 FILEID2 ISCONFIG2 REFRESH2 VERSION2
        .
        .
        .
        PATHIDn PATHn FILEIDn ISCONFIGn REFRESHn VERSIONn
        """
        assert(len(self.strongTroves) == 0)
        assert(len(self.weakTroves) == 0)

        f.write("name %s\n" % self.getName())
        f.write("version %s\n" % self.getVersion().freeze())
        f.write("branch %s\n" % self.getBranch().freeze())
        if self.getLastMerged() is not None:
            f.write("lastmerged %s\n" % self.getLastMerged().freeze())
        if self.getFactory():
            f.write("factory %s\n" % self.getFactory())

        rc = []
        rc.append("%d\n" % (len(list(self.iterFileList()))))

        rc += [ "%s %s %s %s %s\n" % (sha1helper.md5ToString(x[0]),
                                x[1],
                                sha1helper.sha1ToString(x[2]),
                                self.fileInfo[x[0]],
                                x[3].asString())
                for x in sorted(self.iterFileList()) ]

        f.write("".join(rc))


    def changeBranch(self, branch):
        self.branch = branch

    def getBranch(self):
        return self.branch

    def setLastMerged(self, ver = None):
        self.lastMerged = ver

    def getLastMerged(self):
        return self.lastMerged

    def getRecipeFileName(self):
        # XXX this is not the correct way to solve this problem
        # assumes a fully qualified trove name
        name = self.getName().split(':')[0]
        return os.path.join(os.getcwd(), name + '.recipe')

    def expandVersionStr(self, versionStr):
        if versionStr[0] == "@":
            # get the name of the repository from the current branch
            repName = self.getVersion().getHost()
            return repName + versionStr
        elif versionStr[0] != "/" and versionStr.find("@") == -1:
            # non fully-qualified version; make it relative to the current
            # label
            return str(self.getVersion().trailingLabel()) + "/" + versionStr

        return versionStr

    def copy(self, classOverride = None):
        new = trove.Trove.copy(self, classOverride = classOverride)
        new.branch = self.branch.copy()
        new.pathMap = copy.copy(self.pathMap)
        new.fileInfo = copy.copy(self.fileInfo)
        if self.lastMerged:
            new.lastMerged = self.lastMerged.copy()
        else:
            new.lastMerged = None
        return new

    def fileIsConfig(self, pathId, set = None):
        if set is None:
            return self.fileInfo[pathId].isConfig
        self.fileInfo[pathId].isConfig = set

    def fileIsAutoSource(self, pathId, set = None):
        if set is None:
            return self.fileInfo[pathId].isAutoSource
        # not not here makes this a boolean
        self.fileInfo[pathId].isAutoSource = not not set

    def fileNeedsRefresh(self, pathId, set = None):
        if set is None:
            return self.fileInfo[pathId].refresh
        self.fileInfo[pathId].refresh = set

    def getFileRefreshList(self):
        refreshPatterns = []
        for pathId, path, fileId, version in self.iterFileList():
            if self.fileNeedsRefresh(pathId):
                refreshPatterns.append(path)
        return refreshPatterns

    def __init__(self, name, version, branch, changeLog = None,
                 lastmerged = None, troveType = 0, **kw):
        assert(not changeLog)
        assert(troveType == trove.TROVE_TYPE_NORMAL)

        factory = kw.pop('factory', None)

        trove.Trove.__init__(self, name, version, deps.Flavor(),
                             None, **kw)
        if factory:
            self.setFactory(factory)

        self.branch = branch
        self.pathMap = {}
        self.lastMerged = lastmerged
        self.fileInfo = {}

class ConaryStateFromFile(ConaryState):

    __developer_api__ = True

    def parseFile(self, filename, repos=None, parseSource=True):
        f = open(filename)
        lines = f.readlines()

        stateVersion = 0
        if lines[0].startswith('stateversion '):
            stateVersion = int(lines[0].split(None, 1)[1].strip())
            lines.pop(0)

        if stateVersion > self.stateVersion:
            raise ConaryStateError(
                "Cannot read version %d of CONARY state file. Please "
                 "upgrade your conary." % stateVersion)

        contextList = [ x for x in lines if x.startswith('context ') ]
        if contextList:
            contextLine = contextList[-1]
            self.context = contextLine.split(None, 1)[1].strip()
            lines = [ x for x in lines if not x.startswith('context ')]
        else:
            self.context = None

        if lines and parseSource:
            try:
                self.source = SourceStateFromLines(lines, stateVersion,
                                                   repos=repos)
            except ConaryStateError as err:
                raise ConaryStateError('Cannot parse state file %s: %s' % (filename, err))
            if stateVersion != self.stateVersion:
                # update this state w/ the new information
                return True
        else:
            self.source = None
        return False

    def __init__(self, path, repos=None, parseSource=True):
        if not os.path.exists(path):
            raise CONARYFileMissing
        if 'CONARY' not in os.listdir(os.path.dirname(os.path.abspath(path))):
            # Must test for exact case otherwise files/directories called
            # 'conary' will be interpreted as state files
            raise CONARYFileMissing
        if not os.path.isfile(path):
            raise CONARYNotFile

        versionUpdated = self.parseFile(path, repos=repos,
                                        parseSource=parseSource)
        if versionUpdated and os.access(path, os.W_OK):
            self.write(path)

class SourceStateFromLines(SourceState):

    # name : (isVersion, required)
    fields = { 'name'       : (False, True ),
               'branch'     : (True,  True ),
               'lastmerged' : (True,  False),
               'factory'    : (False, False),
               'version'    : (True,  True ) }

    def _readFileList(self, lines, stateVersion, repos):
        configFlagNeeded = []
        autoSourceFlagNeeded = []

        for line in lines[1:]:
            # chop
            line = line[:-1]
            fields = line.split()
            pathId = sha1helper.md5FromString(fields.pop(0))
            version = versions.VersionFromString(fields.pop(-1))

            isConfig = False
            refresh = False

            if stateVersion >= 2:
                info = FileInfo(str = fields.pop())
            elif stateVersion == 1:
                refresh = int(fields.pop(-1))
                isConfig = int(fields.pop(-1))
                info = FileInfo(refresh = refresh, isConfig = isConfig)
            elif stateVersion == 0:
                info = FileInfo()

            fileId = sha1helper.sha1FromString(fields.pop(-1))

            if stateVersion == 0:
                if not isinstance(version, versions.NewVersion):
                    configFlagNeeded.append((pathId, fileId, version))
                    autoSourceFlagNeeded.append((pathId, fileId, version))
            elif stateVersion == 1:
                if not isinstance(version, versions.NewVersion):
                    autoSourceFlagNeeded.append((pathId, fileId, version))

            path = " ".join(fields)

            self.addFile(pathId, path, version, fileId,
                         isConfig = info.isConfig,
                         isAutoSource = info.isAutoSource)
            self.fileNeedsRefresh(pathId, set = info.refresh)

        if configFlagNeeded:
            if not repos:
                raise ConaryStateError('CONARY file has version %s, but this application cannot convert - please run a cvc command, e.g. cvc diff, to convert.' % stateVersion)
            assert(stateVersion == 0)
            fileObjs = repos.getFileVersions(configFlagNeeded)
            for (pathId, fileId, version), fileObj in \
                            zip(configFlagNeeded, fileObjs):
                self.fileIsConfig(pathId, set = fileObj.flags.isConfig())

        if autoSourceFlagNeeded:
            if not repos:
                raise ConaryStateError('CONARY file has version %s, but this application cannot convert - please run a cvc command, e.g. cvc diff, to convert.' % stateVersion)
            assert(stateVersion < 2)
            fileObjs = repos.getFileVersions(autoSourceFlagNeeded)
            for (pathId, fileId, version), fileObj in \
                            zip(autoSourceFlagNeeded, fileObjs):
                self.fileIsAutoSource(pathId,
                                      set = fileObj.flags.isAutoSource())

    def parseLines(self, lines, stateVersion, repos):
        kwargs = {}

        while lines:
            fields = lines[0][:-1].split()

            # the file count ends the list of fields
            if len(fields) == 1: break
            assert(len(fields) == 2)
            del lines[0]

            what = fields[0]
            assert(what not in kwargs)
            if what not in self.fields:
                raise ConaryStateError('Invalid field "%s"' % what)

            isVer = self.fields[what][0]

            if isVer:
                kwargs[what] = versions.ThawVersion(fields[1])
            else:
                kwargs[what] = fields[1]

        required = set([ x[0] for x in list(self.fields.items()) if x[1][1] ])
        assert((set(kwargs.keys()) & required) == required)

        SourceState.__init__(self, **kwargs)

        self._readFileList(lines, stateVersion, repos)

    def __init__(self, lines, stateVersion, repos=None):
        self.parseLines(lines, stateVersion, repos )

    def copy(self):
        return SourceState.copy(self, classOverride = SourceState)

class ConaryStateError(errors.ConaryError):
    pass

class CONARYFileMissing(ConaryStateError):
    """
    This exception is raised when the CONARY file specified does not
    exist
    """
    def __str__(self):
        return 'CONARY state file does not exist.'

class CONARYNotFile(ConaryStateError):
    """
    This exception is raised when the CONARY file specified exists but
    is not a regular file
    """
    def __str__(self):
        return 'CONARY state file is not a normal file'
