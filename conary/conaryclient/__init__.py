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


import os
import pickle

from conary import conarycfg, errors, trove
from conary.cmds import metadata, rollbacks
from conary.conaryclient import clone, cmdline, password, resolve, update
from conary.lib import log, util, openpgpkey, api
from conary.lib import cfgtypes
from conary.local import database
from conary.repository.netclient import NetworkRepositoryClient
from conary.repository import searchsource
from conary.repository import resolvemethod

# mixins for ConaryClient
from conary.conaryclient.branch import ClientBranch
from conary.conaryclient.clone import ClientClone
from conary.conaryclient.update import ClientUpdate
from conary.conaryclient.newtrove import ClientNewTrove
from conary.conaryclient.modelupdate import CMLClient

CloneError = clone.CloneError
CloneIncomplete = clone.CloneIncomplete
UpdateError = update.UpdateError
NoNewTrovesError = update.NoNewTrovesError
DependencyFailure = update.DependencyFailure
DepResolutionFailure = update.DepResolutionFailure
EraseDepFailure = update.EraseDepFailure
NeededTrovesFailure = update.NeededTrovesFailure
InstallPathConflicts = update.InstallPathConflicts

CriticalUpdateInfo = update.CriticalUpdateInfo

ChangeSetFromFile = update.changeset.ChangeSetFromFile

class TroveNotFound(Exception):
    def __init__(self, troveName):
        self.troveName = troveName

    def __str__(self):
        return "trove not found: %s" % self.troveName

class VersionSuppliedError(UpdateError):
    def __str__(self):
        return "version should not be specified when a Conary change set " \
               "is being installed"

