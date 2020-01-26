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
Implements the logging facility for conary.

Similar to the C printf function, the functions in this module take a
format string that specifies how the subsequent variables should be
converted in the log output.

For example::
   log.error("%s not found", foo)
"""

import logging
import os
import sys
import time

from logging import DEBUG, INFO, WARNING, ERROR, CRITICAL
LOWLEVEL=DEBUG - 5
from conary import constants
from conary.lib import xmllog
from conary.lib import timeutil


FORMATS = {
        'apache': timeutil.ISOFormatter(
            '[%(asctime)s] [%(levelname)s] (%(name)s) %(message)s'),
        'apache_short': ('(%(name)s) %(message)s', None),
        'console': ('%(levelname)s: %(message)s', None),
        'file': timeutil.ISOFormatter(
            '%(asctime)s %(levelname)s %(name)s : %(message)s'),
        }

syslog = None

LOGGER_CONARY           = 'conary'
# don't delimit with a dot (.) we don't want to mix output for these loggers
LOGGER_CONARY_FORMATTED = 'conary_formatted'

class SysLog:
    # class responsible for /var/log/conary
    def __call__(self, str, *args):
        "Logs a message to /var/log/conary"
        if not self.f:
            self.open()

        # Only attempt to do substitution if args is specified.
        if args:
            msg = str % args
        else:
            msg = str

        self.f.write(time.strftime("[%Y %b %d %H:%M:%S] ") + self.indent)
        self.f.write(msg)
        self.f.write("\n")
        self.f.flush()

    def command(self):
        self(("version %s: " % constants.version) + ' '.join(sys.argv[1:]))
        self.indent = "  "

    def commandComplete(self):
        self.indent = ""
        self("command complete")

    def traceback(self, lines):
        if not self.f:
            self.open()

        for line in lines:
            self.f.write(line)

        self.indent = ""
        self("command failed")

    def open(self):
        from conary.lib import util
        self.f = None
        logList = [ util.joinPaths(self.root, x) for x in self.path ]
        for pathElement in logList:
            try:
                util.mkdirChain(os.path.dirname(pathElement))
                self.f = open(pathElement, "a")
                util.setCloseOnExec(self.f)
                break
            except:
                pass
        if not self.f:
            raise IOError('could not open any of: ' + ', '.join(logList))

    def close(self):
        """Close the logger's open files"""
        if self.f is not None:
            self.f.close()
            self.f = None

    def __init__(self, root, path):
        self.root = root
        if not isinstance(path, (list, tuple)):
            path = [path]
        self.path = path
        self.indent = ""
        self.f = None

def openSysLog(root, path):
    global syslog
    if not path:
        path = os.devnull
    if root == ':memory:':
        root = '/'
    syslog = SysLog(root, path)

def error(msg, *args):
    "Log an error"
    m = "error: %s" % msg
    logger.error(m, *args)
    if fmtLogger.handlers:
        fmtLogger.error(m, *args)
    hdlr.error = True

def warning(msg, *args):
    "Log a warning"
    m = "warning: %s" % msg
    logger.warning(m, *args)
    if fmtLogger.handlers:
        fmtLogger.warning(m, *args)

def info(msg, *args):
    "Log an informative message"
    m = "+ %s" % msg
    logger.info(m, *args)
    if fmtLogger.handlers:
        fmtLogger.info(m, *args)

def debug(msg, *args):
    "Log a debugging message"
    m = "+ %s" % msg
    logger.debug(m, *args)
    if fmtLogger.handlers:
        fmtLogger.debug(m, *args)

def lowlevel(msg, *args):
    "Log a low-level debugging message"
    m = "+ %s" % msg
    logger.lowlevel(m, *args)
    if fmtLogger.handlers:
        fmtLogger.lowlevel(m, *args)

def errorOccurred():
    return hdlr.error

def resetErrorOccurred():
    hdlr.error = False

def setVerbosity(val):
    return logger.setLevel(val)

def getVerbosity():
    return logger.getEffectiveLevel()

def setMinVerbosity(val):
    """
        Ensures that the log level is at least the given log level.
        Returns the log level before this call if a change was made
        otherwise None
    """
    oldVal = getVerbosity()
    if oldVal > val:
        setVerbosity(val)
        return oldVal

