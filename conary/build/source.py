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
Modules used by recipes to find source code, check GPG signatures on
it, unpack it, and patch it in the correct directory.  Each of the
public classes in this module is accessed from a recipe as addI{Name}.
"""

import itertools
import os
import re
import shutil, subprocess
import shlex
import sys
import tempfile
import stat
import http.client

from conary.lib import debugger, digestlib, log, magic, sha1helper
from conary import rpmhelper
from conary.lib import openpgpfile, util
from conary.build import action, errors, filter
from conary.build.errors import RecipeFileError
from conary.build.manifest import Manifest, ExplicitManifest
from conary.repository import transport

from conary.build.action import TARGET_LINUX
from conary.build.action import TARGET_WINDOWS

class WindowsHelper(object):
    def __init__(self):
        self.path = None
        self.version = None
        self.platform = None
        self.productName = None
        self.productCode = None
        self.upgradeCode = None
        self.components = []
        self.msiArgs = None

    @property
    def flavor(self):
        if self.platform is None:
            return ''
        elif 'x64' in self.platform:
            return 'is: x86_64'
        elif 'x86' in self.platform:
            return 'is: x86'
        else:
            return ''

    def extractMSIInfo(self, path, wbs):
        import robj

        self.fileType = 'msi'
        self.path = path

        # This is here for backwards compatibility.
        if not wbs.startswith('http'):
            wbs = 'http://%s/api' % wbs

        api = robj.connect(wbs)
        api.msis.append(dict(
            path=os.path.split(path)[1],
            size=os.stat(path).st_size,
        ))
        self.resource = api.msis[-1]

        # put the actual file contents
        self.resource.path = open(path)
        self.resource.refresh()

        self.productName = self.resource.name.encode('utf-8')
        name = self.productName.split()
        if len(name) > 1 and '.' in name[-1]:
            name = '-'.join(name[:-1])
        else:
            name = '-'.join(name)
        self.name = name
        self.version = self.resource.version.encode('utf-8')
        self.platform = self.resource.platform.encode('utf-8')
        self.productCode = self.resource.productCode.encode('utf-8')
        self.upgradeCode = self.resource.upgradeCode.encode('utf-8')

        self.components = [ (x.uuid.encode('utf-8'), x.path.encode('utf-8'))
            for x in self.resource.components ]

        # clean up
        try:
            self.resource.delete()
        except http.client.ResponseNotReady:
            pass

    def extractWIMInfo(self, path, wbs, volumeIndex=1):
        self.volumeIndex = volumeIndex

        import robj
        from xobj import xobj

        self.fileType = 'wim'
        self.path = path

        # This is here for backwards compatibility.
        if not wbs.startswith('http'):
            wbs = 'http://%s/api' % wbs

        # create the resource
        api = robj.connect(wbs)
        api.images.append({'createdBy': 'conary-build'})

        try:
            image = api.images[-1]

            # upload the image
            name = os.path.basename(path)
            fobj = open(path, 'rb')
            size = os.fstat(fobj.fileno()).st_size
            image.files.append({'path': name + '.wim',
                                   'type': self.fileType,
                                   'size': size,})
            file_res = image.files[-1]
            data = robj.HTTPData(data=fobj, size=size, chunked=True)
            file_res.path = data

            file_res.refresh()
            self.wimInfoXml = file_res.wimInfo.read()
            self.wimInfo = xobj.parse(self.wimInfoXml)
            self.volumes = {}

            if type(self.wimInfo.WIM.IMAGE) is list:
                for i in self.wimInfo.WIM.IMAGE:
                    if not hasattr(i, 'WINDOWS'):
                        continue
                    info = {}
                    info['name'] = i.NAME.encode('utf-8')
                    info['version'] = "%s.%s.%s" % \
                        (i.WINDOWS.VERSION.MAJOR.encode('utf-8'), \
                         i.WINDOWS.VERSION.MINOR.encode('utf-8'), \
                         i.WINDOWS.VERSION.BUILD.encode('utf-8'))
                    self.volumes[int(i.INDEX)] = info
            else:
                i = self.wimInfo.WIM.IMAGE
                info = {}
                info['name'] = i.NAME.encode('utf-8')
                info['version'] = "%s.%s.%s" % \
                    (i.WINDOWS.VERSION.MAJOR.encode('utf-8'), \
                         i.WINDOWS.VERSION.MINOR.encode('utf-8'), \
                         i.WINDOWS.VERSION.BUILD.encode('utf-8'))
                self.volumes[int(i.INDEX)] = info

            if self.volumeIndex not in self.volumes:
                self.volumeIndex = list(self.volumes.keys())[0]

            self.volume = self.volumes[self.volumeIndex]
            self.name = '-'.join(self.volume['name'].split())

        finally:
            # clean up
            try:
                image.delete()
            except http.client.ResponseNotReady:
                pass

class _AnySource(action.RecipeAction):
    ephemeral = False
    sourceDir = None

    def checkSignature(self, f):
        pass
    # marks classes which have source files which need committing

# This provides the set (and order) in which the suffix is guessed
# Logically, .tar.xz ought to go first because it is smallest, but we
# should wait until it is more prevalent before moving it to the front
# of the list; we should also look for instances of .txz
DEFAULT_SUFFIXES =  ('tar.bz2', 'tar.gz', 'tbz2', 'tgz', 'tar.xz', 'zip')

class _Source(_AnySource):
    keywords = {'rpm': '',
                'dir': '',
                'keyid': None,
                'httpHeaders': {},
                'package': None,
                'sourceDir': None,
                'ephemeral': False,
                }

    supported_targets = (TARGET_LINUX, TARGET_WINDOWS, )

    def __init__(self, recipe, *args, **keywords):
        self.archivePath = None
        sourcename = args[0]
        action.RecipeAction.__init__(self, recipe, *args, **keywords)
        if isinstance(sourcename, (list, tuple)):
            # This adds support for multiple URLs in a source.
            # We stash them in recipe.multiurlMap, keyed on a digest computed
            # over them. We replace the source with a multiurl:// one that
            # includes the digest, and we pass that to the lookaside cache.
            sourcename = [ x % recipe.macros for x in sourcename ]
            # Create a hash of the URLs in the source
            archiveName = ''
            nsources = []
            for x in sourcename:
                # Do some of the work _guessName does - if an archive is
                # provided, use its name
                if x.endswith('/'):
                    baseUrl = x[:-1]
                else:
                    idx = x.rindex('/')
                    baseUrl = x[:idx]
                    fName = x[idx+1:]
                    if not archiveName:
                        archiveName = fName
                    elif archiveName != fName:
                        raise SourceError("Inconsistent archive names: '%s' "
                                          "and '%s'" % (archiveName, fName))

                nsources.append(baseUrl)
            s = digestlib.sha1()
            for src in nsources:
                s.update(src)
            multiurlMapName = s.hexdigest()

            multiurlMap = recipe.multiurlMap
            multiurlMap[multiurlMapName] = nsources
            # If archiveName is not set, it's an empty string, so the
            # source line is well-formed
            self.sourcename = "multiurl://%s/%s" % (multiurlMapName,
                                                    archiveName)
        else:
            self.sourcename = sourcename % recipe.macros
        self._guessName()
        recipe.sourceMap(self.sourcename)
        self.rpm = self.rpm % recipe.macros

        self.manifest = None

    def _initManifest(self):
        assert self.package
        assert not self.manifest

        self.package = self.package % self.recipe.macros
        self.manifest = Manifest(package=self.package, recipe=self.recipe)
        self.manifest.walk()

    def doPrep(self):
        if self.debug:
            debugger.set_trace()
        if self.use:
            if self.linenum is None:
                self._doPrep()
            else:
                oldexcepthook = sys.excepthook
                sys.excepthook = action.genExcepthook(self)
                if self.recipe.buildinfo:
                    self.recipe.buildinfo.lastline = self.linenum
                self._doPrep()
                sys.excepthook = oldexcepthook

    def _doPrep(self):
        if self.rpm:
            self._extractFromRPM()

    def doAction(self):
        self.builddir = self.recipe.macros.builddir
        action.RecipeAction.doAction(self)

    def _addSignature(self, filename):
        sourcename=self.sourcename
        if not self.guessname:
            sourcename=sourcename[:-len(filename)]

        suffixes = ( 'sig', 'sign', 'asc' )

        inRepos, f = self.recipe.fileFinder.fetch(sourcename + filename,
                                                   suffixes=suffixes,
                                                   headers=self.httpHeaders,
                                                   allowNone=True)
        if f:
            self.localgpgfile = f
        else:
            log.warning('No GPG signature file found for %s', self.sourcename)

    def _getPublicKey(self):
        keyringPath = os.path.join(self.recipe.cfg.buildPath, 'pubring.pgp')
        tsdbPath = os.path.join(self.recipe.cfg.buildPath, 'pubring.tsdb')

        keyring = openpgpfile.PublicKeyring(keyringPath, tsdbPath)

        try:
            return keyring.getKey(self.keyid)
        except openpgpfile.KeyNotFound:
            pass

        # OK, we don't have the key.
        keyData = self._downloadPublicKey()
        keyring.addKeysAsStrings([keyData])

        return keyring.getKey(self.keyid)

    def _doDownloadPublicKey(self, keyServer):
        # Uhm. Proxies are not likely to forward traffic to port 11371, so
        # avoid using the system-wide proxy setting for now.
        # proxies = self.recipe.cfg.proxy
        opener = transport.URLOpener()
        url = 'http://%s:11371/pks/lookup?op=get&search=0x%s' % (
                keyServer, self.keyid)
        handle = opener.open(url)
        keyData = openpgpfile.parseAsciiArmorKey(handle)
        return keyData

    def _downloadPublicKey(self):
        # Compose URL for downloading the PGP key
        keyServers = [ 'subkeys.pgp.net', 'pgp.mit.edu', 'wwwkeys.pgp.net' ]
        keyData = None
        # Walk the list several times before giving up
        for ks in itertools.chain(*([ keyServers ] * 3)):
            try:
                keyData = self._doDownloadPublicKey(ks)
                if keyData:
                    break
            except transport.TransportError as e:
                log.info('Error retrieving PGP key %s from key server %s: %s' %
                    (self.keyid, ks, e))
                continue
            except Exception as e:
                log.info('Unknown error encountered while retrieving PGP key %s from key server %s: %s' % (self.keyid, ks, e))

        if keyData is None:
            raise SourceError("Failed to retrieve PGP key %s" % self.keyid)

        return keyData

    def checkSignature(self, filepath):
        if self.keyid:
            filename = os.path.basename(filepath)
            self._addSignature(filename)
        if 'localgpgfile' not in self.__dict__:
            return

        key = self._getPublicKey()

        doc = open(filepath)

        try:
            sig = openpgpfile.readSignature(file(self.localgpgfile))
        except openpgpfile.PGPError:
            raise SourceError("Failed to read signature from %s" % self.localgpgfile)

        # Does the signature belong to this key?
        if sig.getSignerKeyId() != key.getKeyId():
            raise SourceError("Signature file generated with key %s does "
                "not match supplied key %s" %
                    (sig.getSignerKeyId(), self.keyid))

        try:
            sig.verifyDocument(key.getCryptoKey(), doc)
        except openpgpfile.SignatureError:
            raise SourceError("GPG signature %s failed" %(self.localgpgfile))
        log.info('GPG signature %s is OK', os.path.basename(self.localgpgfile))

    def _extractFromRPM(self):
        """
        Extracts filename from rpm file and creates an entry in the
        source lookaside cache for the extracted file.
        """
        # Always pull from RPM
        inRepos, r = self.recipe.fileFinder.fetch(self.rpm,
                headers=self.httpHeaders)
        self.archiveInRepos = inRepos

        # by using the rpm's name in the full path, we ensure that different
        # rpms can contain files of the same name.
        prefix = os.path.sep.join((self.recipe.name, self.rpm))
        prefix = os.path.normpath(prefix)
        # CNY-2627 introduced a separate lookaside stack for rpm contents
        # this dir tree is parallel to NEGATIVE and trovenames.
        # the name =RPM_CONTENTS= was chosen because = is an illegal character
        # in a trovename and thus will never conflict with real troves.
        loc = os.path.sep.join(('=RPM_CONTENTS=', prefix))
        loc = os.path.normpath(loc)
        c = self.recipe.laReposCache.getCachePath(loc, self.sourcename)
        util.mkdirChain(os.path.dirname(c))
        _extractFilesFromRPM(r, targetfile=c, action=self)
        sourcename = os.path.sep.join((prefix, self.sourcename))
        self.archivePath = 'rpm://%s' % os.path.dirname(sourcename)

    def _guessName(self):
        self.guessname = None
        self.suffixes = None
        if self.sourcename.endswith('/'):
            self.guessname = "%(archive_name)s-%(archive_version)s" % self.recipe.macros
            self.suffixes = DEFAULT_SUFFIXES

    def _findSource(self, braceGlob=False):
        if self.sourceDir is not None:
            defaultDir = os.sep.join((self.builddir, self.recipe.theMainDir))
            # blank string should map to maindir, not destdir
            sourceDir = self.sourceDir or '.'
            return action._expandOnePath(util.joinPaths(sourceDir, self.sourcename), self.recipe.macros, defaultDir = defaultDir, braceGlob = braceGlob)

        sourcename = self.sourcename
        if self.guessname:
            sourcename += self.guessname

        searchMethod = self.recipe.fileFinder.SEARCH_ALL
        if (self.recipe.cookType == self.recipe.COOK_TYPE_REPOSITORY and not
                self.ephemeral):
            searchMethod = self.recipe.fileFinder.SEARCH_REPOSITORY_ONLY

        inRepos, source = self.recipe.fileFinder.fetch(sourcename,
                                            headers=self.httpHeaders,
                                           suffixes=self.suffixes,
                                           archivePath=self.archivePath,
                                           searchMethod=searchMethod)
        if self.archivePath:
            inRepos = self.archiveInRepos
        if source and not inRepos:
            self.checkSignature(source)
        return source

    def fetch(self, refreshFilter=None):
        if 'sourcename' not in self.__dict__:
            return None

        toFetch, guessname, suffixes = self.getPathAndSuffix()

        if guessname:
            toFetch += guessname
        inRepos, f = self.recipe.fileFinder.fetch(toFetch,
                                          suffixes=suffixes,
                                          headers=self.httpHeaders,
                                          refreshFilter=refreshFilter,
                                          archivePath = self.archivePath)

        if self.archivePath:
            inRepos = self.archiveInRepos
        if f and not inRepos:
            self.checkSignature(f)
        return f

    def fetchLocal(self):
        # Used by rMake to find files that are not autosourced.
        if self.rpm:
            toFetch = self.rpm
        else:
            toFetch = self.sourcename
        return self.recipe._fetchFile(toFetch, localOnly = True)

    def getPath(self):
        return self.getPathAndSuffix()[0]

    def getPathAndSuffix(self):
        if self.rpm:
            return self.rpm, None, None
        else:
            return self.sourcename, self.guessname, self.suffixes

    def do(self):
        raise NotImplementedError



class addArchive(_Source):
    """
    NAME
    ====
    B{C{r.addArchive()}} - Add a source code archive

    SYNOPSIS
    ========
    C{r.addArchive(I{archivename}, [I{dir}=,] [I{keyid}=,] [I{rpm}=,] [I{httpHeaders}=,] [I{package})=,] [I{use}=,] [I{preserveOwnership=,}] [I{preserveSetid,}] [I{preserveDirectories=,}] [I{sourceDir}=,] [I{debArchive}=,] [I{ephemeral}=])}

    DESCRIPTION
    ===========
    The C{r.addArchive()} class adds a source code archive consisting
    of an optionally compressed tar, cpio, xpi or zip archive,
    binary/source RPM, or binary dpkg .deb, and unpacks it to the
    proper directory.

    If the specified I{archivename} is only a URL in the form of
    C{http://www.site.org/}, C{r.addArchive} will automatically attempt
    several combinations of C{%(name)s-%(version)s} combined with common
    archive file extensions, such as (.tar.bz2, .tar.gz, .tbz2, and .tgz) to
    complete I{archivename}.

    If the specified I{archivename} is a URL that begins with C{mirror://},
    C{r.addArchive} will search a set of mirrors contained in files
    specified by the C{mirrorDirs} Conary configuration file entry or a set
    of default mirror files located in the C{/etc/conary/mirrors} directory.
    The mirror files are comprised of mirror URLs, listed  one entry per line.

    If the specified I{archivename} is a list of URLs, C{r.addArchive} will
    attempt to download the files, using the rules described above, from
    each URL, until one of them succeeds. Note that the archive base name (as
    would be printed by the "basename" shell command), if non-empty, has to be
    identical for all URLs.

    KEYWORDS
    ========
    The following keywords are recognized by C{r.addArchive}:

    B{dir} : Instructs C{r.addArchive} to change to the directory
    specified by C{dir} prior to unpacking the source archive.
    An absolute C{dir} value will be considered relative to
    C{%(destdir)s}, whereas a relative C{dir} value will be
    considered relative to C{%(builddir)s}.

    B{keyid} : Using the C{keyid} keyword indicates the eight-digit
    GNU Privacy Guard (GPG) key ID, without leading C{0x} for the
    source code archive signature should be sought, and checked.
    If you provide the C{keyid} keyword, C{r.addArchive} will
    search for a file named I{archivename}C{.{sig,sign,asc}}, and
    ensure it is signed with the appropriate GPG key. A missing signature
    results in a warning; a failed signature check is fatal.

    B{preserveOwnership} : If C{preserveOwnership} is True and the files
    are unpacked into the destination directory, the packaged files are owned
    by the same user and group which owned them in the archive. Only cpio,
    rpm, and tar achives are allowed when C{preserveOwnership} is used.

    B{preserveSetid} : If C{preserveSetid} is True and the files
    are unpacked into the destination directory, the packaged files preserve
    setuid and setgid bits that were in the archive.  Only tar archives
    are allowed when C{preserveSetid} is True.

    B{preserveDirectories} : If C{preserveDirectories} is True and the files
    are unpacked into the destination directory, the packaged files preserve
    setuid and setgid bits that were in the archive.  Only tar archives
    are allowed when C{preserveDirectories} is True.

    B{rpm} : If the C{rpm} keyword is used, C{r.addArchive}
    looks in the file or URL specified by C{rpm} for a binary or
    source RPM containing I{archivename}.

    B{use} : A Use flag, or boolean, or a tuple of Use flags, and/or
    boolean values which determine whether the source code archive is
    actually unpacked, or merely stored in the archive.

    B{httpHeaders} : A dictionary containing a list of headers to send with
    the http request to download the source archive.  For example, you could
    set Authorization credentials, fudge a Cookie, or, if direct links are
    not allowed for some reason (e.g. a click through EULA), a Referer can
    be provided.

    B{package} : (None) If set, must be a string that specifies the package
    (C{package='packagename'}), component (C{package=':componentname'}), or
    package and component (C{package='packagename:componentname'}) in which
    to place the files added while executing this command.
    Previously-specified C{PackageSpec} or C{ComponentSpec} lines will
    override the package specification, since all package and component
    specifications are considered in strict order as provided by the recipe

    B{sourceDir} : Instructs C{r.addArchive} to look in the directory
    specified by C{sourceDir} for the source archive to unpack.
    An absolute C{sourceDir} value will be considered relative to
    C{%(destdir)s}, whereas a relative C{sourceDir} value will be
    considered relative to C{%(builddir)s}. Using C{sourceDir} prompts
    Conary to ignore the lookaside cache in favor of this directory.

    B{debArchive} : When unpacking a dpkg .deb archive, provides the
    prefix used to select the internal archive to unpack.  Defaults to
    C{"data.tar"} (will choose C{"data.tar.gz"} or C{"data.tar.bz2"}
    but can reasonably be set to C{"control.tar"} to instead choose the
    archive containing the scripts.

    B{ephemeral} : If True, the file will be downloaded again at cook time
    rather than storing it in the repository in the source trove.  Use this
    only if the file can be recreated precisely in the future, for example if
    the file comes from a source control system.  Only valid for URLs.

    EXAMPLES
    ========
    The following examples demonstrate invocations of C{r.addArchive}
    from within a recipe:

    C{r.addArchive('initscripts-%(upmajver)s.tar.bz2', rpm=srpm)}

    Demonstrates use with a local source code archive file, and the C{rpm}
    keyword.

    C{r.addArchive('ftp://ftp.visi.com/users/hawkeyd/X/Xaw3d-%(version)s.tar.gz')}

    Demonstrates use with a source code archive accessed via an FTP URL.

    C{r.addArchive('ftp://ftp.pbone.net/mirror/ftp.sourceforge.net/pub/sourceforge/g/gc/gcompris/gcompris-7.4-1.i586.rpm')}

    Demonstrates use with a binary RPM file accessed via an FTP URL.

    C{r.addArchive('http://ipw2200.sourceforge.net/firmware.php?i_agree_to_the_license=yes&f=%(name)s-%(version)s.tgz', httpHeaders={'Referer': 'http://ipw2200.sourceforge.net/firmware.php?fid=7'})}

    Demonstrates use with a source code archive accessed via an HTTP URL, and
    sending a Referer header through the httpHeader keyword.

    C{r.addArchive('http://example.com/downloads/blah.iso', dir='/')}

    Demonstrates unpacking the contents of an iso image directly into
    the destdir.  Note that only Rock Ridge or Joliet images are handled,
    and that permissions and special files are not preserved.

    C{r.addArchive('mirror://sourceforge/%(name)s/%(name)s/%(version)s/', keyid='9BB19A22')}

    Demonstrates use with mirror URL and C{sourceforge} mirrors list for
    retrieving package source from SourceForge.
    """
    keywords = dict(_Source.keywords)
    keywords['preserveOwnership'] = None
    keywords['preserveSetid'] = None
    keywords['preserveDirectories'] = None
    keywords['debArchive'] = None

    def __init__(self, recipe, *args, **keywords):
        """
        @param recipe: The recipe object currently being built is provided
        automatically by the C{PackageRecipe} object. Passing in  C{recipe}
        from within a recipe is unnecessary.
        @keyword dir: Instructs C{r.addArchive} to change to the directory
        specified by C{dir} prior to unpacking the source archive.
        An absolute C{dir} value will be considered relative to
        C{%(destdir)s}, whereas a relative C{dir} value will be
        considered relative to C{%(builddir)s}.
        @keyword keyid: Using the C{keyid} keyword indicates the eight-digit
        GNU Privacy Guard (GPG) key ID, without leading C{0x} for the
        source code archive signature should be sought, and checked.
        If you provide the C{keyid} keyword, C{r.addArchive} will
        search for a file named I{archivename}C{.{sig,sign,asc}} and
        ensure it is signed with the appropriate GPG key. A missing signature
        results in a warning; a failed signature check is fatal.
        @keyword rpm: If the C{rpm} keyword is used, C{r.addArchive} looks in the
        file, or URL specified by C{rpm} for an RPM containing I{archivename}.
        @keyword use: A Use flag, or boolean, or a tuple of Use flags, and/or
        boolean values which determine whether the source code archive is
        actually unpacked, or merely stored in the archive.
        @keyword httpHeaders: A dictionary containing headers to add to an http request
        when downloading the source code archive.
        @keyword package: A string that specifies the package, component, or package and
        component in which to place the files added while executing this command
        @keyword debArchive: When unpacking a dpkg .deb archive, provides the
        prefix used to select the internal archive to unpack.  Defaults to
        C{"data.tar"} (will choose C{"data.tar.gz"} or C{"data.tar.bz2"}
        but can reasonably be set to C{"control.tar"} to instead choose the
        archive containing the scripts.
        """
        _Source.__init__(self, recipe, *args, **keywords)

    def doDownload(self):
        return self._findSource()

    @staticmethod
    def _cpioOwners(fullOutput):
        lines = fullOutput.split('\n')
        for line in lines:
            if not line: continue
            fields = line.split(None, 8)
            yield (fields[8], fields[2], fields[3])

    @staticmethod
    def _tarOwners(fullOutput):
        lines = fullOutput.split('\n')
        for line in lines:
            if not line: continue
            if line.startswith('d') and ' Creating directory: ' in line:
                # CNY-3060 -- intermediate directory not included in archive
                continue
            fields = line.split(None, 5)
            owner, group = fields[1].split('/')
            yield (fields[5], owner, group)

    def do(self):
        f = self.doDownload()
        Ownership  = {}
        destDir = action._expandOnePath(self.dir, self.recipe.macros,
                                        defaultDir=self.builddir)

        if self.package:
            self._initManifest()

        if (self.preserveOwnership
            or self.preserveSetid
            or self.preserveDirectories) and not(
                destDir.startswith(self.recipe.macros.destdir)):
            raise SourceError(
                "preserveOwnership, preserveSetid, and preserveDirectories"
                " not allowed when unpacking into build directory")

        guessMainDir = (not self.recipe.explicitMainDir and
                        not self.dir.startswith('/'))

        if guessMainDir:
            bd = self.builddir
            join = os.path.join

            before = set(x for x in os.listdir(bd) if os.path.isdir(join(bd, x)))
            if self.recipe.mainDir() in before:
                mainDirPath = '/'.join((bd, self.recipe.mainDir()))
                mainDirBefore = set(os.listdir(mainDirPath))

        util.mkdirChain(destDir)

        log.info('unpacking archive %s' %os.path.basename(f))
        if f.endswith(".zip") or f.endswith(".xpi") or f.endswith(".jar") or f.endswith(".war"):
            if (self.preserveOwnership or self.preserveSetid or self.preserveDirectories):
                raise SourceError('cannot preserveOwnership, preserveSetid, or preserveDirectories for xpi or zip archives')

            util.execute("unzip -q -o -d '%s' '%s'" % (destDir, f))
            self._addActionPathBuildRequires(['unzip'])

        elif f.endswith(".rpm"):
            if (self.preserveSetid or self.preserveDirectories):
                raise SourceError('cannot preserveSetid or preserveDirectories for rpm archives')
            self._addActionPathBuildRequires(['/bin/cpio'])
            log.info("extracting %s into %s" % (f, destDir))
            ownerList = _extractFilesFromRPM(f, directory=destDir, action=self)
            if self.preserveOwnership:
                for (path, user, group, _, _, _, _, _, _, _, _) in ownerList:
                    if user != 'root' or group != 'root':
                        # trim off the leading / (or else path.joining it with
                        # self.dir will result in /dir//foo -> /foo.
                        path = path.lstrip('/')
                        path = util.normpath(os.path.join(self.dir, path))
                        d = Ownership.setdefault((user, group),[])
                        d.append(path)
        elif f.endswith(".iso"):
            if (self.preserveOwnership or self.preserveSetid or self.preserveDirectories):
                raise SourceError('cannot preserveOwnership, preserveSetid, or preserveDirectories for iso images')

            self._addActionPathBuildRequires(['isoinfo'])
            _extractFilesFromISO(f, directory=destDir)

        else:
            m = magic.magic(f)
            _uncompress = "cat"
            # command to run to get ownership info; if this isn't set, use
            # stdout from the command
            ownerListCmd = None
            # function which parses the ownership string to get file ownership
            # details
            ownerParser = None
            ExcludeDirectories = []

            actionPathBuildRequires = []
            # Question: can magic() ever get these wrong?!
            if f.endswith('deb'):
                # We want to use the normal tar processing so we can
                # preserve ownership
                if self.debArchive is None:
                    self.debArchive = 'data.tar'

                # binutils is needed for ar
                actionPathBuildRequires.append('ar')

                # Need to determine how data is compressed
                cfile = util.popen('ar t %s' %f)
                debData = [ x.strip() for x in cfile.readlines()
                            if x.startswith(self.debArchive) ]
                cfile.close()
                if not debData:
                    raise SourceError('no %s found in %s' %(self.debArchive, f))
                debData = debData[0]

                if debData.endswith('.gz'):
                    _uncompress = "gzip -d -c"
                    actionPathBuildRequires.append('gzip')
                elif debData.endswith('.bz2'):
                    _uncompress = "bzip2 -d -c"
                    actionPathBuildRequires.append('bzip2')
                elif debData.endswith('.xz'):
                    _uncompress = "xz -d -c"
                    actionPathBuildRequires.append('xz')
                elif debData.endswith('.lzma'):
                    _uncompress = "xz -d -c"
                    actionPathBuildRequires.append('xz')
                else:
                    # data.tar?  Alternatively, yet another
                    # compressed format that we need to add
                    # support for
                    _uncompress = 'cat'
                    actionPathBuildRequires.append('cat')

            if isinstance(m, magic.bzip) or f.endswith("bz2"):
                _uncompress = "bzip2 -d -c"
                actionPathBuildRequires.append('bzip2')
            if isinstance(m, magic.xz) or f.endswith('xz'):
                _uncompress = 'xz -d -c'
                actionPathBuildRequires.append('xz')
            elif isinstance(m, magic.gzip) or f.endswith("gz") \
                   or f.endswith(".Z"):
                _uncompress = "gzip -d -c"
                actionPathBuildRequires.append('gzip')
            elif isinstance(m, magic.lzo) or f.endswith(".lzo"):
                _uncompress = "lzop -dcq"
                actionPathBuildRequires.append('lzop')

            # There are things we know we know...
            _tarSuffix  = ['tar', 'tgz', 'tbz2', 'txz', 'taZ',
                           'tar.gz', 'tar.bz2', '.tar.xz', 'tar.Z',
                           'tar.lzo',
                           ]
            _cpioSuffix = ["cpio", "cpio.gz", "cpio.bz2"]

            if True in [f.endswith(x) for x in _tarSuffix]:
                preserve = ''
                if self.dir.startswith('/'):
                    preserve = 'p'
                _unpack = ("%(tar)s -C '%%s' -xvvS%%sf -"
                        % self.recipe.macros % (destDir, preserve))
                ownerParser = self._tarOwners
                actionPathBuildRequires.append(self.recipe.macros.tar)
            elif True in [f.endswith(x) for x in _cpioSuffix]:
                _unpack = "( cd '%s' && cpio -iumd --quiet )" % (destDir,)
                ownerListCmd = "cpio -tv --quiet"
                ownerParser = self._cpioOwners
                actionPathBuildRequires.append('cpio')
            elif _uncompress != 'cat':
                # if we know we've got an archive, we'll default to
                # assuming it's an archive of a tar for now
                # TODO: do something smarter about the contents of the
                # archive
                # Note: .deb handling currently depends on this default
                _unpack = (("%(tar)s -C '%%s' -xvvSpf -" % self.recipe.macros)
                        % (destDir,))
                ownerParser = self._tarOwners
                actionPathBuildRequires.append('tar')
            else:
                raise SourceError("unknown archive format: " + f)

            self._addActionPathBuildRequires(actionPathBuildRequires)
            if f.endswith('.deb'):
                # special handling for .deb files - need to put
                # the .deb file on the command line
                cmd = "ar p '%s' %s | %s | %s" %(
                             f, debData, _uncompress, _unpack)
            else:
                cmd = "%s < '%s' | %s" % (_uncompress, f, _unpack)
            fObj = os.popen(cmd)
            s = fObj.read()
            output = ""
            while s:
                output += s
                s = fObj.read()

            fObj.close()

            if ownerListCmd:
                cmd = "%s < '%s' | %s" % (_uncompress, f, ownerListCmd)
                fObj = os.popen(cmd)
                s = fObj.read()
                output = ""
                while s:
                    output += s
                    s = fObj.read()

                fObj.close()

            if ownerParser and self.preserveOwnership:
                destdir = self.recipe.macros.destdir
                for (path, user, group) in ownerParser(output):
                    if user != 'root' or group != 'root':
                        path = util.normpath(os.path.join(self.dir, path))
                        d = Ownership.setdefault((user, group),[])
                        d.append(path)
                        if self.preserveDirectories:
                            if os.path.isdir('/'.join((destdir, path))):
                                ExcludeDirectories.append( path )

            if self.preserveSetid or self.preserveDirectories:
                destlen = len(self.recipe.macros.destdir)
                for dirpath, dirnames, filenames in os.walk(destDir):
                    if self.preserveSetid:
                        for filename in filenames + dirnames:
                            path = util.normpath(os.path.join(dirpath,
                                                              filename))
                            mode = os.lstat(path).st_mode
                            sidbits = mode & 0o6000
                            if sidbits:
                                destPath = path[destlen:]
                                self.recipe.setModes(destPath, sidbits=sidbits)
                        for dirname in dirnames:
                            path = util.normpath(os.path.join(dirpath,
                                                              dirname))
                            mode = os.lstat(path).st_mode
                            destPath = path[destlen:]
                            if mode & 0o7777 != 0o755:
                                ExcludeDirectories.append( destPath )
                            if self.preserveSetid:
                                sidbits = mode & 0o6000
                                self.recipe.setModes(destPath, sidbits=sidbits)
                    if self.preserveDirectories:
                        if not filenames and not dirnames:
                            # preserve empty directories
                            ExcludeDirectories.append( dirpath[destlen:] )

            if len(ExcludeDirectories):
                self.recipe.ExcludeDirectories(exceptions=filter.PathSet(
                    ExcludeDirectories))

        if guessMainDir:
            bd = self.builddir
            after = set(x for x in os.listdir(bd) if os.path.isdir(join(bd, x)))

            if self.recipe.mainDir() in before:
                mainDirAfter = set(os.listdir(mainDirPath))
                mainDirDifference = mainDirAfter - mainDirBefore
            else:
                mainDirDifference = set()
            difference = after - before
            oldMainDir = self.recipe.mainDir()
            if len(difference) == 1 and not len(mainDirDifference):
                # Archive produced something outside of mainDir
                # and did not put any contents into mainDir
                candidate = difference.pop()
                if os.path.isdir('%s/%s' %(self.builddir, candidate)):
                    self.recipe.mainDir(candidate)
                    try:
                        os.rmdir('/'.join((self.builddir, oldMainDir)))
                    except OSError:
                        raise SourceError(
                            'Sources do not agree on main directory:'
                            ' files exist in %s but first archive wants %s;'
                            ' try calling addArchive() before other'
                            ' source actions such as addSource()',
                            '/'.join((self.builddir, oldMainDir)),
                            '/'.join((self.builddir, candidate))
                        )
                else:
                    self.recipe.mainDir(oldMainDir)
            else:
                self.recipe.mainDir(oldMainDir)
        if self.package:
            self.manifest.create()

        for key, pathList in list(Ownership.items()):
            user, group = key
            self.recipe.Ownership(user, group, filter.PathSet(pathList))

        return f
Archive = addArchive

class addPatch(_Source):
    """
    NAME
    ====
    B{C{r.addPatch()}} - Add a patch to source code

    SYNOPSIS
    ========
    C{r.addPatch(I{patchfilename}, [I{backup}=,] [I{dir}=,] [I{extraArgs}=,] [I{keyid}=,] [I{httpHeaders}=,] [I{package})=,] [I{level}=,] [I{macros}=,] [I{rpm}=,] [I{use}=,] [I{sourceDir}=,] [I{patchName}=])}

    DESCRIPTION
    ===========
    The C{r.addPatch()} class adds a patch to be applied to the source code
    during the build phase.

    KEYWORDS
    ========
    The following keywords are recognized by C{r.addPatch}:

    B{backup} : The suffix to use when storing file versions before applying
    the patch.

    B{dir} : Instructs C{r.addPatch} to change to the directory specified by
    C{dir} prior to applying the patch. An absolute C{dir} value will be
    considered relative to C{%(destdir)s}, whereas a relative C{dir} value
    will be considered relative to C{%(builddir)s}.


    B{extraArgs} : As a last resort, arbitrary arguments may be passed to the
    patch program  with the C{extraArgs} keyword. This should not normally be
    required, and is indicative of a possible bug which should be reported
    with the suggestion of direct support for the patch arguments in question.

    B{keyid} : Using the C{keyid} keyword indicates the eight-digit GNU
    Privacy Guard (GPG) key ID, without leading C{0x} for the source code
    archive signature should be sought, and checked. If you provide the
    C{keyid} keyword, {r.addPatch} will search for a file named
    I{patchfilename}C{.{sig,sign,asc}}, and ensure it is signed with the
    appropriate GPG key. A missing signature results in a warning; a failed
    signature check is fatal.

    B{level} : By default, conary attempts to patch the source using
    levels 1, 0, 2, and 3, in that order. The C{level} keyword can
    be given an integer value to resolve ambiguity, or if an even
    higher level is required.  (This is the C{-p} option to the
    patch program.)

    B{macros} : The C{macros} keyword accepts a boolean value, and defaults
    to false. However, if the value of C{macros} is true, recipe macros in the
    body  of the patch will be interpolated before applying the patch. For
    example, a patch which modifies the value C{CFLAGS = -02} using
    C{CFLAGS = %(cflags)s} will update the C{CFLAGS} parameter based upon the
    current setting of C{recipe.macros.cflags}.

    B{filter} : The C{filter} keyword provides a shell command that
    takes the patch with macros applied (if applicable) on standard
    input and provides on standard output a modified patch.  Put
    spaces around any C{|} characters in the filter pipeline that
    implement shell pipes.

    B{rpm} : If the C{rpm} keyword is used, C{Archive}
    looks in the file, or URL specified by C{rpm} for an RPM
    containing I{patchfilename}.

    B{use} : A Use flag, or boolean, or a tuple of Use flags, and/or
    boolean values which determine whether the source code archive is
    actually unpacked or merely stored in the archive.

    B{httpHeaders} : A dictionary containing a list of headers to send with
    the http request to download the source archive.  For example, you could
    set Authorization credentials, fudge a Cookie, or, if direct links are
    not allowed for some reason (e.g. a click through EULA), a Referer can
    be provided.

    B{package} : (None) If set, must be a string that specifies the package
    (C{package='packagename'}), component (C{package=':componentname'}), or
    package and component (C{package='packagename:componentname'}) in which
    to place the files added while executing this command.
    Previously-specified C{PackageSpec} or C{ComponentSpec} lines will
    override the package specification, since all package and component
    specifications are considered in strict order as provided by the recipe

    B{sourceDir} : Instructs C{r.addPatch} to look in the directory
    specified by C{sourceDir} for the patch to apply.
    An absolute C{sourceDir} value will be considered relative to
    C{%(destdir)s}, whereas a relative C{sourceDir} value will be
    considered relative to C{%(builddir)s}. Using C{sourceDir} directs
    Conary to ignore the lookaside cache in favor of this directory.

    B{patchName} : Name of patch program to run (Default: C{patch})

    B{ephemeral} : If True, the file will be downloaded again at cook time
    rather than storing it in the repository in the source trove.  Use this
    only if the file can be recreated precisely in the future, for example if
    the file comes from a source control system.  Only valid for URLs.

    EXAMPLES
    ========
    The following examples demonstrate invocations of C{r.addPatch}
    from within a recipe:

    C{r.addPatch('iptables-1.3.0-no_root.patch')}

    Simple usage of C{r.addPatch} specifying the application of the patch
    C{iptables-1.3.0-no_root.patch}.

    C{r.addPatch('Xaw3d-1.5E-xorg-imake.patch', level=0, dir='lib/Xaw3d')}

    Uses the C{level} keyword specifying that no initial subdirectory names be
    stripped, and a C{dir} keyword, instructing C{r.addPatch} to change to the
    C{lib/Xaw3d} directory prior to applying the patch.
    """
    keywords = {'level': None,
                'backup': '',
                'macros': False,
                'filter': None,
                'extraArgs': '',
                'patchName': 'patch'}


    def __init__(self, recipe, *args, **keywords):
        """
        @param recipe: The recipe object currently being built is provided
        automatically by the PackageRecipe object. Passing in  C{recipe} from
        within a recipe is unnecessary.
        @keyword backup: The suffix to use when storing file versions before
        applying the patch.
        @keyword extraArgs: As a last resort, arbitrary arguments may be passed
        to the patch program  with the C{extraArgs} keyword. This should not
        normally be required, and is indicative of a possible bug which
        should be reported with the suggestion of direct support for the
        patch arguments in question.
        @keyword dir: Instructs C{r.addPatch} to change to the directory
        specified by C{dir} prior to applying the patch. An absolute C{dir}
        value will be considered relative to C{%(destdir)s}, whereas a
        relative C{dir} value will be considered
        relative to C{%(builddir)s}.
        @keyword keyid: Using the C{keyid} keyword indicates the eight-digit GNU
        Privacy Guard (GPG) key ID, without leading C{0x} for the source code
        archive signature should be sought, and checked. If you provide the
        C{keyid} keyword, {r.addPatch} will search for a file named
        I{patchname}C{.{sig,sign,asc}}, and ensure it is signed with the
        appropriate GPG key. A missing signature results in a warning; a
        failed signature check is fatal.
        @keyword level: By default, conary attempts to patch the source using
        levels 1, 0, 2, and 3, in that order. The C{level} keyword can
        be given an integer value to resolve ambiguity, or if an even
        higher level is required.  (This is the C{-p} option to the
        patch program.)
        @keyword macros: The C{macros} keyword accepts a boolean value, and
        defaults to false. However, if the value of C{macros} is true, recipe
        macros in the body  of the patch will be interpolated before applying
        the patch. For example, a patch which modifies the value
        C{CFLAGS = -02} using C{CFLAGS = %(cflags)s} will update the C{CFLAGS}
        parameter based upon the current setting of C{recipe.macros.cflags}.
        @keyword filter: The C{filter} keyword provides a shell command that
        takes the patch with macros applied (if applicable) on standard
        input and provides on standard output a modified patch.  Put
        spaces around any C{|} characters in the filter pipeline that
        implement shell pipes.
        @keyword rpm: If the C{rpm} keyword is used, C{addArchive} looks in the file,
        or URL specified by C{rpm} for an RPM containing I{patchname}.
        @keyword use: A Use flag, or boolean, or a tuple of Use flags, and/or
        boolean values which determine whether the source code archive is
        actually unpacked, or merely stored in the archive.
        @keyword httpHeaders: A dictionary containing headers to add to an http request
        when downloading the source code archive.
        @keyword package: A string that specifies the package, component, or package
        and component in which to place the files added while executing this command
        """
        _Source.__init__(self, recipe, *args, **keywords)
        self.applymacros = self.macros

    def _applyPatch(self, patchlevel, patch, destDir, dryRun=True):
        patchProgram = self.patchName %self.recipe.macros
        self._addActionPathBuildRequires([patchProgram])
        patchArgs = [ patchProgram, '-d', destDir, '-p%s'%patchlevel, ]
        if self.backup:
            patchArgs.extend(['-b', '-z', self.backup])
        if self.extraArgs:
            if isinstance(self.extraArgs, str):
                patchArgs.append(self.extraArgs)
            else:
                patchArgs.extend(self.extraArgs)

        fd, path = tempfile.mkstemp()
        os.unlink(path)
        logFile = os.fdopen(fd, 'w+')
        if dryRun:
            patchArgs.append('--dry-run')

        try:
            p2 = subprocess.Popen(patchArgs,
                                  stdin=subprocess.PIPE,
                                  stderr=logFile, shell=False, stdout=logFile,
                                  close_fds=True)
        except OSError as e:
            raise SourceError('Could not run %s: %s' % (patchProgram, e))

        p2.stdin.write(patch)
        p2.stdin.close() # since stdin is closed, we can't
                         # answer y/n questions.

        failed = p2.wait()

        logFile.flush()
        logFile.seek(0,0)
        return failed, logFile


    def _patchAtLevels(self, patchPath, patch, destDir, patchlevels):
        logFiles = []
        log.info('attempting to apply %s to %s with patch level(s) %s'
                 %(patchPath, destDir, ', '.join(str(x) for x in patchlevels)))

        for patchlevel in patchlevels:
            failed, logFile = self._applyPatch(patchlevel, patch, destDir,
                                              dryRun=True)

            if failed:
                # patch failed - keep patchlevel and logfile for display
                # later
                logFiles.append((patchlevel, logFile))
                continue

            failed, logFile = self._applyPatch(patchlevel, patch, destDir,
                                              dryRun=False)

            log.info(logFile.read().strip())
            logFile.close()
            # close any saved log files before we return
            for _, f in logFiles:
                f.close()
            if failed:
                # this shouldn't happen.
                raise SourceError('could not apply patch %s - applied with --dry-run but not normally' % patchPath)
            # patch was successful - re-run this time actually applying
            log.info('applied successfully with patch level %s' %patchlevel)
            return

        # all attemps were unsuccessful.  display relevant logs
        rightLevels = []
        # do two passes over all the log files.  Once to find
        # which log files are probably interesting ones
        # and one to actually print them.
        for idx, (patchlevel, logFile) in enumerate(logFiles):
            s = logFile.read().strip()
            if "can't find file to patch" not in s:
                rightLevels.append(idx)
            logFiles[idx] = (patchlevel, s)
            logFile.close()

        # attempt one more time for the cases where --dry-run causes
        # patches to fail to apply because they modify the same file
        # more than once.
        if len(rightLevels) == 1:
            fallbackLevel = logFiles[rightLevels[0]][0]
        elif len(patchlevels) == 1:
            fallbackLevel = patchlevels[0]
        else:
            fallbackLevel = 1
        log.info('patch did not apply with --dry-run, trying level %s directly' % fallbackLevel)
        failed, logFile = self._applyPatch(fallbackLevel, patch, destDir,
                                           dryRun=False)
        if not failed:
            logFile.close()
            log.info('applied successfully with patch level %s' % fallbackLevel)
            return
        # update the logFile value to match what we had here
        idx = [idx for (idx, (patchlevel, _)) in enumerate(logFiles)
               if patchlevel == fallbackLevel ][0]
        logFiles[idx] = (fallbackLevel, logFile.read().strip())


        for idx, (patchlevel, s) in enumerate(logFiles):
            if rightLevels and idx not in rightLevels:
                log.info('patch level %s failed - probably wrong level'
                         %(patchlevel))
                continue
            log.info('patch level %s FAILED' % patchlevel)
            log.info(s)
        log.error('could not apply patch %s in directory %s', patchPath,
                  destDir)
        raise SourceError('could not apply patch %s' % patchPath)

    def doDownload(self):
        f = self._findSource(braceGlob = self.sourceDir is not None)
        return f

    def do(self):
        pathRes = self.doDownload()
        if not isinstance(pathRes, (list, tuple)):
            pathRes = (pathRes,)
        for patchPath in sorted(pathRes):
            self.doFile(patchPath)

    def doFile(self, patchPath):
        provides = "cat"
        if self.sourcename.endswith(".gz"):
            provides = "zcat"
        elif self.sourcename.endswith(".bz2"):
            provides = "bzcat"
        elif self.sourcename.endswith(".xz"):
            provides = "xzcat"
        self._addActionPathBuildRequires([provides])
        defaultDir = os.sep.join((self.builddir, self.recipe.theMainDir))
        destDir = action._expandOnePath(self.dir, self.recipe.macros,
                                                  defaultDir=defaultDir)
        if self.level != None:
            leveltuple = (self.level,)
        else:
            leveltuple = (1, 0, 2, 3,)
        util.mkdirChain(destDir)

        filterString = ''
        if self.filter:
            filterString = '| %s' %self.filter
            # enforce appropriate buildRequires
            commandReported = False
            for shellToken in shlex.split(self.filter):
                if shellToken.startswith('|'):
                    shellToken = shellToken[1:]
                    commandReported = False
                if not commandReported:
                    if shellToken:
                        self._addActionPathBuildRequires([shellToken])
                        commandReported = True

        pin = util.popen("%s '%s' %s" %(provides, patchPath, filterString))
        if self.applymacros:
            patch = pin.read() % self.recipe.macros
        else:
            patch = pin.read()
        pin.close()
        self._patchAtLevels(patchPath, patch, destDir, leveltuple)
Patch = addPatch

class addSource(_Source):
    """
    NAME
    ====
    B{C{r.addSource()}} - Copy a file into build or destination directory

    SYNOPSIS
    ========
    C{r.addSource(I{sourcename}, [I{apply}=,] [I{dest}=,] [I{dir}=,] [I{httpHeaders}=,] [I{keyid}=,] [I{macros}=,] [I{mode}=,] [I{package}=,] [I{rpm}=,] [I{use}=,] [I{sourceDir}=])}

    DESCRIPTION
    ===========
    The C{r.addSource()} class copies a file into the build directory or
    destination directory.

    KEYWORDS
    ========
    The following keywords are recognized by C{r.addSource}:

    B{apply} : A command line to run after storing the file. Macros will be
    interpolated into this command.

    B{dest} : If set, provides the target name of the file in the build
    directory. A full pathname can be used. Use either B{dir}, or B{dest} to
    specify directory information, but not both. Useful mainly  when fetching
    the file from an source outside your direct control, such as a URL to a
    third-party web site, or copying a file out of an RPM package.
    An absolute C{dest} value will be considered relative to C{%(destdir)s},
    whereas a relative C{dest} value will be considered relative to
    C{%(builddir)s}.

    B{dir} : The directory in which to store the file, relative to the build
    directory. An absolute C{dir} value will be considered relative to
    C{%(destdir)s}, whereas a relative C{dir} value will be considered
    relative to C{%(builddir)s}. Defaults to storing file directly in the
    build directory.

    B{keyid} : Using the C{keyid} keyword indicates the eight-digit
    GNU Privacy Guard (GPG) key ID, without leading C{0x} for the
    source code archive signature should be sought, and checked.
    If you provide the C{keyid} keyword, C{r.addSource} will
    search for a file named I{sourcename}C{.{sig,sign,asc}}, and
    ensure it is signed with the appropriate GPG key. A missing signature
    results in a warning; a failed signature check is fatal.

    B{macros} : If True, interpolate recipe macros in the body of a patch
    before applying it.  For example, you might have a patch that changes
    C{CFLAGS = -O2} to C{CFLAGS = %(cflags)s}, which will cause C{%(cflags)s}
    to be replaced with the current setting of C{recipe.macros.cflags}.
    Defaults to False.

    B{mode}: If set, provides the mode to set on the file.

    B{rpm} : If the C{rpm} keyword is used, C{addSource} looks in the file, or
    URL specified by C{rpm} for an RPM containing I{sourcename}.

    B{use} : A Use flag or boolean, or a tuple of Use flags and/or booleans,
    that determine whether the archive is actually unpacked or merely stored
    in the archive.

    B{httpHeaders} : A dictionary containing a list of headers to send with
    the http request to download the source archive.  For example, you could
    set Authorization credentials, fudge a Cookie, or, if direct links are
    not allowed for some reason (e.g. a click through EULA), a Referer can
    be provided.

    B{package} : (None) If set, must be a string that specifies the package
    (C{package='packagename'}), component (C{package=':componentname'}), or
    package and component (C{package='packagename:componentname'}) in which
    to place the files added while executing this command.
    Previously-specified C{PackageSpec} or C{ComponentSpec} lines will
    override the package specification, since all package and component
    specifications are considered in strict order as provided by the recipe

    B{sourceDir} : Instructs C{r.addSource} to look in the directory
    specified by C{sourceDir} for the file to install.
    An absolute C{sourceDir} value will be considered relative to
    C{%(destdir)s}, whereas a relative C{sourceDir} value will be
    considered relative to C{%(builddir)s}. Using C{sourceDir} prompts
    Conary to ignore the lookaside cache in favor of this directory.

    B{ephemeral} : If True, the file will be downloaded again at cook time
    rather than storing it in the repository in the source trove.  Use this
    only if the file can be recreated precisely in the future, for example if
    the file comes from a source control system.  Only valid for URLs.

    EXAMPLES
    ========
    The following examples demonstrate invocations of C{r.addSource}
    from within a recipe:

    C{r.addSource('usbcam.console')}

    The example above is a typical, simple invocation of C{r.addSource()}
    which adds the file C{usbcam.console} directly to the build directory.

    C{r.addSource('pstoraster' , rpm=srpm, dest='pstoraster.new')}

    The above example of C{r.addSource} specifies the file C{pstoraster} is
    to be sought in a source RPM file, and is to be added to the build
    directory as C{pstoraster.new}.
    """

    keywords = {'apply': '',
                'contents': None,
                'macros': False,
                'dest': None,
                'mode': None}


    def __init__(self, recipe, *args, **keywords):
        """
        @param recipe: The recipe object currently being built is provided
        automatically by the PackageRecipe object. Passing in C{recipe} from
        within a recipe is unnecessary.
        @keyword dest: If set, provides the target name of the file in the build
        directory. A full pathname can be used. Use either B{dir}, or
        B{dest} to specify directory information, but not both. Useful mainly
        when fetching the file from an source outside your direct control, such
        as a URL to a third-party web site, or copying a file out of an
        RPM package. An absolute C{dest} value will be considered relative to
        C{%(destdir)s}, whereas a relative C{dest} value will be considered
        relative to C{%(builddir)s}.
        @keyword dir: The directory in which to store the file, relative to
        the build directory. An absolute C{dir} value will be considered
        relative to C{%(destdir)s}, whereas a relative C{dir} value will be
        considered relative to C{%(builddir)s}. Defaults to storing file
        directly in the build directory.
        @keyword keyid: Using the C{keyid} keyword indicates the eight-digit GNU
        Privacy Guard (GPG) key ID, without leading C{0x} for the source code
        archive signature should be sought, and checked. If you provide the
        C{keyid} keyword, C{r.addArchive} will search for a file named
        I{sourcename}C{.{sig,sign,asc}}, and ensure it is signed with the
        appropriate GPG key. A missing signature results in a warning; a
        failed signature check is fatal.
        @keyword macros: If True, interpolate recipe macros in the body of a
        patch before applying it.  For example, you might have a patch that
        changes C{CFLAGS = -O2} to C{CFLAGS = %(cflags)s}, which will cause
        C{%(cflags)s} to be replaced with the current setting of
        C{recipe.macros.cflags}. Defaults to False.
        @keyword mode: If set, provides the mode to set on the file.
        @keyword use : A Use flag, or boolean, or a tuple of Use flags, and/or boolean
        values which determine whether the source code archive is actually
        unpacked, or merely stored in the archive.
        @keyword rpm: If the C{rpm} keyword is used, C{addArchive} looks in the file,
        or URL specified by C{rpm} for an RPM containing I{sourcename}.
        @keyword use: A Use flag or boolean, or a tuple of Use flags and/or
        booleans, that determine whether the archive is actually unpacked or
        merely stored in the archive.
        @keyword httpHeaders: A dictionary containing headers to add to an http request
        when downloading the source code archive.
        @keyword package: A string that specifies the package, component, or package
        and component in which to place the files added while executing this command
        """
        _Source.__init__(self, recipe, *args, **keywords)
        if self.dest:
            # make sure that user did not pass subdirectory in
            fileName = os.path.basename(self.dest %recipe.macros)
            if fileName != self.dest:
                if self.dir:
                    self.init_error(RuntimeError,
                                    'do not specify a directory in both dir and'
                                    ' dest keywords')
                elif (self.dest % recipe.macros)[-1] == '/':
                    self.dir = self.dest
                    self.dest = os.path.basename(self.sourcename)
                else:
                    self.dir = os.path.dirname(self.dest % recipe.macros)
                    self.dest = fileName
                    # unfortunately, dir is going to be macro expanded again
                    # later, make sure any %s in the path name survive
                    self.dir.replace('%', '%%')
        else:
            self.dest = os.path.basename(self.sourcename)

        if self.contents is not None:
            # Do not look for a file that does not exist...
            self.sourcename = ''
        if self.macros:
            self.applymacros = True
        else:
            self.applymacros = False

    def doDownload(self):
        if self.contents is not None:
            return
        f = self._findSource()
        return f

    def do(self):
        if self.package:
            self._initManifest()
        # make sure the user gave a valid source, and not a directory
        baseFileName = os.path.basename(self.sourcename)
        if not baseFileName and not self.contents:
            raise SourceError('cannot specify a directory as input to '
                'addSource')
        log.info('adding source file %s' %baseFileName)

        defaultDir = os.sep.join((self.builddir, self.recipe.theMainDir))
        destDir = action._expandOnePath(self.dir, self.recipe.macros,
                                                  defaultDir=defaultDir)
        util.mkdirChain(destDir)
        destFile = os.sep.join((destDir, self.dest))
        util.removeIfExists(destFile)
        if self.contents is not None:
            pout = file(destFile, "w")
            if self.applymacros:
                pout.write(self.contents %self.recipe.macros)
            else:
                pout.write(self.contents)
            pout.close()
        else:
            f = self.doDownload()
            if self.applymacros:
                log.info('applying macros to source %s' %f)
                pin = file(f)
                pout = file(destFile, "w")
                log.info('copying %s to %s' %(f, destFile))
                pout.write(pin.read()%self.recipe.macros)
                pin.close()
                pout.close()
            else:
                util.copyfile(f, destFile)
        if self.mode:
            os.chmod(destFile, self.mode)
        if self.apply:
            util.execute(self.apply %self.recipe.macros, destDir)
        if self.package:
            self.manifest.create()

Source = addSource


class addCapsule(_Source):

    """
    NAME
    ====

    B{C{r.addCapsule()}} - Add an encapsulated file

    SYNOPSIS
    ========
    C{r.addCapsule(I{capsulename}, [I{dir}=,] [I{httpHeaders}=,] [I{keyid}=,] [I{mode}=,] [I{package}=,] [I{sourceDir}=,] [I{ignoreConflictingPaths}=,])}

    DESCRIPTION
    ===========
    The C{r.addCapsule()} class adds an encapsulated file to the package.

    KEYWORDS
    ========
    The following keywords are recognized by C{r.addCapsule}:

    B{dir} : The directory in which to store the file, relative to the build
    directory. An absolute C{dir} value will be considered relative to
    C{%(destdir)s}, whereas a relative C{dir} value will be considered
    relative to C{%(builddir)s}. Defaults to storing file directly in the
    build directory.

    B{keyid} : Using the C{keyid} keyword indicates the eight-digit
    GNU Privacy Guard (GPG) key ID, without leading C{0x} for the
    source code archive signature should be sought, and checked.
    If you provide the C{keyid} keyword, C{r.addCapsule} will
    search for a file named I{sourcename}C{.{sig,sign,asc}}, and
    ensure it is signed with the appropriate GPG key. A missing signature
    results in a warning; a failed signature check is fatal.

    B{mode}: If set, provides the mode to set on the file.

    B{httpHeaders} : A dictionary containing a list of headers to send with
    the http request to download the source archive.  For example, you could
    set Authorization credentials, fudge a Cookie, or, if direct links are
    not allowed for some reason (e.g. a click through EULA), a Referer can
    be provided.

    B{package} : (None) If set, must be a string that specifies the package
    (C{package='packagename'}), component (C{package=':componentname'}), or
    package and component (C{package='packagename:componentname'}) in which
    to place the files added while executing this command. If not specified,
    the default componentname is C{:rpm}. Previously-specified C{PackageSpec}
    or C{ComponentSpec} lines will override the package specification, since
    all package and component specifications are considered in strict order as
    provided by the recipe

    B{sourceDir} : Instructs C{r.addCapsule} to look in the directory
    specified by C{sourceDir} for the file to install.
    An absolute C{sourceDir} value will be considered relative to
    C{%(destdir)s}, whereas a relative C{sourceDir} value will be
    considered relative to C{%(builddir)s}. Using C{sourceDir} prompts
    Conary to ignore the lookaside cache in favor of this directory.

    B{ignoreConflictingPaths} : A list of paths in which C{r.addCapsule} will
    not check files for conflicting contents.

    B{wimVolumeIndex} : The image index of a Windows Imaging Formatted file that
    will be reprented in this package.

    B{msiArgs} : (Optional) Arguments passed to msiexec at install time. The
    default set of arguments at the time of this writing in the rPath Tools
    Install Service are "/q /l*v".

    B{ephemeral} : If True, the file will be downloaded again at cook time
    rather than storing it in the repository in the source trove.  Use this
    only if the file can be recreated precisely in the future, for example if
    the file comes from a source control system.  Only valid for URLs.

    EXAMPLES
    ========
    The following examples demonstrate invocations of C{r.addCapsule}
    from within a recipe:

    C{r.addCapsule('foo.rpm')}

    The example above is a typical, simple invocation of C{r.addCapsule()}
    which adds the file C{foo.rpm} as a capsule file and creates the C{:rpm}
    component.

    C{r.addCapsule('Setup.msi')}

    The example above is a typical, simple invocation of C{r.addCapsule()}
    which adds the file C{Setup.msi} as a capsule and creates the C{:msi}
    component.

    C{r.addCapsule('sample.wim')}

    The example above is a typical, simple invocation of C{r.addCapsule()}
    which adds the file C{sample.wim} as a capsule and creates the C{:wim}
    component.
    """

    keywords = {'ignoreConflictingPaths': set(),
                'ignoreAllConflictingTimes': None,  # deprecated
                'wimVolumeIndex' : 1,
                'msiArgs': None,
               }

    def __init__(self, recipe, *args, **keywords):
        """
        @param recipe: The recipe object currently being built is provided
        automatically by the PackageRecipe object. Passing in C{recipe} from
        within a recipe is unnecessary.
        @keyword dir: The directory in which to store the file, relative to
        the build directory. An absolute C{dir} value will be considered
        relative to C{%(destdir)s}, whereas a relative C{dir} value will be
        considered relative to C{%(builddir)s}. Defaults to storing file
        directly in the build directory.
        @keyword keyid: Using the C{keyid} keyword indicates the eight-digit GNU
        Privacy Guard (GPG) key ID, without leading C{0x} for the source code
        archive signature should be sought, and checked. If you provide the
        C{keyid} keyword, C{r.addCapsule} will search for a file named
        I{sourcename}C{.{sig,sign,asc}}, and ensure it is signed with the
        appropriate GPG key. A missing signature results in a warning; a
        failed signature check is fatal.
        @keyword mode: If set, provides the mode to set on the file.
        @keyword httpHeaders: A dictionary containing headers to add to an http
        request when downloading the source code archive.
        @keyword package: A string that specifies the package, component, or
        package and component in which to place the files added while executing
        this command
        @keyword sourceDir: A directory in which C{r.addCapsule} will search
        for files.
        @keyword ignoreConflictingPaths: A list of paths that will not be
        checked for conflicting file contents
        """
        self.capsuleMagic = None
        self.capsuleType = None

        _Source.__init__(self, recipe, *args, **keywords)

    def _initManifest(self):
        assert self.package
        assert not self.manifest

        self.package = self.package % self.recipe.macros
        self.manifest = ExplicitManifest(package=self.package,
                                         recipe=self.recipe)

    def _getCapsuleMagic(self, path):
        if not self.capsuleMagic:
            self.capsuleMagic = magic.magic(path)
            if self.capsuleMagic is None:
                raise SourceError('unknown capsule type for file %s', path)
            self.capsuleType = self.capsuleMagic.name.lower()
        assert(path==self.capsuleMagic.path)
        return self.capsuleMagic

    def doDownload(self):
        f = self._findSource()

        # identify the capsule type
        m = self._getCapsuleMagic(f)

        # here we guarantee that package contains a package:component
        # designation.  This is required for _addComponent().
        if self.capsuleType == 'rpm':
            pname = m.contents['name']
        elif self.capsuleType == 'msi':
            self.recipe.winHelper = WindowsHelper()
            if not self.recipe.cfg.windowsBuildService:
                raise SourceError('MSI capsules cannot be added without a '
                                  'windowsBuildService defined in the conary '
                                  'configuration')
            else:
                self.recipe.winHelper.extractMSIInfo(f,
                    self.recipe.cfg.windowsBuildService)
            self.recipe.winHelper.msiArgs = self.msiArgs
            pname = self.recipe.winHelper.name
        elif self.capsuleType == 'wim':
            self.recipe.winHelper = WindowsHelper()
            if not self.recipe.cfg.windowsBuildService:
                raise SourceError('WIM capsules cannot be added without a '
                                  'windowsBuildService defined in the conary '
                                  'configuration')
            else:
                self.recipe.winHelper.extractWIMInfo(f,
                    self.recipe.cfg.windowsBuildService,
                    volumeIndex=self.wimVolumeIndex)
            pname = self.recipe.winHelper.name
        else:
            raise SourceError('unknown capsule type %s', self.capsuleType)

        if self.package is None:
            self.package = pname + ':' + self.capsuleType
        else:
            p,c = util.splitExact(self.package, ':', 1)
            if not p:
                p = pname
            if not c:
                c = self.capsuleType
            self.package = '%s:%s' % (p,c)
        return f

    def do(self):
        # make sure the user gave a valid source, and not a directory
        baseFileName = os.path.basename(self.sourcename)
        if not baseFileName and not self.contents:
            raise SourceError('cannot specify a directory as input to '
                '%s' % self.__class__)
        log.info('adding capsule %s' %baseFileName)

        # normally destDir defaults to builddir (really) but in this
        # case it is actually macros.destdir
        destDir = self.recipe.macros.destdir

        f = self.doDownload()
        if f in self.recipe.capsuleFileSha1s:
            raise SourceError('cannot add the same capsule multiple times: '
                              '%s' % f)

        # If we just now figured out the package:component, we need to
        # initialize the manifest
        self._initManifest()

        if self.capsuleType == 'rpm':
            self.doRPM(f, destDir)
        elif self.capsuleType == 'msi':
            self.doMSI(f, destDir)
        elif self.capsuleType == 'wim':
            self.doWIM(f, destDir)

    def doRPM(self,f,destDir):
        # read ownership, permissions, file type, etc.
        ownerList = _extractFilesFromRPM(f, directory=destDir, action=self)

        sha1Map = {}
        totalPathList=[]
        totalPathData=[]
        ExcludeDirectories = []
        InitialContents = []
        Config = []
        MissingOkay = []

        for (path, user, group, mode, size,
             rdev, flags, vflags, digest, filelinktos, mtime) in ownerList:

            fullpath = util.joinPaths(destDir,path)

            totalPathList.append(path)
            # CNY-3304: some RPM versions allow impossible modes on symlinks
            if stat.S_ISLNK(mode):
                mode = stat.S_IFLNK | 0o777
            totalPathData.append((path, user, group, mode, digest, mtime))

            devtype = None
            if stat.S_ISBLK(mode):
                devtype = 'b'
            elif stat.S_ISCHR(mode):
                devtype = 'c'
            if devtype:
                minor = rdev & 0xff | (rdev >> 12) & 0xffffff00
                major = (rdev >> 8) & 0xfff
                self.recipe.MakeDevices(path, devtype, major, minor,
                                        user, group, mode=stat.S_IMODE(mode),
                                        package=self.package)

            if stat.S_ISDIR(mode):
                fullpath = os.sep.join((destDir, path))
                util.mkdirChain(fullpath)
                ExcludeDirectories.append( path )
            else:
                if flags & rpmhelper.RPMFILE_GHOST:
                    InitialContents.append(path)
                    # RPM does not actually create Ghost files but
                    # we need them for policy
                    fullpath = os.sep.join((destDir, path))
                    util.mkdirChain(os.path.dirname(fullpath))
                    if stat.S_ISREG(mode):
                        file(fullpath, 'w')
                    elif stat.S_ISLNK(mode):
                        if not filelinktos:
                            raise SourceError('Ghost Symlink in RPM has no target')
                        if util.exists(fullpath):
                            contents = os.readlink(fullpath)
                            if contents != filelinktos:
                                raise SourceError(
                                    "Inconsistent symlink contents for %s:"
                                    "'%s' != '%s'" % (
                                        path, contents, filelinktos))
                        else:
                            os.symlink(filelinktos, fullpath)
                    elif stat.S_ISFIFO(mode):
                        os.mkfifo(fullpath)
                    else:
                        raise SourceError('Unknown Ghost Filetype defined in RPM')
                elif flags & (rpmhelper.RPMFILE_CONFIG |
                              rpmhelper.RPMFILE_MISSINGOK |
                              rpmhelper.RPMFILE_NOREPLACE):
                    if size:
                        Config.append(path)
                    else:
                        InitialContents.append(path)
                elif vflags:
                    # CNY-3254: improve verification mapping; %doc are regular
                    if ((stat.S_ISREG(mode) and
                            not (vflags & rpmhelper.RPMVERIFY_FILEDIGEST))
                            or (stat.S_ISLNK(mode) and
                            not (vflags & rpmhelper.RPMVERIFY_LINKTO))):
                        InitialContents.append(path)

            if flags & rpmhelper.RPMFILE_MISSINGOK:
                MissingOkay.append(path)

            if stat.S_ISREG(mode) and util.exists(fullpath):
                sha1Map[path] = sha1helper.sha1FileBin(fullpath)


        if len(ExcludeDirectories):
            self.recipe.ExcludeDirectories(exceptions=filter.PathSet(
                ExcludeDirectories))

        if len(InitialContents):
            self.recipe.InitialContents(filter.PathSet(InitialContents))

        if len(Config):
            self.recipe.Config(filter.PathSet(Config))

        if len(MissingOkay):
            self.recipe.MissingOkay(filter.PathSet(MissingOkay))


        assert f not in self.recipe.capsuleFileSha1s
        self.recipe.capsuleFileSha1s[f] = sha1Map
        self.manifest.recordRelativePaths(totalPathList)
        self.manifest.create()
        self.recipe._validatePathInfoForCapsule(totalPathData,
            self.ignoreConflictingPaths)

        self.recipe._setPathInfoForCapsule(f, totalPathData, self.package)
        self.recipe._addCapsule(f, self.capsuleType, self.package, )

        # Now store script info:
        scriptDir = '/'.join((
            os.path.dirname(self.recipe.macros.destdir),
            '_CAPSULE_SCRIPTS_'))
        _extractScriptsFromRPM(f, scriptDir)

    def doMSI(self, f, destDir):
        totalPathList = []
        self.manifest.recordRelativePaths(totalPathList)
        self.manifest.create()
        self.recipe._addCapsule(f, self.capsuleType, self.package)

    def doWIM(self, f, destDir):
        totalPathList = []
        self.manifest.recordRelativePaths(totalPathList)
        self.manifest.create()
        self.recipe._addCapsule(f, self.capsuleType, self.package)

    def checkSignature(self, filepath):
        # generate the magic object in order to populate the capsuleType
        self._getCapsuleMagic(filepath)

        if self.keyid:
            key = self._getPublicKey()
            validKeys = [ key ]
        else:
            validKeys = None

        capsuleFileObj = util.ExtendedFile(filepath, buffering = False)
        if self.capsuleType == 'rpm':
            try:
                rpmhelper.verifySignatures(capsuleFileObj, validKeys)
            except rpmhelper.SignatureVerificationError as e:
                raise SourceError(str(e))
        elif self.capsuleType == 'msi':
            ### WRITE ME ###
            pass
        log.info('GPG signature for %s is OK', os.path.basename(filepath))


class addAction(action.RecipeAction):
    """
    NAME
    ====
    B{C{r.addAction()}} - Executes a shell command

    SYNOPSIS
    ========
    C{r.addAction([I{action},] [I{dir}=,] [I{package})=,] [I{use}=,])}

    DESCRIPTION
    ===========
    The C{r.addAction()} class executes a shell command during the source
    preparation stage, in a manner similar to C{r.Run}, except that
    C{r.Run} executes shell commands later, during the build stage.

    KEYWORDS
    ========
    The following keywords are recognized by C{r.addAction}:

    B{dir} : Specify a directory to change into prior to executing the
    command. An absolute directory specified as the C{dir} value
    is considered relative to C{%(destdir)s}.

    B{use} : A Use flag, or boolean, or a tuple of Use flags, and/or
    boolean values which determine whether the source code archive is
    actually unpacked or merely stored in the archive.

    B{package} : (None) If set, must be a string that specifies the package
    (C{package='packagename'}), component (C{package=':componentname'}), or
    package and component (C{package='packagename:componentname'}) in which
    to place the files added while executing this command.
    Previously-specified C{PackageSpec} or C{ComponentSpec} lines will
    override the package specification, since all package and component
    specifications are considered in strict order as provided by the recipe

    EXAMPLES
    ========
    The following examples demonstrate invocations of C{r.addAction}
    from within a recipe:

    C{r.addAction('sed -i "s/^SUBLEVEL.*/SUBLEVEL = %(sublevel)s/" Makefile')}

    Demonstrates use of a command line with macro interpolation upon the file
    C{Makefile}.

    C{r.addAction('mv lib/util/shhopt.h lib/util/pbmshhopt.h')}

    Demonstrates renaming a file via the C{mv} command.

    C{r.addAction('sh ./prep.sh', dir='/tmp')}

    Demonstrates changing into the directory C{%(destdir)s/var/log} and
    executing the script C{prep.sh}.
    """

    keywords = {'dir': '', 'package': None }

    def __init__(self, recipe, *args, **keywords):
        """
        @param recipe: The recipe object currently being built is provided
        automatically by the PackageRecipe object. Passing in  C{recipe} from
        within a recipe is unnecessary.
        @keyword dir: Specify a directory to change into prior to executing the
        command. An absolute directory specified as the C{dir} value
        is considered relative to C{%(destdir)s}.
        @keyword use: A Use flag, or boolean, or a tuple of Use flags, and/or
        boolean values which determine whether the source code archive is
        actually unpacked or merely stored in the archive.
        @keyword package: A string that specifies the package, component, or package
        and component in which to place the files added while executing this command
        """
        action.RecipeAction.__init__(self, recipe, *args, **keywords)
        self.action = args[0]

    def doDownload(self):
        return None

    def do(self):
        builddir = self.recipe.macros.builddir
        defaultDir = os.sep.join((builddir, self.recipe.theMainDir))
        destDir = action._expandOnePath(self.dir, self.recipe.macros,
                                                  defaultDir)
        util.mkdirChain(destDir)

        if self.package:
            self._initManifest()
        util.execute(self.action %self.recipe.macros, destDir)
        if self.package:
            self.manifest.create()

    def fetch(self, refreshFilter=None):
        return None
Action = addAction

class _RevisionControl(addArchive):

    keywords = {'dir': '',
                'package': None}

    def fetch(self, refreshFilter=None):
        fullPath = self.getFilename()
        url = 'lookaside:/' + fullPath
        reposPath = '/'.join(fullPath.split('/')[:-1] + [ self.name ])

        # don't look in the lookaside for a snapshot if we need to refresh
        # the lookaside
        if not refreshFilter or not refreshFilter(os.path.basename(fullPath)):
            ff = self.recipe.fileFinder
            inRepos, path = ff.fetch(url, allowNone=True,
                            refreshFilter=refreshFilter)
            if not inRepos:
                self.checkSignature(path)
            if path:
                return path

        # the source doesn't exist; we need to create the snapshot
        repositoryDir = self.recipe.laReposCache.getCachePath(self.recipe.name,
                                                  reposPath)
        del reposPath

        if not os.path.exists(repositoryDir):
            # get a new archive
            util.mkdirChain(os.path.dirname(repositoryDir))
            self.createArchive(repositoryDir)
        else:
            self.updateArchive(repositoryDir)

        self.showInfo(repositoryDir)

        path = self.recipe.laReposCache.getCachePath(self.recipe.name, fullPath)
        self.createSnapshot(repositoryDir, path)

        return path

    def showInfo(self, lookasideDir):
        # To be implemented in sub-classes
        pass

    def doDownload(self):
        return self.fetch()

class addGitSnapshot(_RevisionControl):

    """
    NAME
    ====
    B{C{r.addGitSnapshot()}} - Adds a snapshot from a git
    repository.

    SYNOPSIS
    ========
    C{r.addGitSnapshot([I{url},] [I{tag}=,] [I{branch}=,])}

    DESCRIPTION
    ===========
    The C{r.addGitSnapshot()} class extracts sources from a
    git repository, places a tarred, bzipped archive into
    the source component, and extracts that into the build directory
    in a manner similar to r.addArchive.

    KEYWORDS
    ========
    The following keywords are recognized by C{r.addAction}:

    B{dir} : Specify a directory to change into prior to executing the
    command. An absolute directory specified as the C{dir} value
    is considered relative to C{%(destdir)s}.

    B{package} : (None) If set, must be a string that specifies the package
    (C{package='packagename'}), component (C{package=':componentname'}), or
    package and component (C{package='packagename:componentname'}) in which
    to place the files added while executing this command.
    Previously-specified C{PackageSpec} or C{ComponentSpec} lines will
    override the package specification, since all package and component
    specifications are considered in strict order as provided by the recipe

    B{tag} : Git tag to use for the snapshot.

    B{branch} : Git branch to use for the snapshot.

    B{ephemeral} : If True, the snapshot will be recreated again at cook time
    rather than storing it in the repository in the source trove.  Use this
    only if the snapshot can be recreated precisely in the future.  For source
    control snapshots, this typically means that a precise revision has been
    passed in the B{tag} argument.  Only valid for URLs.
    """

    name = 'git'

    def getFilename(self):
        urlBits = self.url.split('//', 1)
        if len(urlBits) == 1:
            dirPath = self.url
        else:
            dirPath = urlBits[1]
        dirPath = dirPath.replace('/', '_')

        return '/%s/%s--%s--%s.tar.bz2' % (dirPath, self.url.split('/')[-1],
                self.branch, self.tag)

    def createArchive(self, lookasideDir):
        os.makedirs(lookasideDir)
        util.execute("cd '%s' && git init --bare -q" % (lookasideDir,))
        self.updateArchive(lookasideDir)

    def updateArchive(self, lookasideDir):
        log.info('Updating repository %s', self.url)
        util.execute("cd '%s' && git fetch -q '%s' +%s:%s" % (lookasideDir,
            self.url, self.branch, self.branch))

    def showInfo(self, lookasideDir):
        log.info('Most recent repository commit message:')
        util.execute("cd '%s' && git --no-pager log -1 '%s'" % (lookasideDir,
            self.branch))

    def createSnapshot(self, lookasideDir, target):
        if self.tag != 'HEAD':
            tag = self.tag
        else:
            tag = self.branch
        log.info('Creating repository snapshot for %s tag %s', self.url, tag)
        util.execute("cd '%s' && git archive --prefix=%s-%s/ %s | "
                        "bzip2 > '%s'" %
                        (lookasideDir, self.recipe.name, tag, tag, target))

    def __init__(self, recipe, url, tag='HEAD', branch='master', **kwargs):
        self.url = url % recipe.macros
        self.tag = tag % recipe.macros
        self.branch = branch % recipe.macros
        sourceName = 'lookaside:/' + self.getFilename()
        _RevisionControl.__init__(self, recipe, sourceName, **kwargs)


class addMercurialSnapshot(_RevisionControl):

    """
    NAME
    ====
    B{C{r.addMercurialSnapshot()}} - Adds a snapshot from a mercurial
    repository.

    SYNOPSIS
    ========
    C{r.addMercurialSnapshot([I{url},] [I{tag}=,])}

    DESCRIPTION
    ===========
    The C{r.addMercurialSnapshot()} class extracts sources from a
    mercurial repository, places a tarred, bzipped archive into
    the source component, and extracts that into the build directory
    in a manner similar to r.addArchive.

    KEYWORDS
    ========
    The following keywords are recognized by C{r.addAction}:

    B{dir} : Specify a directory to change into prior to executing the
    command. An absolute directory specified as the C{dir} value
    is considered relative to C{%(destdir)s}.

    B{package} : (None) If set, must be a string that specifies the package
    (C{package='packagename'}), component (C{package=':componentname'}), or
    package and component (C{package='packagename:componentname'}) in which
    to place the files added while executing this command.
    Previously-specified C{PackageSpec} or C{ComponentSpec} lines will
    override the package specification, since all package and component
    specifications are considered in strict order as provided by the recipe

    B{tag} : Mercurial tag to use for the snapshot.

    B{ephemeral} : If True, the snapshot will be recreated again at cook time
    rather than storing it in the repository in the source trove.  Use this
    only if the snapshot can be recreated precisely in the future.  For source
    control snapshots, this typically means that a precise revision has been
    passed in the B{tag} argument.  Only valid for URLs.
    """

    name = 'hg'

    def getFilename(self):
        urlBits = self.url.split('//', 1)
        if len(urlBits) == 1:
            dirPath = self.url
        else:
            dirPath = urlBits[1]
        dirPath = dirPath.replace('/', '_')

        return '/%s/%s--%s.tar.bz2' % (dirPath, self.url.split('/')[-1],
                                       self.tag)

    def createArchive(self, lookasideDir):
        log.info('Cloning repository from %s', self.url)
        util.execute('hg -q clone -U %s \'%s\'' % (self.url, lookasideDir))

    def updateArchive(self, lookasideDir):
        log.info('Updating repository %s', self.url)
        util.execute("cd '%s' && hg -q pull '%s'" % (lookasideDir, self.url))

    def showInfo(self, lookasideDir):
        log.info('Most recent repository commit message:')
        util.execute("cd '%s' && hg log --limit 1" % lookasideDir)

    def createSnapshot(self, lookasideDir, target):
        log.info('Creating repository snapshot for %s tag %s', self.url,
                 self.tag)
        util.execute("cd '%s' && hg archive -r '%s' -t tbz2 '%s'" %
                        (lookasideDir, self.tag, target))

    def __init__(self, recipe, url, tag = 'tip', **kwargs):
        self.url = url % recipe.macros
        self.tag = tag % recipe.macros
        sourceName = 'lookaside:/' + self.getFilename()
        _RevisionControl.__init__(self, recipe, sourceName, **kwargs)

class addCvsSnapshot(_RevisionControl):

    """
    NAME
    ====
    B{C{r.addCvsSnapshot()}} - Adds a snapshot from a CVS
    repository.

    SYNOPSIS
    ========
    C{r.addCvsSnapshot([I{root},] [I{project},] [I{tag}=,])}

    DESCRIPTION
    ===========
    The C{r.addCvsSnapshot()} class extracts sources from a
    CVS repository, places a tarred, bzipped archive into
    the source component, and extracts that into the build directory
    in a manner similar to r.addArchive.

    KEYWORDS
    ========
    The following keywords are recognized by C{r.addAction}:

    B{dir} : Specify a directory to change into prior to executing the
    command. An absolute directory specified as the C{dir} value
    is considered relative to C{%(destdir)s}.

    B{package} : (None) If set, must be a string that specifies the package
    (C{package='packagename'}), component (C{package=':componentname'}), or
    package and component (C{package='packagename:componentname'}) in which
    to place the files added while executing this command.
    Previously-specified C{PackageSpec} or C{ComponentSpec} lines will
    override the package specification, since all package and component
    specifications are considered in strict order as provided by the recipe

    B{tag} : CVS tag to use for the snapshot.

    B{ephemeral} : If True, the snapshot will be recreated again at cook time
    rather than storing it in the repository in the source trove.  Use this
    only if the snapshot can be recreated precisely in the future.  For source
    control snapshots, this typically means that a precise revision has been
    passed in the B{tag} argument.  Only valid for URLs.
    """

    name = 'cvs'

    def getFilename(self):
        s = '%s/%s--%s.tar.bz2' % (self.root, self.project, self.tag)
        if s[0] == '/':
            s = s[1:]

        return s

    def createArchive(self, lookasideDir):
        # cvs export always downloads from the repository, no need to cache
        return

    def updateArchive(self, lookasideDir):
        # cvs export always downloads from the repository, no need to cache
        return

    def createSnapshot(self, lookasideDir, target):
        log.info('Creating repository snapshot for %s tag %s', self.project,
                 self.tag)
        tmpPath = tempfile.mkdtemp()
        dirName = self.project + '--' + self.tag
        stagePath = tmpPath + os.path.sep + dirName
        # don't use cvs export -d <dir> as it is fragile
        util.mkdirChain(stagePath)
        util.execute(("cd %%s && cvs -Q -d '%%s' export -r '%%s' '%%s' && cd '%%s/%%s' && "
                  "%(tar)s cjf '%%s' '%%s'" % self.recipe.macros) % 
                        (stagePath, self.root, self.tag, self.project,
                         tmpPath, dirName, target, self.project))
        shutil.rmtree(tmpPath)

    def __init__(self, recipe, root, project, tag = 'HEAD', **kwargs):
        self.root = root % recipe.macros
        self.project = project % recipe.macros
        self.tag = tag % recipe.macros
        sourceName = 'lookaside:/' + self.getFilename()
        _RevisionControl.__init__(self, recipe, sourceName, **kwargs)

class addSvnSnapshot(_RevisionControl):

    """
    NAME
    ====
    B{C{r.addSvnSnapshot()}} - Adds a snapshot from a subversion
    repository.

    SYNOPSIS
    ========
    C{r.addSvnSnapshot([I{url},] [I{project}=,])}

    DESCRIPTION
    ===========
    The C{r.addSvnSnapshot()} class extracts sources from a
    subversion repository, places a tarred, bzipped archive into
    the source component, and extracts that into the build directory
    in a manner similar to r.addArchive.

    KEYWORDS
    ========
    The following keywords are recognized by C{r.addAction}:

    B{dir} : Specify a directory to change into prior to executing the
    command. An absolute directory specified as the C{dir} value
    is considered relative to C{%(destdir)s}.

    B{package} : (None) If set, must be a string that specifies the package
    (C{package='packagename'}), component (C{package=':componentname'}), or
    package and component (C{package='packagename:componentname'}) in which
    to place the files added while executing this command.
    Previously-specified C{PackageSpec} or C{ComponentSpec} lines will
    override the package specification, since all package and component
    specifications are considered in strict order as provided by the recipe

    B{ephemeral} : If True, the snapshot will be recreated again at cook time
    rather than storing it in the repository in the source trove.  Use this
    only if the snapshot can be recreated precisely in the future.  For source
    control snapshots, this typically means that a precise revision has been
    passed in the B{tag} argument.  Only valid for URLs.
    """

    name = 'svn'

    def getFilename(self):
        urlBits = self.url.split('//', 1)
        dirPath = urlBits[1].replace('/', '_')

        # we need to preserve backwards compatibility with conarys (conaries?)
        # prior to 1.2.3, which do not have a revision tag. Without this bit,
        # conary 1.2.3+ will see sources committed with <=1.2.2 as not having
        # the svn tarball stored correctly
        if self.revision == 'HEAD':
            denoteRevision = ''
        else:
            denoteRevision = '-revision-%s' % self.revision

        return '/%s/%s--%s%s.tar.bz2' % (dirPath, self.project,
                        self.url.split('/')[-1], denoteRevision)

    def createArchive(self, lookasideDir):
        os.mkdir(lookasideDir)
        log.info('Checking out %s, revision %s' % (self.url, self.revision))
        util.execute('svn --quiet checkout --revision \'%s\' \'%s\' \'%s\''
                     % (self.revision, self.url, lookasideDir))

    def updateArchive(self, lookasideDir):
        log.info('Updating repository %s to revision %s'
                  % (self.project,self.revision))
        util.execute('cd \'%s\' && svn --quiet update --revision \'%s\''
                      % ( lookasideDir, self.revision ))

    def showInfo(self, lookasideDir):
        log.info('Most recent repository commit message:')
        util.execute("svn log --limit 1 '%s'" % lookasideDir)

    def createSnapshot(self, lookasideDir, target):
        log.info('Creating repository snapshot for %s, revision %s'
                  % (self.url, self.revision))
        tmpPath = tempfile.mkdtemp()
        stagePath = tmpPath + '/' + self.project + '--' + \
                            self.url.split('/')[-1]
        util.execute(("svn --quiet export --revision '%%s' '%%s' '%%s' && cd "
                  "'%%s' && %(tar)s cjf '%%s' '%%s'" % self.recipe.macros) %
                        (self.revision, lookasideDir, stagePath,
                         tmpPath, target, os.path.basename(stagePath)))
        shutil.rmtree(tmpPath)

    def __init__(self, recipe, url, project = None, revision = 'HEAD', **kwargs):
        self.url = url % recipe.macros
        self.revision = revision % recipe.macros
        if project is None:
            self.project = recipe.name
        else:
            self.project = project % recipe.macros
        sourceName = 'lookaside:/' + self.getFilename()

        _RevisionControl.__init__(self, recipe, sourceName, **kwargs)

class addBzrSnapshot(_RevisionControl):

    """
    NAME
    ====
    B{C{r.addBzrSnapshot()}} - Adds a snapshot from a bzr repository.

    SYNOPSIS
    ========
    C{r.addBzrSnapshot([I{url},] [I{tag}=,])}

    DESCRIPTION
    ===========
    The C{r.addBzrSnapshot()} class extracts sources from a
    bzr repository, places a tarred, bzipped archive into
    the source component, and extracts that into the build directory
    in a manner similar to r.addArchive.

    KEYWORDS
    ========
    The following keywords are recognized by C{r.addBzrSnapshot}:

    B{tag} : Specify a specific tagged revision to checkout.

    B{ephemeral} : If True, the snapshot will be recreated again at cook time
    rather than storing it in the repository in the source trove.  Use this
    only if the snapshot can be recreated precisely in the future.  For source
    control snapshots, this typically means that a precise revision has been
    passed in the B{tag} argument.  Only valid for URLs.
    """

    name = 'bzr'

    def getFilename(self):
        urlBits = self.url.split('//', 1)
        if len(urlBits) == 1:
            dirPath = self.url
        else:
            dirPath = urlBits[1]
        dirPath = dirPath.replace('/', '_')

        return '/%s/%s--%s.tar.bz2' % (dirPath, self.url.split('/')[-1],
                                       self.tag or '')

    def createArchive(self, lookasideDir):
        log.info('Cloning repository from %s', self.url)
        util.execute('bzr branch \'%s\' \'%s\'' % (self.url, lookasideDir))

    def updateArchive(self, lookasideDir):
        log.info('Updating repository %s', self.url)
        util.execute("cd '%s' && bzr pull %s --overwrite %s && bzr update" % \
                (lookasideDir, self.url, self.tagArg))

    def showInfo(self, lookasideDir):
        log.info('Repository commit message:')
        if self.tag:
            self.revno=self.tag
        else:
            self.revno='-1'
        util.execute("bzr log -r '%s' --long '%s'" % (self.revno, lookasideDir))

    def createSnapshot(self, lookasideDir, target):
        log.info('Creating repository snapshot for %s %s', self.url,
                 self.tag and 'tag %s' % self.tag or '')
        util.execute("cd '%s' && bzr export %s '%s'" %
                        (lookasideDir, self.tagArg, target))

    def __init__(self, recipe, url, tag = None, **kwargs):
        self.url = url % recipe.macros
        if tag:
            self.tag = tag % recipe.macros
            self.tagArg = '-r %s' % self.tag
        else:
            self.tag = tag
            self.tagArg = ''
        sourceName = 'lookaside:/' + self.getFilename()

        _RevisionControl.__init__(self, recipe, sourceName, **kwargs)

class TroveScript(_AnySource):

    keywords = { 'contents' : None,
                 'groupName' : None }
    _packageAction = False
    _groupAction = True
    _scriptName = None
    _compatibilityMap = None

    def __init__(self, recipe, *args, **keywords):
        _AnySource.__init__(self, recipe, *args, **keywords)
        if args:
            self.sourcename = args[0]
        else:
            self.sourcename = ''

        if not self.sourcename and not self.contents:
            raise RecipeFileError('no contents given for group script')
        elif self.sourcename and self.contents:
            raise RecipeFileError('both contents and filename given for '
                                  'group script')

    def fetch(self, refreshFilter=None):
        if self.contents is None:
            f = self.recipe._fetchFile(self.sourcename,
                refreshFilter=refreshFilter)
            if f is None:
                raise RecipeFileError('file "%s" not found for group script' %
                                      self.sourcename)
            self.contents = open(f).read()
            return f

    def fetchLocal(self):
        # Used by rMake to find files that are not autosourced.
        if self.contents is None:
            return self.recipe._fetchFile(self.sourcename, localOnly = True)

    def getPath(self):
        return self.sourcename

    def getPathAndSuffix(self):
        return self.sourcename, None, None

    def doDownload(self):
        return self.fetch()

    def do(self):
        self.doDownload()
        self.recipe._addScript(self.contents, self.groupName, self._scriptName,
                               fromClass = self._compatibilityMap)

class addPostInstallScript(TroveScript):

    """
    NAME
    ====
    B{C{r.addPostInstallScript()}} - Specify the post install script for a trove.

    SYNOPSIS
    ========
    C{r.addPostInstallScript(I{sourcename}, [I{contents},] [I{groupName}]}

    DESCRIPTION
    ===========
    The C{r.addPostInstallScript} command specifies the post install script
    for a group. This script is run after the group has been installed
    for the first time (not when the group is being upgraded from a
    previously installed version to version defining the script).

    PARAMETERS
    ==========
    The C{r.addPostInstallScript()} command accepts the following parameters,
    with default values shown in parentheses:

    B{contents} : (None) The contents of the script
    B{groupName} : (None) The name of the group to add the script to
    """

    _scriptName = 'postInstallScripts'

class addPreRollbackScript(TroveScript):

    """
    NAME
    ====
    B{C{r.addPreRollbackScript()}} - Specify the pre rollback script for a trove.

    SYNOPSIS
    ========
    C{r.addPreRollbackScript(I{sourcename}, [I{contents},] [I{groupName}]}

    DESCRIPTION
    ===========
    The C{r.addPreRollbackScript} command specifies the pre rollback script
    for a group. This script is run before the group defining the script
    has been rolled back to a previously-installed version of the group.

    PARAMETERS
    ==========
    The C{r.addPreRollbackScript()} command accepts the following parameters,
    with default values shown in parentheses:

    B{contents} : (None) The contents of the script
    B{groupName} : (None) The name of the group to add the script to
    """

    _scriptName = 'preRollbackScripts'


class addPostRollbackScript(TroveScript):

    """
    NAME
    ====
    B{C{r.addPostRollbackScript()}} - Specify the post rollback script for a trove.

    SYNOPSIS
    ========
    C{r.addPostRollbackScript(I{sourcename}, I[{contents},] [I{groupName}]}

    DESCRIPTION
    ===========
    The C{r.addPostRollbackScript} command specifies the post rollback
    script for a group. This script is run after the group defining the
    script has been rolled back to a previous version of the group.

    PARAMETERS
    ==========
    The C{r.addPostRollbackScript()} command accepts the following parameters,
    with default values shown in parentheses:

    B{contents} : (None) The contents of the script
    B{groupName} : (None) The name of the group to add the script to
    B{toClass} : (None) The trove compatibility classes this script
    is able to support rollbacks to. This may be a single integer
    or a list of integers.
    """

    _scriptName = 'postRollbackScripts'
    keywords = dict(TroveScript.keywords)
    keywords['toClass'] = None

    def __init__(self, *args, **kwargs):
        TroveScript.__init__(self, *args, **kwargs)
        if self.toClass:
            self._compatibilityMap = self.toClass

class addPostUpdateScript(TroveScript):

    """
    NAME
    ====
    B{C{r.addPostUpdateScript()}} - Specify the post update script for a trove.

    SYNOPSIS
    ========
    C{r.addPostUpdateScript(I{sourcename}, [I{contents},] [I{groupName}]}

    DESCRIPTION
    ===========
    The C{r.addPostUpdateScript} command specifies the post update script
    for a group. This script is run after the group has been updated from
    a previously-installed version to the version defining the script.

    PARAMETERS
    ==========
    The C{r.addPostUpdateScript()} command accepts the following parameters,
    with default values shown in parentheses:

    B{contents} : (None) The contents of the script
    B{groupName} : (None) The name of the group to add the script to
    """

    _scriptName = 'postUpdateScripts'

class addPreUpdateScript(TroveScript):

    """
    NAME
    ====
    B{C{r.addPreUpdateScript()}} - Specify the pre update script for a trove.

    SYNOPSIS
    ========
    C{r.addPreUpdateScript(I{sourcename}, [I{contents},] [I{groupName}]}

    DESCRIPTION
    ===========
    The C{r.addPreUpdateScript} command specifies the pre update script
    for a group. This script is run before the group is updated from
    a previously-installed version to the version defining the script.

    PARAMETERS
    ==========
    The C{r.addPreUpdateScript()} command accepts the following parameters,
    with default values shown in parentheses:

    B{contents} : (None) The contents of the script
    B{groupName} : (None) The name of the group to add the script to
    """

    _scriptName = 'preUpdateScripts'

def _extractScriptsFromRPM(rpm, directory):
    r = file(rpm, 'r')
    h = rpmhelper.readHeader(r)

    baseDir = '/'.join((directory, os.path.basename(rpm), ''))
    util.mkdirChain(baseDir[:-1])

    scripts = (
        ('prein', rpmhelper.PREIN, rpmhelper.PREINPROG),
        ('postin', rpmhelper.POSTIN, rpmhelper.POSTINPROG),
        ('preun', rpmhelper.PREUN, rpmhelper.PREUNPROG),
        ('postun', rpmhelper.POSTUN, rpmhelper.POSTUNPROG),
        ('verify', rpmhelper.VERIFYSCRIPT, rpmhelper.VERIFYSCRIPTPROG),
    )
    for scriptName, tag, progTag in scripts:
        if tag in h or progTag in h:
            scriptFile = file('/'.join((baseDir, scriptName)), 'w')
            if progTag in h:
                scriptFile.write('#!%s\n' %str(h[progTag]))
            if tag in h:
                scriptFile.write(str(h[tag]))
                scriptFile.write('\n')
            scriptFile.close()

    if rpmhelper.TRIGGERSCRIPTS not in h:
        return


    triggerTypes = {
        rpmhelper.RPMSENSE_TRIGGERIN:     'triggerin',
        rpmhelper.RPMSENSE_TRIGGERUN:     'triggerun',
        rpmhelper.RPMSENSE_TRIGGERPOSTUN: 'triggerpostun',
        rpmhelper.RPMSENSE_TRIGGERPREIN:  'triggerprein',
    }
    triggerMask = (rpmhelper.RPMSENSE_TRIGGERIN|
                   rpmhelper.RPMSENSE_TRIGGERUN|
                   rpmhelper.RPMSENSE_TRIGGERPOSTUN|
                   rpmhelper.RPMSENSE_TRIGGERPREIN)
    verCmpTypes = {
        rpmhelper.RPMSENSE_LESS:    '< ',
        rpmhelper.RPMSENSE_GREATER: '> ',
        rpmhelper.RPMSENSE_EQUAL:   '= ',
    }
    verCmpMask = (rpmhelper.RPMSENSE_LESS|
                  rpmhelper.RPMSENSE_GREATER|
                  rpmhelper.RPMSENSE_EQUAL)

    triggers = zip(h[rpmhelper.TRIGGERSCRIPTS],
                              h[rpmhelper.TRIGGERNAME],
                              h[rpmhelper.TRIGGERVERSION],
                              h[rpmhelper.TRIGGERFLAGS],
                              h[rpmhelper.TRIGGERINDEX],
                              h[rpmhelper.TRIGGERSCRIPTPROG],
                              )
    for script, tname, tver, tflag, ti, tprog in triggers:
        triggerType = tflag & triggerMask
        triggerType = triggerTypes.get(triggerType, 'unknown')
        scriptName = 'trigger_%s_%s_%d' %(triggerType, str(tname), ti)
        scriptFile = file('/'.join((baseDir, scriptName)), 'w')
        scriptFile.write('#!%s\n' %tprog)
        scriptFile.write('TYPE="%s"\n' %triggerType)
        scriptFile.write('ID="%d"\n' %ti)
        scriptFile.write('NAME="%s"\n' %str(tname))
        cmpSense = tflag & verCmpMask
        cmpSense = verCmpTypes.get(cmpSense, '')
        scriptFile.write('VERSIONCMP="%s%s"\n' %(cmpSense, str(tver)))
        scriptFile.write(str(script))
        scriptFile.write('\n')
        scriptFile.close()


_forbiddenRPMTags = (
    # RPM tags that we do not currently handle and want to raise an
    # error rather than packaging (possibly incorrectly)
    ('BLINKPKGID', rpmhelper.BLINKPKGID),
    ('BLINKHDRID', rpmhelper.BLINKHDRID),
    ('BLINKNEVRA', rpmhelper.BLINKNEVRA),
    ('FLINKPKGID', rpmhelper.BLINKPKGID),
    ('FLINKHDRID', rpmhelper.BLINKHDRID),
    ('FLINKNEVRA', rpmhelper.BLINKNEVRA),
)

def _extractFilesFromRPM(rpm, targetfile=None, directory=None, action=None):
    assert targetfile or directory
    if not directory:
        directory = os.path.dirname(targetfile)
    r = file(rpm, 'r')
    h = rpmhelper.readHeader(r)

    # CNY-3404
    forbiddenTags = [(tagName, tag) for (tagName, tag) in _forbiddenRPMTags
                     if tag in h]
    if forbiddenTags:
        raise SourceError('Unhandled RPM tags: %s ' %
            ', '.join(('%s(%d)'%x for x in forbiddenTags)))

    # The rest of this function gets information on the files stored
    # in an RPM.  Some RPMs intentionally contain no files, and
    # therefore have no files and no file-related data, but are
    # still meaningful.
    if rpmhelper.FILEUSERNAME not in h:
        return []

    cpioArgs = ['/bin/cpio', 'cpio', '-iumd', '--quiet']

    # tell cpio to skip directories; we let cpio create those automatically
    # rather than based on the cpio to make sure they aren't made with funny
    # permissions. worst bit is that we have to use DIR, ./DIR, and /DIR
    # because RPM is inconsistent with how it names things in the cpio ball

    cpioSkipArgs = ['-f']
    for (path, mode) in zip(h[rpmhelper.OLDFILENAMES],
                                       h[rpmhelper.FILEMODES]):
        if (stat.S_ISDIR(mode) or stat.S_ISBLK(mode) or
                stat.S_ISCHR(mode)):
            if stat.S_ISDIR(mode):
                util.mkdirChain(directory + path)
            cpioSkipArgs.append(path)
            cpioSkipArgs.append('.' + path)
            cpioSkipArgs.append(path[1:])
    if len(cpioSkipArgs) > 1:
        cpioArgs.extend(cpioSkipArgs)

    if targetfile:
        if os.path.exists(targetfile):
            os.remove(targetfile)
        filename = os.path.basename(targetfile)
        cpioArgs.append(filename)
        errorMessage = 'extracting %s from RPM %s' %(
            filename, os.path.basename(rpm))
    else:
        errorMessage = 'extracting RPM %s' %os.path.basename(rpm)

    # assemble the path/owner/group/etc list
    ownerList = list(zip(h[rpmhelper.OLDFILENAMES],
                                    h[rpmhelper.FILEUSERNAME],
                                    h[rpmhelper.FILEGROUPNAME],
                                    h[rpmhelper.FILEMODES],
                                    h[rpmhelper.FILESIZES],
                                    h[rpmhelper.FILERDEVS],
                                    h[rpmhelper.FILEFLAGS],
                                    h[rpmhelper.FILEVERIFYFLAGS],
                                    h[rpmhelper.FILEDIGESTS],
                                    h[rpmhelper.FILELINKTOS],
                                    h[rpmhelper.FILEMTIMES],
                                    ))

    uncompressed = rpmhelper.UncompressedRpmPayload(r)
    if isinstance(uncompressed, util.LZMAFile):
        if action is not None:
            action._addActionPathBuildRequires([uncompressed.executable])
    (rpipe, wpipe) = os.pipe()
    pid = os.fork()
    if not pid:
        try:
            try:
                os.close(wpipe)
                os.dup2(rpipe, 0)
                os.chdir(directory)
                util.massCloseFileDescriptors(3, 252)
                os.execl(*cpioArgs)
            except Exception as e:
                print('Could not execute %s: %s' % (cpioArgs[0], e))
                os.close(rpipe)
        finally:
            os._exit(1)
    os.close(rpipe)
    while 1:
        buf = uncompressed.read(16384)
        if not buf:
            break
        try:
            os.write(wpipe, buf)
        except OSError:
            break
    os.close(wpipe)
    (pid, status) = os.waitpid(pid, 0)
    if not os.WIFEXITED(status):
        raise IOError('cpio died %s' %errorMessage)
    if os.WEXITSTATUS(status):
        raise IOError('cpio returned failure %d %s' %(
                os.WEXITSTATUS(status), errorMessage))
    if targetfile and not os.path.exists(targetfile):
        raise IOError('failed to extract source %s from RPM %s' \
                       %(filename, os.path.basename(rpm)))

    return ownerList

def _extractFilesFromISO(iso, directory):
    isoInfo = util.popen("isoinfo -d -i '%s'" %iso).read()
    JolietRE = re.compile('Joliet.*found')
    RockRidgeRE = re.compile('Rock Ridge.*found')
    if JolietRE.search(isoInfo):
        isoType = '-J'
    elif RockRidgeRE.search(isoInfo):
        isoType = '-R'
    else:
        raise IOError('ISO %s contains neither Joliet nor Rock Ridge info'
                      %iso)

    log.info("extracting ISO %s into %s" % (iso, directory))
    filenames = util.popen("isoinfo -i '%s' '%s' -f" %(iso, isoType)).readlines()
    filenames = [ x.strip() for x in filenames ]

    for filename in filenames:
        r = util.popen("isoinfo '%s' -i '%s' -x '%s'" %(isoType, iso, filename))
        fullpath = '/'.join((directory, filename))
        dirName = os.path.dirname(fullpath)
        if not util.exists(dirName):
            os.makedirs(dirName)
        else:
            if not os.path.isdir(dirName):
                os.remove(dirName)
                os.makedirs(dirName)
        w = file(fullpath, "w")
        while 1:
            buf = r.read(16384)
            if not buf:
                break
            w.write(buf)
        w.close()



class SourceError(errors.CookError):
    """
    Base class from which source error classes inherit
    """
    def __init__(self, msg, *args):
        self.msg = msg %args

    def __repr__(self):
        return self.msg

    def __str__(self):
        return repr(self)
