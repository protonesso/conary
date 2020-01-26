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
Implements the file handling for the /etc/conary/system-model file,
which is written in CML.
"""

import os
import stat
import tempfile

from conary.lib import util

class SystemModelFile(object):
    '''
    Implements file manipulation of a system model file.  This includes
    snapshot files, which are used to store the target state while the
    system is in transition.
    '''

    def __init__(self, model, fileName=None, snapshotExt='.next'):
        """
        @param model: A CML object that implements a serialized
        model.
        @param fileName: (optional) name of file to write to (defaults
        to C{model.cfg.modelPath})
        @param snapshotExt: (optional) extension to use when writing
        a snapshot
        """
        if fileName is None:
            fileName = model.cfg.modelPath
        self.fileName = fileName
        self.snapName = fileName + snapshotExt
        self.mtime = None
        self.root = model.cfg.root
        self.model = model

        self.fileFullName = self.root+fileName
        self.snapFullName = self.fileFullName + snapshotExt

        if self.exists():
            self.parse()

    @property
    def contents(self):
        return ''.join(self.model.filedata)

    def snapshotExists(self):
        return util.exists(self.snapFullName)

    def exists(self):
        return util.exists(self.fileFullName)

    def read(self, fileName=None):
        if fileName is None:
            if self.snapshotExists():
                fileName = self.snapFullName
            else:
                fileName = self.fileFullName
        fobj = open(fileName, 'r')
        self.mtime = os.fstat(fobj.fileno()).st_mtime
        self.model.filedata = fobj.readlines()
        fobj.close()
        return self.model.filedata, fileName

    def parse(self, fileName=None, fileData=None):
        if fileData is None:
            fileData, _ = self.read(fileName=fileName)
        else:
            fileName = None
            self.model.filedata = fileData
        if fileName is None:
            fileName = self.fileName
        self.model.parse(fileData=self.model.filedata,
                         context=fileName)

    def write(self, fileName=None):
        '''
        Writes the current system model to the specified file (relative
        to the configured root), or overwrites the previously-specified
        file if no filename is provided.
        @param fileName: (optional) name of file to which to write the model
        @type fileName: string
        '''
        if fileName == None:
            fileName = self.fileName
        fileFullName = self.model.cfg.root+fileName
        if util.exists(fileFullName):
            fileMode = stat.S_IMODE(os.stat(fileFullName)[stat.ST_MODE])
        else:
            fileMode = 0o644

        dirName = os.path.dirname(fileFullName)
        fd, tmpName = tempfile.mkstemp(prefix='system-model', dir=dirName)
        f = os.fdopen(fd, 'w')
        self.model.write(f)
        os.chmod(tmpName, fileMode)
        os.rename(tmpName, fileFullName)

    def writeSnapshot(self):
        '''
        Write the current state of the model to the snapshot file
        '''
        self.write(fileName=self.snapName)

    def closeSnapshot(self):
        '''
        Indicate that a model has been fully applied to the system by
        renaming the snapshot, if it exists, over the previous model file.
        '''
        if self.snapshotExists():
            os.rename(self.snapFullName, self.fileFullName)

    def deleteSnapshot(self):
        '''
        Remove any snapshot without applying it to a system; normally
        as part of rolling back a partially-applied update.
        '''
        if self.snapshotExists():
            os.unlink(self.snapFullName)