class ErrorCheckingHandler(logging.StreamHandler):
    def __init__(self, *args, **keywords):
        self.error = False
        logging.StreamHandler.__init__(self, *args, **keywords)

    def emit(self, record):
        logging.StreamHandler.emit(self, record)

class ConaryLogger(logging.Logger):
    def lowlevel(self, msg, *args, **kwargs):
        if self.manager.disable >= LOWLEVEL:
            return
        if LOWLEVEL >= self.getEffectiveLevel():
            self._log(*(LOWLEVEL, msg, args), **kwargs)

def openFormattedLog(path):
    fmtHdlr = xmllog.XmlHandler(path)
    fmtLogger.addHandler(fmtHdlr)

def closeFormattedLog(path):
    for fmtHdlr in fmtLogger.handlers:
        if fmtHdlr.path == path:
            fmtHdlr.close()
            fmtLogger.handlers.remove(fmtHdlr)

def pushLogDescriptor(desc):
    for fmtHdlr in fmtLogger.handlers:
        fmtHdlr.pushDescriptor(desc)

def popLogDescriptor():
    for fmtHdlr in fmtLogger.handlers:
        fmtHdlr.popDescriptor()

def addLogRecordData(key, val):
    for fmtHdlr in fmtLogger.handlers:
        fmtHdlr.addRecordData(key, val)

def delLogRecordData(key):
    for fmtHdlr in fmtLogger.handlers:
        fmtHdlr.delRecordData(key)

if "logger" not in globals():
    # override the default logger class with one that has a more low-level
    # level
    logging.setLoggerClass(ConaryLogger)
    logger = logging.getLogger(LOGGER_CONARY)
    # conary's module importer resets globals() so we need an extra check to
    # ensure we don't alter the logLevel or handler settings.
    # the setLoggerClass above is stored as a global in the logging module,
    # so it's safe to be left where it is.
    if not hasattr(logger, '_loaded'):
        hdlr = ErrorCheckingHandler(sys.stderr)
        formatter = logging.Formatter('%(message)s')
        hdlr.setFormatter(formatter)
        logger.addHandler(hdlr)
        logger.setLevel(logging.WARNING)
        logger._loaded = True

if 'fmtLogger' not in globals():
    fmtLogger = logging.getLogger(LOGGER_CONARY_FORMATTED)
    # all messages should be emitted by the formatted logger.
    fmtLogger.setLevel(1)


# Alternate logging setup
def setupLogging(logPath=None, consoleLevel=logging.WARNING,
        consoleFormat='console', fileLevel=logging.INFO, fileFormat='file',
        logger='', consoleStream=None):

    logger = logging.getLogger(logger)
    logger.handlers = []
    logger.propagate = False
    level = 100

    # Console handler
    if consoleLevel is not None:
        consoleFormatter = _getFormatter(consoleFormat)
        consoleHandler = logging.StreamHandler(consoleStream or sys.stderr)
        consoleHandler.setFormatter(consoleFormatter)
        consoleHandler.setLevel(consoleLevel)
        logger.addHandler(consoleHandler)
        level = min(level, consoleLevel)

    # File handler
    if logPath and fileLevel is not None:
        logfileFormatter = _getFormatter(fileFormat)
        logfileHandler = logging.FileHandler(logPath)
        logfileHandler.setFormatter(logfileFormatter)
        logfileHandler.setLevel(fileLevel)
        logger.addHandler(logfileHandler)
        level = min(level, fileLevel)

    # Undo default conary logger
    cnylog = logging.getLogger(LOGGER_CONARY)
    cnylog.handlers[:] = []
    cnylog.setLevel(logging.NOTSET)
    cnylog._loaded = True

    logger.setLevel(level)
    return logger


def _getFormatter(format):
    """Logging formats can be:
     * A string - the record format
     * A tuple - the record format and timestamp format
     * An instance of Formatter or a subclass
     * A string selecting a tuple or instance from FORMATS
    """
    if format in FORMATS:
        format = FORMATS[format]
    if isinstance(format, str):
        format = (format,)
    if isinstance(format, logging.Formatter):
        return format
    return logging.Formatter(*format)
