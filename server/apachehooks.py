#
# Copyright (c) 2004-2005 Specifix, Inc.
#
# This program is distributed under the terms of the Common Public License,
# version 1.0. A copy of this license should have been distributed with this
# source file in a file called LICENSE. If it is not present, the license
# is always available at http://www.opensource.org/licenses/cpl.php.
#
# This program is distributed in the hope that it will be useful, but
# without any waranty; without even the implied warranty of merchantability
# or fitness for a particular purpose. See the Common Public License for
# full details.
# 

from mod_python import apache
from mod_python import util
import base64
import os
import traceback
import xmlrpclib
import zlib

from repository.netrepos import netserver
from http import HttpHandler
import conarycfg

import kid
kid.enable_import()
from templates import error as kid_error

BUFFER=1024 * 256

class ServerConfig(conarycfg.ConfigFile):

    defaults = {
        'commitAction'      :  None,
        'forceSSL'          :  [ conarycfg.BOOLEAN, False ],
        'logFile'           :  None,
        'repositoryMap'     :  [ conarycfg.STRINGDICT, {} ],
        'repositoryDir'     :  None,
        'serverName'        :  None,
        'tmpDir'            :  "/var/tmp",
        'cacheChangeSets'   :  [ conarycfg.BOOLEAN, False ],
    }

def getAuth(req, repos):
    if not 'Authorization' in req.headers_in:
        return ('anonymous', 'anonymous')

    info = req.headers_in['Authorization'].split()
    if len(info) != 2 or info[0] != "Basic":
        return apache.HTTP_BAD_REQUEST

    try:
        authString = base64.decodestring(info[1])
    except:
        return apache.HTTP_BAD_REQUEST

    if authString.count(":") != 1:
        return apache.HTTP_BAD_REQUEST
      
    authToken = authString.split(":")

    return authToken

def checkAuth(req, repos):
    if not req.headers_in.has_key('Authorization'):
        return None
    else:
        authToken = getAuth(req, repos)
        if type(authToken) != tuple:
            return authToken

        if not repos.auth.checkUserPass(authToken):
            return None
            
    return authToken

def post(port, isSecure, repos, httpHandler, req):
    if req.headers_in['Content-Type'] == "text/xml":
        authToken = getAuth(req, repos)
        if type(authToken) is int:
            return authToken

        if authToken[0] != "anonymous" and not isSecure and repos.forceSecure:
            return apache.HTTP_FORBIDDEN

        (params, method) = xmlrpclib.loads(req.read())

        if isSecure:
            protocol = "https"
        else:
            protocol = "http"

        try:
            result = repos.callWrapper(protocol, port, method, authToken, 
                                       params)
        except netserver.InsufficientPermission:
            return apache.HTTP_FORBIDDEN

        resp = xmlrpclib.dumps((result,), methodresponse=1)
        req.content_type = "text/xml"
        encoding = req.headers_in.get('Accept-encoding', '')
        if len(resp) > 200 and 'zlib' in encoding:
            req.headers_out['Content-encoding'] = 'zlib'
            resp = zlib.compress(resp, 5)
        req.write(resp) 
    else:
        cmd = os.path.basename(req.uri)
        if httpHandler.requiresAuth(cmd):
            authToken = checkAuth(req, repos)
            if type(authToken) is int or authToken is None or authToken[0] is None:
                req.err_headers_out['WWW-Authenticate'] = \
                                    'Basic realm="Conary Repository"'
                return apache.HTTP_UNAUTHORIZED
        else:
            authToken = (None, None)

        if authToken[0] is not None and authToken[0] != "anonymous" and \
                    not isSecure and repos.forceSecure:
            return apache.HTTP_FORBIDDEN
    
        req.content_type = "text/html"
        try:
            httpHandler.handleCmd(req.write, cmd, authToken,
                                  util.FieldStorage(req))
        except netserver.InsufficientPermission:
            return apache.HTTP_FORBIDDEN
        except:
            writeTraceback(req)

    return apache.OK

