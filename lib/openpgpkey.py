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

import os
from getpass import getpass
from time import time

from Crypto.PublicKey import DSA
from Crypto.PublicKey import RSA
from Crypto.Util.number import getPrime
from openpgpfile import getPrivateKey
from openpgpfile import getPublicKey
from openpgpfile import getPublicKeyFromString
from openpgpfile import getFingerprint
from openpgpfile import getKeyEndOfLife
from openpgpfile import getKeyTrust
from openpgpfile import seekNextKey
from openpgpfile import IncompatibleKey
from openpgpfile import BadPassPhrase
from openpgpfile import KeyNotFound

#-----#
#OpenPGPKey structure:
#-----#

class OpenPGPKey:
    def __init__(self, fingerprint, cryptoKey, revoked, timestamp, trustLevel=255):
        """
        instantiates a OpenPGPKey object

        @param fingerprint: string key fingerprint of this key
        @type fingerprint: str
        @param cyptoKey: DSA or RSA key object
        @type cryptoKey: instance
        @param revoked: is this key revoked
        @type revoked: bool
        @param trustLevel: the trust level of this key, as stored locally
        @type trustLevel: int
        """

        self.fingerprint = fingerprint
        self.cryptoKey = cryptoKey
        self.revoked = revoked
        self.timestamp = timestamp
        self.trustLevel = trustLevel

    def getTrustLevel(self):
        return self.trustLevel

    def isRevoked(self):
        return self.revoked

    def getFingerprint(self):
        return self.fingerprint

    def getTimestamp(self):
        return self.timestamp

    def _gcf(self, a, b):
        while b:
            a, b = b, a % b
        return a

    def _bitLen(self, a):
        r=0
        while a:
            a, r = a/2, r+1
        return r

    def _getRelPrime(self, q):
        # We /dev/random instead of /dev/urandom. This was not a mistake;
        # we want the most random data available
        rand=open('/dev/random','r')
        b = self._bitLen(q)/8 + 1
        r = 0L
        while r < 2:
            for i in range(b):
                r = r*256 + ord(rand.read(1))
                r %= q
            while self._gcf(r, q-1) != 1:
                r = (r+1) % q
        rand.close()
        return r

    def signString(self, data):
        if isinstance(self.cryptoKey,(DSA.DSAobj_c, DSA.DSAobj)):
            K = self.cryptoKey.q + 1
            while K > self.cryptoKey.q:
                K = self._getRelPrime(self.cryptoKey.q)
        else:
            K = 0
        timeStamp = int(time())
        return (self.fingerprint, timeStamp,
                self.cryptoKey.sign(data+str(timeStamp), K))

    def verifyString(self, data, sig):
        """
        verifies a digital signature

        returns -1 if the signature does not verify.  Otherwise it returns
        the trust value of the public key that corresponds to the private
        key that signed the data.

        @param data: the data that has been signed
	@type name: str
	@param sig: the digital signature to verify
	@type sig: 4-tuple (fingerprint, timestamp, signature, K)
        @rtype int
        """
        # this function was not designed to throw an exception at this level
        # because in some cases the calling function wants to aggregate a list
        # of failed/passed signatures all at once.

        if (self.fingerprint == sig[0]
            and self.cryptoKey.verify(data+str(sig[1]), sig[2])):
            return self.trustLevel
        else:
            return -1

class OpenPGPKeyCache:
    """
    Base class for a key cache
    """
    def __init__(self):
        self.publicDict = {}
        self.privateDict = {}

    def getPublicKey(self, keyId):
        raise NotImplementedError

    def getPrivateKey(self, keyId, passphrase=None):
        raise NotImplementedError

    def reset(self):
        self.publicDict = {}
        self.privateDict = {}

class OpenPGPKeyFileCache(OpenPGPKeyCache):
    """
    OpenPGPKeyCache based object that reads keys from public and private
    keyrings
    """
    def __init__(self):
        OpenPGPKeyCache.__init__(self)
        if 'HOME' not in os.environ:
            self.publicPaths = [ '/etc/conary/pubring.gpg' ]
            self.privatePath = None
        else:
            self.publicPaths = [ os.environ['HOME'] + '/.gnupg/pubring.gpg',
                                 '/etc/conary/pubring.gpg' ]
            self.trustDbPaths = [ os.environ['HOME'] + '/.gnupg/trustdb.gpg',
                                 '/etc/conary/trustdb.gpg' ]
            self.privatePath = os.environ['HOME'] + '/.gnupg/secring.gpg'

    def setPublicPath(self, path):
        self.publicPaths = [ path ]

    def setTrustDbPath(self, path):
        self.trustDbPaths = [ path ]

    def addPublicPath(self, path):
        self.publicPaths.append(path)

    def setPrivatePath(self, path):
        self.privatePath = path

    def getPublicKey(self, keyId):
        # if we have this key cached, return it immediately
        if keyId in self.publicDict:
            return self.publicDict[keyId]

        # otherwise search for it
        for i in range(len(self.publicPaths)):
            try:
                publicPath = self.publicPaths[i]
                trustDbPath = self.trustDbPaths[i]
                # translate the keyId to a full fingerprint for consistency
                fingerprint = getFingerprint(keyId, publicPath)
                revoked, timestamp = getKeyEndOfLife(keyId, publicPath)
                cryptoKey = getPublicKey(keyId, publicPath)
                trustLevel = getKeyTrust(trustDbPath, fingerprint)
                self.publicDict[keyId] = OpenPGPKey(fingerprint, cryptoKey, revoked, timestamp, trustLevel)
                return self.publicDict[keyId]
            except KeyNotFound:
                pass
        raise KeyNotFound(keyId)

    def getPrivateKey(self, keyId, passphrase=None):
        if keyId in self.privateDict:
            return self.privateDict[keyId]

        # translate the keyId to a full fingerprint for consistency
        fingerprint = getFingerprint(keyId, self.privatePath)
        revoked, timestamp = getKeyEndOfLife(keyId, self.privatePath)

        # if we were supplied a password, use it.  The caller will need
        # to deal with handling BadPassPhrase exceptions
        if passphrase is not None:
            cryptoKey = getPrivateKey(keyId, passphrase, self.privatePath)
            self.privateDict[keyId] = OpenPGPKey(fingerprint, cryptoKey, revoked, timestamp)
            return self.privateDict[keyId]

        # next, see if the key has no passphrase (WHY???)
        # if it's readable, there's no need to prompt the user
        try:
            cryptoKey = getPrivateKey(keyId, '', self.privatePath)
            self.privateDict[keyId] = OpenPGPKey(fingerprint, cryptoKey, revoked, timestamp)
            return self.privateDict[keyId]
        except BadPassPhrase:
            pass

        # FIXME: make this a callback
        print "\nsignature key is: %s"% keyId

        tries = 0
        while tries < 3:
            # FIXME: make this a callback
            passPhrase = getpass("Passphrase: ")
            try:
                cryptoKey = getPrivateKey(keyId, passPhrase, self.privatePath)
                self.privateDict[keyId] = OpenPGPKey(fingerprint, cryptoKey, revoked, timestamp)
                return self.privateDict[keyId]
            except BadPassPhrase:
                print "Bad passphrase. Please try again."
            tries += 1

        raise BadPassPhrase

_keyCache = OpenPGPKeyFileCache()

def getKeyCache():
    global _keyCache
    return _keyCache

def setKeyCache(keyCache):
    global _keyCache
    _keyCache = keyCache