class ConaryClient(ClientClone, ClientBranch, ClientUpdate, ClientNewTrove,
                   CMLClient):
    """
    ConaryClient is a high-level class to some useful Conary operations,
    including trove updates and erases.
    """
    @api.publicApi
    def __init__(self, cfg = None, passwordPrompter = None,
                 resolverClass=resolve.DependencySolver, updateCallback=None,
                 repos=None, modelFile=None):
        """
        @param cfg: a custom L{conarycfg.ConaryConfiguration} object.
                    If None, the standard Conary configuration is loaded
                    from /etc/conaryrc, ~/.conaryrc, and ./conaryrc.
        @type cfg: L{conarycfg.ConaryConfiguration}
        """

        ClientUpdate.__init__(self, callback=updateCallback)

        if cfg == None:
            cfg = conarycfg.ConaryConfiguration()
            cfg.initializeFlavors()
        self.repos = None

        self.cfg = cfg
        self.db = database.Database(cfg.root, cfg.dbPath, cfg.modelPath,
                                    modelFile=modelFile)
        if repos:
            self.repos = repos
        else:
            self.repos = self.createRepos(self.db, cfg,
                                          passwordPrompter = passwordPrompter)

        log.openSysLog(self.cfg.root, self.cfg.logFile)

        if not resolverClass:
            resolverClass = resolve.DependencySolver

        self.resolver = resolverClass(self, cfg, self.db)

        # Set up the callbacks for the PGP key cache
        keyCache = openpgpkey.getKeyCache()
        keyCache.setPublicPath(cfg.pubRing)
        keyCacheCallback = openpgpkey.KeyCacheCallback(self.repos,
                                                       cfg)
        keyCache.setCallback(keyCacheCallback)

    def createRepos(self, db, cfg, passwordPrompter=None, userMap=None):
        if self.repos:
            if passwordPrompter is None:
                passwordPrompter = self.repos.getPwPrompt()
            if userMap is None:
                userMap = self.repos.getUserMap()
        else:
            if passwordPrompter is None:
                passwordPrompter = password.getPassword
            if userMap is None:
                userMap = cfg.user

        repos = NetworkRepositoryClient(cfg=cfg, pwPrompt=passwordPrompter,
                localRepository=db)
        repos.setFlavorPreferenceList(cfg.flavorPreferences)
        return repos

    @api.publicApi
    def getRepos(self):
        """
        @return: a repository client object
        @rtype: conary.repository.netclient.NetworkRepositoryClient
        """
        return self.repos

    def setRepos(self, repos):
        self.repos = repos

    @api.publicApi
    def getDatabase(self):
        """
        Return the L{conary.local.database.Database} instance
        contained within this ConaryClient instance

        @rtype: L{conary.local.database.Database}
        """
        return self.db

    def disconnectRepos(self):
        """Disconnect the client from repositories.

        This method is useful if the changesets are applied from local media
        or were previously downloaded with L{downloadUpdate}.
        """
        self.repos = None

    def getMetadata(self, troveList, label, cacheFile = None,
                    cacheOnly = False, saveOnly = False):
        md = {}
        if cacheFile and not saveOnly:
            try:
                cacheFp = open(cacheFile, "r")
                cache = pickle.load(cacheFp)
                cacheFp.close()
            except (IOError, EOFError):
                if cacheOnly:
                    return {}
            else:
                lStr = label.asString()

                t = troveList[:]
                for troveName, branch in t:
                    bStr = branch.asString()

                    if lStr in cache and\
                       bStr in cache[lStr] and\
                       troveName in cache[lStr][bStr]:
                        md[troveName] = metadata.Metadata(cache[lStr][bStr][troveName])
                        troveList.remove((troveName, branch))

        # if the cache missed any, grab from the repos
        if not cacheOnly and troveList:
            if self.repos is None:
                raise errors.RepositoryError("Repository not available")
            md.update(self.repos.getMetadata(troveList, label))
            if md and cacheFile:
                try:
                    cacheFp = open(cacheFile, "rw")
                    cache = pickle.load(cacheFp)
                    cacheFp.close()
                except IOError as EOFError:
                    cache = {}

                cacheFp = open(cacheFile, "w")

                # filter down troveList to only contain items for which we found metadata
                cacheTroves = [x for x in troveList if x[0] in md]

                lStr = label.asString()
                for troveName, branch in cacheTroves:
                    bStr = branch.asString()

                    if lStr not in cache:
                        cache[lStr] = {}
                    if bStr not in cache[lStr]:
                        cache[lStr][bStr] = {}

                    cache[lStr][bStr][troveName] = md[troveName].freeze()

                pickle.dump(cache, cacheFp)
                cacheFp.close()

        return md

    def _createChangeSetList(self, csList, recurse = True,
                             skipNotByDefault = False,
                             excludeList=cfgtypes.RegularExpressionList(),
                             callback = None):
        primaryList = []
        headerList = []
        for (name, (oldVersion, oldFlavor),
                   (newVersion, newFlavor), abstract) in csList:
            if newVersion:
                primaryList.append((name, newVersion, newFlavor))
                if oldVersion:
                    headerList.append( (name, (None, None),
                                              (oldVersion, oldFlavor), True) )

                headerList.append( (name, (None, None),
                                          (newVersion, newFlavor), True) )
            else:
                primaryList.append((name, oldVersion, oldFlavor))

        cs = self.repos.createChangeSet(headerList, recurse = recurse,
                                        withFiles = False, callback = callback)

        finalList = set()
        jobList = csList[:]
        while jobList:
            job = jobList.pop(-1)
            (name, (oldVersion, oldFlavor),
                   (newVersion, newFlavor), abstract) = job

            skip = False

            # troves explicitly listed should never be excluded
            if (name, newVersion, newFlavor) not in primaryList:
                if excludeList.match(name):
                    skip = True

            if skip:
                continue

            finalList.add(job)

            if not recurse or not trove.troveIsCollection(name):
                continue

            if job[2][1] is None:
                continue
            elif job[1][0] is None:
                oldTrove = None
            else:
                oldTrove = trove.Trove(cs.getNewTroveVersion(name, oldVersion,
                                                             oldFlavor))

            newTrove = trove.Trove(cs.getNewTroveVersion(name, newVersion,
                                                         newFlavor))

            trvCs, filesNeeded, trovesNeeded = newTrove.diff(
                                            oldTrove, (oldTrove == None))

            for subJob in trovesNeeded:
                if not subJob[2][0]:
                    jobList.append(subJob)
                    continue

                if skipNotByDefault and not newTrove.includeTroveByDefault(
                                    subJob[0], subJob[2][0], subJob[2][1]):
                    continue

                jobList.append(subJob)

        finalList = list(finalList)

        # recreate primaryList without erase-only troves for the primary trove
        # list
        primaryList = [ (x[0], x[2][0], x[2][1]) for x in csList
                        if x[2][0] is not None ]

        return (finalList, primaryList)

    @api.publicApi
    def createChangeSet(self, csList, recurse = True,
                        skipNotByDefault = True,
                        excludeList = cfgtypes.RegularExpressionList(),
                        callback = None, withFiles = False,
                        withFileContents = False):
        """
        Like self.createChangeSetFile(), but returns a change set object.
        withFiles and withFileContents are the same as for the underlying
        repository call.
        """
        if self.repos is None:
            raise errors.RepositoryError("Repository not available")
        (fullCsList, primaryList) = self._createChangeSetList(csList,
                recurse = recurse, skipNotByDefault = skipNotByDefault,
                excludeList = excludeList, callback = callback)

        return self.repos.createChangeSet(fullCsList, recurse = False,
                                       primaryTroveList = primaryList,
                                       callback = callback,
                                       withFiles = withFiles,
                                       withFileContents = withFileContents)

    def createChangeSetFile(self, path, csList, recurse = True,
                            skipNotByDefault = True,
                            excludeList = cfgtypes.RegularExpressionList(),
                            callback = None):
        """
        Creates <path> as a change set file.

        @param path: path to write the change set to
        @type path: string
        @param csList: list of (troveName, (oldVersion, oldFlavor),
                                (newVersion, newFlavor), isAbsolute)
        @param recurse: If true, conatiner troves are recursed through
        @type recurse: boolean
        @param skipNotByDefault: If True, troves which are included in
        a container with byDefault as False are not included (this flag
        doesn't do anything if recurse is False)
        @type skipNotByDefault: boolean
        @param excludeList: List of regular expressions which are matched
        against recursively included trove names. Troves which match any
        of the expressions are left out of the change set (this list
        is meaningless if recurse is False).
        @param callback: Callback object
        @type callback: callbacks.UpdateCallback
        """

        if self.repos is None:
            raise errors.RepositoryError("Repository not available")
        (fullCsList, primaryList) = self._createChangeSetList(csList,
                recurse = recurse, skipNotByDefault = skipNotByDefault,
                excludeList = excludeList, callback = callback)

        self.repos.createChangeSetFile(fullCsList, path, recurse = False,
                                       primaryTroveList = primaryList,
                                       callback = callback)

    def checkWriteableRoot(self):
        """
        Prepares the installation root for trove updates and change
        set applications.
        """
        if not os.path.exists(self.cfg.root):
            util.mkdirChain(self.cfg.root)
        if not self.db.writeAccess():
            raise UpdateError("Write permission denied on conary database %s" % self.db.dbpath)

    @api.publicApi
    def pinTroves(self, troveList, pin = True):
        """
        Calls L{conary.local.database.Database.pintroves}

        @param troveList: a list of troves to pin
        @type troveList: list of troves

        @note: As this call makes database updates, any of the errors
        documented in L{conary.dbstore.sqlerrors} may be raised.

        @rtype: None
        """
        self.db.pinTroves(troveList, pin = pin)

    def getConaryUrl(self, version, flavor):
        """
        returns url to a conary changeset for updating the local client to
        @param version: a conary client version object, L{versions.Version}
        @param flavor: a conary client flavor object, L{deps.deps.Flavor}
        """
        if self.repos is None:
            raise errors.RepositoryError("Repository not available")
        return self.repos.getConaryUrl(version, flavor)

    @api.publicApi
    def iterRollbacksList(self):
        """
        Iterate over rollback list.
        Yield (rollbackName, rollback)
        @raises ConaryError: raised when the rollbacks directory cannot be read
        """
        return self.db.getRollbackStack().iter()

    @api.publicApi
    def getSearchSource(self, flavor=0, troveSource=None, installLabelPath=0):
        """
        @return: a searchSourceStack
        @rtype: conary.repository.searchsource.NetworkSearchSource
        @raises ConaryError: raised if SearchSourceStack creation fails
        @raises ParseError: raised if an element in the search path is malformed.
        """
        # a flavor of None is common in some cases so we use 0
        # as our "unset" case.
        if flavor is 0:
            flavor = self.cfg.flavor
        if installLabelPath is 0:
            installLabelPath = self.cfg.installLabelPath

        searchMethod = resolvemethod.RESOLVE_LEAVES_FIRST
        if troveSource is None:
            troveSource = self.getRepos()
            if troveSource is None:
                return None
        searchSource = searchsource.NetworkSearchSource(troveSource,
                            installLabelPath,
                            flavor, self.db,
                            resolveSearchMethod=searchMethod)
        if self.cfg.searchPath:
            return searchsource.createSearchSourceStackFromStrings(
                                                         searchSource,
                                                         self.cfg.searchPath,
                                                         flavor,
                                                         db=self.db)
        else:
            return searchSource

    @api.publicApi
    def removeInvalidRollbacks(self):
        """
        Removes rollbacks from the system which have been outdated.
        """
        self.db.removeInvalidRollbacks()

    @api.publicApi
    def applyRollback(self, rollbackSpec,
            replaceFiles=None,
            callback=None,
            tagScript=None,
            justDatabase=None,
            transactionCounter=None,
            showInfoOnly=False,
            abortOnError=False,
            noScripts=False,
            capsuleChangesets=[],
            replaceManagedFiles=False,
            replaceModifiedFiles=False,
            replaceUnmanagedFiles=False,
            ):
        """
        Apply a rollback.

        @param rollbackSpec: Rollback specififier. This is either a number (in
        which case it refers to the absolute position in the rollback stack, with
        0 being the oldest rollback) or a string like C{r.128}, as listed by
        C{conary rblist}.
        @type rollbackSpec: string

        @param replaceFiles:
        @type replaceFiles: bool

        @param callback: Callback for communicating information back to the
        invoker of this method.
        @type callback: L{callbacks.UpdateCallback}

        @param tagScript: A tag script.
        @type tagScript: path

        @param justDatabase: Change only the database, do not revert the
        filesystem.
        @type justDatabase: bool

        @param noScripts: Do not run trove scripts, including rpm scripts.
        @type noScripts: bool

        @param transactionCounter: The Conary database contains a counter that
        gets incremented with every change. This argument is the counter's value
        at the time the rollback was computed from the specifier. It is used to
        ensure that no uninteded rollbacks are performed, if a concurrent update
        happens between the moment of reading the database state and the moment
        of performing the rollback.
        @type transactionCounter: int

        @param abortOnError: Abort the rollback if any pre-rollback scripts
        fail.  Normally, the rollback continues even if there are pre-rollback
        script failures.
        @type abortOnError: bool

        @param capsuleChangesets: List of paths to changesets. Any capsules
        included in those changesets are made available to the rollback
        in case they are needed. If a directory is given, every changeset
        in that directory is included (nonrecursively) while non-changeset
        files are ignored.
        @type capsuleChangesets: list of str

        @raise UpdateError: Generic update error. Can occur if the root is not
        writeable by the user running the command.

        @raise RollbackError: Generic rollback error. Finer grained rollback
        errors are L{RollbackDoesNotExist<database.RollbackDoesNotExist>}
        (raised if the rollback specifier was invalid) and
        L{RollbackOrderError<database.RollbackOrderError>}
        (if the rollback was attempted not
        following the rollback stack order). It can also be raised if the
        database state has changed between the moment the rollback was
        computed and the moment of performing the rollback. See also the
        description for C{transactionCounter}.

        @raise ConaryError: Generic Conary error. Raised if the user running the
        command does not have permissions to access the rollback directory, or the
        directory is missing.
        """
        # We used to pass a **kwargs to this function, but that makes it hard
        # to document the keyword arguments.
        d = dict(tagScript = tagScript,
            justDatabase = justDatabase,
            noScripts = noScripts,
            transactionCounter = transactionCounter,
            callback = callback,
            replaceFiles = replaceFiles,
            showInfoOnly = showInfoOnly,
            abortOnError = abortOnError,
            capsuleChangesets = capsuleChangesets,
            replaceManagedFiles=replaceManagedFiles,
            replaceModifiedFiles=replaceModifiedFiles,
            replaceUnmanagedFiles=replaceUnmanagedFiles,
        )
        # If any of these arguments are None, don't even pass them, the
        # defaults are going to apply
        d = dict((x, y) for (x, y) in list(d.items()) if y is not None)

        return rollbacks.applyRollback(self, rollbackSpec, **d)

    def close(self):
        """Close this client and release all associated resources"""
        self.lzCache.release()
        # self.db accepts to be closed multiple times
        self.db.close()

        # Close the log files too
        log.syslog.close()

def getClient(context=None, environ=None, searchCurrentDir=False, cfg=None):
    """
        Returns a ConaryClient object that has the context set as it would
        be if the conary command line were used.

        This means it checks for the explicit "context" variable passed in
        manually.  It follows by checking the eviron dict
        (defaults to os.environ) for the CONARY_CONTEXT variable.  It then
        falls back to the CONARY file and looks for a context set there.
        Finally, if these checks fail to find a context, it will look at the
        context specified in the cfg variable.

        @param context: a context override string or None
        @param environ: a dict representing the current environment or None to
            use os.environ
        @param searchCurrentDir: if True, look in the current directory for
            a CONARY file and set the context from there if needed.  Otherwise,
            do not look for a CONARY file.  (Default False)
        @param cfg: ConaryConfiguration to use.  If None, read the
            configuration as conary would, from /etc/conaryrc, ~/.conaryrc,
            and ./conaryrc.
    """
    if cfg is None:
        cfg = conarycfg.ConaryConfiguration(True)
    cmdline.setContext(cfg, context, environ, searchCurrentDir)
    return ConaryClient(cfg)
