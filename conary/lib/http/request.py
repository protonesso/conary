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


import base64
import urllib.parse
import zlib

from conary.lib import networking
from conary.lib import util
from conary.lib.compat import namedtuple


class URL(namedtuple('URL', 'scheme userpass hostport path')):

    def __new__(cls, scheme, userpass=None, hostport=None, path=None):
        if userpass is None and hostport is None and path is None:
            return cls.parse(scheme)
        else:
            return tuple.__new__(cls, (scheme, userpass, hostport, path))

    @classmethod
    def parse(cls, url, defaultScheme='http'):
        if '://' not in url and defaultScheme:
            url = '%s://%s' % (defaultScheme, url)
        (scheme, username, password, host, port, path, query, fragment,
                ) = util.urlSplit(url)
        if not port and port != 0:
            if scheme[-1] == 's':
                port = 443
            else:
                port = 80
        hostport = networking.HostPort(host, port)
        path = urllib.parse.urlunsplit(('', '', path, query, fragment))
        return cls(scheme, (username, password), hostport, path)

    def unsplit(self):
        username, password = self.userpass
        host, port = self.hostport
        if port in (80, 443):
            port = None
        return util.urlUnsplit((self.scheme, username, password, str(host),
            port, self.path, None, None))

    def join(self, suffix):
        """
        Evaluate a possibly relative URL C{suffix} against the current URL and
        return the new absolute URL.
        """
        if '://' in suffix:
            # Fully qualified URL
            return URL.parse(suffix)
        elif suffix.startswith('//'):
            # Relative to scheme
            return URL.parse(self.scheme + ':' + suffix)
        elif suffix.startswith('/'):
            # Relative to host
            return self._replace(path=suffix)
        # Fully relative
        path = self.path or ''
        path = path.split('?')[0]
        path = path.split('/')
        # Strip leading slash(es)
        if path and path[0] == '':
            path.pop(0)
        # Strip trailing basename
        if path and path[-1]:
            path.pop()
        for elem in suffix.split('/'):
            if elem == '..':
                if path:
                    path.pop()
            elif elem == '.':
                pass
            else:
                path.append(elem)
        if not path or path[0] != '':
            path.insert(0, '')
        path = '/'.join(path)
        return self._replace(path=path)

    def __str__(self):
        rv = self.unsplit()
        if hasattr(rv, '__safe_str__'):
            rv = rv.__safe_str__()
        return rv


class HTTPHeaders(object):
    __slots__ = ('_headers',)

    def __init__(self, headers=None):
        self._headers = {}
        if headers:
            if isinstance(headers, dict):
                headers = iter(headers.items())
            for key, value in headers:
                self[key] = value

    @staticmethod
    def canonical(key):
        return '-'.join(x.capitalize() for x in key.split('-'))

    def __getitem__(self, key):
        key = self.canonical(key)
        return self._headers[key]

    def __setitem__(self, key, value):
        key = self.canonical(key)
        self._headers[key] = value

    def __delitem__(self, key):
        key = self.canonical(key)
        del self._headers[key]

    def __contains__(self, key):
        key = self.canonical(key)
        return key in self._headers

    def get(self, key, default=None):
        key = self.canonical(key)
        return self._headers.get(key)

    def iteritems(self):
        return iter(self._headers.items())

    def setdefault(self, key, default):
        key = self.canonical(key)
        return self._headers.setdefault(key, default)


class Request(object):

    def __init__(self, url, method='GET', headers=()):
        if isinstance(url, str):
            url = URL.parse(url)
        self.url = url
        self.method = method
        self.headers = HTTPHeaders(headers)

        # Params for sending request entity
        self.abortCheck = None
        self.data = None
        self.size = None
        self.chunked = False
        self.callback = None
        self.rateLimit = None

    def setData(self, data, size=None, compress=False, callback=None,
            chunked=False, rateLimit=None):
        if compress:
            data = zlib.compress(data, 9)
            size = len(data)
            self.headers['Accept-Encoding'] = 'deflate'
            self.headers['Content-Encoding'] = 'deflate'
        self.data = data
        self.callback = callback
        self.rateLimit = rateLimit
        if size is None:
            try:
                size = len(data)
            except TypeError:
                pass
        self.size = size
        if size is not None:
            self.headers['Content-Length'] = str(size)
        if chunked or size is None:
            self.chunked = True
            self.headers['Transfer-Encoding'] = 'chunked'
        else:
            self.chunked = False

    def setAbortCheck(self, abortCheck):
        self.abortCheck = abortCheck

    def sendRequest(self, conn, isProxied=False):
        if isProxied:
            cleanUrl = self.url._replace(userpass=(None,None))
            path = str(cleanUrl)
        else:
            path = self.url.path
        conn.putrequest(self.method, path, skip_host=1, skip_accept_encoding=1)
        self.headers.setdefault('Accept-Encoding', 'identity')
        for key, value in self.headers.items():
            conn.putheader(key, value)
        if 'Host' not in self.headers:
            hostport = self.url.hostport
            if hostport.port in (80, 443):
                hostport = hostport._replace(port=None)
            host = str(hostport)
            if isinstance(host, str):
                host = host.encode('idna')
            conn.putheader("Host", host)
        if 'Authorization' not in self.headers and self.url.userpass[0]:
            conn.putheader("Authorization",
                    "Basic " + base64.b64encode(":".join(self.url.userpass)))
        conn.endheaders()
        self._sendData(conn)

    def _sendData(self, conn):
        if self.data is None:
            return
        if not hasattr(self.data, 'read'):
            conn.send(self.data)
            return

        if self.chunked:
            # Use chunked coding
            output = wrapper = ChunkedSender(conn)
        elif self.size is not None:
            # Use identity coding
            output = conn
            wrapper = None
        else:
            raise RuntimeError("Request must use chunked transfer coding "
                    "if size is not known.")
        util.copyfileobj(self.data, output, callback=self.callback,
                rateLimit=self.rateLimit, abortCheck=self.abortCheck,
                sizeLimit=self.size)
        if wrapper:
            wrapper.close()

    def reset(self):
        if hasattr(self.data, 'read'):
            self.data.seek(0)


class ChunkedSender(object):
    """
    Do HTTP chunked transfer coding by wrapping a socket-like object,
    intercepting send() calls and sending the correct leading and trailing
    metadata.
    """

    def __init__(self, target):
        self.target = target

    def send(self, data):
        self.target.send("%x\r\n%s\r\n" % (len(data), data))

    def close(self, trailer=''):
        self.target.send("0\r\n%s\r\n" % (trailer,))
