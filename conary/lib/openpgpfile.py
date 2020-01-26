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
import binascii
import itertools
import os
import stat
import struct
import sys
import tempfile
import textwrap
import time
import math

try:
    from Crypto.Hash import RIPEMD
except ImportError:
    RIPEMD = 'RIPEMD'
try:
    from io import StringIO
except ImportError:
    from io import StringIO
from Crypto.Cipher import AES
from Crypto.Cipher import DES3
from Crypto.Cipher import Blowfish
from Crypto.Cipher import CAST
from Crypto.PublicKey import RSA

# We know pycrypto imports sha directly
import warnings
warnings.filterwarnings("ignore", r"the sha module is deprecated",
    DeprecationWarning, "^.*Crypto\.Hash\.SHA.*$")
from Crypto.PublicKey import DSA

from Crypto.PublicKey import pubkey

from conary import constants
from conary.lib import util, digestlib

# key types defined in RFC 2440 page 49
PK_ALGO_RSA                  = 1
PK_ALGO_RSA_ENCRYPT_ONLY     = 2  # deprecated
PK_ALGO_RSA_SIGN_ONLY        = 3  # deprecated
PK_ALGO_ELGAMAL_ENCRYPT_ONLY = 16
PK_ALGO_DSA                  = 17
PK_ALGO_ELLIPTIC_CURVE       = 18
PK_ALGO_ECDSA                = 19
PK_ALGO_ELGAMAL              = 20

PK_ALGO_ALL_RSA = (PK_ALGO_RSA, PK_ALGO_RSA_ENCRYPT_ONLY,
                   PK_ALGO_RSA_SIGN_ONLY)
PK_ALGO_ALL_ELGAMAL = (PK_ALGO_ELGAMAL_ENCRYPT_ONLY, PK_ALGO_ELGAMAL)

# packet tags are defined in RFC 2440 - 4.3. Packet Tags
PKT_RESERVED           = 0  # a packet type must not have this value
PKT_PUB_SESSION_KEY    = 1  # Public-Key Encrypted Session Key Packet
PKT_SIG                = 2  # Signature Packet
PKT_SYM_SESSION_KEY    = 3  # Symmetric-Key Encrypted Session Key Packet
PKT_ONE_PASS_SIG       = 4  # One-Pass Signature Packet
PKT_SECRET_KEY         = 5  # Secret Key Packet
PKT_PUBLIC_KEY         = 6  # Public Key Packet
PKT_SECRET_SUBKEY      = 7  # Secret Subkey Packet
PKT_COMPRESSED_DATA    = 8  # Compressed Data Packet
PKT_SYM_ENCRYPTED_DATA = 9  # Symmetrically Encrypted Data Packet
PKT_MARKER             = 10 # Marker Packet
PKT_LITERAL_DATA       = 11 # Literal Data Packet
PKT_TRUST              = 12 # Trust Packet
PKT_USERID             = 13 # User ID Packet
PKT_PUBLIC_SUBKEY      = 14 # Public Subkey Packet
# Additions from http://tools.ietf.org/html/draft-ietf-openpgp-rfc2440bis-22
PKT_USER_ATTRIBUTE     = 17 # User Attribute Packet
PKT_DATA_PACKET        = 18 # Sym. Encrypted and Integrity Protected Data Packet
PKT_MOD_DETECTION      = 19 # Modification Detection Code Packet
PKT_PRIVATE1           = 60 # 60 to 63 -- Private or Experimental Values
PKT_PRIVATE2           = 61
PKT_PRIVATE3           = 62
PKT_PRIVATE4           = 63

PKT_ALL_SECRET = (PKT_SECRET_KEY, PKT_SECRET_SUBKEY)
PKT_ALL_PUBLIC = (PKT_PUBLIC_KEY, PKT_PUBLIC_SUBKEY)
PKT_ALL_KEYS = PKT_ALL_SECRET + PKT_ALL_PUBLIC
PKT_MAIN_KEYS = (PKT_SECRET_KEY, PKT_PUBLIC_KEY)
PKT_SUB_KEYS = (PKT_SECRET_SUBKEY, PKT_PUBLIC_SUBKEY)

PKT_ALL_USER = set([PKT_USERID, PKT_USER_ATTRIBUTE])

# 5.2.1 Signature Types
SIG_TYPE_BINARY_DOC    = 0x00
SIG_TYPE_TEXT_DOC      = 0x01
SIG_TYPE_STANDALONE    = 0x02
SIG_TYPE_CERT_0        = 0x10
SIG_TYPE_CERT_1        = 0x11
SIG_TYPE_CERT_2        = 0x12
SIG_TYPE_CERT_3        = 0x13
SIG_TYPE_SUBKEY_BIND   = 0x18
SIG_TYPE_PRKEY_BIND    = 0x19
SIG_TYPE_DIRECT_KEY    = 0x1F
SIG_TYPE_KEY_REVOC     = 0x20
SIG_TYPE_SUBKEY_REVOC  = 0x28
SIG_TYPE_CERT_REVOC    = 0x30
SIG_TYPE_TIMESTAMP     = 0x40
SIG_TYPE_THIRD_PARTY_CONFIRM    = 0x50

SIG_CERTS = (SIG_TYPE_CERT_0, SIG_TYPE_CERT_1,
             SIG_TYPE_CERT_2, SIG_TYPE_CERT_3, )
SIG_KEY_REVOCS = (SIG_TYPE_KEY_REVOC, SIG_TYPE_SUBKEY_REVOC)

# 5.2.3.1 Signature Subpacket Types
SIG_SUBPKT_CREATION       = 2
SIG_SUBPKT_SIG_EXPIRE     = 3
SIG_SUBPKT_EXPORTABLE     = 4
SIG_SUBPKT_TRUST          = 5
SIG_SUBPKT_REGEX          = 6
SIG_SUBPKT_REVOCABLE      = 7
SIG_SUBPKT_KEY_EXPIRE     = 9
SIG_SUBPKT_PLACEHOLDER    = 10
SIG_SUBPKT_PREF_SYM_ALGS  = 11
SIG_SUBPKT_REVOC_KEY      = 12
SIG_SUBPKT_ISSUER_KEYID   = 16
SIG_SUBPKT_NOTATION_DATA  = 20
SIG_SUBPKT_PREF_HASH_ALGS = 21
SIG_SUBPKT_PREF_COMP_ALGS = 22
SIG_SUBPKT_KEYSRVR_PREFS  = 23
SIG_SUBPKT_PREF_KEYSRVR   = 24
SIG_SUBPKT_PRIM_UID       = 25
SIG_SUBPKT_POLICY_URL     = 26
SIG_SUBPKT_KEY_FLAGS      = 27
SIG_SUBPKT_SIGNERS_UID    = 28
SIG_SUBPKT_REVOC_REASON   = 29
SIG_SUBPKT_FEATURES       = 30
SIG_SUBPKT_SIG_TARGET     = 31
SIG_SUBPKT_EMBEDDED_SIG   = 32
SIG_SUBPKT_INTERNAL_0     = 100
SIG_SUBPKT_INTERNAL_1     = 101
SIG_SUBPKT_INTERNAL_2     = 102
SIG_SUBPKT_INTERNAL_3     = 103
SIG_SUBPKT_INTERNAL_4     = 104
SIG_SUBPKT_INTERNAL_5     = 105
SIG_SUBPKT_INTERNAL_6     = 106
SIG_SUBPKT_INTERNAL_7     = 107
SIG_SUBPKT_INTERNAL_8     = 108
SIG_SUBPKT_INTERNAL_9     = 109
SIG_SUBPKT_INTERNAL_A     = 110

# 3.6.2.1. Secret key encryption
ENCRYPTION_TYPE_UNENCRYPTED    = 0x00
ENCRYPTION_TYPE_S2K_SPECIFIED  = 0xff
# GPG man page hints at existence of "sha cehcksum" and claims it
#     will be part of "the new forthcoming extended openpgp specs"
#     for now: experimentally determined to be 0xFE
ENCRYPTION_TYPE_SHA1_CHECK = 0xfe

S2K_TYPE_SIMPLE = 0x00
S2K_TYPE_SALTED = 0x01
S2K_TYPE_ITER_SALTED = 0x03

OLD_PKT_LEN_ONE_OCTET  = 0
OLD_PKT_LEN_TWO_OCTET  = 1
OLD_PKT_LEN_FOUR_OCTET = 2

# User Attribute Subpackets (5.12)
USR_ATTR_SUBPKT_IMG = 1

# trust levels
TRUST_UNTRUSTED = 0
TRUST_MARGINAL  = 4
TRUST_FULL      = 5
TRUST_ULTIMATE  = 6

TRUST_TRUSTED   = 120

#trust packet headers
TRP_VERSION     = chr(1)
TRP_KEY         = chr(12)
TRP_USERID      = chr(13)

TRUST_PACKET_LENGTH = 40

SEEK_SET = 0
SEEK_CUR = 1
SEEK_END = 2

class PGPError(Exception):
    pass

class InvalidPacketError(PGPError):
    pass

class KeyringError(PGPError):
    pass

class MalformedKeyRing(PGPError):
    def __str__(self):
        return self.error

    def __init__(self, reason="Malformed Key Ring"):
        self.error = "Malformed Key Ring: %s" %reason

class UnsupportedEncryptionAlgorithm(PGPError):
    def __init__(self, alg):
        self.alg = alg

    def __str__(self):
        return "Unsupported encryption algorithm code %s" % self.alg

class UnsupportedHashAlgorithm(PGPError):
    def __init__(self, alg):
        self.alg = alg

    def __str__(self):
        return "Unsupported hash algorithm code %s" % self.alg

class IncompatibleKey(PGPError):
    def __str__(self):
        return self.error

    def __init__(self, reason="Incompatible Key"):
        self.error = "Incompatible Key: %s" %reason

class InvalidKey(PGPError):
    def __str__(self):
        return self.error

    def __init__(self, reason="Invalid Key"):
        self.error = "Invalid Key: %s" %reason

class KeyNotFound(PGPError):
    def __str__(self):
        return self.error

    def __init__(self, keyId, reason=None):
        if keyId:
            self.error = "OpenPGP key not found for key ID %s" %keyId
            if isinstance(keyId, list):
                self.keys = keyId
            else:
                self.keys = [keyId]
        else:
            self.error = "No OpenPGP keys found"
        if reason:
            self.error += ': %s' %reason

class BadPassPhrase(PGPError):
    def __str__(self):
        return self.error

    def __init__(self, reason="Bad passphrase"):
        self.error = reason

class BadSelfSignature(PGPError):
    def __str__(self):
        return "Key %s failed self signature check" % self.keyId

    def __init__(self, keyId):
        self.keyId = keyId

class InvalidBodyError(PGPError):
    pass

class ShortReadError(InvalidBodyError):
    def __init__(self, expected, actual):
        self.expected = expected
        self.actual = actual

class MergeError(PGPError):
    pass

class SignatureError(PGPError):
    pass

def getKeyId(keyRing):
    pkt = PGP_Message.newPacketFromStream(keyRing, start = -1)
    assert pkt is not None
    return pkt.getKeyId()

def getKeyFromString(keyId, data):
    msg = PGP_Message(util.ExtendedStringIO(data))
    return msg.getKeyByKeyId(keyId)

def seekKeyById(keyId, keyRing):
    if isinstance(keyRing, str):
        try:
            keyRing = util.ExtendedFile(keyRing, buffering = False)
        except (IOError, OSError) as e:
            # if we can't read/find the key, it's not there.
            return False
    msg = PGP_Message(keyRing)
    try:
        return next(msg.iterByKeyId(keyId))
    except StopIteration:
        return False

def exportKey(keyId, keyRing, armored=False):
    """Export the key from the keyring, performing the necessary locking

    @param keyId: the key ID
    @type keyId: str
    @param keyRing: the keyring from where the key is to be extracted
    @type keyRing: file or path
    @param armored: If True, exports the key in a Radix-64 encoding (armor)
    @type armored: bool
    @rtype: stream
    @return: the key in a stream
    """
    if isinstance(keyRing, str):
        try:
            keyRing = util.ExtendedFile(keyRing, buffering = False)
        except (IOError, OSError) as e:
            # if we can't read/find the key, it's not there.
            raise KeyNotFound(keyId)
    msg = PGP_Message(keyRing)
    key = msg.getKeyByKeyId(keyId)
    # if the key we requested was a subkey, use the main key
    if isinstance(key, PGP_SubKey):
        key = key.getParentPacket()
    sio = util.ExtendedStringIO()
    key.writeAll(sio)

    if armored:
        sio.seek(0)
        keyData = sio.read()
        sio.truncate(0)
        armorKeyData(keyData, sio)
    sio.seek(0)
    return sio

def armorKeyData(keyData, stream):
    """
    Write the Radix-64 encoded version of the key data.

    @param keyData: key data
    @type keyData: str
    @param stream: A stream open in write mode
    @type stream: file
    """
    assert(isinstance(keyData, str))
    assert(hasattr(stream, "write"))

    crc = CRC24(keyData).base64digest()

    stream.write("-----BEGIN PGP PUBLIC KEY BLOCK-----\n")
    stream.write("Version: Conary ")
    stream.write(constants.version)
    stream.write("\n\n")
    for line in textwrap.wrap(base64.b64encode(keyData), 72):
        stream.write(line)
        stream.write('\n')
    # Add the CRC
    stream.write('=')
    stream.write(crc)
    stream.write("\n-----END PGP PUBLIC KEY BLOCK-----")
    return stream

def verifySelfSignatures(keyId, stream):
    msg = PGP_Message(stream, start = 0)
    pkt = msg.getKeyByKeyId(keyId)

    return pkt.verifySelfSignatures()

def fingerprintToInternalKeyId(fingerprint):
    if len(fingerprint) == 0:
        return ''
    fp = fingerprint[-16:]
    return binascii.unhexlify(fp)

def binSeqToString(sequence):
    """sequence is a sequence of unsigned chars.
    Return the string with a corresponding char for each item"""
    return "".join([ chr(x) for x in sequence ])

def stringToAscii(sequence):
    """sequence is a sequence of characters.
    Return the string with the hex representation for each character"""
    return "".join("%02x" % ord(c) for c in sequence).upper()

