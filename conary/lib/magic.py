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


import os
import re
import stat
import string
import struct
import xml.dom.minidom
import zipfile
import gzip as gzip_module
import zlib
import bz2

from conary import rpmhelper
from conary.lib import debhelper
from conary.lib import elf
from conary.lib import javadeps
from conary.lib import util

MSI_MAGIC_STRINGS = (
    ("\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
     "\x00\x00\x00\x00\x00\x00\x3E\x00\x03\x00\xFE\xFF\x09\x00\x06"),
    ("\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
     "\x00\x00\x00\x00\x00\x00\x3e\x00\x04\x00\xfe\xff\x0c\x00\x06"),
)

WIM_MAGIC_STRING = "MSWIM\0\0"


class Magic(object):
    __slots__ = ['path', 'basedir', 'contents', 'name']
    # The file type is a generic string for a specific file type
    def __init__(self, path, basedir):
        self.path = path
        self.basedir = basedir
        if not hasattr(self, 'contents'):
            self.contents = {}
        self.name = self.__class__.__name__


class ELF(Magic):
    def __init__(self, path, basedir='', buffer=''):
        Magic.__init__(self, path, basedir)
        fullpath = basedir+path
        self.contents['stripped'] = elf.stripped(fullpath)
        if self.__class__ is ELF:
            # ar doesn't deal with hasDebug or RPATH
            try:
                self.contents['hasDebug'] = elf.hasDebug(fullpath)
            except elf.error: pass
            try:
                self.contents['RPATH'] = elf.getRPATH(fullpath)
            except elf.error: pass
            try:
                self.contents['Type'] = elf.getType(fullpath)
            except elf.error: pass
        try:
            requires, provides = elf.inspect(fullpath)
            # Filter None abi flags
            requires = [ x for x in requires
                         if x[0] != 'abi' or x[2][0] is not None ]
            self.contents['requires'] = requires
            self.contents['provides'] = provides
            for req in requires:
                if req[0] == 'abi':
                    self.contents['abi'] = req[1:]
                    self.contents['isnset'] = req[2][1]
            for prov in provides:
                if prov[0] == 'soname':
                    self.contents['soname'] = prov[1]
        except elf.error: pass

class ar(ELF):
    def __init__(self, path, basedir='', buffer=''):
        ELF.__init__(self, path, basedir)
        # no point in looking for __.SYMDEF because GNU ar always keeps
        # symbol table up to date
        # ar archives, like ELF files, are investigated by our elf module.
        # We do still want to be able to distinguish between them via magic,
        # thus the two classes.

class tar(Magic):
    def __init__(self, path, basedir = '', buffer = ''):
        Magic.__init__(self, path, basedir)
        self.contents['GNU'] = (buffer[257:265] == 'ustar  \0')

class gzip(Magic):
    def __init__(self, path, basedir='', buffer=''):
        Magic.__init__(self, path, basedir)
        if buffer[3] == '\x08':
            self.contents['name'] = _string(buffer[10:])
        if buffer[8] == '\x02':
            self.contents['compression'] = '9'
        else:
            self.contents['compression'] = '1'

class tar_gz(gzip, tar):
    def __init__(self, path, basedir = '', gzipBuffer = '', tarBuffer = ''):
        gzip.__init__(self, path, basedir = basedir, buffer = gzipBuffer)
        tar.__init__(self, path, basedir = basedir, buffer = tarBuffer)

class bzip(Magic):
    def __init__(self, path, basedir='', buffer=''):
        Magic.__init__(self, path, basedir)
        self.contents['compression'] = buffer[3]

class tar_bz2(bzip, tar):
    def __init__(self, path, basedir = '', bzipBuffer = '', tarBuffer = ''):
        bzip.__init__(self, path, basedir = basedir, buffer = bzipBuffer)
        tar.__init__(self, path, basedir = basedir, buffer = tarBuffer)

