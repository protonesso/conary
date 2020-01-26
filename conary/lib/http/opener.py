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


import http.client
import logging
import socket

from conary.lib import httputils
from conary.lib import util
from conary.lib import timeutil
from conary.lib.http import connection as conn_mod
from conary.lib.http import http_error
from conary.lib.http import proxy_map
from conary.lib.http import request as req_mod

log = logging.getLogger(__name__)


class URLOpener(object):
    contentType = 'application/x-www-form-urlencoded'
    userAgent = 'conary-http-client/0.1'

    connectionFactory = conn_mod.Connection
    requestFactory = req_mod.Request

    # Only try proxies with these schemes.
    proxyFilter = ('http', 'https')
    connectAttempts = 3
    redirectAttempts = 5

    def __init__(self, proxyMap=None, caCerts=None, persist=False,
            connectAttempts=None, followRedirects=False):
        if proxyMap is None:
            proxyMap = proxy_map.ProxyMap()
        self.proxyMap = proxyMap
        self.caCerts = caCerts
        self.persist = persist
        if connectAttempts:
            self.connectAttempts = connectAttempts
        self.followRedirects = followRedirects

        self.connectionCache = {}
        self.lastProxy = None

    def newRequest(self, url, data=None, method=None, headers=()):
        req = self.requestFactory(url, method, headers)
        if data is not None:
            req.setData(data)
        return req

    def open(self, url, data=None, method=None, headers=(), forceProxy=False):
        """Open a URL and return a file-like object from which to read the
        response.

        @param url: The URL to open as a string or URL object, or a Request
            object. If a Request object, C{data}, C{method}, and C{headers} are
            ignored.
        @param data: A request entity to POST to the URL.
        @param method: The HTTP verb to use for the request.
        @param headers: Extra headers to send with the request.
        @param forceProxy: Use the given proxy spec instead of the
            pre-configured proxyMap. C{None} forces thes use of no proxy.
        """
        if isinstance(url, req_mod.Request):
            req = url
        else:
            if isinstance(url, str):
                url = req_mod.URL.parse(url)
            elif isinstance(url, req_mod.URL):
                pass
            else:
                raise TypeError("Expected a URL or Request object")

            if method is None:
                if data is None:
                    method = 'GET'
                else:
                    method = 'POST'
            req = self.newRequest(url, data, method, headers)

        req.headers.setdefault('Content-Type', self.contentType)
        req.headers.setdefault('User-Agent', self.userAgent)

        if req.url.scheme == 'file':
            return self._handleFileRequest(req)
        elif req.url.scheme not in ('http', 'https'):
            raise http_error.ParameterError(
                    "Unknown URL scheme %r" % (req.url.scheme,))

        for x in range(self.redirectAttempts):
            response = self._doRequest(req, forceProxy=forceProxy)
            if response.status == 200:
                return self._handleResponse(req, response)
            elif self.followRedirects and response.status in (
                    301, 302, 303, 307):
                if x < self.redirectAttempts - 1:
                    req = self._followRedirect(req, response)
                    log.debug("Following redirect to %s", req.url)
            else:
                return self._handleError(req, response)
        log.error("Too many redirects")
        return self._handleError(req, response)

    def _handleResponse(self, req, response):
        fp = response
        encoding = response.getheader('content-encoding', None)
        if encoding == 'deflate':
            fp = util.decompressStream(fp)
            fp.seek(0)
        elif encoding == 'gzip':
            fp = util.GzipFile(fileobj=fp)
            fp.seek(0)
        return ResponseWrapper(fp, response)

    @staticmethod
    def _drain(response):
        """
        When processing an error, consume the response body to ensure it isn't
        mixed up with the next request in case the connection is kept alive.
        """
        try:
            response.read()
        except socket.error:
            pass

    def _handleError(self, req, response):
        self._drain(response)
        self._handleProxyErrors(response.status)
        raise http_error.ResponseError(req.url, self.lastProxy,
                response.status, response.reason, response.msg)

    def _handleFileRequest(self, req):
        return open(req.url.path, 'rb')

    def _followRedirect(self, req, response):
        self._drain(response)
        base = req.url
        dest = response.msg['Location']
        req.url = base.join(dest)
        return req

    def _doRequest(self, req, forceProxy):
        resetResolv = False
        lastError = response = None
        timer = timeutil.BackoffTimer()
        totalAttempts = 0
        # Make at least 'connectAttempts' connection attempts, stopping after
        # both passing the connectAttempts limit *and* hitting the end of the
        # iterator.
        while True:
            if totalAttempts >= self.connectAttempts:
                break
            # Reset the failed proxy list each time around so we don't end up
            # blacklisting everything if a second pass succeeds.
            failedProxies = set()

            if forceProxy is False:
                connIterator = self.proxyMap.getProxyIter(req.url,
                        protocolFilter=self.proxyFilter)
            else:
                connIterator = [forceProxy]

            for proxySpec in connIterator:
                totalAttempts += 1
                if proxySpec == proxy_map.DirectConnection:
                    proxySpec = None
                elif not forceProxy and self._shouldBypass(req.url, proxySpec):
                    proxySpec = None
                if lastError:
                    if proxySpec == self.lastProxy:
                        log.debug("Failed to open URL %s; trying again: %s",
                                req.url, lastError.format())
                    else:
                        log.info("Failed to open URL %s; trying the next "
                                "proxy: %s", req.url, lastError.format())
                    try:
                        req.reset()
                    except:
                        log.exception("Failed to rewind request body file, "
                                "unable to retry request:")
                        break
                # If a proxy was used, save it here
                self.lastProxy = proxySpec

                try:
                    response = self._requestOnce(req, proxySpec)
                    break
                except http_error.RequestError as err:
                    # Retry if an error occurred while sending the request.
                    lastError = err.wrapped
                    err = lastError.value
                    if lastError.check(socket.error):
                        self._processSocketError(err)
                        lastError.replace(err)
                except http.client.BadStatusLine:
                    # closed connection without sending a response.
                    lastError = util.SavedException()
                except socket.error as err:
                    # Fatal error, but attach proxy information to it.
                    self._processSocketError(err)
                    util.rethrow(err, False)

                # try resetting the resolver - /etc/resolv.conf
                # might have changed since this process started.
                if not resetResolv:
                    util.res_init()
                    resetResolv = True
                if proxySpec:
                    failedProxies.add(proxySpec)

                timer.sleep()

            if response:
                break

        if not response:
            if lastError:
                lastError.throw()
            else:
                # There wasn't anything to connect to, for some reason.
                raise http_error.TransportError("Unable to connect to host %s"
                        % (req.url.hostport,))

        # Only blacklist proxies if something succeeded, otherwise we might
        # blacklist all strategies.
        if failedProxies:
            if self.lastProxy:
                lastStr = "via proxy %s" % (self.lastProxy,)
            else:
                lastStr = "directly"
            log.warning("Successfully contacted remote server %s", lastStr)
        for proxySpec in failedProxies:
            self.proxyMap.blacklistUrl(proxySpec)

        return response

    def _shouldBypass(self, url, proxy):
        if proxy is None:
            return False
        dest = str(url.hostport.host)
        pdest = str(proxy.hostport.host)

        # Don't proxy localhost unless the proxy is also localhost.
        if dest in httputils.LocalHosts and pdest not in httputils.LocalHosts:
            return True

        # Ignore no_proxy for Conary proxies.
        if proxy.scheme in ('conary', 'conarys'):
            return False

        # Check no_proxy
        npFilt = util.noproxyFilter()
        return npFilt.bypassProxy(dest)

    def _requestOnce(self, req, proxy):
        """Issue a request to a a single destination."""
        key = (req.url.scheme, req.url.hostport, proxy)
        conn = self.connectionCache.get(key)
        if conn is None:
            conn = self.connectionFactory(req.url, proxy, self.caCerts)
            if self.persist:
                self.connectionCache[key] = conn

        if not self.persist:
            req.headers.setdefault('Connection', 'close')

        response = conn.request(req)
        self._handleProxyErrors(response.status)
        return response

    def _handleProxyErrors(self, errcode):
        """Translate proxy error codes into exceptions."""
        if errcode == 503:
            # Service unavailable, make it a socket error
            e = socket.error(111, "Service unavailable")
        elif errcode == 502:
            # Bad gateway (server responded with some broken answer)
            e = socket.error(111, "Bad Gateway (error reported by proxy)")
        else:
            return
        # Proxy errors are treated as request errors, which are retriable.
        saved = util.SavedException(e)
        raise http_error.RequestError(saved)

    def _processSocketError(self, error):
        """Append proxy information to an exception."""
        if not self.lastProxy:
            return
        if self.lastProxy.scheme in ('conary', 'conarys'):
            kind = 'Conary'
        else:
            kind = 'HTTP'
        args, msg = http_error.splitSocketError(error)
        msgError = "%s (via %s proxy %s)" % (msg, kind, self.lastProxy)
        error.args = args + (msgError,)
        if hasattr(error, 'strerror'):
            error.strerror = msgError

    def close(self):
        for conn in list(self.connectionCache.values()):
            conn.close()
        self.connectionCache.clear()


