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


from testrunner import testcase, testhelp
from testutils import mock
from testutils.servers import memcache_server
import copy
import os

from conary_test import rephelp

from conary import conaryclient
from conary import trove
from conary.files import ThawFile
from conary.repository import errors
from conary.repository.netrepos import proxy as netreposproxy
from conary.repository.netrepos import netserver
from conary.repository.netrepos.auth_tokens import AuthToken


def runproxy(**params):

    def deco(fn):

        def dorunproxy(obj, *args, **kwargs):
            if 'CONARY_PROXY' in os.environ:
                raise testhelp.SkipTestException("testInjectedEntitlements doesn't run with a proxy already running")

            memcache = None
            if params.pop('memcache', False):
                memcache = memcache_server.MemcacheServer()
                memcache.start()
                params['cacheLocation'] = memcache.getHostPort()

            proxy = obj.getConaryProxy(**params)

            obj.stopRepository(1)
            obj.openRepository(1, useSSL = True, forceSSL = True)

            cfg = copy.deepcopy(obj.cfg)
            proxy.addToConfig(cfg)
            client = conaryclient.ConaryClient(cfg)
            repos = client.getRepos()

            proxy.start()

            try:
                fn(obj, repos, *args, **kwargs)
            finally:
                proxy.stop()
                if memcache:
                    memcache.stop()
                server = obj.servers.getServer(1)
                if server is not None:
                    server.reset()
                    obj.stopRepository(1)

        dorunproxy.__name__ = fn.__name__

        return dorunproxy

    return deco

class ProxyUnitTest(testcase.TestCaseWithWorkDir):
    def testGetChangeSet(self):
        # Now mock ChangesetCache, to log things
        origChangesetCache = netreposproxy.ChangesetCache
        lockLogFile = os.path.join(self.workDir, "locks.log")
        class MockChangesetCache(origChangesetCache):
            llf = file(lockLogFile, "a")
            def get(slf, key, shouldLock = True):
                csPath = origChangesetCache.hashKey(slf, key)
                ret = origChangesetCache.get(slf, key, shouldLock=shouldLock)
                if shouldLock and ret is None:
                    slf.llf.write("Lock acquired for %s\n" % csPath)
                    self.assertTrue(os.path.exists(csPath + '.lck'))
                return ret

            def set(slf, key, value):
                csPath = origChangesetCache.hashKey(slf, key)
                if csPath in slf.locksMap:
                    slf.llf.write("Releasing lock for %s\n" % csPath)
                return origChangesetCache.set(slf, key, value)

            def resetLocks(slf):
                for csPath in sorted(slf.locksMap):
                    slf.llf.write("Resetting unused lock for %s\n" % csPath)
                return origChangesetCache.resetLocks(slf)

        self.mock(netreposproxy, 'ChangesetCache', MockChangesetCache)


        cfg = netserver.ServerConfig()
        cfg.changesetCacheDir = os.path.join(self.workDir, "changesetCache")
        cfg.proxyContentsDir = os.path.join(self.workDir, "proxyContents")
        prs = netreposproxy.ProxyRepositoryServer(cfg, "/someUrl")
        rawUrl = '/blah'
        headers = {'X-Conary-Proxy-Host' : 'repos.example.com'}
        prs.setBaseUrlOverride(rawUrl, headers, isSecure = True)
        # callWrapper normally sets this, but nothing here invokes it
        prs._serverName = 'repos.example.com'

        caller = mock.mockClass(netreposproxy.ProxyCaller)()
        caller._getBasicUrl._mock.setDefaultReturn('http://blah')
        caller.checkVersion._mock.setDefaultReturn([51, 52, 53])
        # Make sure we present the fingerprints in non-sorted order, we need
        # to verify we sort them
        suf = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
        fingerprints = ['aac3aac3' + suf, 'aaa1aaa1' + suf, 'aab2aab2' + suf]
        caller.getChangeSetFingerprints._mock.setDefaultReturn(fingerprints)
        csSizes = [ 12, 13, 14 ]
        allInfo = [
            (str(x), 'trovesNeeded%d' % i, 'filesNeeded%d' % i,
                'removedTroves%d' % i, str(x))
            for i, x in enumerate(csSizes) ]
        csFileObj = file(os.path.join(self.workDir, "changeset"), "w+")
        magic = netreposproxy.filecontainer.FILE_CONTAINER_MAGIC
        fver = netreposproxy.filecontainer.FILE_CONTAINER_VERSION_FILEID_IDX
        fver = netreposproxy.filecontainer.struct.pack("!I", fver)
        for i, csSize in enumerate(csSizes):
            csFileObj.write(magic)
            csFileObj.write(fver)
            rest = csSize - len(magic) - len(fver)
            csFileObj.write((chr(ord('a') + i) * rest))
        csFileObj.seek(0)

        changeSetList = [ (x, (None, None), (None, None), False) for x in
                            ['a', 'b', 'c'] ]

        caller.getChangeSet._mock.appendReturn(
            ('http://repos.example.com/my-changeset-url', allInfo),
            53, changeSetList, False, True, False, True, 2007022001,
            False, False)

        urlOpener = mock.MockObject()
        uo = mock.MockObject()
        self.mock(netreposproxy.transport, 'ConaryURLOpener', urlOpener)
        urlOpener._mock.setDefaultReturn(uo)
        uo.open._mock.appendReturn(
            csFileObj,
            'http://repos.example.com/my-changeset-url',
            forceProxy=caller._lastProxy,
            headers=[('X-Conary-Servername', 'repos.example.com')])

        authToken = AuthToken(None, None, [])
        clientVersion = 51

        prs.getChangeSet(caller, authToken, clientVersion, changeSetList,
            recurse = False, withFiles = True, withFileContents = False,
            excludeAutoSource = True)

        MockChangesetCache.llf.close()
        f = file(lockLogFile)
        contents = [ x.strip() for x in f ]
        sortedFP = sorted(fingerprints)

        logEntries1 = contents[:len(fingerprints)]
        self.assertEqual(logEntries1,
            [ 'Lock acquired for %s/%s/%s-2007022001.1' %
                (cfg.changesetCacheDir, fp[:2], fp[2:])
              for fp in sortedFP ])
        logEntries2 = contents[len(fingerprints):2 * len(fingerprints)]
        self.assertEqual(logEntries2,
            [ 'Releasing lock for %s/%s/%s-2007022001.1' %
                (cfg.changesetCacheDir, fp[:2], fp[2:])
              for fp in fingerprints ])
        # We're not releasing locks we didn't close
        self.assertEqual(len(contents), 2 * len(fingerprints))