def simpleS2K(passPhrase, hashAlg, keySize):
    # RFC 2440 3.6.1.1.
    r = ''
    iteration = 0
    keyLength = ((keySize + 7) // 8)
    while len(r) < keyLength:
        d = hashAlg(chr(0) * iteration)
        d.update(passPhrase)
        r += d.digest()
        iteration += 1
    return r[:keyLength]

def saltedS2K(passPhrase, hashAlg, keySize, salt):
    # RFC 2440 3.6.1.2.
    r = ''
    iteration = 0
    keyLength = ((keySize + 7) // 8)
    while(len(r) < keyLength):
        d = hashAlg()
        buf = chr(0) * iteration
        buf += salt + passPhrase
        d.update(buf)
        r += d.digest()
        iteration += 1
    return r[:keyLength]

def iteratedS2K(passPhrase, hashAlg, keySize, salt, count):
    # RFC 2440 3.6.1.3.
    r=''
    iteration = 0
    count=(16 + (count & 15)) << ((count >> 4) + 6)
    buf = salt + passPhrase
    keyLength = (keySize + 7) // 8
    while(len(r) < keyLength):
        d = hashAlg()
        d.update(iteration * chr(0))
        total = 0
        while (count - total) > len(buf):
            d.update(buf)
            total += len(buf)
        if total:
            d.update(buf[:count-total])
        else:
            d.update(buf)
        r += d.digest()
        iteration += 1
    return r[:keyLength]

def getUserIdsFromString(keyId, data):
    keyRing = util.ExtendedStringIO(data)
    key = seekKeyById(keyId, keyRing)
    if key is None:
        return []
    return list(key.getUserIds())

def getFingerprint(keyId, keyFile=''):
    if keyFile == '':
        if 'HOME' not in os.environ:
            keyFile = None
        else:
            keyFile=os.environ['HOME'] + '/.gnupg/pubring.gpg'
    try:
        keyRing = util.ExtendedFile(keyFile, buffering = False)
    except IOError:
        raise KeyNotFound(keyId, "Couldn't open keyring")
    keyRing.seek(0, SEEK_END)
    limit = keyRing.tell()
    if limit == 0:
        # no keys in a zero length file
        raise KeyNotFound(keyId, "Couldn't open keyring")
    keyRing.seek(0, SEEK_SET)
    msg = PGP_Message(keyRing)
    pkt = msg.getKeyByKeyId(keyId)
    return pkt.getKeyFingerprint()

def addKeys(keys, fpath):
    """Add keys to the file"""
    return addPackets(keys, fpath, "getKeyFingerprint",
        PGP_Message, "iterMainKeys")

def addKeyTimestampPackets(pkts, fpath):
    """Add key timestamp packets to the file"""
    return addPackets(pkts, fpath, "getKeyId",
        TimestampPacketDatabase, "iterTrustPackets")

def addPackets(pkts, fpath, pktIdFunc, messageFactory, streamIterFunc):
    """Add packets to the file. Return the packet IDs for the added packets"""
    # This code really expects the stream to be based on a file, since we need
    # a fileno() too
    import fcntl

    pktsDict = {}
    for k in pkts:
        pktId = getattr(k, pktIdFunc)()
        if pktId in pktsDict:
            pktsDict[pktId].merge(k)
        else:
            pktsDict[pktId] = k
    if not pktsDict:
        return []

    tmpfd, tmpfname = tempfile.mkstemp(prefix=os.path.basename(fpath),
                                       dir=os.path.dirname(fpath))
    tempf = util.ExtendedFdopen(tmpfd)

    pktIds = []

    # Lock the stream
    try:
        stream = file(fpath, "r+")
        while 1:
            streamfd = stream.fileno()
            fcntl.lockf(streamfd, fcntl.LOCK_EX)
            # We got the lock. Did the file change?
            # There is a possibility that another writer that was previously
            # holding the lock replaced the file, which would mean our current
            # stream is stale.
            newstream = file(fpath, "r+")
            oStat = os.fstat(streamfd)
            nStat = os.fstat(newstream.fileno())
            if (oStat.st_dev, oStat.st_ino) == (nStat.st_dev, nStat.st_ino):
                # Same file descriptor, we can continue
                del oStat, nStat
                break
            # Replace our existing stream and continue
            fcntl.lockf(streamfd, fcntl.LOCK_UN)
            stream = newstream

        # At this point, we have an exclusive lock on the stream
        msg = messageFactory(stream, start = 0)
        for ipkt in getattr(msg, streamIterFunc)():
            iPktId = getattr(ipkt, pktIdFunc)()
            if iPktId in pktsDict:
                ipkt.merge(pktsDict[iPktId])
                pktIds.append(iPktId)
                del pktsDict[iPktId]
            ipkt.writeAll(tempf)

        # Add the rest of the packets
        for pkt in pkts:
            pktId = getattr(pkt, pktIdFunc)()
            if pktId not in pktsDict:
                continue
            pkt.writeAll(tempf)
            del pktsDict[pktId]
            pktIds.append(pktId)

        # Now copy the keyring back
        tempf.close()
        os.rename(tmpfname, fpath)
        return pktIds
    finally:
        fcntl.lockf(streamfd, fcntl.LOCK_UN)

def verifyRFC2440Checksum(data):
    # RFC 2440 5.5.3 - Secret Key Packet Formats documents the checksum
    if len(data) < 2:
        return 0
    checksum = [ ord(x) for x in data[-2:] ]
    checksum = int2FromBytes(*checksum)
    runningCount = computeRFC2440Checksum(data[:-2])
    return (runningCount == checksum)

def computeRFC2440Checksum(data):
    runningCount=0
    for c in data:
        runningCount += ord(c)
        runningCount %= 65536
    return runningCount

def verifySHAChecksum(data):
    if len(data) < 20:
        return 0
    digest = computeSHAChecksum(data[:-20])
    return digest == data[-20:]

def computeSHAChecksum(data):
    m = digestlib.sha1()
    m.update(data)
    return m.digest()

def xorStr(str1, str2):
    return ''.join(chr(ord(x) ^ ord(y)) for x, y in zip(str1, str2))

def countKeys(keyRing):
    # counts the public and private keys in a key ring (does not count subkeys)
    msg = PGP_Message(keyRing)
    return len([pkt for pkt in msg.iterPackets()
        if pkt.tag in (PKT_SECRET_KEY, PKT_PUBLIC_KEY)])

def getFingerprints(keyRing):
    # returns the fingerprints for all keys in a key ring file
    msg = PGP_Message(keyRing)
    return [ x.getKeyFingerprint() for x in msg.iterKeys() ]

def readSignature(fileobj):
    """
    Read a signature packet from a stream.

    @param fileobj: a stream to read the key from
    @type fileobj: stream
    @rtype: PGP_Signature
    @return: the Signature packet
    @raise InvalidPacketError: if the file object does not contain a valid
    packet
    """
    sio = util.ExtendedStringIO()
    hasData = parseAsciiArmor(fileobj, sio)
    if not hasData:
        raise InvalidPacketError("No data found")

    msg = PGP_Message(sio, start = 0)
    # We can't really have a StopIteration here, if there was no packet in the
    # message we would have failed in parseAsciiArmor
    try:
        pkt = next(msg.iterPackets())
    except ShortReadError:
        raise InvalidPacketError("Error reading signature packet")
    if not isinstance(pkt, PGP_Signature):
        raise InvalidPacketError("Not a signature packet")
    return pkt

def parseAsciiArmor(asciiData, dest):
    """
    Parse an armored (Radix-64 encoded) PGP message.

    @param asciiData: the Radix-64 encoded PGP message
    @type asciiData: string or stream
    @param dest: a stream to deposit the message into
    @type dest: stream
    @return: True if data was decoded, False otherwise
    @rtype: bool
    @raise PGPError: if the CRC does not match the message
    """

    if hasattr(asciiData, 'read'):
        data = asciiData
    else:
        data = StringIO(asciiData)
    crc = None
    nextLine = data.read(1)
    if nextLine and (ord(nextLine[0]) & 0x80):
        # This is already a binary packet
        dest.write(nextLine)
        PGP_BasePacket._copyStream(data, dest)
        return True

    try:
        while(nextLine[0] != '-'):
            nextLine = data.readline()
        while (nextLine[0] != "\r") and (nextLine[0] != "\n"):
            nextLine = data.readline()
        buf = ""
        nextLine = data.readline()
        while(nextLine[0] != '=' and nextLine[0] != '-'):
            buf = buf + nextLine
            nextLine = data.readline()
        if nextLine[0] == '=':
            # This is the CRC
            crc = nextLine.strip()[1:]
    except IndexError:
        return False

    try:
        keyData = base64.b64decode(buf)
    except TypeError:
        return False
    if crc:
        crcobj = CRC24(keyData)
        ccrc = crcobj.base64digest()
        if crc != ccrc:
            raise PGPError("Message does not verify CRC checksum", crc, ccrc)

    dest.write(keyData)
    return True

def parseAsciiArmorKey(asciiData):
    """
    Parse an armored (Radix-64 encoded) PGP message.

    @param asciiData: the Radix-64 encoded PGP message
    @type asciiData: string or stream
    @return: the unencoded PGP messsage, or None if the encoded message was
        incorrect
    @rtype: string or None
    @raise PGPError: if the CRC does not match the message
    """
    sio = util.ExtendedStringIO()
    ret = parseAsciiArmor(asciiData, sio)
    if not ret:
        return None
    sio.seek(0)
    return sio.read()

class CRC24(object):
    __slots__ = [ '_crc' ]
    CRC24_INIT = 0xb704ce
    CRC24_POLY = 0x1864cfb

    def __init__(self, data=''):
        self._crc = self.CRC24_INIT
        self.update(data)

    def update(self, data):
        crc = self._crc
        for ch in data:
            crc ^= (ord(ch) << 16)
            for i in range(8):
                crc <<= 1
                if crc & 0x1000000:
                    crc ^= self.CRC24_POLY
        self._crc = crc

    def digest(self):
        r = self._crc & 0xffffff
        return chr((r >> 16) & 0xff) + chr((r >> 8) & 0xff) + chr(r & 0xff)

    def base64digest(self):
        return base64.b64encode(self.digest())

def _crc24(stream):
    if isinstance(stream, str):
        stream = StringIO(stream)
    crc = CRC24()
    while 1:
        buf = stream.read(8192)
        if not buf:
            break
        crc.update(buf)
    return crc

def crc24(stream):
    return _crc24(stream).digest()

def crc24base64(stream):
    return _crc24(stream).base64digest()

class PacketTypeDispatcher(object):
    _registry = {}

    @classmethod
    def addPacketType(cls, klass):
        cls._registry[klass.tag] = klass

    @classmethod
    def getClass(cls, tag):
        return cls._registry.get(tag, PGP_Packet)


class PGP_Message(object):
    __slots__ = ['_f', 'pos']
    PacketDispatcherClass = PacketTypeDispatcher

    def __init__(self, message, start = -1):
        self._f = self._openStream(message)
        self.pos = start

    @classmethod
    def _openStream(cls, message):
        if hasattr(message, "pread"):
            return message
        if isinstance(message, str):
            # Assume a path
            return util.ExtendedFile(message, buffering = False)
        # Be tolerant, accept non-Extended objects
        if hasattr(message, 'fileno') and not hasattr(message, "pread"):
            # Try to reopen as an ExtendedFile. We have to dup the file
            # descriptor, otherwise it gets closed unexpectedly when the
            # original message object gets out of scope.  Need to make
            # sure the file buffer is flushed before dup'ing the fd.
            message.flush()
            f = util.ExtendedFdopen(os.dup(message.fileno()))
            f.seek(message.tell())
            return f
        raise MalformedKeyRing("Not an ExtendedFile object")

    def _getPacket(self):
        pkt = self.newPacketFromStream(self._f, start = self.pos)
        return pkt

    def iterPackets(self):
        pkt = self._getPacket()
        while 1:
            if pkt is None:
                break
            yield pkt
            pkt = next(pkt)

    def iterTrustPackets(self):
        """Iterate over all trust packets"""
        for pkt in self.iterPackets():
            if isinstance(pkt, PGP_Trust):
                yield pkt

    def iterKeys(self):
        """Iterate over all keys"""
        for pkt in self.iterMainKeys():
            yield pkt
            for subkey in pkt.iterSubKeys():
                yield subkey

    def iterMainKeys(self):
        """Iterate over main keys"""
        for pkt in self.iterPackets():
            if isinstance(pkt, PGP_MainKey):
                try:
                    pkt.initSubPackets()
                except InvalidBodyError:
                    # Skip this key
                    continue
                yield pkt

    def iterByKeyId(self, keyId):
        """Iterate over the keys with this key ID"""
        for pkt in self.iterKeys():
            if pkt.hasKeyId(keyId):
                yield pkt

    def getKeyByKeyId(self, keyId):
        try:
            return next(self.iterByKeyId(keyId))
        except StopIteration:
            raise KeyNotFound(keyId)

    def seekParentKey(self, keyId):
        """Get a parent key with this keyId or with a subkey with this
        keyId"""
        for pkt in self.iterKeys():
            if isinstance(pkt, PGP_MainKey):
                if pkt.getKeyFingerprint().endswith(keyId.upper()):
                    # This is a main key and it has the keyId we need
                    return pkt
            elif isinstance(pkt, PGP_SubKey):
                if pkt.getKeyFingerprint().endswith(keyId.upper()):
                    # This is a subkey, return the main key
                    return pkt.getMainKey()

    @classmethod
    def newPacketFromStream(cls, stream, start = -1):
        stream = cls._openStream(stream)
        return PGP_PacketFromStream(cls).read(stream, start = start)

    @classmethod
    def newPacket(cls, tag, bodyStream, newStyle = False, minHeaderLen = 2):
        """Create a new Packet"""
        typeDispatcher = cls.PacketDispatcherClass
        klass = typeDispatcher.getClass(tag)
        pkt = klass(bodyStream, newStyle = newStyle, minHeaderLen = minHeaderLen)
        if not hasattr(pkt, 'tag'): # No special class for this packet
            pkt.setTag(tag)
        pkt._msgClass = cls
        return pkt


class PGP_PacketFromStream(object):
    __slots__ = ['_f', 'tag', 'headerLength', 'bodyLength', '_msgClass']
    def __init__(self, msgClass):
        self.tag = None
        self.headerLength = self.bodyLength = 0
        self._f = None
        self._msgClass = msgClass

    def read(self, fileobj, start = -1):
        """Create packet from stream
        Return a PGP_Packet instance"""
        self._f = util.SeekableNestedFile(fileobj, 1, start)
        first = self._f.read(1)
        if not first:
            # No more packets to read from this file object
            return

        first = ord(first)

        if not (first & 0x80):
            raise InvalidPacketError("First bit not 1")

        if first & 0x40:
            newStyle = True
            self._newHeader(first)
        else:
            newStyle = False
            self._oldHeader(first)

        if self.bodyLength is None:
            # Indeterminate length; assume end of file
            fileobj.seek(0, 2)
            self.bodyLength = fileobj.tell() - (self._f.start + self.headerLength)
        _bodyStream = util.SeekableNestedFile(self._f.file,
                     self.bodyLength, self._f.start + self.headerLength)
        if self.bodyLength:
            # Read one octet from the end
            data = _bodyStream.pread(1, self.bodyLength - 1)
            if not data:
                raise ShortReadError(self.bodyLength, -1)
            _bodyStream.seek(0)
        nextStreamPos = self._f.start + self.headerLength + self.bodyLength

        pkt = self._msgClass.newPacket(self.tag, _bodyStream,
                                      newStyle = newStyle,
                                      minHeaderLen = self.headerLength)
        pkt.setNextStream(fileobj, nextStreamPos)
        return pkt

    def _oldHeader(self, first):
        self.tag = (first & 0x3C) >> 2
        lengthType = first & 0x03
        if lengthType in (0, 1, 2):
            headerLength = lengthType + 2
            if lengthType == 2:
                headerLength += 1
            blLen = headerLength - 1
            # Extend file
            self._f.__init__(self._f.file, headerLength, self._f.start)
            self._f.seek(1)
        else:
            if self.tag != PKT_COMPRESSED_DATA:
                raise NotImplementedError("Indeterminate length not supported")
            self.headerLength = 1
            self.bodyLength = None
            return

        self.headerLength = headerLength
        bbytes = PGP_BasePacket._readBin(self._f, blLen)

        bodyLength = 0
        for i in bbytes:
            bodyLength <<= 8
            bodyLength += i
        self.bodyLength = bodyLength

    def _newHeader(self, first):
        # New style
        self.tag = (first & 0x3F)
        # Extend by one more byte
        self._f.__init__(self._f.file, 2, self._f.start)
        self._f.seek(1)

        body1, = PGP_BasePacket._readBin(self._f, 1)

        if body1 & 0xC0 == 0:
            # 4.2.2.1. One-Octet Lengths (less than 192)
            self.headerLength = 2
            self.bodyLength = body1
            return

        if 192 <= body1 < 223:
            # 4.2.2.2. Two-Octet Lengths (between 192 and 223):
            self.headerLength = 3
            self._f.__init__(self._f.file, self.headerLength, self._f.start)
            self._f.seek(2)

            body2, = PGP_BasePacket._readBin(self._f, 1)
            self.bodyLength = len2bytes(body1, body2)
            return

        if body1 == 0xFF:
            # 4.2.2.3. Five-Octet Lengths (exactly 255)
            self.headerLength = 6

            self._f.__init__(self._f.file, self.headerLength, self._f.start)
            self._f.seek(2)

            rest = PGP_BasePacket._readBin(self._f, 4)
            self.bodyLength = int4FromBytes(*rest)
            return
        # 4.2.2.4. Partial Body Lengths
        partialBodyLength = 1 << (body1 & 0x1F)
        raise NotImplementedError("Patial body lengths not implemented")

class PGP_BasePacket(object):
    __slots__ = ['_bodyStream', 'headerLength', 'bodyLength',
                 '_newStyle', '_nextStream', '_nextStreamPos',
                 '_parentPacket', '_msgClass']

    tag = None
    BUFFER_SIZE = 16384

    _parentPacketTypes = set()

    def __init__(self, bodyStream, newStyle = False, minHeaderLen = 2):
        assert hasattr(bodyStream, 'pread')
        self._newStyle = newStyle
        self._bodyStream = bodyStream
        self.bodyLength = self._getBodyLength()
        self.headerLength = self._getHeaderLength(minHeaderLen = minHeaderLen)
        # Keep a reference to the next stream we link to
        self._nextStream = None
        self._nextStreamPos = 0
        self._parentPacket = None
        self.setUp()
        self.initialize()

    def setNextStream(self, stream, pos):
        if stream:
            assert hasattr(stream, 'pread')
        self._nextStream = stream
        self._nextStreamPos = pos

    def setParentPacket(self, pkt, clone = True):
        """Add a parent packet to this packet"""
        if pkt is None:
            self._parentPacket = None
            return

        assert pkt.tag in self._parentPacketTypes
        if clone:
            self._parentPacket = pkt.clone()
        else:
            self._parentPacket = pkt

    def getParentPacket(self):
        return self._parentPacket


    def clone(self):
        """Produce another packet identical with this one"""
        # Create new body stream sharing the same file
        newBodyStream = util.SeekableNestedFile(self._bodyStream.file,
            self._bodyStream.size, self._bodyStream.start)

        newPkt = self._msgClass.newPacket(self.tag, newBodyStream,
                    newStyle = self._newStyle, minHeaderLen = self.headerLength)
        newPkt.setNextStream(self._nextStream, self._nextStreamPos)
        newPkt.setParentPacket(self.getParentPacket(), clone = False)
        return newPkt

    def setUp(self):
        """setUp is executed at object creation time."""
        pass

    def initialize(self):
        """initialize is executed at object creation time, after setUp(),
        and generally everywhere the state has to be reset.
        To be overridden by various subclasses"""
        pass

    def _getHeaderLength(self, minHeaderLen = 2):
        # bsrepr is the body size representation
        if self._newStyle:
            # For new style, we can't really force the minimum header length
            if self.bodyLength < 192:
                return 2
            if 192 <= self.bodyLength < 8384:
                return 3
            return 6
        if minHeaderLen > 3 or self.bodyLength > 65535:
            # 4-byte packet length field
            bsrepr = 4
        elif minHeaderLen > 2 or self.bodyLength > 255:
            # 2-byte packet length field
            bsrepr = 2
        else:
            # 1 byte packet-length field
            bsrepr = 1

        return bsrepr + 1

    def _getBodyLength(self):
        """Determine the body length"""
        pos = self._bodyStream.tell()
        self._bodyStream.seek(0, SEEK_END)
        blen = self._bodyStream.tell()
        self._bodyStream.seek(pos, SEEK_SET)
        return blen

    def writeHeader(self, stream):
        # Generate packet header
        if self._newStyle:
            return self._writeHeaderNewStyle(stream)

        return self._writeHeaderOldStyle(stream)

    def _writeHeaderNewStyle(self, stream):
        # bit 7 is set, bit 6 is set (new packet format)
        fbyte = 0xC0

        # Add the tag.
        fbyte |= self.tag

        stream.write(chr(fbyte))

        if self.headerLength == 6:
            # 5-byte body length length, first byte is 255
            stream.write(chr(255))
            blen = self.bodyLength & 0xffffffff
            self._writeBin(stream, len4ToBytes(blen))
            return
        if self.headerLength == 3:
            # 2-byte body length length
            if not (192 <= self.bodyLength < 8384):
                raise InvalidPacketError("Invalid body length %s for "
                    "header length %s" % (self.bodyLength, self.headerLength))
            self._writeBin(stream, len2ToBytes(self.bodyLength))
            return
        if self.headerLength == 2:
            # 1-byte body length length
            if not (self.bodyLength < 192):
                raise InvalidPacketError("Invalid body length %s for "
                    "header length %s" % (self.bodyLength, self.headerLength))
            stream.write(chr(self.bodyLength))
            return
        raise InvalidPacketError("Invalid header length %s" % self.headerLength)

    def _writeHeaderOldStyle(self, stream):
        # bit 7 is set, bit 6 is not set (old packet format)
        fbyte = 0x80

        # Add the tag, bits 5432. For old-style headers, they are represented
        # on 4 bits only.
        fbyte |= (0x0f & self.tag) << 2

        # bsrepr is the body size representation
        if self.headerLength == 5:
            # 4-byte packet length field
            fbyte |= 2
            bsrepr = 4
        elif self.headerLength == 3:
            # 2-byte packet length field
            fbyte |= 1
            bsrepr = 2
        else:
            # 1 byte packet-length field (no changes to first byte needed)
            bsrepr = 1

        stream.write(chr(fbyte))
        # prepare the size octets
        for i in range(1, bsrepr + 1):
            stream.write(chr((self.bodyLength >> ((bsrepr - i) << 3)) & 0xff))

    def writeBody(self, stream):
        self.resetBody()
        self._copyStream(self._bodyStream, stream)

    def write(self, stream):
        self.writeHeader(stream)
        self.writeBody(stream)

    def writeAll(self, stream):
        # Write this packet and all subpackets
        self.write(stream)
        for pkt in self.iterSubPackets():
            pkt.write(stream)

    def resetBody(self):
        self._bodyStream.seek(0)

    def readBody(self, bytes = -1):
        """Read bytes from stream"""
        return self._bodyStream.pread(bytes, 0)

    def seek(self, pos, whence = SEEK_SET):
        return self._bodyStream.seek(pos, whence)

    @staticmethod
    def _readExact(stream, bytes):
        """Read bytes from stream, checking that enough bytes were read"""
        data = stream.read(bytes)
        if bytes > 0 and len(data) != bytes:
            raise ShortReadError(bytes, len(data))
        return data

    @staticmethod
    def _readBin(stream, bytes):
        """Read bytes from stream, checking that enough bytes were read.
        Return a list of bytes"""
        return [ ord(x) for x in PGP_BasePacket._readExact(stream, bytes) ]

    def readExact(self, bytes):
        """Read bytes from stream, checking that enough bytes were read"""
        return self._readExact(self._bodyStream, bytes)

    def readBin(self, bytes):
        """Read bytes from stream, checking that enough bytes were read.
        Return a list of bytes"""
        return self._readBin(self._bodyStream, bytes)

    @staticmethod
    def _writeBin(stream, bytes):
        """Write the bytes in binary format"""
        for b in bytes:
            stream.write(chr(b))

    @staticmethod
    def _copyStream(src, dst):
        """Copy stream src into dst"""
        while 1:
            buf = src.read(PGP_BasePacket.BUFFER_SIZE)
            if not buf:
                break
            dst.write(buf)

    @staticmethod
    def _updateHash(hashObj, stream):
        """Update the hash object with data from the stream"""
        while 1:
            buf = stream.read(PGP_BasePacket.BUFFER_SIZE)
            if not buf:
                break
            hashObj.update(buf)

    @staticmethod
    def _updateHashBin(hashObj, binSeq):
        """
        Update the hash object with binary octets from a sequence of octets

        @param hashObj: a hash object
        @param binSeq: a sequence of bytes
        """
        for b in binSeq:
            hashObj.update(chr(b))

    @staticmethod
    def checkStreamLength(stream, length):
        """Checks that the stream has exactly the length specified extra
        bytes from the current position"""
        pos = stream.tell()
        stream.seek(0, SEEK_END)
        if length != stream.tell() - pos:
            raise ShortReadError(length, stream.tell() - pos)
        # SeekableNestedFiles will happily pass the previous test, so be more
        # devious: seek to the (end - 1), try to read one byte
        # Determining the actual length is hard, but worth it
        i = stream.tell() - 1
        while i > pos:
            stream.seek(i, SEEK_SET)
            if len(stream.read(1)) == 1:
                break
            i -= 1
        if length != stream.tell() - pos:
            raise ShortReadError(length, stream.tell() - pos)
        # Rewind
        stream.seek(pos)

    @staticmethod
    def readTimestamp(stream):
        """Reads a timestamp from the stream"""
        stream.seek(0)
        PGP_BasePacket.checkStreamLength(stream, 4)
        return len4bytes(*PGP_BasePacket._readBin(stream, 4))

    def isEmpty(self):
        return self.headerLength == 0

    def __next__(self):
        if self._nextStream is None:
            raise StopIteration()

        newPkt = self._msgClass.newPacketFromStream(self._nextStream,
                                                       self._nextStreamPos)
        if newPkt is None:
            raise StopIteration()

        return newPkt

    def getBodyStream(self):
        return self._bodyStream

    def _iterSubPackets(self, limitTags):
        """Iterate over the packets following this packet, until we reach a
        packet of the specified type as the limit"""
        pkt = next(self)
        while not pkt.isEmpty() and pkt.tag not in limitTags:
            yield pkt
            pkt = next(pkt)

    @staticmethod
    def _hashSet(items):
        """Hashes the items in items through sha, and return a set of the
        computed digests.
        Each item is expected to be a stream"""

        ret = set([])
        for stream in items:
            stream.seek(0)
            hobj = digestlib.sha1()
            PGP_BasePacket._updateHash(hobj, stream)
            ret.add(hobj.digest())
        return ret

class PGP_Packet(PGP_BasePacket):
    """Anonymous PGP packet"""
    __slots__ = ['tag']
    def setTag(self, tag):
        self.tag = tag

class PGP_BaseKeySig(PGP_BasePacket):
    """Base class for keys and signatures"""
    __slots__ = []

    def _getMPICount(self, algType):
        """This returns the right number of MPIs for converting a private key
        to a public key. Overwrite in subclasses for any other usage"""
        if algType in PK_ALGO_ALL_RSA:
            numMPI = 2
        elif algType in PK_ALGO_ALL_ELGAMAL:
            numMPI = 3
        elif algType == PK_ALGO_DSA:
            numMPI = 4
        else:
            # unhandled algorithm
            raise UnsupportedEncryptionAlgorithm(algType)
        return numMPI


    def _readMPIs(self, stream, algType, discard = True):
        """Read the corresponding number of MPIs for the specified algorithm
        type from the stream
        @raise UnsupportedEncryptionAlgorithm:
        """
        numMPI = self._getMPICount(algType)
        return self._readCountMPIs(stream, numMPI, discard = discard)

    @staticmethod
    def _readCountMPIs(stream, count, discard = True):
        """Read count MPIs from the current position in stream.
        @raise UnsupportedEncryptionAlgorithm:
        """

        ret = []
        for i in range(count):
            buf = PGP_BaseKeySig._readBin(stream, 2)
            mLen = (int2FromBytes(*buf) + 7) // 8
            if discard:
                # Skip the MPI len
                PGP_BaseKeySig._readExact(stream, mLen)
                ret.append(None)
            else:
                data = PGP_BaseKeySig._readBin(stream, mLen)
                r = 0
                for i in data:
                    r = r * 256 + i
                ret.append(r)
        return ret

    @staticmethod
    def _writeMPI(stream, mpi):
        bytes = []
        while mpi != 0:
            bytes.append(mpi & 0xFF)
            mpi >>= 8

        # Compute length in bits
        if not bytes:
            # Zero length
            bitlen = 0
        else:
            # The only variable part can be the one in the most significant
            # octet, which is the last
            bitlen = 8 * (len(bytes) - 1) + num_bitLen(bytes[-1])
        PGP_BaseKeySig._writeBin(stream, int2ToBytes(bitlen))
        PGP_BaseKeySig._writeBin(stream, reversed(bytes))


    def skipMPIs(self, stream, algType):
        self._readMPIs(stream, algType, discard = True)

    def readMPIs(self, stream, algType):
        return self._readMPIs(stream, algType, discard = False)

class PGP_Signature(PGP_BaseKeySig):
    __slots__ = ['version', 'sigType', 'pubKeyAlg', 'hashAlg', 'hashSig',
                 'mpiFile', 'signerKeyId', 'hashedFile', 'unhashedFile',
                 'creation', '_parsed', '_sigDigest', '_parentPacket',
                 '_hashedSubPackets', '_unhashedSubPackets', '_verifies',]
    tag = PKT_SIG

    _parentPacketTypes = set(PKT_ALL_KEYS).union(PKT_ALL_USER)

    HashAlgList = {
        1: digestlib.md5,
        2: digestlib.sha1,
        8: digestlib.sha256,
        9: digestlib.sha384,
        10: digestlib.sha512,
        11: digestlib.sha224,
    }
    # hashPads from RFC2440 section 5.2.2
    HashAlgPads = {
        1:  "\x30\x20\x30\x0C\x06\x08\x2A\x86"
            "\x48\x86\xF7\x0D\x02\x05\x05\x00"
            "\x04\x10",
        2:  "\x30\x21\x30\x09\x06\x05\x2b\x0E"
            "\x03\x02\x1A\x05\x00\x04\x14",
        8:  "\x30\x31\x30\x0d\x06\x09\x60\x86"
            "\x48\x01\x65\x03\x04\x02\x01\x05"
            "\x00\x04\x20",
        9:  "\x30\x41\x30\x0d\x06\x09\x60\x86"
            "\x48\x01\x65\x03\x04\x02\x02\x05"
            "\x00\x04\x30",
        10: "\x30\x51\x30\x0d\x06\x09\x60\x86"
            "\x48\x01\x65\x03\x04\x02\x03\x05"
            "\x00\x04\x40",
        11: "\x30\x31\x30\x0d\x06\x09\x60\x86"
            "\x48\x01\x65\x03\x04\x02\x04\x05"
            "\x00\x04\x1C",
    }

    def initialize(self):
        self.version = self.sigType = self.pubKeyAlg = self.hashAlg = None
        self.hashSig = self.mpiFile = self.signerKeyId = None
        self.hashedFile = self.unhashedFile = None
        self.creation = None
        self._parsed = False
        self._sigDigest = None
        self._hashedSubPackets = None
        self._unhashedSubPackets = None
        self._verifies = None

    def parse(self, force = False):
        """Parse the signature body and initializes the internal data
        structures for other operations"""
        if self._parsed and not force:
            return
        self.resetBody()
        # Reset all internal state
        self.initialize()

        sigVersion, = self.readBin(1)
        if sigVersion not in [2, 3, 4]:
            raise InvalidBodyError("Invalid signature version %s" % sigVersion)
        self.version = sigVersion
        # Version 2 signatures are documented in RFC1991, and are identical to
        # version 3 signatures
        if sigVersion in [2, 3]:
            self._readSigV3()
        else:
            self._readSigV4()
        self._parsed = True

    def _getMPICount(self, algType):
        if algType in PK_ALGO_ALL_RSA:
            numMPI = 1
        elif algType in PK_ALGO_ALL_ELGAMAL:
            numMPI = 2
        elif algType == PK_ALGO_DSA:
            numMPI = 2
        else:
            # unhandled algorithm
            raise UnsupportedEncryptionAlgorithm(algType)
        return numMPI

    def parseMPIs(self):
        self.parse()
        assert hasattr(self, 'mpiFile') and self.mpiFile is not None
        self.mpiFile.seek(0)
        return self.readMPIs(self.mpiFile, self.pubKeyAlg)

    def _readSigV3(self):
        hLen, sigType = self.readBin(2)
        if hLen != 5:
            raise PGPError('Expected 5 octets of length of hashed material, '
                           'got %d' % hLen)

        self.creation = self.readBin(4)
        self.signerKeyId = self.readBin(8)
        pkAlg, hashAlg = self.readBin(2)
        hashSig = self.readExact(2)

        # MPI data
        mpiFile = util.SeekableNestedFile(self._bodyStream,
            self.bodyLength - self._bodyStream.tell())

        self.sigType = sigType
        self.pubKeyAlg = pkAlg
        self.hashAlg = hashAlg
        self.hashSig = hashSig
        self.mpiFile = mpiFile

    def _readSigV4(self):
        sigType, pkAlg, hashAlg = self.readBin(3)
        # Hashed subpacket data length
        arr = self.readBin(2)
        hSubPktLen = (arr[0] << 8) + arr[1]
        hSubpktsFile = util.SeekableNestedFile(self._bodyStream, hSubPktLen)

        # Skip over the packets, we've decoded them already
        self.seek(hSubPktLen, SEEK_CUR)

        # Unhashed subpacket data length
        arr = self.readBin(2)
        uSubPktLen = (arr[0] << 8) + arr[1]

        uSubpktsFile = util.SeekableNestedFile(self._bodyStream, uSubPktLen)
        # Skip over the packets, we've decoded them already
        self.seek(uSubPktLen, SEEK_CUR)

        # Two-octet field holding left 16 bits of signed hash value.
        hashSig = self.readExact(2)

        # MPI data
        mpiFile = util.SeekableNestedFile(self._bodyStream,
            self.bodyLength - self._bodyStream.tell())

        self.sigType = sigType
        self.pubKeyAlg = pkAlg
        self.hashAlg = hashAlg
        self.mpiFile = mpiFile
        self.hashSig = hashSig
        self.hashedFile = hSubpktsFile
        self.unhashedFile = uSubpktsFile

    def _writeSigV4(self):
        self.parse()

        stream = util.ExtendedStringIO()
        self.hashedFile.seek(0, SEEK_END)

        self.unhashedFile.seek(0, SEEK_END)
        unhashedLen = self.unhashedFile.tell()

        self._writeBin(stream, [4, self.sigType, self.pubKeyAlg, self.hashAlg])

        for sstream in [ self.hashedFile, self.unhashedFile ]:
            # Determine length
            sstream.seek(0, SEEK_END)
            slen = sstream.tell()
            # subpackets data length
            self._writeBin(stream, int2ToBytes(slen))
            # And the stream itself
            sstream.seek(0)
            self._copyStream(sstream, stream)

        # 2-octet hash sig
        stream.write(self.hashSig)

        # MPI file
        self.mpiFile.seek(0)
        self._copyStream(self.mpiFile, stream)
        return stream

    def _getSubpacketInt4(self, subpacketTypes):
        stream = self._getSubpacketStream(subpacketTypes)
        if stream is None:
            return None
        return int4FromBytes(*self._readBin(stream, 4))

    def _getSubpacketStream(self, subpacketTypes):
        pkts = [ x[1] for x in self.decodeHashedSubpackets()
                 if x[0] in subpacketTypes ]
        if not pkts:
            return None
        pkt = pkts[0]
        pkt.seek(0, SEEK_SET)
        return pkt

    def getCreation(self):
        """Return the signature creation timestamp, or 0 if no creation time
        is available"""
        if self.creation is not None:
            return self.creation
        creation = self._getSubpacketInt4([SIG_SUBPKT_CREATION])
        if creation is None:
            creation = 0
        self.creation = creation
        return self.creation

    def getExpiration(self):
        """Return the expiration offset, or None if the signature does not
        expire"""
        return self._getSubpacketInt4([SIG_SUBPKT_SIG_EXPIRE])

    def getKeyExpiration(self):
        """Return the key expiration offset, or None if the signature does not
        contain one"""
        return self._getSubpacketInt4([SIG_SUBPKT_KEY_EXPIRE])

    def getTrust(self):
        """Return the trust level, the trust amount and the trust regex for
        this signature"""
        spktTypes = set([SIG_SUBPKT_TRUST, 0x80 | SIG_SUBPKT_TRUST])
        stream = self._getSubpacketStream(spktTypes)
        if stream is None:
            return None, None, None
        tlevel, tamt = self._readBin(stream, 2)

        # Look for a trust regex
        # critical packets are ANDed with 0x80
        spktTypes = set([SIG_SUBPKT_REGEX, 0x80 | SIG_SUBPKT_REGEX])
        stream = self._getSubpacketStream(spktTypes)
        if stream is None:
            return tlevel, tamt, None
        # Trust packet is NULL-terminated
        tregex = stream.read()[:-1]
        return tlevel, tamt, tregex

    def rewriteBody(self):
        """Re-writes the body after the signature has been modified"""
        if not (isinstance(self.unhashedFile, util.ExtendedStringIO) or
                isinstance(self.hashedFile, util.ExtendedStringIO)):
            # Not changed
            return

        # Re-write ourselves
        bodyStream = self._writeSigV4()
        ns, nsp = self._nextStream, self._nextStreamPos
        parentPkt = self._parentPacket
        self.__init__(bodyStream, newStyle = self._newStyle)
        self.setNextStream(ns, nsp)
        self.setParentPacket(parentPkt)
        self.initialize()

    def getSigId(self):
        """Get the key ID of the issuer for this signature.
        Return None if the packet did not contain an issuer key ID"""
        self.parse()
        if self.version in [2, 3]:
            assert self.signerKeyId is not None
            return binSeqToString(self.signerKeyId)
        # Version 3 packets should have already set signerKeyId
        assert self.version == 4
        for spktType, dataf in self.decodeUnhashedSubpackets():
            if spktType != SIG_SUBPKT_ISSUER_KEYID:
                continue
            # Verify it only contains 8 bytes
            dataf.seek(0, SEEK_SET)
            try:
                self.checkStreamLength(dataf, 8)
            except ShortReadError as e:
                raise InvalidPacketError("Expected %s bytes, got %s instead" %
                    (e.expected, e.actual))
            self.signerKeyId = self._readBin(dataf, 8)
            return binSeqToString(self.signerKeyId)

    def getSignerKeyId(self):
        return stringToAscii(self.getSigId())

    def decodeHashedSubpackets(self):
        self.parse()
        if self._hashedSubPackets is None:
            self._hashedSubPackets = list(self._decodeSigSubpackets(self.hashedFile))
        return self._hashedSubPackets

    def decodeUnhashedSubpackets(self):
        self.parse()
        if self._unhashedSubPackets is None:
            self._unhashedSubPackets = list(self._decodeSigSubpackets(self.unhashedFile))
        return self._unhashedSubPackets

    @staticmethod
    def _decodeSigSubpackets(fobj):
        fobj.seek(0, SEEK_END)
        ocount = fobj.tell()
        fobj.seek(0)
        while fobj.tell() < ocount:
            yield PGP_Signature._getNextSubpacket(fobj)

    @staticmethod
    def _getNextSubpacket(fobj):
        len0, = PGP_BaseKeySig._readBin(fobj, 1)

        if len0 < 0xC0:
            pktlenlen = 1
            pktlen = len0
        elif len0 == 0xFF:
            pktlenlen = 5
            data = PGP_BaseKeySig._readBin(fobj, 4)
            pktlen = len4bytes(*data)
        else:
            pktlenlen = 2
            len1, = PGP_BaseKeySig._readBin(fobj, 1)
            pktlen = len2bytes(len0, len1)

        spktType, = PGP_BaseKeySig._readBin(fobj, 1)

        # The packet length includes the subpacket type
        dataf = util.SeekableNestedFile(fobj, pktlen - 1)
        # Do we have enough data?
        try:
            PGP_Signature.checkStreamLength(dataf, pktlen - 1)
        except ShortReadError as e:
            raise ShortReadError(pktlen + pktlenlen, e.actual + pktlenlen + 1)
        dataf.seek(0, SEEK_SET)

        # Skip the data
        fobj.seek(pktlen - 1, SEEK_CUR)
        return spktType, dataf

    def _writeSigPacketsToStream(self):
        self.parse()
        sio = util.ExtendedStringIO()
        parentPacket = self.getParentPacket()
        # XXX we could probably rewrite this if/then/else
        if isinstance(parentPacket, PGP_MainKey):
            parentPacket.toPublicKey(minHeaderLen = 3).write(sio)
        elif isinstance(parentPacket, (PGP_SubKey, PGP_UserID)):
            pkpkt = parentPacket.getParentPacket().toPublicKey(minHeaderLen = 3)
            pkpkt.write(sio)
            if isinstance(parentPacket, PGP_UserID):
                parentPacket.writeHash(sio, keyVersion = self.version)
            else:
                parentPacket.toPublicKey(minHeaderLen = 3).write(sio)
        else:
            raise InvalidPacketError("Unexpected parent", self._parentPacket)
        return sio

    def resetSignatureHash(self):
        self._sigDigest = None
        self._verifies = None

    def getSignatureHash(self):
        """Compute the signature digest"""
        if self._sigDigest is not None:
            return self._sigDigest

        sio = self._writeSigPacketsToStream()

        self._sigDigest = self._computeSignatureHash(sio)
        return self._sigDigest

    def getDocumentHash(self, stream):
        """
        Compute the hash over the supplied document.

        @param stream: stream containing the document to be hashed
        @type stream: file

        @rtype: str
        @return: the hash of the supplied document
        """

        self.parse()
        if self.sigType != SIG_TYPE_BINARY_DOC:
            raise PGPError("Non-binary documents not supported")

        digest = self._computeSignatureHash(stream)
        return digest

    def getShortSigHash(self):
        """Return the 16-leftmost bits for the signature hash"""
        self.parse()
        return self.hashSig

    def setShortSigHash(self, val):
        """Set the 16-leftmost bits"""
        assert(len(val) == 2)
        self.hashSig = val

    def merge(self, other):
        """Merge this signature with the other signature.
        Returns True if it modified the current packet"""
        assert self.tag == other.tag
        # The signed part of the signature is immutable, there is no way we
        # can merge it. The only things we might be able to merge are the
        # unhashed signature subpackets
        # However, gpg does not do that, so we will not do that either
        if self.hashSig != other.hashSig:
            raise MergeError("Signature packets with different hash")
        if self.getSignatureHash() != other.getSignatureHash():
            raise MergeError("Signature packets with different hash")
        # Not much more to do here
        return False

    def _prepareSubpackets(self):
        # XXX this is most likely going to change
        if self._unhashedSubPackets is not None:
            stream = util.ExtendedStringIO()
            for spktType, spktStream in self._unhashedSubPackets:
                self._writeSubpacket(stream, spktType, spktStream)
            self.unhashedFile = stream
        if self._hashedSubPackets is not None:
            stream = util.ExtendedStringIO()
            for spktType, spktStream in self._hashedSubPackets:
                self._writeSubpacket(stream, spktType, spktStream)
            self.hashedFile = stream
        self._parsed = True

    @staticmethod
    def _writeSubpacket(stream, spktType, spktStream):
        """Write the subpacket into the stream"""
        # First, determine the subpacket length
        spktStream.seek(0, SEEK_END)
        spktLen = spktStream.tell()
        spktStream.seek(0, SEEK_SET)

        # The subpacket length includes the type octet
        spktLen += 1

        header = []
        if spktLen < 192:
            # 1-octet length
            header.append(spktLen)
        elif spktLen < 16320:
            # 2-octet length
            header.extend(len2ToBytes(spktLen))
        else:
            # 5-octet length
            header.append(255)
            header.extend(len4ToBytes(spktLen))
        for d in header:
            stream.write(chr(d))
        # Type
        stream.write(chr(spktType))
        PGP_Signature._copyStream(spktStream, stream)

    def _completeHashV3(self, hashObj):
        self._updateHashBin(hashObj, [self.sigType])
        self._updateHashBin(hashObj, self.creation)

    def _completeHashV4(self, hashObj):
        # (re)compute the hashed packet subpacket data length
        self.hashedFile.seek(0, SEEK_END)
        hSubPktLen = self.hashedFile.tell()
        self.hashedFile.seek(0, SEEK_SET)

        # Write signature version, sig type, pub alg, hash alg
        self._updateHashBin(hashObj, [ self.version, self.sigType,
                                       self.pubKeyAlg, self.hashAlg ])
        # Write hashed data length
        self._updateHashBin(hashObj, int2ToBytes(hSubPktLen))
        # Write the hashed data
        self._updateHash(hashObj, self.hashedFile)

        # We've added 6 bytes for the header
        dataLen = hSubPktLen + 6

        # Append trailer - 6-byte trailer
        self._updateHashBin(hashObj, [ 0x04, 0xFF,
            (dataLen // 0x1000000) & 0xFF, (dataLen // 0x10000) & 0xFF,
            (dataLen // 0x100) & 0xFF, dataLen & 0xFF ])

    def _computeSignatureHash(self, dataFile):
        """Compute the signature digest for this signature, using the
        key serialized in dataFile"""
        self.parse()

        hashFunc = self.HashAlgList.get(self.hashAlg)
        if hashFunc is None:
            raise UnsupportedHashAlgorithm(self.hashAlg)
        hashObj = hashFunc()

        dataFile.seek(0, SEEK_SET)
        self._updateHash(hashObj, dataFile)

        if self.version == 3:
            self._completeHashV3(hashObj)
        elif self.version == 4:
            self._completeHashV4(hashObj)
        else:
            raise InvalidKey("Signature is not a V3 or V4 signature")

        sigDigest = hashObj.digest()
        return sigDigest

    @staticmethod
    def finalizeSignature(sigString, cryptoKey, pubKeyAlg, hashAlg):
        # if this is an RSA signature, it needs to properly padded
        # RFC 2440 5.2.2 and RFC 2313 10.1.2
        if pubKeyAlg in PK_ALGO_ALL_RSA:
            hashPad = PGP_Signature.HashAlgPads[hashAlg]
            padLen = (len(hex(cryptoKey.n)) - 5 -
                2 * (len(sigString) + len(hashPad) + 1)) // 2 - 1
            sigString = chr(1) + chr(0xFF) * padLen + chr(0) + hashPad + sigString

        return sigString

    def verify(self, cryptoKey, keyId):
        """Verify the signature as generated with cryptoKey"""

        # Compute the signature digest
        sigString = self.getSignatureHash()
        # Validate it against the short digest
        if sigString[:2] != self.hashSig:
            raise BadSelfSignature(keyId)

        digSig = self.parseMPIs()
        if not self.verifySignature(sigString, cryptoKey, digSig,
                                    self.pubKeyAlg, self.hashAlg):
            self.setVerifies(False)
            raise BadSelfSignature(keyId)
        self.setVerifies(True)

    def verifyDocument(self, cryptoKey, stream):
        """
        Verify the signature on the supplied document stream
        """
        digest = self.getDocumentHash(stream)
        keyId = self.getSignerKeyId()

        # Per FIPS-180-3 DSA keys require the following digest truncation
        # A succinct description http://tools.ietf.org/html/rfc4880#section-13.6
        if self.pubKeyAlg == PK_ALGO_DSA:
            # get q bit length
            qLen = int(math.ceil(math.log(cryptoKey.q, 2)))
            # 384 is not required by the standard, but we add it since
            # our testsuite has this case
            assert qLen in [ 160, 224, 256, 384 ]
            if int(math.ceil(math.log(pubkey.bytes_to_long(digest), 2))) > qLen:
                digest = digest[:(qLen/8)]

        # Validate it against the short digest
        if digest[:2] != self.hashSig:
            raise SignatureError(keyId)

        digSig = self.parseMPIs()
        if not self.verifySignature(digest, cryptoKey, digSig,
                                    self.pubKeyAlg, self.hashAlg):
            self.setVerifies(False)
            raise SignatureError(keyId)
        self.setVerifies(True)

    @staticmethod
    def verifySignature(sigString, cryptoKey, signature, pubKeyAlg, hashAlg):
        """Verify the signature on sigString generated with cryptoKey"""
        sigString = PGP_Signature.finalizeSignature(sigString, cryptoKey,
                                                    pubKeyAlg, hashAlg)
        return cryptoKey.verify(sigString, signature)

    def initSubPackets(self):
        self._hashedSubPackets = []
        self._unhashedSubPackets = []

    # Handling signature generation
    def addTrust(self, level, amount, regexLimit = None):
        """Mark this signature packet as being a trust signature"""
        stream = util.ExtendedStringIO()
        stream.write(chr(level))
        stream.write(chr(amount))
        self._hashedSubPackets.append((SIG_SUBPKT_TRUST, stream))
        if regexLimit:
            stream = util.ExtendedStringIO()
            stream.write(regexLimit)
            stream.write('\x00')
            # Mark this packet as critical
            self._hashedSubPackets.append((0x80 | SIG_SUBPKT_REGEX, stream))

    def addIssuerKeyId(self, keyId):
        stream = util.ExtendedStringIO()
        stream.write(fingerprintToInternalKeyId(keyId))

        # The key ID is part of the unhashed data
        self._unhashedSubPackets.append((SIG_SUBPKT_ISSUER_KEYID, stream))

    def addCreation(self, timestamp = None):
        """Add a creation timestamp sub-packet"""
        if timestamp is None:
            timestamp = time.time()
        self._hashedSubPackets.append((SIG_SUBPKT_CREATION,
                                       self._addInt4(timestamp)))

    def addExpiration(self, seconds):
        """Add an expiration sub-packet"""
        self._hashedSubPackets.append((SIG_SUBPKT_SIG_EXPIRE,
                                       self._addInt4(seconds)))

    def _addInt4(self, int4):
        int4 = int(int4)
        stream = util.ExtendedStringIO()
        self._writeBin(stream, int4ToBytes(int4))
        return stream

    def setVerifies(self, flag=True):
        self._verifies = flag

    def getVerifies(self):
        return self._verifies

PacketTypeDispatcher.addPacketType(PGP_Signature)

class PGP_UserID(PGP_BasePacket):
    __slots__ = ['id', 'signatures', '_parentPacket']
    tag = PKT_USERID

    _parentPacketTypes = set(PKT_MAIN_KEYS)

    # Constant used for signing. See #5.2.4
    signingConstant = 0xB4
    def initialize(self):
        self.resetBody()
        self.parseBody()
        # Signatures for this user ID
        self.signatures = None
        self._parentPacket = None

    def parseBody(self):
        # A user ID's data is just the user ID
        self.id = self.readBody()

    def toString(self):
        return self.id

    def addSignatures(self, signatures):
        """Add signatures to this UserID"""
        if self.signatures is None:
            self.signatures = []
        for sig in signatures:
            assert isinstance(sig, PGP_Signature)
            # No circular reference here, setParentPacket does a clone
            sig.setParentPacket(self)
            self.signatures.append(sig)

    def adoptSignature(self, sig):
        """Adopt the signature, if it's not ours already"""
        pp = sig.getParentPacket()
        if isinstance(pp, self.__class__) and self.id == pp.id:
            return

        sig.resetSignatureHash()
        sig.setParentPacket(self)

    def iterSignatures(self):
        """Iterate over this user's UserID"""
        if self.signatures is not None:
            return iter(self.signatures)
        raise PGPError("Key packet not parsed")

    iterSubPackets = iterSignatures

    def iterKeySignatures(self, keyId):
        intKeyId = fingerprintToInternalKeyId(keyId)
        # Look for a signature by this key
        for pkt in self.iterSignatures():
            if intKeyId != pkt.getSigId():
                continue
            yield pkt

    def iterCertifications(self):
        for pkt in self.iterSignatures():
            pkt.parse()
            if pkt.sigType not in SIG_CERTS:
                continue
            yield pkt

    def writeHash(self, stream, keyVersion):
        """Write a UserID packet in a stream, in order to be hashed.
        Described in RFC 4880 5.2.4 computing signatures."""
        if keyVersion == 4:
            stream.write(chr(self.signingConstant))
            stream.write(struct.pack("!I", self.bodyLength))
        self.writeBody(stream)

    def merge(self, other):
        """Merges this UserID packet to the other one.
        Returns True if it changed the current packet"""
        assert self.tag == other.tag

        if self.id != other.id:
            raise MergeError("User packets with different identifier")

        finalsigs = _mergeSignatures(self.iterSignatures(),
                                     other.iterSignatures())
        if self.signatures == finalsigs:
            return False
        self.signatures = finalsigs
        return True

    def getExpiration(self):
        """Return the key expiration offset, or None if the key does not
        expire.
        If the key is revoked, -1 is returned"""
        # Iterate over all self signatures
        key = self.getParentPacket()
        selfSigs = [ x for x in self.iterKeySignatures(key.getKeyId()) ]
        if not selfSigs:
            raise PGPError("User packet with no self signature")
        revocs = []
        certs = []
        for sig in selfSigs:
            sig.parse()
            if sig.sigType == SIG_TYPE_CERT_REVOC:
                revocs.append(sig)
            elif sig.sigType in SIG_CERTS:
                certs.append(sig)
        # If we have a revocation, return a negative
        if revocs:
            return -1

        # Sort signatures by creation time, and reverse them
        certs.sort(key = lambda x: x.getCreation(), reverse = True)

        # Walk the signatures, grab the first one that has a key expiration in
        # it
        for sig in certs:
            exps = [ x[1] for x in sig.decodeHashedSubpackets()
                     if x[0] == SIG_SUBPKT_KEY_EXPIRE ]
            if not exps:
                continue
            expstr = exps[0]
            expstr.seek(0, SEEK_SET)
            return int4FromBytes(*self._readBin(expstr, 4))
        # No expiration
        return None

PacketTypeDispatcher.addPacketType(PGP_UserID)

class PGP_UserAttribute(PGP_UserID):
    __slots__ = ['id', 'signatures', 'subpackets']
    tag = PKT_USER_ATTRIBUTE

    signingConstant = 0xD1

    def parseBody(self):
        # Digest the packet
        m = digestlib.sha1()
        self._updateHash(m, self.getBodyStream())

        self.id = '[image, digest = %s]' % m.hexdigest().upper()

PacketTypeDispatcher.addPacketType(PGP_UserAttribute)

class PGP_Key(PGP_BaseKeySig):
    __slots__ = ['_parsed', 'version', 'createdTimestamp', 'pubKeyAlg',
                 'mpiFile', 'mpiLen', 'daysValid', '_keyId']
    # Base class for public/secret keys/subkeys
    tag = None

    def initialize(self):
        self.version = self.createdTimestamp = self.pubKeyAlg = None
        self.mpiFile = self.mpiLen = None
        self.daysValid = None
        # Cache
        self._keyId = None
        self._parsed = False

    def parse(self, force = False):
        """Parse the signature body and initializes the internal data
        structures for other operations"""
        if self._parsed and not force:
            return

        self.resetBody()
        # Reset all internal state
        self.initialize()
        keyVersion, = self.readBin(1)
        if keyVersion not in [3, 4]:
            raise InvalidBodyError("Invalid key version %s" % keyVersion)
        self.version = keyVersion

        if keyVersion == 3:
            self._readKeyV3()
        else:
            self._readKeyV4()
        self._parsed = True

    def _readKeyV3(self):
        # RFC 2440, sect. 5.5.2
        # We only support V4 keys
        self.createdTimestamp = len4bytes(*self._readBin(self._bodyStream, 4))

        ## daysValid
        data = self.readBin(2)
        self.daysValid = int2FromBytes(*data)

        ## Public key algorithm
        self.pubKeyAlg, = self.readBin(1)

        # Record current position in body
        mpiStart = self._bodyStream.tell()
        ## Read and discard 2 MPIs
        self.skipMPIs(self._bodyStream, self.pubKeyAlg)
        self.mpiLen = self._bodyStream.tell() - mpiStart
        self.mpiFile = util.SeekableNestedFile(self._bodyStream, self.mpiLen,
            start = mpiStart)

    def _readKeyV4(self):
        # RFC 2440, sect. 5.5.2
        # Key creation
        self.createdTimestamp = len4bytes(*self._readBin(self._bodyStream, 4))

        # Public key algorithm
        self.pubKeyAlg, = self.readBin(1)

        # Record current position in body
        mpiStart = self._bodyStream.tell()
        # Skip over the MPIs
        self.skipMPIs(self._bodyStream, self.pubKeyAlg)
        self.mpiLen = self._bodyStream.tell() - mpiStart
        self.mpiFile = util.SeekableNestedFile(self._bodyStream, self.mpiLen,
            start = mpiStart)

    def rewrite(self, stream):
        """
        Rewrite this key to a different stream, usually after it was
        modified (otherwise write is more efficient)"""
        self._rewritePacket()
        self.write(stream)

    def rewriteAll(self, stream):
        """
        Rewrite this key to a different stream, usually after it was
        modified (otherwise write is more efficient)"""
        self._rewritePacket()
        self.writeAll(stream)

    def _rewritePacket(self):
        bodyStream = util.ExtendedStringIO()
        self.rewriteBody(bodyStream)
        self._bodyStream = bodyStream
        self.bodyLength = self._getBodyLength()
        # We invalidated this packet
        self._parsed = False

    def rewriteBody(self, stream):
        # We only write keys version 4
        # Write key version
        self._writeBin(stream, [ 4 ])
        self._writeKeyV4(stream)

    def _writeKeyV4(self, stream):
        self._writeBin(stream, int4ToBytes(self.createdTimestamp))
        self._writeBin(stream, [ self.pubKeyAlg ])
        self.mpiFile.seek(0)
        self._copyStream(self.mpiFile, stream)

    def getKeyFingerprint(self):
        if self._keyId is not None:
            if self.version == 3:
                return self._keyId[0]
            return self._keyId
        self.parse()

        if self.version == 3:
            # See section "Key IDs and Fingerprints" for a description of how
            # v3 fingerprints and key IDs are different

            # Key ID is low 64 bits of the modulus
            self.mpiFile.seek(0)
            self._readCountMPIs(self.mpiFile, 1, discard = True)
            end1 = self.mpiFile.tell()
            octets = self.mpiFile.pread(8, end1 - 8)

            # The fingerprint of a V3 key is formed by hashing the body (but
            # not the two-octet length) of the MPIs that form the key material
            # (public modulus n, followed by exponent e) with MD5.
            self._readCountMPIs(self.mpiFile, 1, discard = True)
            end2 = self.mpiFile.tell()
            fpr = digestlib.md5()
            # Skip the 2-octet length
            fpr.update(self.mpiFile.pread(end1 - 2, 2))
            fpr.update(self.mpiFile.pread((end2 - end1) - 2, end1 + 2))
            fpr = fpr.hexdigest().upper()
            self._keyId = fpr, stringToAscii(octets)
            return fpr

        # Convert to public key

        pkt = self.toPublicKey(minHeaderLen = 3)

        # Why minHeaderLen = 3?

        # This is a holdover from the days of PGP 2.6.2
        # RFC 2440 section 11.2 does a really bad job of explaining this.
        # RFC 2440 section 5.2.4 refers to this for self signature computation.
        # One of the least documented gotchas of Key fingerprints:
        # they're ALWAYS calculated as if they were a public key main key block.
        # this means private keys will be treated as public keys, and subkeys
        # will be treated as main keys for the purposes of this test.
        # Furthermore if the length was one byte long it must be translated
        # into a 2 byte long length (upper octet is 0)
        # not doing this will result in key fingerprints which do not match the
        # output produced by OpenPGP compliant programs.
        # this will result in the first octet ALWYAS being 0x99
        # in binary 10 0110 01
        # 10 indicates old style PGP packet
        # 0110 indicates public key
        # 01 indicates 2 bytes length

        m = digestlib.sha1()
        sio = util.ExtendedStringIO()
        # Write only the header, we can copy the body directly from the
        # body stream
        pkt.writeHeader(sio)
        m.update(sio.getvalue())

        pkt.resetBody()
        self._updateHash(m, pkt.getBodyStream())

        self._keyId = m.hexdigest().upper()
        return self._keyId

    def getKeyId(self):
        if self.version == 3:
            self.getKeyFingerprint()
            return self._keyId[1]
        return self.getKeyFingerprint()[-16:]

    def hasKeyId(self, keyId):
        keyId = keyId.upper()
        if self.version == 3:
            if self.getKeyId().endswith(keyId):
                return True
        return self.getKeyFingerprint().endswith(keyId)

    def hasKeyId(self, keyId):
        keyId = keyId.upper()
        if self.version == 3:
            if self.getKeyId().endswith(keyId):
                return True
        return self.getKeyFingerprint().endswith(keyId)

    def getCreatedTimestamp(self):
        self.parse()
        return self.createdTimestamp

    def iterSelfSignatures(self):
        return self._iterSelfSignatures(self.getKeyId())

    def _iterSelfSignatures(self, keyId):
        """Iterate over all the self-signatures"""
        self.parse()

        intKeyId = fingerprintToInternalKeyId(keyId)
        # Look for a self signature
        for pkt in self.iterSignatures():
            if intKeyId != pkt.getSigId():
                continue
            yield pkt

    def iterAllSelfSignatures(self):
        """Iterate over direct signatures and UserId signatures"""
        return self._iterAllSelfSignatures(self.getKeyFingerprint())

    def _iterAllSelfSignatures(self, keyId):
        for pkt in self.iterSelfSignatures():
            yield pkt
        intKeyId = fingerprintToInternalKeyId(keyId)
        for uid in self.iterUserIds():
            for pkt in uid.iterSignatures():
                if intKeyId != pkt.getSigId():
                    continue
                yield pkt

    def assertSigningKey(self):
        # Find self signature of this key
        # first search for the public key algortihm octet. if the key is really
        # old, this might be the only hint that it's legal to use this key to
        # make digital signatures.
        self.parse()

        if self.pubKeyAlg in (PK_ALGO_RSA_SIGN_ONLY, PK_ALGO_DSA):
            # the public key algorithm octet satisfies this test. no more
            # checks required.
            return True

        keyId = self.getKeyFingerprint()

        # If it's a subkey, look for the master key
        if self.tag in PKT_SUB_KEYS:
            pkt = self.getMainKey()
            return pkt.assertSigningKey()

        # Look for a self signature
        for pkt in self.iterAllSelfSignatures():
            # We know it's a ver4 packet, otherwise getSigId would have failed
            for spktType, dataf in pkt.decodeHashedSubpackets():
                if spktType == SIG_SUBPKT_KEY_FLAGS:
                    # RFC 2440, sect. 5.2.3.20
                    dataf.seek(0, SEEK_SET)
                    foct, = self._readBin(dataf, 1)
                    if foct & 0x02:
                        return True
        # No subpacket or no key flags
        raise IncompatibleKey('Key %s is not a signing key.'% keyId)

    def getPublicKeyTuple(self):
        """Return the key material"""
        self.parse()
        self.mpiFile.seek(0, SEEK_SET)
        return self.readMPIs(self.mpiFile, self.pubKeyAlg)

    def makePgpKey(self, passPhrase = None):
        assert passPhrase is None
        pkTuple = self.getPublicKeyTuple()
        if self.pubKeyAlg in PK_ALGO_ALL_RSA:
            n, e = pkTuple
            return RSA.construct((n, e))
        if self.pubKeyAlg == PK_ALGO_DSA:
            p, q, g, y = pkTuple
            return DSA.construct((y, g, p, q))
        raise MalformedKeyRing("Can't use El-Gamal keys in current version")

    def getCryptoKey(self, passPhrase = None):
        assert passPhrase is None
        self.verifySelfSignatures()
        return self.makePgpKey()

    def adoptSignature(self, sig):
        """Adopt the signature, if it's not ours already"""
        pp = sig.getParentPacket()
        if isinstance(pp, self.__class__) and \
                pp.getKeyFingerprint() == self.getKeyFingerprint():
            return

        sig.resetSignatureHash()
        sig.setParentPacket(self)

    def getEndOfLife(self):
        """Parse self signatures to find timestamp(s) of key expiration.
        Also seek out any revocation timestamps.
        We don't need to actually verify these signatures, but we do require
        that they were verified previously using verifySelfSignatures().
        Signatures that do not validate are ignored for the purposes of
        calculating the revocation and expiration
        Returns bool, timestamp (is revoked, expiration)
        """
        expireTimestamp = revocTimestamp = 0

        # Key creation time
        cTimestamp = self.createdTimestamp

        # Creation time for the signature that exposed most recently a key
        # expiration subpacket
        sigExpCreationTimestamp = 0

        # Note that in this respect we are bug-compatible with gpg. A
        # key that does not expire has signatures with no key expiration
        # subpackets. This means once you've set an expiration on a key and
        # published it, you can not make it never expire.

        for sig in self.iterSelfSigCertifications():
            verifies = sig.getVerifies()
            assert verifies is not None, "Please verify signatures first"
            # If the sig doesn't verify, skip it
            if not verifies:
                continue
            eTimestamp = sig.getKeyExpiration()
            if eTimestamp is None:
                continue
            sigCreation = sig.getCreation()
            if sigCreation <= sigExpCreationTimestamp:
                # This signature has the same or an earlier creation than
                # the one that supplied an expiration. Skip it. This works
                # across different uids too
                continue
            if eTimestamp > 0:
                eTimestamp += cTimestamp
            expireTimestamp = eTimestamp
            sigExpCreationTimestamp = sigCreation

        # Now iterate over direct signatures, looking for a key revocation
        for sig in self.iterSelfSigRevocations():
            verifies = sig.getVerifies()
            assert verifies is not None, "Please verify signatures first"
            # If the sig doesn't verify, skip it
            if not verifies:
                continue
            sigCreation = sig.getCreation()
            if revocTimestamp == 0 or sigCreation < revocTimestamp:
                revocTimestamp = sigCreation

        return (revocTimestamp, expireTimestamp)


class PGP_MainKey(PGP_Key):
    def initSubPackets(self):
        if hasattr(self, "subkeys"):
            # Already processed
            return

        self.parse()

        self.revsigs = []
        self.uids = []
        self.subkeys = []

        subpkts = [ x for x in self._iterSubPackets(PKT_MAIN_KEYS) ]

        # Start reading signatures until we hit a UserID or another key
        limit = set(PKT_SUB_KEYS)
        limit.add(PKT_USERID)
        limit.add(PKT_USER_ATTRIBUTE)
        i = 0
        for pkt in subpkts:
            if pkt.tag in limit:
                # UserID or subkey
                break
            i += 1
            if not isinstance(pkt, PGP_Signature):
                continue
            pkt.parse()
            if pkt.sigType in (SIG_TYPE_KEY_REVOC, SIG_TYPE_DIRECT_KEY):
                # Key revocation
                # No circular reference here, setParentPacket does a clone
                pkt.setParentPacket(self)
                self.revsigs.append(pkt)
                continue
            # According to sect. 10.1, there should not be other signatures
            # here.
            assert False, "Unexpected signature type %s" % pkt.sigType

        sigLimit = i

        # Read until we hit a subkey
        limit = set(PKT_SUB_KEYS)
        i = 0
        for pkt in subpkts[sigLimit:]:
            if pkt.tag in limit:
                break
            i += 1
            # Certification revocations live together with regular signatures
            # or so is the RFC saying
            if isinstance(pkt, PGP_UserID):
                # No circular reference here, setParentPacket does a clone
                pkt.setParentPacket(self)
                self.uids.append(pkt)
                continue
            if isinstance(pkt, PGP_Signature):
                # This can't be the first packet, or we wouldn't have stopped
                # in the previous loop
                # Add this signature to the last user id we found
                self.uids[-1].addSignatures([pkt])
                continue
            # We ignore other packets (like trust)

        uidLimit = sigLimit + i

        # Read until the end
        # We don't want to point back to ourselves, or we'll create a
        # circular loop.
        for pkt in subpkts[uidLimit:]:
            if isinstance(pkt, PGP_SubKey):
                # No circular reference here, setParentPacket does a clone
                pkt.setParentPacket(self)
                self.subkeys.append(pkt)
                continue
            if isinstance(pkt, PGP_Signature):
                # This can't be the first packet, or we wouldn't have stopped
                # in the previous loop
                subkey = self.subkeys[-1]
                pkt.parse()
                if pkt.sigType == SIG_TYPE_SUBKEY_REVOC:
                    subkey.setRevocationSig(pkt)
                    continue
                if pkt.sigType == SIG_TYPE_SUBKEY_BIND:
                    subkey.setBindingSig(pkt)
                    continue
                # There should not be any other type of signature here
                continue
            # Ignore other packets

    def iterUserIds(self):
        self.initSubPackets()
        return iter(self.uids)

    def iterSubPackets(self):
        for sig in self.iterSignatures():
            yield sig
        for uid in self.iterUserIds():
            yield uid
            for sig in uid.iterSignatures():
                yield sig
        for subkey in self.iterSubKeys():
            yield subkey
            for pkt in subkey.iterSubPackets():
                yield pkt

    def iterSignatures(self):
        """Iterate over all signature packets"""
        self.initSubPackets()
        return iter(self.revsigs)

    def iterCertifications(self):
        """Iterate over all certification signatures (on user IDs)"""
        for uid in self.iterUserIds():
            for sig in uid.iterCertifications():
                yield sig

    def iterSubKeys(self):
        self.initSubPackets()
        return iter(self.subkeys)

    def verifySelfSignatures(self):
        """
        Verify the self signatures on this key.
        If successful, returns the public key packet associated with this key,
        and crypto key.
        @return: (pubKeyPacket, cryptoKey)
        @raises BadSelfSignature:
        """
        self.parse()
        if self.version not in [3, 4]:
            raise InvalidKey("Version %s keys not supported" % self.version)
        # Convert to a public key (even if it's already a public key)
        pkpkt = self.toPublicKey(minHeaderLen = 3)
        keyFpr = pkpkt.getKeyFingerprint()
        keyId = pkpkt.getKeyId()
        pgpKey = pkpkt.makePgpKey()
        for sig in self.iterSelfSignatures():
            self.adoptSignature(sig)
            sig.verify(pgpKey, keyFpr)
        for uid in self.iterUserIds():
            verified = False
            for sig in uid.iterKeySignatures(keyId):
                uid.adoptSignature(sig)
                try:
                    sig.verify(pgpKey, keyFpr)
                except BadSelfSignature:
                    continue
                verified = True
            if not verified:
                # No signature. Not good, according to our standards
                raise BadSelfSignature(keyFpr)

        return pkpkt, pgpKey

    def isSupersetOf(self, key):
        """Check if this key is a superset of key
        We try to make sure that:
         - the keys have the same ID
         - this key's set of revocation signatures is a superset of the other
           key's revocations
         - this key's set of subkeys is a superset of the other key's subkeys
         - this key's set of userids is a superset of the other key's userids
        """
        if self.tag != key.tag:
            raise IncompatibleKey("Attempting to compare different key types")
        if self.getKeyFingerprint() != key.getKeyFingerprint():
            raise IncompatibleKey("Attempting to compare different keys")

        thisSubkeyIds = dict((x.getKeyFingerprint(), x) for x in self.iterSubKeys())
        otherSubkeyIds = dict((x.getKeyFingerprint(), x) for x in key.iterSubKeys())
        if not set(thisSubkeyIds).issuperset(otherSubkeyIds):
            # Missing subkey
            return False

        thisUids = dict((x.id, x) for x in self.iterUserIds())
        otherUids = dict((x.id, x) for x in key.iterUserIds())
        if not set(thisUids).issuperset(otherUids):
            # Missing uid
            return False

        thisRevSigs = set(x.getSignatureHash() for x in self.revsigs)
        otherRevSigs = set(x.getSignatureHash() for x in key.revsigs)
        if not thisRevSigs.issuperset(otherRevSigs):
            # Missing revocation signature
            return False

        # XXX More work to be done here, we would have to verify that
        # signatures don't change. This is what the old code was doing (and it
        # wasn't actually verifying user ids either ) -- misa
        return True

    def getUserIds(self):
        return [ pkt.id for pkt in self.iterUserIds() ]

    def merge(self, other):
        """Merge this key with the other key
        Return True if the key was modified"""
        assert self.tag == other.tag

        if self.getKeyFingerprint() != other.getKeyFingerprint():
            raise MergeError("Merging keys with a different ID")

        # Both keys must verify their self-signing signatures
        self.verifySelfSignatures()
        other.verifySelfSignatures()

        # Merge revocations / direct keys
        finalsigs = _mergeSignatures(self.iterSignatures(),
                                     other.iterSignatures())
        changed = False
        if self.revsigs != finalsigs:
            changed = True
            self.revsigs = finalsigs

        # Now merge user ids
        changed = self._mergeUserIds(other) or changed

        # And merge subkeys
        changed = self._mergeSubkeys(other) or changed
        return changed

    def _mergeUserIds(self, other):
        luids = {}
        # Preserve order
        finaluids = []
        changed = False
        for uid in itertools.chain(self.iterUserIds(), other.iterUserIds()):
            luidlist = luids.setdefault(uid.id, [])
            # We may have UserID and UserAttribute packets that can collide
            # (though it's very unlikely)
            for luid in luidlist:
                if uid.tag == luid.tag:
                    changed = luid.merge(uid) or changed
                    break
            else: # for
                luidlist.append(uid)
                finaluids.append(uid)
        if self.uids == finaluids and not changed:
            return False
        self.uids = finaluids
        return True

    def _mergeSubkeys(self, other):
        # Subkeys can only have one revocation (revoking a subkey effectively
        # invalidates the key)
        lkids = {}
        # Preserve order
        finalkeys = []
        changed = False
        for skey in itertools.chain(self.iterSubKeys(), other.iterSubKeys()):
            # Verify self signatures
            skey.verifySelfSignatures()

            keyId = skey.getKeyFingerprint()
            if keyId not in lkids:
                lkids[keyId] = skey
                finalkeys.append(skey)
                continue
            changed = lkids[keyId].merge(skey) or changed
        if self.subkeys == finalkeys and not changed:
            return False
        self.subkeys = finalkeys
        return True

    def iterSelfSigCertifications(self):
        """Iterate over all self signature certifications"""
        keyId = self.getKeyId()

        for uid in self.uids:
            # Uhm. We may have to ignore expirations that exist on revoked
            # users (users with a revoked self signature - 5.2.3.3)
            for pkt in uid.iterKeySignatures(keyId):
                if pkt.sigType not in SIG_CERTS:
                    continue
                yield pkt

    def iterSelfSigRevocations(self):
        """Iterate over all self signature revocations"""
        for sig in self.iterSelfSignatures():
            if sig.sigType != SIG_TYPE_KEY_REVOC:
                continue
            yield sig

class PGP_PublicAnyKey(PGP_Key):
    __slots__ = []
    pubTag = None
    def toPublicKey(self, minHeaderLen = 2):
        return self._msgClass.newPacket(self.pubTag, self._bodyStream,
                                           minHeaderLen = minHeaderLen)

class PGP_PublicKey(PGP_PublicAnyKey, PGP_MainKey):
    __slots__ = []
    tag = PKT_PUBLIC_KEY
    pubTag = PKT_PUBLIC_KEY

class PGP_SecretAnyKey(PGP_Key):
    __slots__ = ['s2k', 'symmEncAlg', 's2kType', 'hashAlg', 'salt',
                 'count', 'encMpiFile']
    pubTag = None

    _hashes = [ 'Unknown', digestlib.md5, digestlib.sha1, RIPEMD, 'Double Width SHA',
                'MD2', 'Tiger/192', 'HAVAL-5-160' ]
    # Ciphers and their associated key sizes
    _ciphers = [ ('Unknown', 0), ('IDEA', 0), (DES3, 192), (CAST, 128),
                 (Blowfish, 128), ('SAFER-SK128', 0), ('DES/SK', 0),
                 (AES, 128), (AES, 192), (AES, 256), ]
    _legalCiphers = set([ 2, 3, 4, 7, 8, 9 ])

    def initialize(self):
        PGP_Key.initialize(self)
        self.s2k = self.symmEncAlg = self.s2kType = None
        self.hashAlg = self.salt = self.count = None
        # We do not store the initial vector, it is part of the encoded file
        self.encMpiFile = None

    def parse(self, force = False):
        PGP_Key.parse(self, force = force)

        # Seek to the end of the MPI file, just to be safe (we should be there
        # already)
        self._bodyStream.seek(self.mpiFile.start + self.mpiLen, SEEK_SET)

        self.s2k, = self.readBin(1)

        if self.s2k in [ENCRYPTION_TYPE_SHA1_CHECK,
                        ENCRYPTION_TYPE_S2K_SPECIFIED]:
            self.symmEncAlg, self.s2kType, self.hashAlg = self.readBin(3)
            if self.s2kType:
                if 100 <= self.s2kType <= 110:
                    # Private/Experimental s2k
                    pass
                else:
                    if self.s2kType not in (S2K_TYPE_SALTED, S2K_TYPE_ITER_SALTED):
                        raise IncompatibleKey('Unknown string-to-key type %s' %
                                              self.s2kType)
                    self.salt = self.readExact(8)
                    if self.s2kType == S2K_TYPE_ITER_SALTED:
                        self.count, = self.readBin(1)
        # The Initial Vector is part of the encrypted MPI file
        # The MPIs are most likely encrypted, we'll just have to trust that
        # there are enough of them for now.
        dataLen = self._bodyStream.size - self._bodyStream.tell()
        self.encMpiFile = util.SeekableNestedFile(self._bodyStream, dataLen)

    def rewriteBody(self, stream):
        PGP_Key.rewriteBody(self, stream)
        self._writeBin(stream, [ self.s2k ])
        if self.s2k in [ENCRYPTION_TYPE_SHA1_CHECK,
                        ENCRYPTION_TYPE_S2K_SPECIFIED]:
            self._writeBin(stream, [ self.symmEncAlg, self.s2kType,
                                     self.hashAlg ])
            if self.s2kType in (0x01, 0x03):
                assert len(self.salt) == 8
                stream.write(self.salt)
                if self.s2kType == 0x03:
                    self._writeBin(stream, [ self.count ])
        self.encMpiFile.seek(0)
        self._copyStream(self.encMpiFile, stream)

    def _getSecretMPICount(self):
        if self.pubKeyAlg in PK_ALGO_ALL_RSA:
            return 4
        if self.pubKeyAlg == PK_ALGO_DSA:
            return 1
        if self.pubKeyAlg in PK_ALGO_ALL_ELGAMAL:
            return 1
        raise PGPError("Unsupported public key algorithm %s" % self.pubKeyAlg)

    def toPublicKey(self, minHeaderLen = 2):
        self.parse()

        # Create a nested file starting at the beginning of the body's and
        # with the length equal to the position in the body up to the MPIs
        io = util.SeekableNestedFile(self._bodyStream,
            self.mpiFile.start + self.mpiLen, start = 0)
        pkt = self._msgClass.newPacket(self.pubTag, io,
                                         minHeaderLen = minHeaderLen)
        return pkt

    def recrypt(self, oldPassPhrase, newPassPhrase = None, _salt = None):
        if newPassPhrase is None and self.s2k == ENCRYPTION_TYPE_UNENCRYPTED:
            # Nothing to do here
            return False
        if oldPassPhrase == newPassPhrase:
            # Nothing to do either
            return False
        # decrypt the MPIs
        mpis = self.decrypt(oldPassPhrase)
        stream = util.ExtendedStringIO()
        for mpi in mpis:
            self._writeMPI(stream, mpi)
        if newPassPhrase is None:
            self.s2k = ENCRYPTION_TYPE_UNENCRYPTED
            self.salt = None
            self._writeBin(stream,
                int2ToBytes(computeRFC2440Checksum(stream.getvalue())))
        else:
            self.s2k = ENCRYPTION_TYPE_SHA1_CHECK
            stream.write(computeSHAChecksum(stream.getvalue()))
            # DES3
            self.symmEncAlg = 2
            # We force iterated + salted
            self.s2kType = 0x03
            # SHA1 protection
            self.hashAlg = 2
            # Iterate 96 times (seems pretty standard)
            self.count = 96
            if isinstance(self, PGP_SecretSubKey):
                # Use the same salt as the main key
                self.salt = _salt
            else:
                self.salt = file("/dev/urandom").read(8)

            stream.seek(0)
            io = self._encryptStream(stream, newPassPhrase)
            stream = io
        self.encMpiFile = stream
        stream.seek(0)
        # Re-crypt subkeys too
        for sk in self.iterSubKeys():
            if isinstance(sk, PGP_SecretSubKey):
                sk.recrypt(oldPassPhrase, newPassPhrase, _salt = self.salt)
                sk._rewritePacket()
        return True

    def decrypt(self, passPhrase):
        self.parse()
        self.encMpiFile.seek(0, SEEK_SET)

        if passPhrase is None:
            passPhrase = ''
        if self.s2k == ENCRYPTION_TYPE_UNENCRYPTED:
            return self._readCountMPIs(self.encMpiFile,
                self._getSecretMPICount(), discard = False)

        io = self._decryptStream(self.encMpiFile, passPhrase)
        unenc = io.read()
        if self.s2k == ENCRYPTION_TYPE_S2K_SPECIFIED:
            check = verifyRFC2440Checksum(unenc)
        else:
            check = verifySHAChecksum(unenc)

        if not check:
            raise BadPassPhrase('Pass phrase incorrect')

        io.seek(0)
        return self._readCountMPIs(io, self._getSecretMPICount(),
                                   discard = False)

    def _getCipher(self, passPhrase):
        if self.symmEncAlg not in self._legalCiphers:
            # We only support the algorithms in self._legalCiphers
            if self.symmEngAlg >= len(self._ciphers):
                raise IncompatibleKey("Unknown cipher %s" %
                                      self.symmEngAlg)
            raise IncompatibleKey("Cipher %s is unusable" % self.symmEncAlg)

        if self.hashAlg >= len(self._hashes):
            raise IncompatibleKey("Unknown hash algorithm %s" % self.hashAlg)
        hashAlg = self._hashes[self.hashAlg]
        if isinstance(hashAlg, str):
            raise IncompatibleKey('Hash algorithm %s is not implemented. '
                                  'Key not readable' % hashAlg)

        cipherAlg, cipherKeySize = self._ciphers[self.symmEncAlg]
        if self.s2kType == 0x00:
            key = simpleS2K(passPhrase, hashAlg, cipherKeySize)
        elif self.s2kType == 0x01:
            key = saltedS2K(passPhrase, hashAlg, cipherKeySize, self.salt)
        elif self.s2kType == 0x03:
            key = iteratedS2K(passPhrase, hashAlg, cipherKeySize, self.salt,
                              self.count)
        if self.symmEncAlg > 6:
            cipherBlockSize = 16
        else:
            cipherBlockSize = 8

        cipher = cipherAlg.new(key,1)
        return cipher, cipherBlockSize

    def _decryptStream(self, stream, passPhrase):
        cipher, cipherBlockSize = self._getCipher(passPhrase)
        cryptFunc = cipher.encrypt
        io = util.ExtendedStringIO()

        block = self._readExact(self.encMpiFile, cipherBlockSize)
        FRE = cryptFunc(block)
        while 1:
            block = stream.read(cipherBlockSize)
            io.write(xorStr(FRE, block))
            if len(block) != cipherBlockSize:
                break
            FRE = cryptFunc(block)
        io.seek(0)
        return io

    def _encryptStream(self, stream, passPhrase):
        cipher, cipherBlockSize = self._getCipher(passPhrase)
        io = util.ExtendedStringIO()
        cryptFunc = cipher.encrypt
        # Initial vector is random data
        block = file("/dev/urandom").read(cipherBlockSize)
        FRE = cryptFunc(block)
        io.write(FRE)
        FRE = cryptFunc(FRE)
        while 1:
            block = stream.read(cipherBlockSize)
            block = xorStr(FRE, block)
            io.write(block)
            if len(block) != cipherBlockSize:
                break
            FRE = cryptFunc(block)
        io.seek(0)
        return io

    def makePgpKey(self, passPhrase = None):
        assert passPhrase is not None
        # Secret keys have to be signing keys
        self.assertSigningKey()
        pkTuple = self.getPublicKeyTuple()
        secMPIs = self.decrypt(passPhrase)
        if self.pubKeyAlg in PK_ALGO_ALL_RSA:
            n, e = pkTuple
            d, p, q, u = secMPIs
            return RSA.construct((n, e, d, p, q, u))
        if self.pubKeyAlg == PK_ALGO_DSA:
            p, q, g, y = pkTuple
            x, = secMPIs
            return DSA.construct((y, g, p, q, x))
        raise MalformedKeyRing("Can't use El-Gamal keys in current version")

    def getCryptoKey(self, passPhrase):
        try:
            self.verifySelfSignatures()
        except BadSelfSignature:
            # XXX Make this a callback
            sys.stderr.write("Warning: self-signature on private key does not verify\n")
        return self.makePgpKey(passPhrase)

    def sign(self, packet, passwordCallback, sigType = None, creation = None,
             expiration = None, trustLevel = None, trustAmount = None,
             trustRegex = None, **kwargs):
        """Sign packet (user packet only).
        If expiration is None, the signature will expire when the key expire,
        if the key expires, otherwise it does not expire either.
        To produce a signature that does not expire, regardless of the key's
        expiration, use -1 for the expiration"""

        # We can only sign user IDs for now
        assert(isinstance(packet, PGP_UserID))
        # We need a key linked to this user
        parentPacket = packet.getParentPacket()
        assert(isinstance(parentPacket, PGP_MainKey))

        if creation is None:
            creation = time.time()
        if (trustLevel is None) ^ (trustAmount is None):
            raise Exception("both trustLevel and trustAmount should be "
                            "specified")

        if expiration is None:
            keyExpiration = packet.getExpiration()
            if keyExpiration is None:
                # Key does not expire
                expiration = -1
            elif keyExpiration < 0:
                # Key is revoked
                raise SignatureError("Signing a revoked key")
            else:
                expiration = (parentPacket.getCreatedTimestamp() +
                              keyExpiration - creation)

        # We may have to change this default
        if sigType is None:
            sigType = SIG_TYPE_CERT_0

        # Fetch the crypto key
        cryptoKey = self.makePgpKey(passPhrase = passwordCallback())

        # See comment in OpenPGPKey.getType
        keyType = key_type(cryptoKey)
        if keyType == 'DSA':
            pkAlg = PK_ALGO_DSA
            # Pick a random number that is relatively prime with the crypto
            # key's q
            relprime = cryptoKey.q + 1
            while relprime > cryptoKey.q:
                relprime = num_getRelPrime(cryptoKey.q)
        elif keyType == 'RSA':
            pkAlg = PK_ALGO_RSA
            # RSA doesn't need a prime for signing
            relprime = 0
        else:
            # Maybe we need a different exception?
            raise UnsupportedEncryptionAlgorithm(keyType)

        hashAlg = 2 # sha

        # Create signature packet
        sigp = PGP_Signature(util.ExtendedStringIO())
        # Link it to this user packet (which should be linked to a key)
        sigp.setParentPacket(packet)

        sigp.version = 4
        sigp.sigType = sigType
        sigp.pubKeyAlg = pkAlg
        sigp.hashAlg = hashAlg

        sigp.initSubPackets()

        sigp.addCreation(creation)

        if expiration >= 0:
            sigp.addExpiration(expiration)
        if trustLevel:
            sigp.addTrust(trustLevel, trustAmount, trustRegex)
        sigp.addIssuerKeyId(self.getKeyFingerprint())

        # Prepare the subpacket streams
        sigp._prepareSubpackets()

        # Add the short sig hash (we can compute the real sig hash now)
        sighash = sigp.getSignatureHash()
        sigp.setShortSigHash(sighash[:2])

        sigString = sigp.finalizeSignature(sighash, cryptoKey, sigp.pubKeyAlg,
                                           sigp.hashAlg)

        mpis = cryptoKey.sign(sigString, relprime)

        # Write MPIs
        stream = util.ExtendedStringIO()
        sigp.mpiFile = stream

        for mpi in mpis:
            PGP_Signature._writeMPI(stream, mpi)

        sigp.rewriteBody()
        packet.signatures.append(sigp)
        return sigp

class PGP_SecretKey(PGP_SecretAnyKey, PGP_MainKey):
    __slots__ = []
    tag = PKT_SECRET_KEY
    pubTag = PKT_PUBLIC_KEY

class PGP_SubKey(PGP_Key):
    # Subkeys are promoted to main keys when converted to public keys
    pubTag = PKT_PUBLIC_KEY

    _parentPacketTypes = set(PKT_MAIN_KEYS)

    def setUp(self):
        self.bindingSig = None
        self.revocationSig = None

    def setBindingSig(self, sig):
        self.bindingSig = sig
        # No circular reference here
        self.bindingSig.setParentPacket(self)
        sig.resetSignatureHash()

    def setRevocationSig(self, sig):
        self.revocationSig = sig
        # No circular reference here
        self.revocationSig.setParentPacket(self)
        sig.resetSignatureHash()

    def iterSubPackets(self):
        # Stop at another key
        if self.bindingSig:
            yield self.bindingSig
        if self.revocationSig:
            yield self.revocationSig

    def iterCertifications(self):
        return []

    def iterUserIds(self):
        # Subkeys don't have user ids
        return []

    def iterSelfSignatures(self):
        return self._iterSelfSignatures(self.getMainKey().getKeyFingerprint())

    def iterAllSelfSignatures(self):
        """Iterate over direct signatures and UserId signatures"""
        return self._iterAllSelfSignatures(self.getMainKey().getKeyFingerprint())

    def getMainKey(self):
        """Return the main key for this subkey"""
        return self.getParentPacket()

    def verifySelfSignatures(self):
        # Get the main key associated with this subkey
        mainKey = self.getParentPacket()
        # since this is a subkey, let's go ahead and make sure the
        # main key is valid before we continue
        mainpkpkt, mainPgpKey = mainKey.verifySelfSignatures()

        # Convert this subkey to a public key
        pkpkt = self.toPublicKey(minHeaderLen = 3)

        keyId = pkpkt.getKeyFingerprint()

        # We should have a binding signature or a revocation
        if self.bindingSig is None and self.revocationSig is None:
            raise BadSelfSignature(keyId)

        # Only verify direct signatures
        verified = False
        for sig in self.iterSelfSignatures():
            # We verify both the key binding and the revocation, if available
            # Also make sure we're verifying the right key
            self.adoptSignature(sig)
            sig.verify(mainPgpKey, keyId)
            verified = True
        if not verified:
            # No signatures on the subkey
            raise BadSelfSignature(keyId)

        if self.bindingSig is None:
            # No binding sig to further check (must have been revoked)
            return

        # Iterate over the unhashed packets of the binding signature, there
        # may be a SIG_TYPE_PRKEY_BIND (0x19) embedded signature. See #12.1
        # (Enhanced Key Formats) from the draft spec for details
        embeddedSigs = [ x[1]
                         for x in self.bindingSig.decodeUnhashedSubpackets()
                         if x[0] == SIG_SUBPKT_EMBEDDED_SIG ]
        if not embeddedSigs:
            return
        for sigStream in embeddedSigs:
            sig = PGP_Signature(bodyStream = sigStream)
            sig.parse()
            if sig.sigType != SIG_TYPE_PRKEY_BIND:
                # Non-signing keys can have this packet missing
                continue
            intKeyId = fingerprintToInternalKeyId(keyId)
            if sig.getSigId() != intKeyId:
                continue
            self.adoptSignature(sig)
            # Verify the signature with the subkey's public key
            sig.verify(self.toPublicKey().makePgpKey(), keyId)

    def iterSubKeys(self):
        # Nothing to iterate over, subkeys don't have subkeys
        return []

    def iterSignatures(self):
        for pkt in self.iterSubPackets():
            yield pkt

    def merge(self, other):
        """Merge this subkey with the other key"""
        assert self.tag == other.tag
        # Subkeys MUST have a key binding signature (unless it's been revoked,
        # in which case only the revocation
        # They MAY also have an optional revocation.
        # Revoking a subkey effectively terminates that key. Reconciling
        # revocation signatures is therefore not a big issue - probably
        # keeping one of the revocations would be enough -- misa
        if other.revocationSig is not None:
            # The other key is revoked.
            if self.bindingSig is None:
                if self.revocationSig.getShortSigHash() == \
                        other.revocationSig.getShortSigHash():
                    # Same key
                    return False
                # Our key verifies, so it must have a revocation (since it
                # doesn't have a key binding sig)
                assert(self.revocationSig is not None)

                # we already have a revocation, keep ours
                return False

            # Prefer our own revocation
            changed = False
            if self.revocationSig is None:
                self.revocationSig = other.revocationSig
                changed = True
            if changed:
                # While we are at it, drop the binding key too, it's not
                # needed
                self.bindingSig = None
                # We modified the key
                return True
            return False

        # We verified the other key before we tried to merge, so this should
        # not be possible
        assert(other.bindingSig is not None)

        if self.revocationSig is not None:
            if self.bindingSig is not None:
                # Drop the binding signature
                self.bindingSig = None
                return True
            # This key is revoked, nothing else to do
            return False

        # self.revocationSig is None, we verified the key, so we must have a
        # binding sig.
        assert(self.bindingSig is not None)

        if self.bindingSig.getSignatureHash() != other.bindingSig.getSignatureHash():
            # This is very unlikely, since the binding signature is produced
            # at the time the subkey is created, there should be only one
            raise MergeError("Different binding signatures")

        # Same binding sig, and no revocation
        return False

    def iterSelfSigCertifications(self):
        return [self.bindingSig]

    def iterSelfSigRevocations(self):
        if self.revocationSig:
            return [self.revocationSig]
        return []

    def getEndOfLife(self):
        """Parse self signatures to find timestamp(s) of key expiration.
        Also seek out any revocation timestamps.
        We don't need to actually verify these signatures.
        See verifySelfSignatures()
        Returns bool, timestamp (is revoked, expiration)
        """

        parentRevoked, parentExpire = self.getMainKey().getEndOfLife()

        revoked, expire = PGP_Key.getEndOfLife(self)
        if revoked and parentRevoked:
            revoked = min(revoked, parentRevoked)
        else:
            revoked = max(revoked, parentRevoked)
        # Subkeys don't depend on the main key's expiration date
        return revoked, expire


class PGP_PublicSubKey(PGP_SubKey, PGP_PublicAnyKey):
    __slots__ = []
    tag = PKT_PUBLIC_SUBKEY

class PGP_SecretSubKey(PGP_SubKey, PGP_SecretAnyKey):
    __slots__ = []
    tag = PKT_SECRET_SUBKEY

# Register class processors
for klass in [PGP_PublicKey, PGP_SecretKey, PGP_PublicSubKey, PGP_SecretSubKey]:
    PacketTypeDispatcher.addPacketType(klass)

class PGP_Trust(PGP_BasePacket):
    __slots__ = []
    tag = PKT_TRUST
PacketTypeDispatcher.addPacketType(PGP_Trust)

class PGP_CompressedData(PGP_BasePacket):
    __slots__ = []
    tag = PKT_COMPRESSED_DATA
PacketTypeDispatcher.addPacketType(PGP_CompressedData)

def newKeyFromString(data):
    """Create a new (main) key from the data
    Returns None if a key was not found"""
    return newKeyFromStream(util.ExtendedStringIO(data))

def newKeyFromStream(stream):
    """Create a new (main) key from the stream
    Returns None if a key was not found"""
    pkt = PGP_Message.newPacketFromStream(stream)
    if pkt is None:
        return None
    if not isinstance(pkt, PGP_MainKey):
        return None
    try:
        pkt.initSubPackets()
    except InvalidBodyError:
        return None
    return pkt


def _mergeSignatures(*sources):
    # Merge all signatures from the specified sources
    lsigs = {}
    # Preserve order
    finalsigs = []
    for sig in itertools.chain(*sources):
        lsiglist = lsigs.setdefault(sig.getShortSigHash(), [])
        # Do we already have this sig?
        for lsig in lsiglist:
            if sig.getSignatureHash() == lsig.getSignatureHash():
                lsig.merge(sig)
                break
        else: # for
            # This signature was not found; add it
            lsiglist.append(sig)
            finalsigs.append(sig)
    return finalsigs

def len2bytes(v1, v2):
    """Return the packet body length when represented on 2 bytes"""
    return ((v1 - 192) << 8) + v2 + 192

def len4bytes(v1, v2, v3, v4):
    """Return the packet body length when represented on 4 bytes"""
    return (v1 << 24) | (v2 << 16) | (v3 << 8) | v4

def len2ToBytes(v):
    return (((v - 192) >> 8) & 0xFF) + 192, (v - 192) & 0xFF

def len4ToBytes(v):
    return int4ToBytes(v)

def int2FromBytes(v1, v2):
    return (v1 << 8) + v2

def int4FromBytes(v1, v2, v3, v4):
    return len4bytes(v1, v2, v3, v4)

def int2ToBytes(v):
    return (v >> 8) & 0xFF, v & 0xFF

def int4ToBytes(v):
    b0, b1 = (v >> 24) & 0xFF, (v >> 16) & 0xFF
    b2, b3 = (v >> 8) & 0xFF, v & 0xFF
    return b0, b1, b2, b3

def num_gcd(a, b):
    while b:
        a, b = b, a % b
    return a

def num_bitLen(a):
    r=0
    while a:
        a, r = a/2, r+1
    return r

def num_getRelPrime(q):
    # Use os module to ensure reads are unbuffered so as not to
    # artifically deflate entropy
    randFD = os.open('/dev/urandom', os.O_RDONLY)
    b = num_bitLen(q)/8 + 1
    r = 0
    while r < 2:
        for i in range(b):
            r = r*256 + ord(os.read(randFD, 1))
            r %= q
        while num_gcd(r, q-1) != 1:
            r = (r+1) % q
    os.close(randFD)
    return r

class TimestampPacketDispatcher(PacketTypeDispatcher):
    _registry = {}

class TimestampPacketDatabase(PGP_Message):
    PacketDispatcherClass = TimestampPacketDispatcher

class KeyTimestampPacket(PGP_Trust):
    """This packet is associated with a particular (main) key in
    order to track its "freshness".
    """
    __slots__ = ['_trustPacketVersion', '_keyId', '_refreshTimestamp',
                 '_parsed']
    def setUp(self):
        self._trustPacketVersion = 1
        self._keyId = None
        self._refreshTimestamp = None
        self._parsed = False

    def initialize(self):
        self.setUp()

    def iterSubPackets(self):
        return []

    def parse(self, force = False):
        """Parse the body and initializes the internal data
        structures for other operations"""
        if self._parsed and not force:
            return
        self.resetBody()
        # Reset all internal state
        self.initialize()

        # Key ID
        self._trustPacketVersion = self.readBin(1)[0]
        if self._trustPacketVersion != 1:
            raise PGPError("Unknown trust packet version %s" % self._trustPacketVersion)
        self._keyId = self.readExact(8)
        self._refreshTimestamp = int4FromBytes(*self.readBin(4))

        self._parsed = True

    def getKeyId(self):
        self.parse()
        return stringToAscii(self._keyId)

    def setKeyId(self, keyId):
        assert(len(keyId) >= 16)
        self._keyId = fingerprintToInternalKeyId(keyId)

    def getRefreshTimestamp(self):
        return self._refreshTimestamp

    def setRefreshTimestamp(self, ts):
        self._refreshTimestamp = ts

    def rewriteBody(self):
        """Re-writes the body"""
        # Re-write ourselves
        bodyStream = self._writeBodyV1()
        ns, nsp = self._nextStream, self._nextStreamPos
        parentPkt = self._parentPacket
        self.__init__(bodyStream, newStyle = self._newStyle)
        self.setNextStream(ns, nsp)
        self.setParentPacket(parentPkt)
        self.initialize()

    def _writeBodyV1(self):
        stream = util.ExtendedStringIO()
        stream.write(binSeqToString([self._trustPacketVersion]))
        stream.write(self._keyId)
        stream.write(binSeqToString(int4ToBytes(self._refreshTimestamp)))
        # Write padding
        stream.write('\0' * 25)
        stream.seek(0)
        return stream

    def merge(self, other):
        assert self.tag == other.tag
        ns, nsp = self._nextStream, self._nextStreamPos
        parentPkt = self._parentPacket
        self.__init__(other.getBodyStream(), newStyle = self._newStyle)
        self.setNextStream(ns, nsp)
        self.setParentPacket(parentPkt)
        self.initialize()

TimestampPacketDispatcher.addPacketType(KeyTimestampPacket)

class PublicKeyring(object):
    """A representation of a public keyring."""
    def __init__(self, keyringPath, tsDbPath):
        self._keyringPath = keyringPath
        self._tsDbPath = tsDbPath
        self._tsDbTimestamp = None
        self._cache = {}

        # For debugging purposes only
        self._timeIncrement = 1

    @staticmethod
    def _createFile(fileName):
        try:
            util.mkdirChain(os.path.dirname(fileName))
            file(fileName, "a+")
        except (IOError, OSError) as e:
            raise KeyringError(e.errno, e.strerror, e.filename)

    def addKeys(self, keys, timestamp = None):
        self._createFile(self._keyringPath)
        # Expand generators
        if hasattr(keys, 'next'):
            keys = list(keys)
        for key in keys:
            assert(isinstance(key, PGP_MainKey))
        keyFingerprints = addKeys(keys, self._keyringPath)
        self.updateTimestamps(keyFingerprints, timestamp = timestamp)
        return keyFingerprints

    def _extractKey(self, key):
        if not key:
            return ""
        if ord(key[0]) & 0x80:
            # Most likely already binary
            return key
        return parseAsciiArmorKey(key)

    def addKeysAsStrings(self, keys, timestamp = None):
        sio = util.ExtendedStringIO()
        for k in keys:
            assert(isinstance(k, str))
            sio.write(self._extractKey(k))
        msg = PGP_Message(sio, start = 0)
        return self.addKeys(msg.iterMainKeys(), timestamp = timestamp)

    def updateTimestamps(self, keyIds, timestamp = None):
        self._createFile(self._tsDbPath)
        # Expand generators
        if hasattr(keyIds, 'next'):
            keyIds = list(keyIds)
        for keyId in keyIds:
            assert(len(keyId) >= 16)

        if timestamp is None:
            timestamp = int(time.time())

        pkts = []
        for keyId in keyIds:
            pkt = KeyTimestampPacket(util.ExtendedStringIO())
            pkt.setKeyId(keyId)
            pkt.setRefreshTimestamp(timestamp)
            pkt.rewriteBody()
            pkts.append(pkt)

        mtime0 = os.stat(self._tsDbPath)[stat.ST_MTIME]
        addKeyTimestampPackets(pkts, self._tsDbPath)
        mtime1 = os.stat(self._tsDbPath)[stat.ST_MTIME]
        if mtime0 == mtime1:
            # Cheat, and set the mtime to be a second larger
            os.utime(self._tsDbPath, (mtime1, mtime1 + self._timeIncrement))
        elif self._timeIncrement > 1:
            os.utime(self._tsDbPath, (mtime1, mtime1 + self._timeIncrement))
        # We know for a fact we've touched the file.
        # In order to prevent sub-second updates from not being noticed, reset
        # the mtime.
        self._tsDbTimestamp = None

    def _parseTsDb(self):
        # Stat the timestamp database
        stream = file(self._tsDbPath)
        streamfd = stream.fileno()
        mtime = os.fstat(streamfd).st_mtime
        if self._tsDbTimestamp == mtime:
            # Database hasn't changed
            return

        allKeys = self._getAllKeys()

        self._tsDbTimestamp = mtime
        self._cache.clear()
        for pkt in TimestampPacketDatabase(stream).iterTrustPackets():
            pkt.parse()
            mainKeyId = pkt.getKeyId()
            ts = pkt.getRefreshTimestamp()
            self._cache[mainKeyId] = ts
            for sk in allKeys.get(mainKeyId, []):
                self._cache[sk] = ts

    def getKeyTimestamp(self, keyId):
        assert(len(keyId) >= 16)

        self._parseTsDb()
        # XXX for v3 keys, trimming to the last 8 bytes is not the valid way
        # to get the key ID. But it's just a cache.
        return self._cache.get(keyId[-16:], None)

    def getKey(self, keyId):
        """
        Retrieve the key.

        @param keyId: the key ID.
        @type keyId: str
        @rtype: PGP_Key
        @return: a key with the specified key ID
        @raise KeyNotFound: if the key was not found
        """
        # exportKey will return a fresh file object
        retStream = exportKey(keyId, self._keyringPath)
        # Note that exportKey will export both the main key and the subkeys.
        # Because of this, we can't blindly grab the first key in the new
        # keyring.
        msg = PGP_Message(retStream)
        return next(msg.iterByKeyId(keyId))

    def _getAllKeys(self):
        # Return all keys and subkeys
        # We need them in order to handle subkeys too
        ret = {}
        try:
            msg = PGP_Message(self._keyringPath)
        except (OSError, IOError):
            # We could not read the keyring - no keys found
            return ret
        for pk in msg.iterMainKeys():
            fp = pk.getKeyId()
            ret[fp] = set(x.getKeyId() for x in pk.iterSubKeys())
        return ret


def key_type(cryptoKey):
    # pycrypto's API has no consistent way to tell what kind of key we
    # have. This is apparently the least awful way to do it.
    keyName = cryptoKey.__class__.__name__
    if 'RSA' in keyName:
        return 'RSA'
    elif 'DSA' in keyName:
        return 'DSA'
    else:
        raise TypeError("Unrecognized key type: " + keyName)
