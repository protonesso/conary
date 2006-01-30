#
# Copyright (c) 2005 rPath, Inc.
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
from conary.errors import ConaryError, InternalConaryError
from conary.errors import RepositoryError, TroveNotFound
from conary import versions

class RepositoryMismatch(RepositoryError):
    pass

class InsufficientPermission(ConaryError):

    def __init__(self, server = None):
        self.server = server
        if server:
            msg = ("Insufficient permission to access server %s" % self.server)
        else:
            msg = "Insufficient permission"
        ConaryError.__init__(self, msg)

class IntegrityError(RepositoryError, InternalConaryError):
    """Files were added which didn't match the expected sha1"""

class MethodNotSupported(RepositoryError):
    """Attempt to call a server method which does not exist"""

class RepositoryLocked(RepositoryError):
    def __str__(self):
        return 'The repository is currently busy.  Try again in a few moments.'

class OpenError(RepositoryError):
    """Error occured opening the repository"""

class CommitError(RepositoryError):
    """Error occured commiting a trove"""

class DuplicateBranch(RepositoryError):
    """Error occured commiting a trove"""

class TroveMissing(RepositoryError, InternalConaryError):
    troveType = "trove"
    def __str__(self):
        if type(self.version) == list:
            return ('%s %s does not exist for any of '
                    'the following labels:\n    %s' %
                    (self.troveType, self.troveName,
                     "\n    ".join([x.asString() for x in self.version])))
        elif self.version:
            if isinstance(self.version, versions.Branch):
                return ("%s %s does not exist on branch %s" % \
                    (self.troveType, self.troveName, self.version.asString()))

            return "version %s of %s %s does not exist" % \
                (self.version.asString(), self.troveType, self.troveName)
	else:
	    return "%s %s does not exist" % (self.troveType, self.troveName)

    def __init__(self, troveName, version = None):
	"""
	Initializes a TroveMissing exception.

	@param troveName: trove which could not be found
	@type troveName: str
	@param version: version of the trove which does not exist
	@type version: versions.Version
	"""
	self.troveName = troveName
	self.version = version
        if troveName.startswith('group-'):
            self.type = 'group'
        elif troveName.startswith('fileset-'):
            self.type = 'fileset'
        elif troveName.find(':') != -1:
            self.type = 'component'
        else:
            self.type = 'package'

class UnknownException(RepositoryError, InternalConaryError):

    def __init__(self, eName, eArgs):
	self.eName = eName
	self.eArgs = eArgs
	RepositoryError.__init__(self, "UnknownException: %s %s" % (self.eName, self.eArgs))

class UserAlreadyExists(RepositoryError):
    pass

class GroupAlreadyExists(RepositoryError):
    pass

class GroupNotFound(RepositoryError):
    pass

class UnknownEntitlementGroup(RepositoryError):
    pass

class PermissionAlreadyExists(RepositoryError):
    pass

class UserNotFound(RepositoryError):
    def __init__(self, user = "user"):
        self.user = user
        RepositoryError.__init__(self, "UserNotFound: %s" % self.user)

class InvalidServerVersion(RepositoryError):
    pass

class GetFileContentsError(RepositoryError):
    error = 'Base GetFileContentsError: %s %s'
    def __init__(self, (fileId, fileVer)):
        self.fileId = fileId
        self.fileVer = fileVer
        RepositoryError.__init__(self, self.error % (fileId, fileVer))

class FileContentsNotFound(GetFileContentsError):
    error = '''File Contents Not Found
The contents of the following file was not found on the server:
fileId: %r
fileVersion: %s
'''

class FileStreamNotFound(GetFileContentsError):
    error = '''File Stream Not Found
The following file stream was not found on the server:
fileId: %r
fileVersion: %s
'''

class InvalidClientVersion(RepositoryError):
    pass

class AlreadySignedError(RepositoryError):
    def __init__(self, error = "Already signed"):
        RepositoryError.__init__(self, error)
        self.error = error

class DigitalSignatureError(RepositoryError):
    def __init__(self, error = "Trove can't be signed"):
        RepositoryError.__init__(self, error)
        self.error = error

class InternalServerError(RepositoryError, InternalConaryError):
    def __init__(self,  err):
        self.err = err
        RepositoryError.__init__(self, '''
There was an error contacting the repository.   Either the server is
configured incorrectly or the request you sent to the server was invalid.
%s
''' % (err,))

from conary.trove import DigitalSignatureVerificationError, TroveIntegrityError
from conary.lib.openpgpfile import KeyNotFound, BadSelfSignature, IncompatibleKey

# This is a list of simple exception classes and the text string
# that should be used to marshall an exception instance of that
# class back to the client.  The str() value of the exception will
# be returned as the exception argument.
simpleExceptions = (
    (AlreadySignedError,         'AlreadySignedError'),
    (BadSelfSignature,           'BadSelfSignature'),
    (DigitalSignatureVerificationError, 'DigitalSignatureVerificationError'),
    (GroupAlreadyExists,         'GroupAlreadyExists'),
    (IncompatibleKey,            'IncompatibleKey'),
    (IntegrityError,             'IntegrityError'),
    (InvalidClientVersion,       'InvalidClientVersion'),
    (KeyNotFound,                'KeyNotFound'),
    (UserAlreadyExists,          'UserAlreadyExists'),
    (UserNotFound,               'UserNotFound'),
    (GroupNotFound,              'GroupNotFound'),
    (CommitError,                'CommitError'),
    (DuplicateBranch,            'DuplicateBranch'),
    (TroveIntegrityError,        'TroveIntegrityError'),
    (UnknownEntitlementGroup,    'UnknownEntitlementGroup'),
    )