class xz(Magic):
    def __init__(self, path, basedir='', buffer=''):
        Magic.__init__(self, path, basedir)

class tar_xz(bzip, tar):
    def __init__(self, path, basedir = '', bzipBuffer = '', tarBuffer = ''):
        bzip.__init__(self, path, basedir = basedir, buffer = bzipBuffer)
        tar.__init__(self, path, basedir = basedir, buffer = tarBuffer)

class lzo(Magic):
    def __init__(self, path, basedir='', lzobuffer=''):
        Magic.__init__(self, path, basedir=basedir)

class changeset(Magic):
    def __init__(self, path, basedir='', buffer=''):
        Magic.__init__(self, path, basedir)

class deb(Magic):
    "Debian package"
    _tagMap = dict([
        (debhelper.NAME, 'name'),
        (debhelper.VERSION, 'version'),
        (debhelper.RELEASE, 'release'),
        (debhelper.SUMMARY, 'summary'),
        (debhelper.DESCRIPTION, 'description'),
    ])
    def __init__(self, path, basedir):
        Magic.__init__(self, path, basedir)
        fullPath = basedir + path
        f = file(fullPath)
        try:
            h = debhelper.DebianPackageHeader(f)
        except (debhelper.Error, debhelper.ar.ArchiveError):
            # Probably the control file was missing; ignore the error, we
            # don't know how to extract metadata
            return

        for dtag, cstr in list(self._tagMap.items()):
            self.contents[cstr] = h[dtag]

class jar(Magic):
    def __init__(self, path, basedir='', zipFileObj = None, fileList = []):
        Magic.__init__(self, path, basedir)
        self.contents['files'] = filesMap = {}
        self.contents['provides'] = set()
        self.contents['requires'] = set()

        if zipFileObj is None:
            return

        try:
            for name in fileList:
                contents = zipFileObj.read(name)
                if not _javaMagic(contents):
                    continue
                prov, req = javadeps.getDeps(contents)
                filesMap[name] = (prov, req)
                if prov:
                    self.contents['provides'].add(prov)
                if req:
                    self.contents['requires'].update(req)
        except (IOError, zipfile.BadZipfile):
            # zipfile raises IOError on some malformed zip files
            pass

class WAR(Magic):
    _xmlMetadataFile = "WEB-INF/web.xml"
    def __init__(self, path, basedir='', zipFileObj = None, fileList = []):
        Magic.__init__(self, path, basedir)
        if zipFileObj is None:
            raise ValueError("Expected a Zip file object")
        # Get the contents of the deployment descriptor
        ddcontent = zipFileObj.read(self._xmlMetadataFile)
        try:
            dom = xml.dom.minidom.parseString(ddcontent)
        except Exception as e:
            # Error parsing the XML, move on
            return
        # Grab data from the DOM
        val = dom.getElementsByTagName('display-name')
        if val:
            self.contents['displayName'] = self._getNodeData(val[0])
        val = dom.getElementsByTagName('description')
        if val:
            self.contents['description'] = self._getNodeData(val[0])
        dom.unlink()

    @staticmethod
    def _getNodeData(node):
        node.normalize()
        if not node.hasChildNodes():
            return ''
        return node.childNodes[0].data

class EAR(WAR):
    _xmlMetadataFile = "META-INF/application.xml"

class ZIP(Magic):
    def __init__(self, path, basedir='', zipFileObj = None, fileList = []):
        Magic.__init__(self, path, basedir)

class java(Magic):
    def __init__(self, path, basedir='', buffer=''):
        Magic.__init__(self, path, basedir)
        fullpath = basedir+path
        prov, req = javadeps.getDeps(file(fullpath).read())
        if prov:
            self.contents['provides'] = set([prov])
        if req:
            self.contents['requires'] = req
        self.contents['files'] = { path : (prov, req) }