def get(isSecure, repos, httpHandler, req):
    uri = req.uri
    if uri.endswith('/'):
        uri = uri[:-1]
    cmd = os.path.basename(uri)
    fields = util.FieldStorage(req)

    authToken = getAuth(req, repos)
    if authToken[0] != "anonymous" and not isSecure and repos.forceSecure:
        return apache.HTTP_FORBIDDEN
   
    if cmd != "changeset":
	# we need to redo this with a trailing / for the root menu to work
	cmd = os.path.basename(req.uri)

        if httpHandler.requiresAuth(cmd):
            authToken = checkAuth(req, repos)
            if not authToken:
                req.err_headers_out['WWW-Authenticate'] = 'Basic realm="Conary Repository"'
                return apache.HTTP_UNAUTHORIZED
        else:
            authToken = (None, None)

        req.content_type = "text/html"
        try:
            httpHandler.handleCmd(req.write, cmd, authToken, fields)
        except netserver.InsufficientPermission:
            return apache.HTTP_FORBIDDEN
        except:
            writeTraceback(req)
        return apache.OK

    localName = repos.tmpPath + "/" + req.args + "-out"
    size = os.stat(localName).st_size

    if localName.endswith(".cf-out"):
        try:
            f = open(localName, "r")
        except IOError:
            self.send_error(404, "File not found")
            return None

        if req.args[0:6] != "cache-" or not repos.cacheChangeSets():
            os.unlink(localName)

        items = []
        totalSize = 0
        for l in f.readlines():
            (path, size) = l.split()
            size = int(size)
            totalSize += size
            items.append((path, size))
        del f
    else:
        size = os.stat(localName).st_size;
        items = [ (localName, size) ]
        totalSize = size

    req.content_type = "application/x-conary-change-set"
    req.sendfile(items[0][0])

    # erase single files
    if not localName.endswith(".cf-out") and \
           (req.args[0:6] != "cache-" or not repos.cacheChangeSets()):
        os.unlink(items[0][0])

    return apache.OK

def putFile(port, isSecure, repos, req):
    if not isSecure and repos.forceSecure:
        return apache.HTTP_FORBIDDEN

    path = repos.tmpPath + "/" + req.args + "-in"
    size = os.stat(path).st_size
    if size != 0:
	return apache.HTTP_UNAUTHORIZED

    f = open(path, "w+")
    s = req.read(BUFFER)
    while s:
	f.write(s)
	s = req.read(BUFFER)

    f.close()

    return apache.OK

def writeTraceback(wfile):
    kid_error.write(wfile, pageTitle = "Error",
                           error = traceback.format_exc())

def handler(req):
    repName = req.filename
    if not repositories.has_key(repName):
        cfg = ServerConfig()
        cfg.read(req.filename)

	if os.path.basename(req.uri) == "changeset":
	   rest = os.path.dirname(req.uri) + "/"
	else:
	   rest = req.uri

	rest = req.uri
	# pull out any queryargs
	if '?' in rest:
	    rest = req.uri.split("?")[0]

	# and throw away any subdir portion
	rest = req.uri[:-len(req.path_info)] + '/'
        
	urlBase = "%%(protocol)s://%s:%%(port)d" % \
                        (req.server.server_hostname) + rest

        if not cfg.repositoryDir:
            print "error: repositoryDir is required in %s" % req.filename
            return
        elif not cfg.serverName:
            print "error: serverName is required in %s" % req.filename
            return

	repositories[repName] = netserver.NetworkRepositoryServer(
                                cfg.repositoryDir,
                                cfg.tmpDir,
				urlBase, 
                                cfg.serverName,
                                cfg.repositoryMap,
				commitAction = cfg.commitAction,
                                cacheChangeSets = cfg.cacheChangeSets,
                                logFile = cfg.logFile)

	repositories[repName].forceSecure = cfg.forceSSL

    port = req.server.port
    if not port:
        port = req.parsed_uri[apache.URI_PORT]
        if not port:
            port = 80
    secure = (port == 443)
    
    repos = repositories[repName]
    httpHandler = HttpHandler(repos)
    
    method = req.method.upper()

    if method == "POST":
	return post(port, secure, repos, httpHandler, req)
    elif method == "GET":
	return get(secure, repos, httpHandler, req)
    elif method == "PUT":
	return putFile(port, secure, repos, req)
    else:
	return apache.HTTP_METHOD_NOT_ALLOWED

repositories = {}