class ResponseWrapper(object):

    def __init__(self, fp, response):
        self.fp = fp
        self.response = response
        self._linebuf = ''

        self.status = response.status
        self.reason = response.reason
        self.headers = response.msg
        self.protocolVersion = "HTTP/%.1f" % (response.version / 10.0)

        self.getheader = response.getheader

    def close(self):
        self.fp.close()
        self.response.close()

    def readline(self):
        while '\n' not in self._linebuf:
            d = self.fp.read(1024)
            if not d:
                break
            self._linebuf += d
        idx = self._linebuf.find('\n')
        if idx >= 0:
            idx += 1
            ret, self._linebuf = self._linebuf[:idx], self._linebuf[idx:]
        else:
            ret, self._linebuf = self._linebuf, ''
        return ret

    def __iter__(self):
        while True:
            line = self.readline()
            if not line:
                break
            yield line

    def readlines(self):
        return list(self)

    def read(self, count=None):
        if count is None:
            return self._linebuf + self.fp.read()
        n = min(count, len(self._linebuf))
        if n:
            ret, self._linebuf = self._linebuf[:n], self._linebuf[n:]
            count -= n
            if count:
                ret += self.fp.read(count)
            return ret
        else:
            return self.fp.read(count)

    # Backwards compatibility with urllib.addinfourl
    code = property(lambda self: self.status)
    msg = property(lambda self: self.reason)