class script(Magic):
    interpreterRe = re.compile(r'^#!\s*([^\s]*)')
    lineRe = re.compile(r'^#!\s*(.*)')
    def __init__(self, path, basedir='', buffer=''):
        Magic.__init__(self, path, basedir)
        m = self.interpreterRe.match(buffer)
        self.contents['interpreter'] = m.group(1)
        m = self.lineRe.match(buffer)
        self.contents['line'] = m.group(1)


class ltwrapper(Magic):
    def __init__(self, path, basedir='', buffer=''):
        Magic.__init__(self, path, basedir)


class CIL(Magic):
    def __init__(self, path, basedir='', buffer=''):
        Magic.__init__(self, path, basedir)

class RPM(Magic):
    _tagMap = [
        ("name",    rpmhelper.NAME, str),
        ("version", rpmhelper.VERSION, str),
        ("release", rpmhelper.RELEASE, str),
        ("epoch",   rpmhelper.EPOCH, int),
        ("arch",    rpmhelper.ARCH, str),
        ("summary", rpmhelper.SUMMARY, str),
        ("description", rpmhelper.DESCRIPTION, str),
        ("license", rpmhelper.LICENSE, str),
    ]
    def __init__(self, path, basedir=''):
        Magic.__init__(self, path, basedir)
        try:
            f = file(path)
        except:
            return None
        # Convert list of objects to simple types
        self.hdr = rpmhelper.readHeader(f)
        for key, tagName, valType in self._tagMap:
            val = self.hdr.get(tagName, None)
            if isinstance(val, list):
                if not val:
                    val = None
                else:
                    val = val[0]
            if val is not None:
                if valType == int:
                    val = int(val)
                elif valType == str:
                    val = str(val)
            self.contents[key] = val
        self.contents['isSource'] = self.hdr.isSource

class MSI(Magic):
    _tagMap = [
        ("name",    rpmhelper.NAME, str),
        ("version", rpmhelper.VERSION, str),
    ]
    def __init__(self, path, basedir=''):
        Magic.__init__(self, path, basedir)
        self.version = '(unknown)'
        try:
            open(path)
        except:
            return None
        # Convert list of objects to simple types
        for key, tagName, valType in self._tagMap:
            self.contents[key] = getattr(self,key)


class WIM(Magic):
    def __init__(self, path, basedir=''):
        Magic.__init__(self, path, basedir)
        try:
            open(path)
        except:
            return None

def _javaMagic(b):
    if len(b) > 4 and b[0:4] == "\xCA\xFE\xBA\xBE":
        return True
    return False

def _tarMagic(b):
    return len(b) > 262 and b[257:262] == 'ustar'

