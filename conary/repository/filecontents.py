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


try:
    from io import StringIO
except ImportError:
    from io import StringIO

import errno, os

from conary.lib import util

SEEK_SET=-1
SEEK_CUR=1
SEEK_END=2

class FileContents(object):

    __slots__ = ( "compressed" )

    def copy(self):
        raise NotImplementedError

    def get(self):
        raise NotImplementedError

    def isCompressed(self):
        return self.compressed

    def __init__(self):
        self.compressed = False
        if self.__class__ == FileContents:
            raise NotImplementedError

class FromDataStore(FileContents):

    __slots__ = ( "store", "sha1" )

    def copy(self):
        return self.__class__(self.store, self.sha1)

    def get(self):
        return self.store.openFile(self.sha1)

    def path(self):
        return self.store.hashToPath(self.sha1)

    def __init__(self, store, sha1):
        self.compressed = False
        self.store = store
        self.sha1 = sha1

class CompressedFromDataStore(FileContents):

    __slots__ = ( "store", "sha1" )

    def getSha1(self):
        return self.sha1

    def copy(self):
        return self.__class__(self.store, self.sha1)

    def get(self):
        return self.store.openRawFile(self.sha1)

    def path(self):
        return self.store.hashToPath(self.sha1)

    def __init__(self, store, sha1):
        self.store = store
        self.sha1 = sha1
        self.compressed = True

class FromFilesystem(FileContents):

    __slots__ = ( "path" )

    def get(self):
        try:
            return open(self.path, "r")
        except IOError as e:
            if e.errno != errno.EACCES:
                raise

        mode = os.stat(self.path).st_mode & 0o777
        os.chmod(self.path, mode | 0o400)
        try:
            f = open(self.path, "r")
        finally:
            util.fchmod(f.fileno(), mode)

        return f

    def __init__(self, path, compressed = False):
        self.path = path
        self.compressed = compressed

class FromChangeSet(FileContents):

    __slots__ = ( "cs", "pathId", "fileId" )

    def copy(self):
        return self.__class__(self.cs, self.pathId)

    def get(self):
        return self.cs.getFileContents(self.pathId, self.fileId,
                                       compressed = self.compressed)[1].get()

    def __init__(self, cs, pathId, fileId, compressed = False):
        self.cs = cs
        self.pathId = pathId
        self.fileId = fileId
        self.compressed = compressed

class FromString(FileContents):

    __slots__ = "str"

    def copy(self):
        return self.__class__(self.str)

    def get(self):
        return StringIO(self.str)

    def __eq__(self, other):
        if type(other) is str:
            return self.str == str
        if isinstance(other, FromString):
            return self.str == other.str
        return False

    def __init__(self, str, compressed = False):
        self.str = str
        self.compressed = compressed


class FromFile(FileContents):

    __slots__ = [ "f" ]

    def copy(self):
        # XXX dup the file?
        return self.__class__(self.f)

    def get(self):
        self.f.seek(0)
        return self.f

    def __init__(self, f, compressed = False):
        self.f = f
        self.compressed = compressed

class WithFailedHunks(FileContents):

    __slots__ = ( "fc", "hunks" )

    def copy(self):
        return self.__class__(self.fc, self.hunks)

    def get(self):
        return self.fc.get()

    def getHunks(self):
        return self.hunks

    def __init__(self, fc, hunks):
        self.fc = fc
        self.hunks = hunks
        self.compressed = False
