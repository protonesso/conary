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


from testrunner import testhelp
import base64

try:
    from webunit.webunittest import WebTestCase as _WebTestCase
    from webunit.webunittest import HTTPResponse
    from webunit import SimpleDOM
    WebTestCase = _WebTestCase
    webunitPresent = True
except ImportError:
    print("warning: webunit module not available; skipping web front-end tests")
    class WebTestCase(object):
        def __init__(self, *args):
            pass
    webunitPresent = False

from conary.lib.http import request
from conary_test import rephelp


class WebRepositoryHelper(rephelp.RepositoryHelper, WebTestCase):
    def __init__(self, methodName):
        WebTestCase.__init__(self, methodName)
        rephelp.RepositoryHelper.__init__(self, methodName)

    def useServer(self, num=0):
        self.openRepository(num)
        server = self.servers.getCachedServer(num)
        self.server = server.getName()
        self.port = request.URL(server.getUrl()).hostport[1]

    def setUp(self):
        if not webunitPresent:
            raise testhelp.SkipTestException('this test requires webunit')
        WebTestCase.setUp(self)
        rephelp.RepositoryHelper.setUp(self)

        try:
            # this *may* be required for some versions of webunit but it seems
            # to break things in other cases
            HTTPResponse._TestCase__testMethodName = \
                self._TestCase__testMethodName
        except:
            pass

        self.useServer(0)
        self.registerErrorContent("Traceback (most recent call last)")

    def tearDown(self):
        rephelp.RepositoryHelper.tearDown(self)
        WebTestCase.tearDown(self)

    def DOM(self, page):
        parser = SimpleDOM.SimpleDOMParser()
        parser.parseString(page.body)
        return parser.getDOM()

    def fetch(self, url, *args, **kwargs):
        if url.startswith('/'):
            url = '/conary' + url
        return WebTestCase.fetch(self, url, *args, **kwargs)