class ProxyTest(rephelp.RepositoryHelper):

    def _getRepos(self, proxyRepos):
        hostname = 'localhost1'
        label = self.cfg.buildLabel.asString().replace('localhost', hostname)
        repos = self.openRepository(1, useSSL = True, forceSSL = True)
        return repos, label, hostname


    @runproxy(entitlements = [ ('localhost1', 'ent1234') ])
    def testInjectedEntitlements(self, proxyRepos):
        repos, label, hostname = self._getRepos(proxyRepos)

        repos.addRole(label, 'entgroup')
        repos.addAcl(label, 'entgroup', None, None)
        repos.addEntitlementClass(hostname, 'entgroup', 'entgroup')
        repos.addEntitlementKeys(hostname, 'entgroup', [ 'ent1234' ])
        repos.deleteUserByName(label, 'anonymous')
        repos.deleteUserByName(label, 'test')

        # since both users have been erased from the repository, this can
        # only work if the entitlement got added by the proxy
        proxyRepos.c[hostname].checkVersion()

    def testInjectedEntitlementsNonSSL(self):
        # CNY-3176
        # We are trying to force the situation where an entitlement was
        # injected for a server running on the default http port (80, but the
        # URL not specifying it).
        self.cfg.entitlement.append(('example.com', 'sikrit'))
        authToken = AuthToken('test', 'foo', [], '127.0.0.1')
        caller = netreposproxy.ProxyCallFactory.createCaller('unused', 'unused',
            'http://example.com/conary', proxyMap = self.cfg.getProxyMap(),
            authToken = authToken, localAddr = '1.2.3.4',
            protocolString = "protocolString", headers = {}, cfg = self.cfg,
            targetServerName = 'example.com', remoteIp = '5.6.7.8',
            isSecure = False, baseUrl = "http://blah", systemId='foo')
        self.assertEqual(caller.url.scheme, 'https')
        self.assertEqual(caller.url.hostport.port, 443)
        # This whole thing points out a workaround for _not_ going through SSL
        # if you choose so: add a repositoryMap that explicitly adds :80 to
        # the server URL.

    @runproxy(users = [ ('localhost1', 'otheruser', 'pw') ])
    def testUserOverrides(self, proxyRepos):
        repos, label, hostname = self._getRepos(proxyRepos)

        self.addUserAndRole(repos, label, 'otheruser', 'pw')
        repos.addAcl(label, 'otheruser', None, None)

        repos.deleteUserByName(label, 'anonymous')
        repos.deleteUserByName(label, 'test')

        # since both users have been erased from the repository, this can
        # only work if the 'other' user is added in by the proxy
        proxyRepos.c[hostname].checkVersion()

    def testTruncatedChangesets(self):
        """
        Test that a proxy will not cache a changeset that has been truncated in
        transit.
        """
        # Get a proxy server and a repository server both with changeset caches.
        self.stopRepository(2)
        repos = self.openRepository(2)
        reposServer = self.servers.getCachedServer(2)

        proxyServer = self.getConaryProxy()
        proxyServer.start()
        try:

            cfg = copy.deepcopy(self.cfg)
            proxyServer.addToConfig(cfg)
            client = conaryclient.ConaryClient(cfg)
            proxyRepos = client.getRepos()

            trv = self.addComponent('foo:data', '/localhost2@rpl:linux/1-1-1',
                    repos=repos)
            jobList = [ (trv.getName(), (None, None),
                (trv.getVersion(), trv.getFlavor()), True) ]

            # First populate the repository (not proxy) cscache
            kwargs = dict(recurse=False, withFiles=True, withFileContents=True,
                    excludeAutoSource=False)
            cs = repos.createChangeSet(jobList, **kwargs)

            # Now corrupt the changeset and try to pull it through the proxy
            # cache.  Unfortunately the simplest way to do this is to truncate
            # the contents file which is transcluded into the changeset. We get
            # the path to that using the file contents sha1 from the changeset
            # we fetched earlier.
            assert len(cs.files) == 1
            sha1 = ThawFile(list(cs.files.values())[0], None).contents.sha1()
            sha1 = sha1.encode('hex')
            path = os.path.join(reposServer.contents.getPath(),
                    sha1[:2], sha1[2:4], sha1[4:])

            os.rename(path, path + '.old')
            with open(path, 'w') as f:
                f.write('hahaha')

            # At this point, fetching a changeset through the proxy should fail.
            err = self.assertRaises(errors.RepositoryError,
                    proxyRepos.createChangeSet, jobList, **kwargs)
            if 'truncated' not in str(err) and 'corrupted' not in str(err):
                self.fail("Unexpected error when fetching truncated "
                        "changeset: %s" % str(err))

            # If we put the file back, it should succeed.
            os.rename(path + '.old', path)
            proxyRepos.createChangeSet(jobList, **kwargs)

        finally:
            proxyServer.stop()
            self.stopRepository(2)

    @runproxy(memcache=True)
    def testProxyCaching(self, proxyRepos):
        raise testcase.SkipTestException("fails randomly")
        ver0 = "/localhost1@rpl:linux/1-1-1"
        trv0 = self.addComponent("foo:data", ver0, filePrimer = 1)
        trv = proxyRepos.getTrove(*trv0.getNameVersionFlavor())
        deps = proxyRepos.getDepsForTroveList([ trv.getNameVersionFlavor() ],
                                              provides = True, requires = True)
        ti = proxyRepos.getTroveInfo(trove._TROVEINFO_TAG_SOURCENAME,
                                     [ trv.getNameVersionFlavor() ])
        self.stopRepository(1)
        trv1 = proxyRepos.getTrove(*trv0.getNameVersionFlavor())
        deps1 = proxyRepos.getDepsForTroveList([ trv.getNameVersionFlavor() ],
                                              provides = True, requires = True)
        ti1 = proxyRepos.getTroveInfo(trove._TROVEINFO_TAG_SOURCENAME,
                                      [ trv.getNameVersionFlavor() ])
        self.assertEqual(trv, trv1)
        self.assertEqual(deps, deps1)
        self.assertEqual(ti, ti1)

        # we reopen it for proper cleanup in the runproxy() decorator
        self.openRepository(1)