def magic(path, basedir=''):
    """
    Returns a magic class with information about the file mentioned
    """
    if basedir and not basedir.endswith('/'):
        basedir += '/'

    n = basedir+path
    if not util.exists(n) or not util.isregular(n):
        return None

    oldmode = None
    mode = os.lstat(n)[stat.ST_MODE]
    if (mode & 0o400) != 0o400:
        oldmode = mode
        os.chmod(n, mode | 0o400)

    f = file(n)
    if oldmode is not None:
        os.chmod(n, oldmode)

    b = f.read(4096)
    f.close()

    if len(b) > 4 and b[0] == '\x7f' and b[1:4] == "ELF":
        return ELF(path, basedir, b)
    elif len(b) > 14 and b[0:14] == '!<arch>\ndebian':
        return deb(path, basedir)
    elif len(b) > 7 and b[0:7] == "!<arch>":
        return ar(path, basedir, b)
    elif len(b) > 2 and b[0] == '\x1f' and b[1] == '\x8b':
        try:
            uncompressedBuffer = gzip_module.GzipFile(n).read(4096)
            if _tarMagic(uncompressedBuffer):
                return tar_gz(path, basedir, b, uncompressedBuffer)
        except (IOError, zlib.error, struct.error):
            # gzip sometimes raises lets through other errors instead
            # of any module-specific errors; in either error case just
            # do not consider this a gzip file.
            # Note that gzip or tar_gz magic does not imply that the
            # entire file has been tested to have no compression errors!
            pass
        return gzip(path, basedir, b)
    elif len(b) > 3 and b[0:3] == "BZh":
        try:
            uncompressedBuffer = bz2.BZ2File(n).read(4096)
            if _tarMagic(uncompressedBuffer):
                return tar_bz2(path, basedir, b, uncompressedBuffer)
        except IOError:
            # bz2 raises IOError instead of any module specific errors
            pass
        return bzip(path, basedir, b)
    elif len(b) > 6 and b[0:6] == "\xFD\x37\x7A\x58\x5A\x00":
        # http://tukaani.org/xz/xz-file-format.txt
        return xz(path, basedir, b)
    elif len(b) > 9 and b[0:9] == "\x89\x4c\x5a\x4f\x00\x0d\x0a\x1a\x0a":
        return lzo(path, basedir, b)
    elif len(b) > 4 and b[0:4] == "\xEA\x3F\x81\xBB":
        return changeset(path, basedir, b)
    elif len(b) > 4 and b[0:4] == "PK\x03\x04":
        # Zip file. Peek inside the file to extract the file list
        try:
            zf = zipfile.ZipFile(n)
            namelist = set(i.filename for i in zf.infolist()
                         if not i.filename.endswith('/') and i.file_size > 0)
        except (IOError, zipfile.BadZipfile):
            # zipfile raises IOError on some malformed zip files
            # We are producing a dummy jar or ZIP with no contents
            if path.endswith('.jar'):
                return jar(path, basedir)
            return ZIP(path, basedir)
        except RuntimeError:
            # not a proper zip archive -- likely a .car archive CNY-2871
            namelist = None
        if namelist is not None:
            if 'META-INF/application.xml' in namelist:
                return EAR(path, basedir, zipFileObj = zf, fileList = namelist)
            elif 'WEB-INF/web.xml' in namelist:
                return WAR(path, basedir, zipFileObj = zf, fileList = namelist)
            elif 'META-INF/MANIFEST.MF' in namelist:
                return jar(path, basedir, zipFileObj = zf, fileList = namelist)
            #elif path.endswith('.par'):
            #    perl archive
            else:
                return ZIP(path, basedir, zipFileObj = zf, fileList = namelist)
    elif _javaMagic(b):
        return java(path, basedir, b)
    elif len(b) > 4 and b[0:2] == "#!":
        if b.find(
            '# This wrapper script should never be moved out of the build directory.\n'
            '# If it is, it will not operate correctly.') > 0:
            return ltwrapper(path, basedir, b)
        return script(path, basedir, _line(b))
    elif (len(b) > 130
          and b[0:2] == 'MZ'
          and b[78:117] == "This program cannot be run in DOS mode."
          and b[128:130] == "PE"):
        # FIXME - this is not sufficient to detect a CIL file this
        # will match all PE executables.  See ECMA-335, partition ii,
        # section 25
        return CIL(path, basedir, b)
    elif (len(b) > 4 and b[:4] == "\xed\xab\xee\xdb"):
        return RPM(path, basedir)
    elif (len(b) > len(MSI_MAGIC_STRINGS[0]) and
            [ x for x in MSI_MAGIC_STRINGS if b[:len(x)] == x ]):
        return MSI(path,basedir)
    elif len(b) > len(WIM_MAGIC_STRING) and \
            b[:len(WIM_MAGIC_STRING)] == WIM_MAGIC_STRING:
        return WIM(path,basedir)
    elif _tarMagic(b):
        return tar(path, basedir, b)

    return None

class magicCache(dict):
    def __init__(self, basedir=''):
        self.basedir = basedir
    def __getitem__(self, name):
        if name not in self:
            self[name] = magic(name, self.basedir)
        return dict.__getitem__(self, name)

# internal helpers

def _string(buffer):
    return buffer[:string.find(buffer, '\0')]

def _line(buffer):
    return buffer[:string.find(buffer, '\n')]
