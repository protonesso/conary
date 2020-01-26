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
Provides a cache for storing files locally, including
downloads and unpacking layers of files.
"""
import base64
import http.cookiejar
import errno
import os
import socket
import time
import urllib.request, urllib.parse, urllib.error
import urllib.request, urllib.error, urllib.parse
import copy

from conary.lib import log
from conary.lib import sha1helper
from conary.lib import util
from conary import callbacks
from conary.build.mirror import Mirror
from conary.conaryclient.callbacks import FetchCallback, ChangesetCallback

NETWORK_SCHEMES = ('http', 'https', 'ftp', 'mirror')

NEGATIVE_CACHE_TTL = 60 * 60  # The TTL for negative cache entries (seconds)


class laUrl(object):
    '''This object splits a url string into its various components and stores
    them as named attributes.  It also has several feature that are specific to
    the lookaside cache.'''

    supportedSchemes = set(('file', 'ftp', 'gopher', 'hdl', 'http', 'https',
                            'imap', 'mailto', 'mms', 'news', 'nntp',
                            'prospero', 'rsync', 'rtsp', 'rtspu', 'sftp',
                            'shttp', 'sip', 'sips', 'snews', 'svn', 'svn+ssh',
                            'telnet', 'wais'))

    def __init__(self, urlString, parent=None, extension=None,
                 isHostname=False):
        urlString = urllib.parse.unquote(urlString)
        (self.scheme, self.user, self.passwd, self.host, self.port,
         self.path, self.params, self.fragment) = util.urlSplit(urlString)

        if parent:
            self.path = os.sep.join((self.path, parent.path))
            self.path = self.path.replace('//', '/')
            self.extension = parent.extension
        self.parent = parent
        assert self.parent is not self
        self.extension = extension

    def asStr(self, noAuth=False, quoted=False):
        path = self.path
        if self.extension:
            path += '.' + self.extension

        if quoted:
            path = urllib.parse.quote(path)

        if noAuth:
            return util.urlUnsplit((self.scheme, None, None,
                                     self.host, self.port, path,
                                     self.params, self.fragment))
        return util.urlUnsplit((self.scheme, self.user, self.passwd,
                                 self.host, self.port, path,
                                 self.params, self.fragment))

    def __str__(self):
        return self.asStr()

    def __repr__(self):
        return "<%s.%s instance at %#x; url=%s>" % (
            self.__class__.__module__, self.__class__.__name__,
            id(self), self.asStr())

    def getHostAndPath(self, useParentPath=True):
        if self.parent and useParentPath:
            return self.parent.getHostAndPath()

        host = self.host
        if self.port:
            host = self.host + ":" + str(self.port)
        path = self.path + (self.params and '?%s'
                            % self.params or '')
        fragment = self.fragment

        return (host, path, fragment)

    def filePath(self, useParentPath=True):
        (host, path, fragment) = self.getHostAndPath(useParentPath)

        if self.extension:
            path += '.' + self.extension
        if fragment:
            path += "#" + fragment

        path = path.replace('/../', '/_../')
        if path[0] == '/':
            path = path[1:]

        if host:
            return os.path.join('/', host, path)
        return path

    def explicit(self):
        return self.scheme not in ['mirror', 'multiurl']

    @property
    def basename(self):
        return os.path.basename(self.getHostAndPath()[1])


def checkRefreshFilter(refreshFilter, url):
    if not refreshFilter:
        return False
    if refreshFilter(str(url)):
        return True
    if refreshFilter(url.basename):
        return True
    return False


def searchAll(cfg, repCache, name, location, srcdirs, autoSource=False,
              httpHeaders={}, localOnly=False):
    #some recipes reach into Conary internals here, and have references
    #to searchAll

    return findAll(cfg, repCache, name, location, srcdirs, autoSource,
                   httpHeaders, localOnly, allowNone=True)


def findAll(cfg, repCache, name, location, srcdirs, autoSource=False,
            httpHeaders={}, localOnly=False, guessName=None, suffixes=None,
            allowNone=False, refreshFilter=None, multiurlMap=None,
            unifiedSourcePath=None):
    # this is a bw compatible findAll method.
    if guessName:
        name = name + guessName
    ff = FileFinder(recipeName=location, repositoryCache=repCache,
                          localDirs=srcdirs, multiurlMap=multiurlMap,
                          mirrorDirs=cfg.mirrorDirs,
                          cfg=cfg)

    if localOnly:
        if srcdirs:
            searchMethod = ff.SEARCH_LOCAL_ONLY
        else:
            # BW COMPATIBLE HACK - since we know we aren't actually searching
            # srcdirs since they're empty, we take this to mean
            # repository only.
            searchMethod = ff.SEARCH_REPOSITORY_ONLY
    elif autoSource:
        searchMethod = ff.SEARCH_REPOSITORY_ONLY
    else:
        searchMethod = ff.SEARCH_ALL

    results = ff.fetch(name, suffixes=suffixes, archivePath=unifiedSourcePath,
                       allowNone=allowNone, searchMethod=searchMethod,
                       headers=httpHeaders, refreshFilter=refreshFilter)
    return results[1]


def fetchURL(cfg, name, location, httpHeaders={}, guessName=None, mirror=None):
    #this is a backwards compatible fetchURL method
    repCache = RepositoryCache(None, cfg=cfg)
    ff = FileFinder(recipeName=location, repositoryCache=repCache,
                    cfg=cfg)

    try:
        url = laUrl(name)
        return ff.searchNetworkSources(url, headers=httpHeaders, single=True)
    except PathFound as pathInfo:
        return pathInfo.path


class FileFinder(object):

    SEARCH_ALL = 0
    SEARCH_REPOSITORY_ONLY = 1
    SEARCH_LOCAL_ONLY = 2

    def __init__(self, recipeName, repositoryCache, localDirs=None,
                 multiurlMap=None, refreshFilter=None, mirrorDirs=None,
                 cfg=None):
        self.cfg = cfg
        self.recipeName = recipeName
        self.repCache = repositoryCache
        if self.repCache:
            self.repCache.setConfig(cfg)
        if localDirs is None:
            localDirs = []
        self.localDirs = localDirs
        self.multiurlMap = multiurlMap
        self.mirrorDirs = mirrorDirs
        self.noproxyFilter = util.noproxyFilter()

    def fetch(self, urlStr, suffixes=None, archivePath=None, headers=None,
              allowNone=False, searchMethod=0,  # SEARCH_ALL
              refreshFilter=None):

        urlList = self._getPathsToSearch(urlStr, suffixes)
        single = len(urlList) == 1
        for url in urlList:
            try:
                self._fetch(url,
                            archivePath, headers=headers,
                            refreshFilter=refreshFilter,
                            searchMethod=searchMethod,
                            single=single,
                            )
            except PathFound as pathInfo:
                return pathInfo.isFromRepos, pathInfo.path

        # we didn't find any matching url.
        if not allowNone:
            raise OSError(errno.ENOENT, os.strerror(errno.ENOENT),
                          urlStr)
        return None, None

    def _fetch(self, url, archivePath, searchMethod, headers=None,
               refreshFilter=None, single=False):
        if isinstance(url, str):
            url = laUrl(url)

        refresh = checkRefreshFilter(refreshFilter, url)
        if searchMethod == self.SEARCH_LOCAL_ONLY:
            self.searchFilesystem(url)
            return
        elif searchMethod == self.SEARCH_REPOSITORY_ONLY:
            if archivePath:
                self.searchArchive(archivePath, url)
            elif refresh:
                self.searchNetworkSources(url, headers, single)
            self.searchRepository(url)
        else:  # SEARCH_ALL
            self.searchFilesystem(url)
            if archivePath:
                self.searchArchive(archivePath, url)
            elif refresh:
                self.searchNetworkSources(url, headers, single)
            self.searchRepository(url)
            self.searchLocalCache(url)
            self.searchNetworkSources(url, headers, single)

    def searchRepository(self, url):
        if self.repCache.hasFilePath(url):
            log.info('found %s in repository', url.asStr(noAuth=True))
            path = self.repCache.cacheFilePath(self.recipeName, url)
            raise PathFound(path, True)

    def searchLocalCache(self, url):
        # exact match first, then look for cached responses from other servers
        path = self.repCache.getCacheEntry(self.recipeName, url)
        if path:
            raise PathFound(path, False)

    def searchFilesystem(self, url):
        if url.filePath() == '/':
            return
        path = util.searchFile(url.filePath(), self.localDirs)

        if path:
            raise PathFound(path, False)

    def searchArchive(self, archiveName, url):
        path = self.repCache.getArchiveCacheEntry(archiveName, url)
        if path:
            raise PathFound(path, True)

    def searchNetworkSources(self, url, headers, single):
        if url.scheme not in NETWORK_SCHEMES:
            return

        # check for negative cache entries to avoid spamming servers
        if not single:
            negativePath = self.repCache.checkNegativeCache(self.recipeName,
                    url)
            if negativePath:
                log.warning('not fetching %s (negative cache entry %s exists)',
                        url, negativePath)
                return

        log.info('Trying %s...', str(url))
        if headers is None:
            headers = {}

        inFile = self._fetchUrl(url, headers)
        if inFile is None:
            self.repCache.createNegativeCacheEntry(self.recipeName, url)
        else:
            contentLength = int(inFile.headers.get('Content-Length', 0))
            path = self.repCache.addFileToCache(self.recipeName, url,
                                                inFile, contentLength)
            if path:
                raise PathFound(path, False)
        return

    def _getPathsToSearch(self, urlStr, suffixes):
        url = laUrl(urlStr)

        if url.scheme == 'multiurl':
            multiKey = os.path.dirname(url.filePath())[1:]
            urlObjList = [laUrl(x, parent=url)
                          for x in self.multiurlMap[multiKey]]
        else:
            urlObjList = [url]

        newUrlObjList = []
        for ou in urlObjList:
            if ou.scheme == 'mirror':
                for u in Mirror(self.mirrorDirs, ou.host):
                    mu = laUrl(u, parent=ou)
                    newUrlObjList.append(mu)
            else:
                newUrlObjList.append(ou)
        urlObjList = newUrlObjList

        if suffixes is not None:
            newUrlObjList = []
            for url in urlObjList:
                for suffix in suffixes:
                    newurl = copy.copy(url)
                    newurl.extension = suffix
                    newUrlObjList.append(newurl)
            urlObjList = newUrlObjList
        return urlObjList

    class BasicPasswordManager(urllib.request.HTTPPasswordMgr):
        # password manager class for urllib2 that handles exactly 1 password
        def __init__(self):
            self.user = ''
            self.passwd = ''

        def add_password(self, user, passwd):
            self.user = user
            self.passwd = passwd

        def find_user_password(self, *args, **kw):
            if self.user:
                return self.user, self.passwd
            return (None, None)

    def _fetchUrl(self, url, headers):
        if isinstance(url, str):
            url = laUrl(url)

        retries = 3
        if self.cfg.proxy and not self.noproxyFilter.bypassProxy(url.host):
            retries = 7
        inFile = None
        for i in range(retries):
            try:
                # set up a handler that tracks cookies to handle
                # sites like Colabnet that want to set a session cookie
                cj = http.cookiejar.LWPCookieJar()
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

                # add password handler if needed
                if url.user:
                    url.passwd = url.passwd or ''
                    opener.add_handler(
                            HTTPBasicAuthHandler(url.user, url.passwd))

                # add proxy and proxy password handler if needed
                if self.cfg.proxy and \
                        not self.noproxyFilter.bypassProxy(url.host):
                    proxyPasswdMgr = urllib.request.HTTPPasswordMgr()
                    for v in list(self.cfg.proxy.values()):
                        pUrl = laUrl(v[1])
                        if pUrl.user:
                            pUrl.passwd = pUrl.passwd or ''
                            proxyPasswdMgr.add_password(
                                None, pUrl.asStr(noAuth=True, quoted=True),
                                url.user, url.passwd)

                    opener.add_handler(
                        urllib.request.ProxyBasicAuthHandler(proxyPasswdMgr))
                    opener.add_handler(
                        urllib.request.ProxyHandler(self.cfg.proxy))

                if url.scheme == 'ftp':
                    urlStr = url.asStr(noAuth=False, quoted=True)
                else:
                    urlStr = url.asStr(noAuth=True, quoted=True)
                req = urllib.request.Request(urlStr, headers=headers)

                inFile = opener.open(req)
                if not urlStr.startswith('ftp://'):
                    content_type = inFile.info().get('content-type')
                    if not url.explicit() and 'text/html' in content_type:
                        raise urllib.error.URLError('"%s" not found' % urlStr)
                log.info('Downloading %s...', urlStr)
                break
            except urllib.error.HTTPError as msg:
                if msg.code == 404:
                    return None
                else:
                    log.error('error downloading %s: %s',
                              urlStr, str(msg))
                    return None
            except urllib.error.URLError:
                return None
            except socket.error as err:
                num, msg = err
                if num == errno.ECONNRESET:
                    log.info('Connection Reset by server'
                             'while retrieving %s.'
                             '  Retrying in 10 seconds.', urlStr, msg)
                    time.sleep(10)
                    retries += 1
                else:
                    return None
            except IOError as msg:
                # only retry for server busy.
                ftp_error = msg.args[1]
                if isinstance(ftp_error, EOFError):
                    # server just hung and gave no response
                    return None
                response = msg.args[1].args[0]
                if isinstance(response, str) and response.startswith('421'):
                    log.info('FTP server busy when retrieving %s.'
                             '  Retrying in 10 seconds.', urlStr)
                    time.sleep(10)
                    retries += 1
                else:
                    return None
        return inFile


class RepositoryCache(object):

    def __init__(self, repos, refreshFilter=None, cfg=None):
        self.repos = repos
        self.refreshFilter = refreshFilter
        self.nameMap = {}
        self.cacheMap = {}
        self.quiet = False
        self._basePath = self.downloadRatedLimit = None
        self.setConfig(cfg)

    def setQuiet(self, quiet):
        self.quiet = quiet

    def setConfig(self, cfg):
        if cfg:
            self.quiet = cfg.quiet
            self._basePath = cfg.lookaside
            self.downloadRateLimit = cfg.downloadRateLimit

    def _getBasePath(self):
        if self._basePath is None:
            raise RuntimeError('Tried to use repository cache with unset'
                               ' basePath')
        return self._basePath

    basePath = property(_getBasePath)

    def addFileHash(self, filePath, troveName, troveVersion, pathId, path,
                    fileId, fileVersion, sha1, mode):
        self.nameMap[filePath] = (troveName, troveVersion, pathId, path,
                                  fileId, fileVersion, sha1, mode)

    def hasFilePath(self, url):
        if self.refreshFilter:
            if checkRefreshFilter(self.refreshFilter, url):
                return False
        return url.filePath() in self.nameMap

    def cacheFilePath(self, cachePrefix, url):
        cachePath = self.getCachePath(cachePrefix, url)
        util.mkdirChain(os.path.dirname(cachePath))

        if url.filePath() in self.cacheMap:
            # don't check sha1 twice
            return self.cacheMap[url.filePath()]
        (troveName, troveVersion, pathId, troveFile, fileId,
         troveFileVersion, sha1, mode) = self.nameMap[url.filePath()]
        sha1Cached = None
        cachedMode = None
        if os.path.exists(cachePath):
            sha1Cached = sha1helper.sha1FileBin(cachePath)
        if sha1Cached != sha1:
            if sha1Cached:
                log.info('%s sha1 %s != %s; fetching new...', url.filePath(),
                          sha1helper.sha1ToString(sha1),
                          sha1helper.sha1ToString(sha1Cached))
            else:
                log.info('%s not yet cached, fetching...', url.filePath())

            if self.quiet:
                csCallback = None
            else:
                csCallback = ChangesetCallback()

            f = self.repos.getFileContents(
                [(fileId, troveFileVersion)], callback=csCallback)[0].get()
            outF = util.AtomicFile(cachePath, chmod=0o644)
            util.copyfileobj(f, outF)
            outF.commit()
            fileObj = self.repos.getFileVersion(
                pathId, fileId, troveFileVersion)
            fileObj.chmod(cachePath)

        cachedMode = os.stat(cachePath).st_mode & 0o777
        if mode != cachedMode:
            os.chmod(cachePath, mode)
        self.cacheMap[url.filePath()] = cachePath
        return cachePath

    def addFileToCache(self, cachePrefix, url, infile, contentLength):
        # cache needs to be hierarchical to avoid collisions, thus we
        # use cachePrefix so that files with the same name and different
        # contents in different packages do not collide
        cachedname = self.getCachePath(cachePrefix, url)
        util.mkdirChain(os.path.dirname(cachedname))
        f = util.AtomicFile(cachedname, chmod=0o644)

        try:
            BLOCKSIZE = 1024 * 4

            if self.quiet:
                callback = callbacks.FetchCallback()
            else:
                callback = FetchCallback()

            wrapper = callbacks.CallbackRateWrapper(callback, callback.fetch,
                                                    contentLength)
            util.copyfileobj(infile, f, bufSize=BLOCKSIZE,
                             rateLimit=self.downloadRateLimit,
                             callback=wrapper.callback)

            f.commit()
            infile.close()
        except:
            f.close()
            raise

        # work around FTP bug (msw had a better way?)
        if url.scheme == 'ftp':
            if os.stat(cachedname).st_size == 0:
                os.unlink(cachedname)
                self.createNegativeCacheEntry(cachePrefix, url)
                return None

        return cachedname

    def setRefreshFilter(self, refreshFilter):
        self.refreshFilter = refreshFilter

    def getCacheDir(self, cachePrefix, negative=False):
        if negative:
            cachePrefix = 'NEGATIVE' + os.sep + cachePrefix
        return os.sep.join((self.basePath, cachePrefix))

    def getCachePath(self, cachePrefix, url, negative=False):
        if isinstance(url, str):
            url = laUrl(url)
        cacheDir = self.getCacheDir(cachePrefix, negative=negative)
        cachePath = os.sep.join((cacheDir, url.filePath(not negative)))
        return os.path.normpath(cachePath)

    def clearCacheDir(self, cachePrefix, negative=False):
        negativeCachePath = self.getCacheDir(cachePrefix, negative=negative)
        util.rmtree(os.path.dirname(negativeCachePath), ignore_errors=True)

    def createNegativeCacheEntry(self, cachePrefix, url):
        if isinstance(url, str):
            url = laUrl(url)
        cachePath = self.getCachePath(cachePrefix, url, negative=True)
        util.mkdirChain(os.path.dirname(cachePath))
        open(cachePath, 'w+')

    def findInCache(self, cachePrefix, basename):
        return util.searchPath(basename,
                               os.path.join(self.basePath, cachePrefix))

    def getCacheEntry(self, cachePrefix, url):
        cachePath = self.getCachePath(cachePrefix, url)
        if os.path.exists(cachePath):
            return cachePath

    def checkNegativeCache(self, cachePrefix, url):
        cachePath = self.getCachePath(cachePrefix, url, negative=True)
        if os.path.exists(cachePath):
            if time.time() < (NEGATIVE_CACHE_TTL
                              + os.path.getmtime(cachePath)):
                return cachePath
            else:
                os.remove(cachePath)
        return False

    def getArchiveCachePath(self, archiveName, url=''):
        # CNY-2627 introduced a separate lookaside stack for archive contents
        # this dir tree is parallel to NEGATIVE and trovenames.
        # the name =X_CONTENTS= was chosen because = is an illegal character
        # in a trovename and thus will never conflict with real troves.
        archiveType, trailingPath = archiveName.split('://', 1)
        contentsPrefix = "=%s_CONTENTS=" % archiveType.upper()
        return self.getCachePath(contentsPrefix,
                                 os.path.join(trailingPath, url.filePath()))

    def getArchiveCacheEntry(self, archiveName, url):
        fullPath = self.getArchiveCachePath(archiveName, url)
        if os.path.exists(fullPath):
            return fullPath


class PathFound(Exception):

    def __init__(self, path, isFromRepos):
        self.path = path
        self.isFromRepos = isFromRepos


class HTTPBasicAuthHandler(urllib.request.BaseHandler):

    def __init__(self, user, passwd):
        self.user = user
        self.passwd = passwd

    def http_request(self, request):
        if 'Authorization' not in request.headers:
            request.headers['Authorization'] = 'Basic ' + base64.b64encode(
                    '%s:%s' % (self.user, self.passwd))
        return request
    https_request = http_request