class WebFrontEndTest(WebRepositoryHelper):
    def testFrontPage(self):
        # Frontpage is redirects to the browse; must use explicit login link
        # to login

        # / redirects to /browse, now
        page = self.assertCode('/', code=[302])

        # Browse should be the only publically accessable link to the
        # world
        page = self.assertContent('/browse', 'Login', code=[200])

        # Login is explicitly done via login link
        page = self.assertContent('/login', code=[401],
                                  content='Unauthorized')

        # Login as test user; redirect should happen if successful
        # (back to browse)
        self.setBasicAuth('test', 'foo')
        page = self.assertCode('/login', code=[302])

        # Make sure browse page shows that the user is logged in
        page = self.assertContent('/browse', 'Welcome, <b>test</b>',
                code=[200])

    def testTroveBrowser(self):
        self.addQuickTestCollection("test", "1.0-1-1", [ "test:runtime" ])
        page = self.assertContent('/browse', code=[200],
            content = '<a href="troveInfo?t=test">test</a>')

    def testBrowsePackageFiles(self):
        # UTF-8 in path to validate CNY-3775
        comp = self.addQuickTestComponent("test:runtime",
                fileContents=['contents\xc2\xa0'])
        trv = self.addQuickTestCollection("test", "1.0-1-1", [ comp ])
        n, v, f = trv.getNameVersionFlavor()
        path = self.assertContent('/files?t=%s&v=%s&f=%s' % (
                n, v.freeze(), f.freeze()),
            code=[200], content='<a href="getFile?path=contents%C2%A0')

    def testBrowseDistributedShadow(self):
        raise testhelp.SkipTestException("Fails periodically in automated tests")
        self.openRepository(1)
        v = '/localhost@rpl:1/1.0-1'
        self.addComponent('test:source', v)
        self.mkbranch(v, 'localhost1@rpl:1', 'test:source', shadow = True)

        # use the second repository for the test
        self.useServer(1)
        # first view the troveInfo page for test:source
        page = self.assertContent('/troveInfo?t=test:source', code=[200],
                                  content = '<a href="files?t=test:source')

        # get the "show files' link
        dom = page.getDOM()
        filesLink = None
        for anchor in dom.getByName('a'):
            if 'Show Files' in anchor.getContents():
                filesLink = anchor
                break
        if not filesLink:
            raise RuntimeError('could not find "Show Files" link')
        # get the link to pull down the contents
        page = self.assertContent('/' + filesLink.href, code=[200],
                                  content = '/contents0')
        dom = page.getDOM()
        contentsLink = None
        for anchor in dom.getByName('a'):
            if '/contents0' in anchor.getContents():
                contentsLink = anchor
                break
        # pull down the contents
        page = self.assertContent('/' + contentsLink.href, code=[200],
                                  content = 'hello, world!')

    def testTroveInfo(self):
        raise testhelp.SkipTestException("Fails periodically in automated tests")
        self.addQuickTestComponent('test:runtime', '3.0-1-1', filePrimer=3,
                                   buildTime=None)

        page = self.assertContent('/troveInfo?t=test:runtime', code=[200],
                                  content = 'trove: test:runtime')

        # check to make sure that buildtime is represented properly when
        # no buildtime is set in troveinfo (CNY-990)
        self.assertTrue('<td>Build time:</td><td>(unknown)' in page.body,
                        'Expected build time to be (unknown)')

        dom = page.getDOM()
        filesLink = None
        for anchor in dom.getByName('a'):
            contents = anchor.getContents()
            if len(contents) and 'Show Files' in contents[0]:
                filesLink = anchor
                break

        page = self.assertContent('/' + filesLink.getattr('href'), code=[200],
            content = '-rw-r--r--')

        # test non-frozen version request
        page = self.assertContent(
            '/troveInfo?t=test:runtime;v=/localhost/3.0-1-1',
            code=[200], content = 'Invalid version')

    def testUserlist(self):
        page = self.assertContent('/userlist', code=[401],
                                  content = 'Unauthorized')
        self.setBasicAuth('test', 'foo')
        page = self.assertContent('/userlist', code=[200],
                                  content = 'Member Of')

    def testUserStrangePass(self):
        page = self.assertContent('/userlist', code=[401],
                                  content = 'Unauthorized')
        self.setBasicAuth('test', 'foo:bar')
        page = self.assertContent('/userlist', code=[401],
                                  content = "Unauthorized")
        self.setBasicAuth('test', 'foo')
        page = self.assertContent('/userlist', code=[200],
                                  content = 'Member Of')

    def testAddUser(self):
        # make sure that authentication is required
        page = self.assertContent('/addUserForm', code=[401],
                                  content = "Unauthorized")

        self.setBasicAuth('test', 'foo')
        page = self.assertContent('/addUserForm', code=[200],
                                  content = "Add User")

        page = self.fetch('/addUser',
                          postdata = {'user': 'newuser',
                                      'password': 'newpass'})

        repos = self.getRepositoryClient('newuser', 'newpass')
        # the role should not be automatically added (CNY-2604)
        self.assertEqual(repos.getRoles('localhost'), [])

    def testAddRole(self):
        # make sure that authenticaion is required
        page = self.assertContent('/addRoleForm', code=[401],
                                  content = 'Unauthorized')

        self.setBasicAuth('test', 'foo')

        page = self.postAssertCode('/addRole',
                                   { 'newRoleName' : 'newgroup',
                                     'memberList': [ 'test'] },
                                   code=302)

        repos = self.getRepositoryClient()
        assert('newgroup' in repos.getRoles('localhost'))

    def testAddAcl(self):
        self.setBasicAuth('test', 'foo')
        page = self.postAssertCode('/addPerm',
            {'role': 'test',
             'trove': '.*:source'})
        repos = self.getRepositoryClient()

        acls = repos.listAcls('localhost', 'test')
        assert(acls == [
            {'label': 'ALL', 'item': 'ALL',
             'canWrite': 1, 'canRemove': 1},
            {'label': 'ALL', 'item': '.*:source',
             'canWrite': 0, 'canRemove': 0}])

    def test404(self):
        page = self.assertContent('/asd', code=[404], content = '')

    def testBadAuth(self):
        # this is a hack to set a bad authentication string, which is
        # a bad request
        # Note: see testUserStrangePass for the correct behavior. This should
        # not have been a bad request, but a Forbidden
        self.authinfo = base64.encodestring('foo:bar:baz').strip()
        page = self.assertContent('/userlist', code=[401],
                                  content = 'Unauthorized')

    @testhelp.context('entitlements')
    def testManageEntitlements(self):
        # make sure that authentication is required
        # FIXME! no auth required
        #page = self.assertContent('/manageEntitlements', code=[401],
        #                          content = "Unauthorized")
        #page = self.assertContent('/addEntClassForm', code=[401],
        #                          content = "Unauthorized")

        self.setBasicAuth('test', 'foo')
        page = self.assertContent('/manageEntitlements', code=[200],
                                  content = "Add Entitlement Class")
        page = self.assertContent('/addEntClassForm', code=[200],
                                  content = "Add Entitlement Class")
        # add a non-admin user that will manage the entitlement
        page = self.fetch('/addUser',
                          postdata = {'user': 'newuser',
                                      'password': 'newpass'})
        page = self.fetch('/addRole',
                          postdata = { 'newRoleName' : 'newuser',
                                       'memberList': [ 'newuser'] })
        
        # test not checking any roles in the role box
        page = self.fetch('/addEntClass',
                          postdata = {'entClass': 'entclass',
                                      'roles': [],
                                      'entOwner': 'newuser'})
        assert 'No roles specified' in page.body
        assert page.code == 200

        # test adding a with access to a non-existant role
        page = self.fetch('/addEntClass',
                          postdata = {'entClass': 'entclass',
                                      'roles': ['doesnotexist'],
                                      'entOwner': 'newuser'})
        assert 'Role does not exist' in page.body
        assert page.code == 200

        # now add the real ent class
        page = self.fetch('/addEntClass',
                          postdata = {'entClass': 'entclass',
                                      'roles': ['newuser'],
                                      'entOwner': 'newuser'})
        assert(page.code == 302)
        self.setBasicAuth('newuser', 'newpass')
        page = self.assertContent('/manageEntitlements', code=[200],
                                  content = "entclass")
        page = self.assertContent('/addEntitlementKeyForm?entClass=entclass',
                          code=[200],
                          content = "Add Entitlement")
        page = self.fetch('/addEntitlementKey',
                          postdata = {'entClass': 'entclass',
                                      'entKey': 'entkey'})
        # make sure we got a redirect
        assert (page.code == 302)
        page = self.assertContent('/manageEntitlementForm?entClass=entclass',
                          code=[200],
                          content = "entkey")
        page = self.fetch('/deleteEntitlementKey',
                          postdata = {'entClass': 'entclass',
                                      'entKey': 'entkey'})
        assert (page.code == 302)

        self.registerErrorContent('entkey')
        page = self.assertContent('/manageEntitlementForm?entClass=entclass',
                                  code=[200],
                                  content='Add Entitlement')
        self.removeErrorContent('entkey')

    # FIXME: missing tests:
    # * troveInfo (currently depends on multi-threaded httpd for getChangeSet)
    # * trove files, group contents
    # * change password of user
    # * delete user
    # * edit/delete group
    # * add/edit/delete permission
    # * change logged-in user password
    # * no-anonmous lockdown
    # * all encryption key handling
    # * any alternate code paths for SSL
