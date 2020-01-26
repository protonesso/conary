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
Actions on source components.  This includes creating new packages;
checking in changes; checking out the latest version; displaying logs
and diffs; creating new packages; adding, removing, and renaming files;
and committing changes back to the repository.
"""
import errno
import itertools
import os
import re
import stat
import sys
import time

from conary import callbacks
from conary import changelog
from conary import conaryclient
from conary import deps
from conary import errors
from conary import files
from conary import trove
from conary import versions
from conary.build import derivedrecipe, recipe
from conary.build import loadrecipe, lookaside
from conary.build import errors as builderrors
from conary.build.macros import Macros
from conary.build import cook, use
from conary.conarycfg import selectSignatureKey
from conary.conaryclient import cmdline
from conary.lib import api
from conary.lib import fixeddifflib
from conary.lib import log
from conary.lib import magic
from conary.lib import util
from conary.lib import graph
from conary.local import update
from conary.repository import changeset
from conary.state import ConaryState, ConaryStateFromFile, SourceState

nonCfgRe = re.compile(r'^.*\.(%s)$' % '|'.join((
    'bz2', 'ccs', 'data', 'eps', 'gif', 'gz', 'ico', 'img',
    'jar', 'jpeg', 'jpg', 'lss', 'pdf', 'png', 'ps',
    'msi', 'rpm', 'run',
    'tar', 'tbz', 'tbz2', 'tgz', 'tiff', 'ttf', 'wim',
    'zip',
)))
cfgRe = re.compile(r'(^.*\.(%s)|(^|/)(%s))$' % ('|'.join((
    # extensions
    '(1|2|3|4|5|6|7|8|9)',
    'c', 'cfg', 'cnf', 'conf', 'CONFIG.*',
    'console.*', 'cron.*', '(c|)sh', 'css',
    'desktop', 'diff', 'h', 'html', 'init', 'kid', 'logrotate',
    'pam(d|)', 'patch', 'pl', 'py',
    'recipe', 'sysconfig',
    'tag(handler|description)', 'tmpwatch', 'txt',
    'xml',
    )),
    '|'.join((
    # full filenames
    r'Makefile(|\..*)',
    ))))

# mix UpdateCallback and CookCallback, since we use both.
class CheckinCallback(callbacks.UpdateCallback, callbacks.CookCallback):
    __developer_api__ = True
    def __init__(self, trustThreshold=0, keyCache=None):
        callbacks.UpdateCallback.__init__(self, trustThreshold=trustThreshold,
                                                keyCache=keyCache)
        callbacks.CookCallback.__init__(self)

# makePathId() returns 16 random bytes, for use as a pathId
makePathId = lambda: os.urandom(16)

class UpdateSpec(object):

    __slots__ = [ "targetDir", "versionSpec", "state", "shadowHeadVersion",
                  "parentRootVersion", "parentHeadVersion", "targetBranch" ]

class CheckoutSpec(object):

    __slots__ = [ "targetDir", "conaryState" ]

def _verifyAtHead(repos, headPkg, state):
    # get the latest version on our branch
    headVersion = repos.getTroveLatestVersion(state.getName(),
                                              state.getVersion().branch())
    if headVersion != state.getVersion():
        return False
    if state.getLastMerged():
        # if we've just merged from the right version, we must be at
        # head
        return True

    # make sure the files in this directory are based on the same
    # versions as those in the package at head
    for (pathId, path, fileId, version) in state.iterFileList():
        if isinstance(version, versions.NewVersion):
            assert(not headPkg.hasFile(pathId))
            # new file, it shouldn't be in the old package at all
        else:
            srcFileVersion = headPkg.getFile(pathId)[2]
            if version != srcFileVersion:
                return False

    return True

def _makeFilter(patterns, exactPaths=False):
    if not patterns:
        return lambda x: False

    if exactPaths:
        patterns = [re.escape(x) for x in patterns]
    else:
        # convert globs to regexps, but chop off the final '$'
        patterns = [ util.fnmatchTranslate(x) for x in patterns ]

    if len(patterns) > 1:
        filter = '(' + '|'.join(patterns) + ')'
    elif len(patterns):
        filter = patterns[0]
    else:
        patterns = ''

    filter = re.compile('^' + filter + '$')

    patternsFilter = lambda x: bool(filter.match(x))

    return patternsFilter

def verifyAbsoluteChangesetSignatures(cs, callback):
    # go through all the trove change sets we have in this changeset.
    # verify the digital signatures on each piece
    # return code should be the minimum trust on the entire set
    # Calling the callback's verifyTroveSignatures can raise a
    # DigitalSignatureVerificationError
    assert(hasattr(callback, 'verifyTroveSignatures'))
    r = 256
    missingKeys = []
    for troveCs in [ x for x in cs.iterNewTroveList() ]:
        # instantiate each trove from the troveCs so we can verify
        # the signature
        t = trove.Trove(troveCs)
        verTuple = callback.verifyTroveSignatures(t)
        missingKeys.extend(verTuple[1])
        r = min(verTuple[0], r)
    return r

@api.developerApi
def checkout(repos, cfg, workDir, nameList, callback=None):
    if not callback:
        callback = CheckinCallback(trustThreshold=cfg.trustThreshold)

    fullList = []
    for name in nameList:
        name, versionStr, flavor = cmdline.parseTroveSpec(name)
        if flavor is not None:
            log.error('source troves do not have flavors')
            return

        if not name.endswith(':source'):
            sourceName = name + ":source"
        else:
            sourceName = name

        if not versionStr and not cfg.buildLabel:
            raise errors.CvcError('buildLabel is not set.  Use --build-label '
                                  'or set buildLabel in your conaryrc to '
                                  'check out sources.')
        try:
            trvList = repos.findTrove(cfg.buildLabel,
                                      (sourceName, versionStr, None))
        except errors.TroveNotFound:
            if not versionStr and not cfg.buildLabel:
                raise errors.CvcError('buildLabel is not set.  Use '
                                      '--build-label or set buildLabel in '
                                      'your conaryrc to check out sources.')
            else:
                raise

        # we should never get multiple matches back
        assert(len(trvList) == 1)
        fullList += trvList

    _checkout(repos, cfg, workDir, fullList, callback)

class CheckoutExploder(changeset.AbstractChangesetExploder):

    def __init__(self, cs, pathMap, sourceStateMap):
        self.pathMap = pathMap
        self.sourceStateMap = sourceStateMap
        changeset.AbstractChangesetExploder.__init__(self, cs)

    def installFile(self, trv, path, fileObj):
        (destDir, pathId, fileId, version) = \
                    self.pathMap[(trv.getNameVersionFlavor(), path)]

        self.sourceStateMap[trv.getNameVersionFlavor()].addFile(pathId, path,
                                 version, fileId,
                                 isConfig = fileObj.flags.isConfig(),
                                 isAutoSource = fileObj.flags.isAutoSource())

        if fileObj.flags.isAutoSource():
            return False

        return destDir

    def restoreFile(self, trv, fileObj, contents, destdir, path):
        fileObj.restore(contents, destdir, destdir + '/' + path,
                        nameLookup = False)

    def fileMissing(self, trv, pathId, fileId, path):
        pass

def _checkout(repos, cfg, workDirArg, trvList, callback):
    assert(len(trvList) == 1 or workDirArg is None)
    jobList = []
    checkoutSpecs = []

    for trvInfo in trvList:
        if not workDirArg:
            workDir = trvInfo[0].split(":")[0]
        else:
            workDir = workDirArg

        if not os.path.isdir(workDir):
            try:
                os.mkdir(workDir)
            except OSError as err:
                log.error("cannot create directory %s/%s: %s", os.getcwd(),
                          workDir, str(err))
                return

        jobList.append((trvInfo[0], (None, None), (trvInfo[1], trvInfo[2]),
                        True))

        sourceState = SourceState(trvInfo[0], trvInfo[1], trvInfo[1].branch())
        conaryState = ConaryState(cfg.context, sourceState)

        spec = CheckoutSpec()
        spec.targetDir = workDir
        spec.conaryState = conaryState
        checkoutSpecs.append(spec)

    del workDir
    del conaryState

    cs = repos.createChangeSet(jobList, excludeAutoSource = True,
                               callback=callback)

    verifyAbsoluteChangesetSignatures(cs, callback)

    pathMap = {}
    sourceStateMap = {}

    for trvInfo, spec in zip(trvList, checkoutSpecs):
        sourceState = spec.conaryState.getSourceState()
        troveCs = cs.getNewTroveVersion(*trvInfo)
        trv = trove.Trove(troveCs)
        sourceStateMap[trv.getNameVersionFlavor()] = sourceState
        if trv.getFactory():
            sourceState.setFactory(trv.getFactory())

        for (pathId, path, fileId, version) in troveCs.getNewFileList():
            pathMap[(trvInfo, path)] = (spec.targetDir, pathId, fileId, version)

    CheckoutExploder(cs, pathMap, sourceStateMap)

    for spec in checkoutSpecs:
        spec.conaryState.write(spec.targetDir + "/CONARY")

def commit(repos, cfg, message, callback=None, test=False, force=False):
    if not callback:
        callback = CheckinCallback()

    if cfg.name is None or cfg.contact is None:
        log.error("name and contact information must be set for commits")
        return

    conaryState = ConaryStateFromFile("CONARY", repos)
    state = conaryState.getSourceState()

    troveName = state.getName()
    conflicts = []

    if isinstance(state.getVersion(), versions.NewVersion):
        # new package, so it shouldn't exist yet
        # Don't add TROVE_QUERY_ALL here, removed packages could exist
        # and we'd still want newpkg to work
        matches = repos.getTroveLeavesByLabel(
        { troveName : { state.getBranch().label() : None } }).get(
                                                                troveName, {})
        if matches:
            for version in matches:
                if version.branch() == state.getBranch():
                    log.error("%s is marked as a new package but it "
                              "already exists" % troveName)
                    return
                else:
                    conflicts.append(version)
        srcPkg = None
    else:
        srcPkg = repos.getTrove(troveName, state.getVersion(),
                                deps.deps.Flavor(), callback=callback)
        if not _verifyAtHead(repos, srcPkg, state):
            log.error("contents of working directory are not all "
                      "from the head of the branch; use update")
            return

    # turn off loadInstalled for committing - it ties you too closely
    # to actually being able to build what you are developing locally - often
    # not the case.
    if (not state.getFactory()) or state.getFactory() == 'factory':
        if not [ x[1] for x in state.iterFileList()
                                        if x[1].endswith('.recipe') ]:
            log.error("recipe not in CONARY state file, please run cvc add")
            return

    allPaths = [ x[1] for x in state.iterFileList() ]

    try:
        use.allowUnknownFlags(True)
        recipeClass = loadrecipe.RecipeLoaderFromSourceDirectory(state,
                            cfg = cfg, repos = repos,
                            branch = state.getBranch(),
                            ignoreInstalled = True,
                            sourceFiles = allPaths,
                            buildFlavor=cfg.buildFlavor).getRecipe()
    finally:
        use.allowUnknownFlags(False)

    srcMap = {}
    cwd = os.getcwd()

    # setting the _trove to the last version of the source component
    # allows us to search that source component for files that are
    # not in the current directory or lookaside cache.
    recipeClass._trove = srcPkg
    srcFiles = {}

    # don't download sources for groups or filesets
    if (recipeClass.getType() == recipe.RECIPE_TYPE_PACKAGE or
            recipeClass.getType() == recipe.RECIPE_TYPE_GROUP or
            recipeClass.getType() == recipe.RECIPE_TYPE_CAPSULE):
        lcache = lookaside.RepositoryCache(repos, cfg=cfg)
        srcdirs = [ cwd,
                    cfg.sourceSearchDir % {'pkgname': recipeClass.name} ]

        try:
            if (recipeClass.getType() == recipe.RECIPE_TYPE_PACKAGE or
                recipeClass.getType() == recipe.RECIPE_TYPE_CAPSULE):
                recipeObj = recipeClass(cfg, lcache, srcdirs,
                                        lightInstance=True)
            elif recipeClass.getType() == recipe.RECIPE_TYPE_GROUP:
                v = state.getVersion()
                if isinstance(v, versions.NewVersion):
                    label = cfg.buildLabel
                elif isinstance(v, versions.Version):
                    label = v.trailingLabel()
                else:
                    raise RuntimeError('unable to determine which label to use when instantiating group recipe')
                recipeObj = recipeClass(repos, cfg, label,
                                        None, lcache, srcdirs,
                                        lightInstance = True)
        except builderrors.RecipeFileError as msg:
            log.error(str(msg))
            sys.exit(1)

        recipeObj.sourceVersion = state.getVersion()
        recipeObj.loadPolicy()
        level = log.getVerbosity()
        log.setVerbosity(log.INFO)
        if not 'abstractBaseClass' in recipeObj.__class__.__dict__ or not recipeObj.abstractBaseClass:
            if hasattr(recipeObj, 'setup'):
                cook._callSetup(cfg, recipeObj, recordCalls=False)
            else:
                log.error('you need a setup method for your recipe')
        # setup must be called before lcache is populated
        recipeObj.populateLcache()

        # os.path.basenames stripts the protocol off a url as well
        sourceFiles = [ os.path.basename(x.getPath()) for x in
                recipeObj.getSourcePathList(withEphemeral=False) ]
        # sourceFiles is a list of everything which ought to be autosourced.
        # those are either the same as in the previous trove, new (in which
        # case they are missing from the previous trove), or nonexistant. So
        # simple look for files which were autosourced in the previous
        # trove and haven't been marked as refreshed. such files don't need
        # to be downloaded again.

        # (pathId, fileId, version)
        if srcPkg:
            # this avoids downloding files which are autosourced by reusing
            # the fileId/version from the previous version of the trove
            srcFiles = repos.getFileVersions(
                        [ (x[0], x[2], x[3]) for x in srcPkg.iterFileList() ],
                        allowMissingFiles = bool(callback))
            for srcFileObj, (pathId, path, fileId, version) in \
                            zip(srcFiles, srcPkg.iterFileList() ):
                if not state.hasFile(pathId):
                    # this path no longer exists (it was manually removed)
                    continue
                elif not state.fileIsAutoSource(pathId):
                    # the file is no longer autosourced, so we need to
                    # take the file from the current directoy
                    continue
                elif path not in sourceFiles:
                    # this path is no longer an autosourced file (it's not
                    # referenced in the recipe)
                    continue
                elif state.fileNeedsRefresh(pathId):
                    # the file has been refreshed; we need to make sure
                    # we get the refreshed version of the file
                    continue
                elif srcFileObj.flags.isAutoSource():
                    # as long as the previous version of the file with the
                    # same name wasn't autosourced, reuse that version
                    srcMap[path] = (pathId, fileId)

        # there is no reason to download anything which is already in the
        # current directory and not autosourced
        skipPaths = set(x[1] for x in state.iterFileList()
                if not state.fileIsAutoSource(x[0]))
        skipFilter = _makeFilter(skipPaths, exactPaths=True)
        # These files are specifically marked as having been refreshed, so do
        # not attempt to "fix" the cached copy.
        refreshFilter = _makeFilter(state.getFileRefreshList(), exactPaths=True)
        lcache.setRefreshFilter(refreshFilter)

        # files have now been refreshed; reset the refresh bit
        for pathId in (x[0] for x in state.iterFileList()):
            state.fileNeedsRefresh(pathId, set = False)

        try:
            srcFiles = recipeObj.fetchAllSources(skipFilter=skipFilter,
                    withEphemeral=False)
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise errors.CvcError('Source file %s does not exist' %
                                      e.filename)
            else:
                raise errors.CvcError('Error accessing source file %s: %s' %
                                      (e.filename, e.strerror))

        log.setVerbosity(level)

        for fullPath in srcFiles:
            # the loader makes sure the basenames are unique
            base = os.path.basename(fullPath)
            path = None
            for (pathId, path, fileId, version) in state.iterFileList():
                if path == base: break

            if path != base:
                # new file -- we need to do an implicit add
                if os.path.dirname(fullPath) == cwd:
                    # files in the cwd have to be explicitly added
                    log.error('%s (in current directory) must be added with '
                              'cvc add' % base)
                    return

                pathId = makePathId()
                state.addFile(pathId, base, versions.NewVersion(), "0" * 20,
                              isConfig = False, isAutoSource = True)

            if not state.fileIsAutoSource(pathId):
                # we don't do anything else for files unless they are
                # autosourced
                continue

            if base in srcMap:
                # this file was in the previous version of the trove; no
                # need to add it again
                continue

            if os.path.dirname(fullPath) != cwd:
                srcMap[base] = fullPath

    # now remove old files. this is done separately in case the recipe type
    # changed (a package changing to a redirect, for example)
    if srcPkg:
        for (pathId, path, fileId, version) in list(state.iterFileList()):
            if not state.fileIsAutoSource(pathId): continue
            if path not in srcMap:
                # the file doesn't exist anymore
                state.removeFilePath(path)

    state.setPathMap(srcMap)

    recipeVersionStr = recipeClass.version

    if (state.getLastMerged()
          and recipeVersionStr == state.getLastMerged().trailingRevision().getVersion()):
        # If we've merged, and our changes did not affect the original
        # version, then we try to maintain appropriate shadow dots
        newVersion = state.getLastMerged()
        newVersion = newVersion.createShadow(
                                    state.getVersion().branch().label())
        newVersion.incrementSourceCount()
    else:
        # repos.nextVersion seems like a good idea, but it doesn't know how to
        # handle shadow merges. this is easier than teaching it
        d = repos.getTroveVersionsByBranch({ troveName :
                                            { state.getBranch() : None } },
                                            troveTypes=repos.TROVE_QUERY_ALL)
        versionList = list(d.get(troveName, {}).keys())
        versionList.sort()

        ver = None
        if (state.getVersion() == versions.NewVersion()
            or state.getVersion().trailingRevision().getVersion() != \
                                    recipeVersionStr):
            for ver in reversed(versionList):
                if ver.trailingRevision().getVersion() == recipeVersionStr:
                    break

            if ver and ver.trailingRevision().getVersion() == recipeVersionStr:
                newVersion = ver.copy()
            else:
                newVersion = state.getBranch().createVersion(
                           versions.Revision("%s-0" % recipeVersionStr))
        else:
            newVersion = state.getVersion().copy()

        newVersion.incrementSourceCount()
        if troveName in d:
            while newVersion in versionList:
                newVersion.incrementSourceCount()

        del d

    try:
        result = update.buildLocalChanges(repos,
                        [(state, srcPkg, newVersion,
                          update.UpdateFlags(ignoreUGids = True))],
                        forceSha1=True,
                        crossRepositoryDeltas = False,
                        allowMissingFiles = bool(callback))
    except OSError as e:
        if e.errno == errno.ENOENT:
            raise errors.CvcError('File %s does not exist' % e.filename)
        else:
            raise errors.CvcError('Error accessing %s: %s' %
                                  (e.filename, e.strerror))

    # an error occurred.  buildLocalChanges() should have a useful
    # message, so we just return
    if not result: return

    (changeSet, ((isDifferent, newState),)) = result

    if not isDifferent and state.getLastMerged() is None:
        # if there are no changes, but this is the result of a
        # merge, we want to commit anyway
        log.info("no changes have been made to commit")
        return

    if message and message[-1] != '\n':
        message += '\n'

    cl = changelog.ChangeLog(cfg.name, cfg.contact, message)
    if message is None and not cl.getMessageFromUser():
        log.error("no change log message was given")
        return



    if cfg.interactive:
        print('The following commits will be performed:')
        print()
        print('\t%s=%s' % (troveName, newVersion.asString()))
        print()
    if not force and conflicts:
        print('WARNING: performing this commit will switch the active branch:')
        print()
        print('New version %s=%s' % (troveName, newVersion))
        for otherVersion in conflicts:
            print('   obsoletes existing %s=%s' % (troveName, otherVersion))

        if not cfg.interactive:
            print('error: interactive mode is required when changing active branch')
            return

    if cfg.interactive:
        okay = cmdline.askYn('continue with commit? [Y/n]', default=True)

        if not okay:
            return

    newState.changeChangeLog(cl)
    newState.invalidateDigests()
    newState.computeDigests()
    if srcPkg:
        newState.copyMetadata(srcPkg)
    signatureKey = selectSignatureKey(cfg,
                                      newState.getBranch().label().asString())
    if signatureKey is not None:
        # skip integrity checks since we just want to compute the
        # new sha1 with all our changes accounted for
        newState.addDigitalSignature(signatureKey,
                                     skipIntegrityChecks=True)
    if not srcPkg:
        troveCs = newState.diff(None, absolute = True)[0]
    else:
        troveCs = newState.diff(srcPkg)[0]

    if (troveCs.getOldVersion() is not None
        and troveCs.getOldVersion().getHost() !=
            troveCs.getNewVersion().getHost()):
        # we can't commit across hosts, so just make an absolute change
        # set instead (yeah, a bit of a hack). this can happen on shadows
        fileMap = {}
        for (pathId, path, fileId, version) in state.iterFileList():
            fullPath = state.pathMap.get(path, None)
            if fullPath is None:
                fullPath = os.path.join(os.getcwd(), path)
                fileObj = files.FileFromFilesystem(fullPath, pathId)
            else:
                fileObj = files.FileFromFilesystem(fullPath, pathId)
                fileObj.flags.isAutoSource(set = True)

            fileMap[pathId] = (fileObj, fullPath, path)

        changeSet = changeset.CreateFromFilesystem([ (None, newState,
                                                      fileMap) ])
        troveCs = next(changeSet.iterNewTroveList())

    # this replaces the TroveChangeSet update.buildLocalChanges put in
    # the changeset
    changeSet.newTrove(troveCs)

    if state.getLastMerged():
        shadowLabel = state.getVersion().branch().label()
        shadowedVer = state.getLastMerged().createShadow(shadowLabel)
        noDeps = deps.deps.Flavor()

        # Conary requires that if you're committing a source change that
        # contains an upstream merge, you also commit a shadow of that
        # version, for tracking purposes.  This allows future merges
        # to know that you've already merged to this point.
        if not repos.hasTrove(troveName, shadowedVer, noDeps):

            client = conaryclient.ConaryClient(cfg)
            # FIXME: if creating this shadow fails, then there's a race
            #        condition on commit.  Catch and raise a reasonable error.
            log.debug('creating shadow of %s for merging...' % state.getLastMerged())
            shadowCs = client.createShadowChangeSet(str(shadowLabel),
                                [(troveName, state.getLastMerged(), noDeps)],
                                sigKeyId = signatureKey)[1]

            # writable changesets can't do merging, so create a parent
            # readonly one
            if not test:
                repos.commitChangeSet(shadowCs, callback = callback)

    if test:
        # everything past this point assumes the changeset has been
        # committed
        return

    repos.commitChangeSet(changeSet, callback = callback)

    # committing to the repository changes the version timestamp; get the
    # right timestamp to put in the CONARY file
    matches = repos.getTroveVersionsByBranch({ newState.getName() :
                                { newState.getVersion().branch() : None } })
    for ver in matches[newState.getName()]:
        if ver == newState.getVersion():
            break
    assert(ver == newState.getVersion())
    newState.changeVersion(ver)

    newState.setLastMerged(None)
    conaryState.setSourceState(newState)
    conaryState.write("CONARY")
    #FIXME: SIGN HERE

def annotate(repos, filename):
    state = ConaryStateFromFile("CONARY", repos).getSourceState()
    curVersion = state.getVersion()
    branch = state.getBranch()
    troveName = state.getName()

    labelVerList = repos.getTroveVersionsByBranch(
                        {troveName: { branch : None}})[troveName]
    labelVerList = list(labelVerList.keys())
    # sort verList into ascending order (first commit is first in list)
    labelVerList.sort()

    branchVerList = {}
    for ver in labelVerList:
        b = ver.branch()
        if b not in branchVerList:
            branchVerList[b] = []
        branchVerList[b].append(ver)

    found = False
    for (pathId, name, fileId, someFileV) in state.iterFileList():
        if name == filename:
            found = True
            break

    if not found:
        log.error("%s is not a member of this source trove", filename)
        return

    if not state.fileIsConfig(pathId):
        log.error("%s is not a text file", filename)
        return

    # finalLines contains the current version of the file and the
    # annotated information about its creation
    finalLines = []

    # lineMap maps lines in an earlier version of the file to version
    # in finalLines.  This map allows a diff showing line changes
    # between two older versions to be mapped to the latest version
    # of the file
    # Linemap has to be a dict because it is potentially a spare array:
    # Line 2301 of an older version could be the same as line 10 in the
    # newest version.
    lineMap = {}

    s = fixeddifflib.SequenceMatcher(None)
    newV = newTrove = newLines = newFileV = newContact = None

    verList = [ v for v in branchVerList[branch] if not v.isAfter(curVersion) ]

    while verList:
        # iterate backwards from newest to oldest through verList
        oldV = verList.pop()
        oldTrove = repos.getTrove(troveName, oldV, deps.deps.Flavor())

        try:
            name, oldFileId, oldFileV = oldTrove.getFile(pathId)
        except KeyError:
            # this file doesn't exist from this version forward
            break

        if oldFileV != newFileV:
            # this file is distinct for this version of the trove,
            # perform diffs
            oldFile = repos.getFileContents([ (oldFileId, oldFileV) ])[0]
            oldLines = oldFile.get().readlines()
            oldContact = oldTrove.changeLog.getName()
            if newV == None:
                # initialization case -- set up finalLines
                # and lineMap
                index = 0
                for line in oldLines:
                    # mark all lines as having come from this version
                    finalLines.append([line, None])
                    lineMap[index] = index
                    index = index + 1
                unmatchedLines = index
            else:
                for i in range(0, len(newLines)):
                    if lineMap.get(i, None) is not None:
                        assert(newLines[i] == finalLines[lineMap[i]][0])
                # use fixeddifflib SequenceMatcher to
                # find lines that are shared between old and new files
                s.set_seqs(oldLines, newLines)
                blocks = s.get_matching_blocks()
                laststartnew = 0
                for (startold, startnew, lines) in blocks:
                    # range (laststartnew, startnew) is the list of all
                    # lines in the newer of the two files being diffed
                    # that don't exist in the older of the two files
                    # being diffed.  Associate those lines with the
                    # the newer file.
                    for i in range(laststartnew, startnew):
                        # i is a line in newFile here the two versions of the
                        # file do not match, if that line maps back to
                        # a line in finalLines, mark is as changed here
                        if lineMap.get(i,None) is not None:
                            # if this entry does not exist in lineMap,
                            # then line i in this file does not match
                            # to any line in the final file
                            assert(newLines[i] == finalLines[lineMap[i]][0])
                            assert(finalLines[lineMap[i]][1] is None)
                            finalLines[lineMap[i]][1] = (newV, newContact)
                            lineMap[i] = None
                            unmatchedLines = unmatchedLines - 1
                            assert(unmatchedLines >= 0)
                    laststartnew = startnew + lines

                if unmatchedLines == 0:
                    break

                # future diffs
                changes = {}
                for (startold, startnew, lines) in blocks:
                    if startold == startnew:
                        continue
                    # the range(startnew, startnew + lines) are the lines
                    # that are the same between newfile and oldfile.  Since
                    # all future diffs will be against oldfile, we want to
                    # ensure that the lineMap points from the line numbers
                    # in the old file to the line numbers in the final file

                    for i in range(0, lines):
                        if lineMap.get(startnew + i, None) is not None:
                            changes[startold + i] = lineMap[startnew + i]
                            # the pointer at lineMap[startnew + i]
                            # is now invalid; the correct pointer is
                            # now at lineMap[startold + i]
                            if startnew + i not in changes:
                                changes[startnew + i] = None
                lineMap.update(changes)
        (newV, newTrove, newContact) = (oldV, oldTrove, oldContact)
        (newFileV, newLines) = (oldFileV, oldLines)

        # assert that the lineMap is still correct --
        for i in range(0, len(newLines)):
            if lineMap.get(i, None) is not None:
                assert(newLines[i] == finalLines[lineMap[i]][0])

        # there are still unmatched lines, and there is a parent branch,
        # so search the parent branch for matches
        if not verList and branch.hasParentBranch():
            branch = branch.parentBranch()
            if branch not in branchVerList:
                labelVerList = repos.getTroveVersionsByBranch(
                        { troveName : { branch : None }})[troveName]
                keys = list(labelVerList.keys())
                keys.sort()

                for ver in keys:
                    b = ver.branch()
                    if b not in branchVerList:
                        branchVerList[b] = []
                    branchVerList[b].append(ver)
            verList = [ v for v in  branchVerList[branch] \
                                            if not v.isAfter(curVersion)]

    if unmatchedLines > 0:
        contact = oldTrove.changeLog.getName()
        # these lines are in the original version of the file
        for line in finalLines:
            if line[1] is None:
                line[1] = (oldV, contact)

    # we have to do some preprocessing try to line up the code w/ long
    # branch names, otherwise te output is (even more) unreadable
    maxV = 0
    maxN= 0
    for line in finalLines:
        version = line[1][0]
        name = line[1][1]
        maxV = max(maxV, len(version.asString(defaultBranch=branch)))
        maxN = max(maxN, len(name))

    for line in finalLines:
        version = line[1][0]
        tv = version.trailingRevision()
        name = line[1][1]
        date = time.strftime('%Y-%m-%d', time.localtime(tv.timeStamp))
        info = '(%-*s %s):' % (maxN, name, date)
        # since the line is not necessary starting at a tabstop,
        # lines might not line up
        line[0] = line[0].replace('\t', ' ' * 8)
        print("%-*s %s %s" % (maxV, version.asString(defaultBranch=branch), info, line[0]), end=' ')

def _describeShadow(oldVersion, newVersion):
    return "New shadow:\n  %s\n  of\n  %s" %(newVersion, oldVersion)

# findRelativeVersion might move to another module?
def findRelativeVersion(repos, troveName, count, newV):
    vers = repos.getTroveVersionsByBranch(
                            { troveName : { newV.branch() : None } } )
    vers = list(vers[troveName].keys())
    vers.sort()
    # erase everything later than us
    i = vers.index(newV)
    del vers[i:]

    branchList = []
    for v in vers:
        if v.branch() == newV.branch():
            branchList.append(v)

    if len(branchList) < count:
        oldV = None
        old = None
    else:
        oldV = branchList[-count]
        old = (troveName, oldV, deps.deps.Flavor())

    return old, oldV


def rdiff(repos, buildLabel, troveName, oldVersion, newVersion):
    i = _getIterRdiff(repos, buildLabel, troveName, oldVersion, newVersion)
    if i is not None:
        for line in i:
            print(line)

def rdiffChangeSet(repos, job):
    # creates a changeset, but only puts in file contents for config files, and
    # always puts those contents in as diffs (unless the file is new of course)
    # returns a list of the files whose contents are included in the changeset
    oldTroveInfo = (job[0], job[1][0], job[1][1])
    newTroveInfo = (job[0], job[2][0], job[2][1])

    if oldTroveInfo[1] is None:
        oldTroveInfo = None
        oldTrove, newTrove = None, repos.getTrove(*newTroveInfo)
    else:
        oldTrove, newTrove = repos.getTroves( [ oldTroveInfo, newTroveInfo ] )

    trvCs, fileChangeList = newTrove.diff(oldTrove)[0:2]
    cs = changeset.ChangeSet()
    cs.newTrove(trvCs)

    filesNeeded = []
    for (pathId, oldFileId, oldFileVersion, newFileId, newFileVersion) in \
                                                fileChangeList:
        if oldFileId:
            filesNeeded.append((pathId, oldFileId, oldFileVersion))

        filesNeeded.append((pathId, newFileId, newFileVersion))

    fileObjects = repos.getFileVersions(filesNeeded)
    contentsNeeded = []
    contentRequest = []
    for (pathId, oldFileId, oldFileVersion, newFileId, newFileVersion) in \
                                                fileChangeList:
        if oldFileId:
            oldFile = fileObjects.pop(0)
        else:
            oldFile = None

        newFile = fileObjects.pop(0)

        fileDiff = newFile.diff(oldFile)
        cs.addFile(oldFileId, newFileId, fileDiff)

        if not newFile.hasContents or not newFile.flags.isConfig():
            pass
        elif not oldFile or not oldFile.hasContents:
            contentRequest.append((newFileId, newFileVersion))
            contentsNeeded.append((pathId, oldFile, False, newFile,
                                   newFileId))
        elif oldFile.contents.sha1() == newFile.contents.sha1():
            # don't get contents for files which haven't changed
            continue
        elif (oldFile and oldFile.flags.isConfig()) or newFile.flags.isConfig():
            contentsNeeded.append((pathId, oldFile, True, newFile,
                                   newFileId))
            contentRequest.append((oldFileId, oldFileVersion))
            contentRequest.append((newFileId, newFileVersion))

    contents = repos.getFileContents(contentRequest)
    pathList = []
    for (pathId, oldFile, oldFileHasContents, newFile, newFileId) \
                                                    in contentsNeeded:
        if oldFileHasContents:
            oldContents = contents.pop(0)
        else:
            oldContents = None

        newContents = contents.pop(0)

        (contType, cont) = changeset.fileContentsDiff(oldFile, oldContents,
                                                      newFile, newContents)

        cs.addFileContents(pathId, newFileId, contType, cont, True)

        pathList.append(newTrove.getFile(pathId)[0])

    return cs, pathList

def _getIterRdiff(repos, buildLabel, troveName, oldVersion, newVersion):
    if not troveName.endswith(":source"):
        troveName += ":source"

    new = repos.findTrove(buildLabel, (troveName, newVersion, None))
    if len(new) > 1:
        log.error("%s matches multiple versions" % newVersion)
        return
    new = new[0]
    newV = new[1]

    try:
        count = -int(oldVersion)
        if count == 1 and newV.isShadow() and not newV.isModifiedShadow():
            print(_describeShadow(newV.parentVersion().asString(), newVersion))
            return
        old, oldV = findRelativeVersion(repos, troveName, count, newV)

    except ValueError:
        if newV.isShadow() and not newV.isModifiedShadow() and \
           newV.parentVersion().asString() == oldVersion:
            print(_describeShadow(oldVersion, newVersion))
            return

        old = repos.findTrove(buildLabel, (troveName, oldVersion, None))
        if len(old) > 1:
            log.error("%s matches multiple versions" % oldVersion)
            return
        old = old[0]
        oldV = old[1]

    if old:
        old, new = repos.getTroves((old, new))
    else:
        new = repos.getTrove(*new)

    cs, pathList = rdiffChangeSet(repos,
                                  (troveName, (oldV, deps.deps.Flavor()),
                                              (newV, deps.deps.Flavor()),
                                   False) )

    return _iterChangeSet(repos, cs, old, new, pathList = pathList)

def revert(repos, fileList):
    conaryState = ConaryStateFromFile("CONARY")
    state = conaryState.getSourceState()

    origTrove = repos.getTrove(state.getName(),
                               state.getVersion().canonicalVersion(),
                               deps.deps.DependencySet())

    checkList = []

    pathsToCheck = set(fileList)
    # look file files we've been asked to revert
    for fileInfo in origTrove.iterFileList():
        if not fileList:
            if not state.fileIsAutoSource(fileInfo[0]):
                checkList.append(fileInfo)
        elif fileInfo[1] in fileList:
            checkList.append(fileInfo)
            pathsToCheck.remove(fileInfo[1])

    if pathsToCheck:
        includedFiles = set( x[1] for x in state.iterFileList() )
        #includedFiles.update(set( x[1] for x in origTrove.iterFileList() ))
        for path in pathsToCheck:
            if path in includedFiles:
                log.error('file %s was newly added; use cvc remove to '
                          'remove it' % path)
            else:
                log.error('file %s not found in source component' % path)

        return 1

    del pathsToCheck

    fileObjects = repos.getFileVersions(
                            [ (x[0], x[2], x[3]) for x in checkList ] )
    contentsNeeded = [ (x[0][2], x[0][3]) for x in
                            zip(checkList, fileObjects)
                            if x[1].hasContents ]
    contents = repos.getFileContents(contentsNeeded)

    currentDir = os.getcwd()

    for fileInfo, fileObj in zip(checkList, fileObjects):
        if fileObj.flags.isAutoSource():
            raise errors.CvcError('autosource files cannot be '
                                  'reverted')

        path = fileInfo[1]

        if fileObj.hasContents:
            content = contents.pop(0)
        else:
            content = None

        if os.path.exists(path):
            currentFileObj = files.FileFromFilesystem(path, fileInfo[0])
            currentFileObj.flags.thaw(fileObj.flags.freeze())
            if fileObj.__eq__(currentFileObj, ignoreOwnerGroup = True):
                continue

        log.info('reverting %s', path)
        fileObj.restore(content, '/', currentDir + '/' + path,
                        nameLookup = False)

        # the user originally to removed the file (which means marking it
        # as autosource!) but now wants it back
        if state.fileIsAutoSource(fileInfo[0]):
            state.fileIsAutoSource(fileInfo[0], set = False)

    conaryState.write("CONARY")

def diff(repos, versionStr = None, pathList = None, logErrors = True, dirName='.'):
    # return 0 if no differences, 1 if differences, 2 on error
    i = _getIterDiff(repos, versionStr, pathList, logErrors, dirName)
    if i in (0, 2):
        return i
    for line in i:
        print(line)
    return 1

def _getIterDiff(repos, versionStr, pathList=None, logErrors=True, dirName='.'):
    def error(*s):
        if logErrors: log.error(*s)

    dirName = os.path.abspath(dirName)
    state = ConaryStateFromFile(os.sep.join((dirName, 'CONARY')),
                                repos).getSourceState()

    if state.getVersion() == versions.NewVersion():
        error("no versions have been committed")
        return 2

    if pathList is not None:
        for path in pathList:
            if not os.path.exists(path):
                error("File %s can not be found" % path)
                return 2

    if versionStr:
        versionStr = state.expandVersionStr(versionStr)

        try:
            pkgList = repos.findTrove(None, (state.getName(), versionStr, None))
        except errors.TroveNotFound as e:
            log.error("Unable to find source component %s with version %s: %s",
                      state.getName(), versionStr, str(e))
            return 2

        if len(pkgList) > 1:
            log.error("%s specifies multiple versions" % versionStr)
            return 2

        oldTrove = repos.getTrove(*pkgList[0])
    else:
        oldTrove = repos.getTrove(state.getName(), state.getVersion(), deps.deps.Flavor())

    result = update.buildLocalChanges(repos,
            [(state, oldTrove, versions.NewVersion(),
              update.UpdateFlags(ignoreUGids = True))],
            forceSha1=True, ignoreAutoSource = True,
            root = dirName)
    if not result: return 2

    result = localAutoSourceChanges(oldTrove, result)

    (changeSet, ((isDifferent, newState),)) = result
    if not isDifferent: return 0

    return _iterChangeSet(repos, changeSet, oldTrove, state,
                          displayAutoSourceFiles = False, pathList = pathList)

def _iterChangeSet(repos, changeSet, oldTrove, newTrove,
                   displayAutoSourceFiles = True, pathList = None):
    troveChanges = changeSet.iterNewTroveList()
    troveCs = next(troveChanges)
    assert(util.assertIteratorAtEnd(troveChanges))

    for line in formatOneLog(troveCs.getNewVersion(), troveCs.getChangeLog()):
        yield line
    yield ''

    fileList = [ (x[0], x[1], True, x[2], x[3]) for x in troveCs.getNewFileList() ]
    fileList += [ (x[0], x[1], False, x[2], x[3]) for x in
                            troveCs.getChangedFileList() ]

    # sort by pathId to match the changeset order
    fileList.sort()
    for (pathId, path, isNew, fileId, newVersion) in fileList:
        if isNew:
            yield '%s: new' % path
            chg = changeSet.getFileChange(None, fileId)
            f = files.ThawFile(chg, pathId)
            if pathList is not None and os.path.basename(path) not in pathList:
                continue

            if (displayAutoSourceFiles or not f.flags.isAutoSource()) \
                    and f.hasContents and f.flags.isConfig():
                (contType, contents) = changeSet.getFileContents(pathId, fileId)
                lines = contents.get().readlines()

                yield '--- /dev/null'
                yield '+++ ' +path
                yield '@@ -0,0 +%s @@' %len(lines)
                for line in lines:
                    yield '+%s' %line.rstrip('\n')
                yield ''
            continue

        # changed file
        if path:
            dispStr = path
            if oldTrove:
                oldPath = oldTrove.getFile(pathId)[0]
                dispStr += " (aka %s)" % oldPath
        else:
            path = oldTrove.getFile(pathId)[0]
            dispStr = path

        #if files to show diff passed - check if current is in
        if pathList is not None and os.path.basename(dispStr) not in pathList:
            continue;

        oldFileId = oldTrove.getFile(pathId)[1]

        if not newVersion:
            yield dispStr
            continue

        yield dispStr + ': changed'

        yield 'Index: %s' %path
        yield '=' * 68

        csInfo = changeSet.getFileChange(oldFileId, fileId)
        if csInfo:
            for line in files.fieldsChanged(csInfo):
                yield line
        else:
            yield 'version'

        if csInfo and files.contentsChanged(csInfo):
            (contType, contents) = changeSet.getFileContents(pathId, fileId)
            if contType == changeset.ChangedFileTypes.diff:
                yield '--- %s %s' %(path, oldTrove.getVersion().asString())
                yield '+++ %s %s' %(path, newVersion.asString())
                for line in contents.get().readlines():
                    yield line.rstrip('\n')
                yield ''
                yield ''

    for pathId in troveCs.getOldFileList():
        path = oldTrove.getFile(pathId)[0]
        yield '%s: removed' % path

@api.developerApi
def updateSrc(repos, versionList = None, callback = None):
    try:
        return nologUpdateSrc(repos, versionList, callback)
    except builderrors.UpToDate as e:
        e.logInfo()
    except builderrors.CheckinError as e:
        e.logError()

@api.developerApi
def nologUpdateSrc(repos, versionList = None, callback = None):
    if not versionList:
        updateSpecs = [ (os.getcwd(), None) ]
    else:
        updateSpecs = []

        for versionStr in versionList:
            if os.path.isdir(versionStr):
                targetDir = versionStr
                versionStr = None
            elif '=' in versionStr:
                targetDir, versionStr = versionStr.split('=', 1)
                if not versionStr:
                    versionStr = None
            else:
                targetDir = os.getcwd()

            updateSpecs.append( (targetDir, versionStr) )
        del targetDir, versionStr

    if not callback:
        callback = CheckinCallback()

    for i, (targetDir, versionStr) in enumerate(updateSpecs):
        conaryState = ConaryStateFromFile(targetDir + "/CONARY", repos)
        state = conaryState.getSourceState()
        if state.getVersion() == versions.NewVersion():
            raise builderrors.NotCheckedInError(state.getName())

        updateSpecs[i] = (targetDir, versionStr, state)

    latestVersions = [ x for x in enumerate(updateSpecs) if x[1][1] is None ]
    specificVersions = [ x for x in enumerate(updateSpecs) if x[1][1]
                                                                is not None ]

    if latestVersions:
        q = {}
        for state in [ x[1][2] for x in latestVersions ]:
            q.update({ state.getName() : { state.getBranch().label() :
                                                    [ deps.deps.Flavor() ] } } )

        r = repos.getTroveLatestByLabel(q)

        for i, state in [ (x[0], x[1][2]) for x in latestVersions ]:
            headVersion = list(r[state.getName()].keys())[0]
            newBranch = None
            if headVersion == state.getVersion():
                raise builderrors.UpToDate(targetDir)

            if headVersion.branch() != state.getBranch():
                #TODO: Get rid of this API level logging
                log.info("switching directory %s to branch %s", targetDir,
                         headVersion.branch())
                newBranch = headVersion.branch()

            updateSpecs[i] = (updateSpecs[i][0], state, headVersion, newBranch)

    if specificVersions:
        q = [ (state.getName(), state.expandVersionStr(versionStr), None)
                for i, (targetDir, versionStr, state) in specificVersions ]

        try:
            matches = repos.findTroves(None, q)
        except errors.TroveNotFound as e:
            raise builderrors.NoSourceTroveFound(str(e))

        for i, (targetDir, versionStr, state) in specificVersions:
            l = matches[(state.getName(), state.expandVersionStr(versionStr),
                         None)]
            if len(l) > 1:
                raise builderrors.MultipleSourceVersions(versionStr)
            elif not len(l):
                raise builderrors.NoSuchSourceVersion(state.getName(), versionStr)

            headVersion = l[0][1]
            newBranch = headVersion.branch()
            updateSpecs[i] = (updateSpecs[i][0], state, headVersion, newBranch)

    job = [ (state.getName(), (state.getVersion(), deps.deps.Flavor()),
                              (headVersion,        deps.deps.Flavor()), False)
            for (targetDir, state, headVersion, newBranch) in updateSpecs ]

    fullChangeSet = repos.createChangeSet(job,
                                          excludeAutoSource = True,
                                          callback = callback)
    callback.done()

    success = True
    for targetDir, state, headVersion, newBranch in updateSpecs:
        # this changeSet manipulation creates a changeset with only the
        # single trove in it becaues the update code deals with entire
        # changesets and can't send bits to different root directories
        fullChangeSet.reset()
        changeSet = changeset.ReadOnlyChangeSet()
        changeSet.merge(fullChangeSet)

        troveCs = changeSet.getNewTroveVersion(state.getName(),
                                               headVersion, deps.deps.Flavor())
        if not troveCs.getOldVersion():
            raise errors.CvcError(
                      "Current version of this source has been removed "
                      "from the repository; it cannot be updated in place. ")

        l = [ x for x in changeSet.iterNewTroveList() ]
        [ changeSet.delNewTrove(x.getName(), x.getNewVersion(),
                                x.getNewFlavor()) for x in l ]
        changeSet.newTrove(troveCs)

        localVer = state.getVersion().createShadow(versions.LocalLabel())
        fsJob = update.FilesystemJob(repos, changeSet,
                     { (state.getName(), localVer, state.getFlavor()) : state },
                     root = targetDir,
                     flags = update.UpdateFlags(ignoreUGids = True,
                                                merge = True))
        errList = fsJob.getErrorList()
        if errList:
            raise builderrors.CheckinErrorList(errList)

        fsJob.apply()
        newPkgs = fsJob.iterNewTroveList()
        newState = next(newPkgs)
        assert(util.assertIteratorAtEnd(newPkgs))

        if newState.getVersion() == troveCs.getNewVersion() and newBranch:
            newState.changeBranch(newBranch)

        conaryState.setSourceState(newState)
        conaryState.write(targetDir + "/CONARY")

    return success

def _determineRootVersion(repos, state):
    ver = state.getVersion()
    assert(ver.isShadow())
    if ver.hasParentVersion():
        d = {state.getName(): {ver.parentVersion() : None}}
        return list(repos.getTroveVersionFlavors(d)[state.getName()].keys())[0]
    else:
        branch = ver.branch()
        name = state.getName()
        vers = repos.getTroveVersionsByBranch({name: {branch : None}})[name]
        for ver in reversed(sorted(vers)):
            # find latest shadowed version

            if ver.hasParentVersion():
                parentVer = ver.parentVersion()
                # we need to get the timestamp for this version
                parentVer = list(repos.getTroveVersionFlavors(
                            { name : { parentVer : None } })[name].keys())[0]
                return parentVer
        # We must have done a shadow at some point.
        assert(0)

def merge(cfg, repos, versionSpec=None, callback=None):
    # merges the head of the current shadow with the head of the branch
    # it shadowed from
    try:
        conaryState = ConaryStateFromFile("CONARY", repos)
        state = conaryState.getSourceState()
    except OSError:
        return

    if not callback:
        callback = CheckinCallback()

    troveName = state.getName()
    troveBranch = state.getBranch()

    if state.getLastMerged():
        log.error("outstanding merge must be committed before merging again")
        return

    if not state.getVersion().isShadow():
        log.error("%s=%s is not a shadow" % (troveName, troveBranch.asString()))
        return

    # make sure the current version is at head
    r = repos.getTroveLatestByLabel(
            { troveName : { troveBranch.label() : [ deps.deps.Flavor() ] } } )
    shadowHeadVersion = list(r[troveName].keys())[0]
    if state.getVersion() != shadowHeadVersion:
        log.info("working directory is not the latest on label %s" %
                            troveBranch.label())
        return

    # safe to call parentBranch() b/c a shadow will always have a parent branch
    if versionSpec:
        parentBranch = troveBranch.parentBranch()
        parentLabel = parentBranch.label()
        if versionSpec[0] == '/':
            version = versions.VersionFromString(versionSpec)
            if isinstance(version, versions.Branch):
                log.error("Cannot specify branches to merge")
                return
            elif version.branch() != parentBranch:
                log.error("Can only merge from parent branch %s" % parentBranch)
                return
        else:
            for disallowedChar in ':@/':
                if disallowedChar in versionSpec:
                    log.error("Can only specify upstream version,"
                              " upstream version + source count"
                              " or full versions to merge")
                    return
        versionList = repos.findTrove(parentLabel,
                                     (troveName, versionSpec, None), None)
        # we use findTrove so we can support both upstream version and
        # upstream version + release.
        if not versionList:
            log.error("Revision %s of %s not found on branch %s" % (versionSpec, troveName, parentBranch))
            return
        assert(len(versionList) == 1)
        parentHeadVersion = versionList[0][1]
    else:
        r = repos.getTroveLatestByLabel(
                { troveName : { troveBranch.parentBranch().label() :
                                [ deps.deps.Flavor() ] } } )
        assert(len(r[troveName]) == 1)
        parentHeadVersion = list(r[troveName].keys())[0]

    parentRootVersion = _determineRootVersion(repos, state)

    if parentHeadVersion < parentRootVersion:
        # our head is earlier than the base.  The user specified something wacky.
        assert(versionSpec) # otherwise something is very wrong
        log.error("Cannot merge: version specified is before the last "
                  "merge point, would be merging backwards")
        return
    elif parentHeadVersion == parentRootVersion:
        # merging to the version we're based on doesn't make much sense
        log.error("No changes have been made on the parent branch; nothing "
                  "to merge.")
        return
    elif parentRootVersion.branch() != parentHeadVersion.branch():
        targetBranch = parentHeadVersion.branch().createShadow(
                            shadowHeadVersion.trailingLabel())
        r = repos.getTroveLeavesByBranch(
                    { troveName : { targetBranch : [ deps.deps.Flavor() ] } } )
        if r:
            log.info("Merging from %s onto %s", parentRootVersion.branch(),
                     parentHeadVersion.branch())
        else:
            log.info("Merging from %s onto new shadow %s",
                     parentRootVersion.branch(), parentHeadVersion.branch())
    else:
        targetBranch = shadowHeadVersion.branch()

    if os.path.exists(state.getRecipeFileName()):
        use.allowUnknownFlags(True)
        recipeClass = loadrecipe.RecipeLoaderFromSourceDirectory(state,
                                cfg = cfg, repos = repos,
                                branch = state.getBranch(),
                                ignoreInstalled = True,
                                buildFlavor=cfg.buildFlavor).getRecipe()
    else:
        recipeClass = None.__class__

    if getattr(recipeClass, '_isDerived', False):
        # Merges between non-derived recipes and derived recipes don't
        # do a patch merge.
        loader = loadrecipe.recipeLoaderFromSourceComponent(troveName, cfg,
                               repos, versionStr = str(parentHeadVersion),
                               buildFlavor=cfg.buildFlavor)[0]
        parentRecipeClass = loader.getRecipe()
        if not issubclass(parentRecipeClass,
                          derivedrecipe.DerivedPackageRecipe):
            newVersion = parentHeadVersion.trailingRevision().getVersion()
            if True or newVersion != state.getVersion().trailingRevision.getVersion():
                recipePath = state.getRecipeFileName()
                recipe = open(recipePath).read()
                regexp = re.compile('''^[ \t]*version *=[ ]*['"](.*)['"] *$''',
                                    re.MULTILINE) #' emacs cant grok above quotes
                l = list(regexp.finditer(recipe))
                if len(l) != 1:
                    log.warning("Couldn't find version assignment in %s. The "
                                "version for this recipe needs to be set to "
                                "%s." % (recipePath, newVersion) )
                else:
                    match = l[0]
                    newRecipe = recipe[0:match.start(1)] + newVersion + \
                                recipe[match.end(1):]
                    open(recipePath, "w").write(newRecipe)

            state.setLastMerged(parentHeadVersion)
            state.changeVersion(shadowHeadVersion)
            conaryState.write("CONARY")
            return

    changeSet = repos.createChangeSet([(troveName,
                            (parentRootVersion, deps.deps.Flavor()),
                            (parentHeadVersion, deps.deps.Flavor()),
                            0)], excludeAutoSource = True, callback = callback)

    # make sure there are changes to apply
    troveChanges = changeSet.iterNewTroveList()
    troveCs = next(troveChanges)
    assert(util.assertIteratorAtEnd(troveChanges))

    localVer = parentRootVersion.createShadow(versions.LocalLabel())
    fsJob = update.FilesystemJob(repos, changeSet,
                                 { (state.getName(), localVer,
                                    state.getFlavor()) : state },
                                 os.getcwd(),
                                 flags = update.UpdateFlags(ignoreUGids = True,
                                                            merge = True) )
    errList = fsJob.getErrorList()
    if errList:
        for err in errList: log.error(err)
        return 1
    fsJob.apply()

    newPkgs = fsJob.iterNewTroveList()
    newState = next(newPkgs)
    assert(util.assertIteratorAtEnd(newPkgs))

    # this check succeeds if the merge was successful
    if newState.getVersion() == troveCs.getNewVersion():
        newState.setLastMerged(parentHeadVersion)
        newState.changeVersion(shadowHeadVersion)
        newState.changeBranch(targetBranch)

    conaryState.setSourceState(newState)
    conaryState.write("CONARY")

def markRemoved(cfg, repos, troveSpec):
    troveSpec = cmdline.parseTroveSpec(troveSpec)
    trvList = repos.findTrove(cfg.buildLabel, troveSpec,
                              defaultFlavor = cfg.flavor)
    if len(trvList) > 1:
        log.error("multiple troves found " +
            " ".join([ "%s=%s[%s]" % x for x in trvList ] ))
        return 1

    # XXX should this do a full recursive descent? seems scary.
    existingTrove = repos.getTrove(withFiles = False, *trvList[0])
    if not existingTrove.getName().startswith('group'):
        trvList += [ x for x in
                     existingTrove.iterTroveList(strongRefs = True) ]

    cs = changeset.ChangeSet()

    for (name, version, flavor) in trvList:
        trv = trove.Trove(name, version, flavor,
                          type = trove.TROVE_TYPE_REMOVED)
        trv.computeDigests()
        signatureKey = selectSignatureKey(cfg,
                                          version.trailingLabel().asString())
        if signatureKey is not None:
            # skip integrity checks since we just want to compute the
            # new sha1 with all our changes accounted for
            trv.addDigitalSignature(signatureKey, skipIntegrityChecks=True)

        cs.newTrove(trv.diff(None, absolute = True)[0])

    # XXX This forces interactive mode for removing troves. Seems like a good
    # idea.
    if True or cfg.interactive:
        print('The contents of the following troves will be removed:')
        print()
        for (name, version, flavor) in trvList:
            print('\t%s=%s[%s]' % (name, version.asString(), str(flavor)))
        print()
        okay = cmdline.askYn('continue with commit? [Y/n]', default=True)

        if not okay:
            return

    repos.commitChangeSet(cs)

def addFiles(fileList, ignoreExisting=False, text=False, binary=False,
             repos=None, defaultToText=True):

    def _addFile(filename, state, stateHash):
        if filename == "." or filename == "..":
            log.error("cannot add special directory %s to trove" % filename)
            return

        try:
            os.lstat(filename)
        except OSError:
            log.error("file %s does not exist", filename)
            return

        # Normalize the file path. This will remove things like
        # ./CONARY, ././CONARY etc.
        filename = os.path.normpath(filename)

        if filename in stateHash:
            (pathId, path, fileId, version) = stateHash[filename]
            if state.fileIsAutoSource(pathId):
                state.addFile(pathId, path, version,
                              fileId,
                              isConfig = state.fileIsConfig(pathId),
                              isAutoSource = False)
            elif not ignoreExisting:
                log.error("file %s is already part of this source component" % path)
            return

        if filename == "CONARY":
            log.error("refusing to add CONARY to the list of managed sources")
            return
        fileMagic = magic.magic(filename)
        if fileMagic and fileMagic.name == "changeset":
            log.error("do not add changesets to source components")
            return

        pathId = makePathId()

        sb = os.lstat(filename)

        if filename.endswith('.recipe'):
            isConfig = True
        elif (not(stat.S_ISREG(sb.st_mode)) or binary or
              nonCfgRe.match(filename)):
            isConfig = False
        elif text or cfgRe.match(filename) or (
            fileMagic and isinstance(fileMagic, magic.script)):
            isConfig = True
        elif defaultToText:
            # this option should most likely not be used for modern clients
            # that are adding files, however, for backwards compatibility
            # purposes we need to allow this setting to be passed in.
            log.warning('unknown file type for %s - setting to text mode.' % filename)
            isConfig = True
        else:
            log.error("cannot determine if %s is binary or text. please add "
                      "--binary or --text and rerun cvc add for %s",
                      filename, filename)
            return

        if isConfig:
            sb = os.stat(filename)
            if sb.st_size > 0 and stat.S_ISREG(sb.st_mode):
                fobj = file(filename, "r")
                fobj.seek(-1, 2)
                term = fobj.read(1)
                fobj.close()
                if term != '\n':
                    log.error("%s does not end with a trailing new line",
                                filename)

                    # XXX Should this terminate the import of the whole set,
                    # or just of this file?
                    return

        version = versions.NewVersion()
        fileId = "0" * 20
        state.addFile(pathId, filename, version, fileId,
                      isConfig = isConfig, isAutoSource = False)
        # Remove silly ./././ if present.
        filename = os.path.normpath(filename)
        stateHash[filename] = (pathId, filename, fileId, version)


    assert(not text or not binary)
    try:
        conaryState = ConaryStateFromFile("CONARY", repos=repos)
        state = conaryState.getSourceState()
    except OSError:
        return

    # Iterate over the state, and hash the files (by path)
    stateHash = {}
    for ent in state.iterFileList():
        # ent is (pathId, path, fileId, version)
        path = ent[1]
        stateHash[path] = ent

    grph = graph.DirectedGraph()
    tlinks = {}
    for filename in fileList:
        # Defer the addition of symlinks since we want to make sure the
        # de-referenced value is tracked
        if not os.path.islink(filename):
            _addFile(filename, state, stateHash)
            continue

        filename = os.path.normpath(filename)

        # dereference the symlink
        deref = os.readlink(filename)
        if deref[0] == '/':
            # Absolute paths not allowed
            log.error("not adding absolute symlink %s -> %s"
                      % (filename, deref))
            continue

        # Don't allow .. in paths, we don't want to let the symlink escape
        # the current directory
        if '..' in util.splitPathReverse(deref):
            log.error("not adding symlink with bad destination %s -> %s"
                % (filename, deref))
            continue

        deref = os.path.normpath(deref)

        # No dangling symlinks
        if not os.path.exists(deref):
            log.error("not adding broken symlink %s -> %s" % (filename, deref))
            continue

        grph.addNode(filename)
        grph.addNode(deref)
        grph.addEdge(filename, deref)
        # Add transposed link
        tlinks.setdefault(deref, set()).add(filename)

    # t-sort the graph
    # We cannot have symlink loops at this point, it turns out that, for the
    # case l1 -> l2 -> l3 -> l1, os.path.exists(l1) will return False
    for deref in reversed(grph.getTotalOrdering()):
        if deref not in tlinks:
            # This is a source with nothing referencing it
            continue
        if deref not in stateHash:
            # Grab one link
            linkname = tlinks[deref].pop()
            log.error("not adding link with dereferenced file not tracked "
                      "%s -> %s" % (linkname, deref))
            continue
        for linkname in tlinks[deref]:
            _addFile(linkname, state, stateHash)

    conaryState.write("CONARY")

def removeFile(filename, repos=None):
    conaryState = ConaryStateFromFile("CONARY", repos)
    state = conaryState.getSourceState()

    path = None
    for (pathId, path, fileId, version) in state.iterFileList():
        if path == filename:
            break

    if path != filename:
        log.error("file %s is not under management" % filename)
        return 1

    if version == versions.NewVersion():
        # newly added file is being removed; go ahead and remove it now
        state.removeFile(pathId)
    else:
        # we don't remove the file here; we mark it as autosource instead. the
        # commit will remove it if need be or commit it as autosource if that's
        # what's needed
        state.fileIsAutoSource(pathId, set = True)

    if util.exists(filename):
        sb = os.lstat(filename)
        try:
            if sb.st_mode & stat.S_IFDIR:
                os.rmdir(filename)
            else:
                os.unlink(filename)
        except OSError as e:
            log.error("cannot remove %s: %s" % (filename, e.strerror))
            return 1

    conaryState.write("CONARY")

@api.developerApi
def newTrove(repos, cfg, name, dir = None, template = None, buildBranch=None,
             factory = None):
    parts = name.split('=', 1)
    if len(parts) == 1:
        label = cfg.buildLabel
    else:
        versionStr = parts[1]
        name = parts[0]
        try:
            label = versions.Label(versionStr)
        except versions.ParseError:
            log.error("%s is not a valid label" % versionStr)
            return
    if not trove.troveNameIsValid(name):
        raise errors.CvcError('%s is not a valid package name', name)
    component = "%s:source" % name

    if factory == 'factory' and not name.startswith('factory-'):
        raise errors.CvcError('The name of factory troves must begin with '
                              '"factory-"')
    elif factory != 'factory' and name.startswith('factory-'):
        raise errors.CvcError('Only factory troves may use "factory-" in '
                'their name. Add --factory=factory to create a factory trove.')

    # XXX this should really allow a --build-branch or something; we can't
    # create new packages on branches this way
    if not buildBranch:
        branch = versions.Branch([label])
    else:
        branch = buildBranch
    sourceState = SourceState(component, versions.NewVersion(), branch)
    sourceState.setFactory(factory)
    conaryState = ConaryState(cfg.context, sourceState)

    # see if this package exists on our build label
    if repos and repos.getTroveLeavesByLabel(
                        { component : { label : None } },
                        ).get(component, []):
        log.error("package %s already exists" % component)
        return

    if dir is None:
        dir = name

    if not template:
        if trove.troveIsGroup(name):
            template = cfg.groupTemplate
        elif trove.troveIsFactory(name):
            template = cfg.factoryTemplate
        else:
            template = cfg.recipeTemplate

    if not os.path.isdir(dir):
        try:
            os.mkdir(dir)
        except:
            log.error("cannot create directory %s/%s", os.getcwd(), dir)
            return

    recipeFile = '%s.recipe' % name
    recipeFileDir = os.path.join(dir, recipeFile)
    if not os.path.exists(recipeFileDir) and template:
        try:
            path = util.findFile(template, cfg.recipeTemplateDirs)
        except OSError:
            log.error("recipe template '%s' not found" % template)
            return

        macros = Macros()
        className = util.convertPackageNameToClassName(name)
        macros.update({'contactName': cfg.name,
                       'contact': cfg.contact,
                       'year': str(time.localtime()[0]),
                       'name': name,
                       'upperName': className,
                       'className': className})

        template = open(path).read()
        recipe = open(recipeFileDir, 'w')

        try:
            recipe.write(template % macros)
        except builderrors.MacroKeyError as e:
            log.error("could not replace '%s' in recipe template '%s'" % (e.args[0], path))
            return
        recipe.close()

    if os.path.exists(recipeFileDir):
        cwd = os.getcwd()
        try:
            os.chdir(dir)
            pathId = makePathId()
            sourceState.addFile(pathId, recipeFile, versions.NewVersion(), "0" * 20, isConfig = True, isAutoSource = False)
        finally:
            os.chdir(cwd)

    conaryState.write(os.path.join(dir, "CONARY"))

def renameFile(oldName, newName, repos=None):
    conaryState = ConaryStateFromFile("CONARY", repos=repos)
    sourceState = conaryState.getSourceState()

    if not os.path.exists(oldName):
        log.error("%s does not exist or is not a regular file" % oldName)
        return

    try:
        os.lstat(newName)
    except:
        pass
    else:
        log.error("%s already exists" % newName)
        return

    for (pathId, path, fileId, version) in sourceState.iterFileList():
        if path == oldName:
            os.rename(oldName, newName)
            sourceState.addFile(pathId, newName, version, fileId,
                        isConfig = sourceState.fileIsConfig(pathId),
                        isAutoSource = sourceState.fileIsAutoSource(pathId))
            conaryState.write("CONARY")
            return

    log.error("file %s is not under management" % oldName)

def showLog(repos, branch = None, newer = False):
    for message in iterLog(repos, branch = branch, newer = newer):
        print(message)

@api.developerApi
def iterLog(repos, branch = None, newer = False, dirName = '.'):
    '''
    Iterator that yeilds log message lines relative to the
    source checkout in the current directory.  If C{branch}
    is specified, that branch of the package is used instead
    as a source of log messages.
    Alternatively, if C{newer} is set, then only log messages
    newer than the current version in the source checkout
    are yeilded.
    The C{branch} and C{newer} keyword arguments may not both
    be set, because cross-branch comparisons do not have a
    concept of "newer".
    '''
    if branch and newer:
        raise errors.CvcError(
            'cannot specify --newer and a different branch together')
    dirName = os.path.abspath(dirName)
    state = ConaryStateFromFile(os.sep.join((dirName, "CONARY")),
                                repos).getSourceState()
    if not branch:
        branch = state.getBranch()
    else:
        if branch[0] != '/':
            log.error("branch name expected instead of %s" % branch)
            return
        branch = versions.VersionFromString(branch)

    troveName = state.getName()

    verList = repos.getTroveVersionsByBranch(
                            { troveName : { branch : None } } )
    if not verList:
        yield 'nothing has been committed'
        return

    verList = list(verList[troveName].keys())
    verList.sort()
    verList.reverse()
    if newer:
        newer= state.getVersion()
    l = []
    for version in verList:
        if newer and not version.isAfter(newer):
            break
        l.append((troveName, version, deps.deps.Flavor()))

    yield 'Name  : ' + troveName
    yield 'Branch: ' + branch.asString()
    yield ''

    troves = repos.getTroves(l)
    for logMessage in iterLogMessages(troves):
        yield logMessage

def iterLogMessages(troves):
    for trove in troves:
        v = trove.getVersion()
        cl = trove.getChangeLog()
        for line in formatOneLog(v, cl):
            yield line
        # separate log entries (RBLD-115)
        yield ''

def showOneLog(version, changeLog=''):
    print('\n'.join(formatOneLog(version, changeLog)))

def formatOneLog(version, changeLog=''):
    when = time.strftime("%c", time.localtime(version.timeStamps()[-1]))
    logMessage = []

    if version == versions.NewVersion():
        versionStr = "(working version)"
    else:
        versionStr = version.trailingRevision().asString()

    if changeLog.getName():
        logMessage.append("%s %s (%s) %s" %
            (versionStr, changeLog.getName(), changeLog.getContact(), when))
        lines = changeLog.getMessage().split("\n")
        if not lines[-1]:
            # remove trailing newline in log message (RBLD-115)
            del lines[-1]
        for l in lines:
            logMessage.append("    %s" % l)
    else:
        logMessage.append('%s %s (no log message)' %(versionStr, when))
    return logMessage

def setContext(cfg, contextName=None, ask=False, repos=None):
    def _ask(txt, *args):
        if len(args) == 0:
            default = defaultText = None
        elif len(args) == 1:
            defaultText = default = args[0]
        else:
            defaultText, default = args[0:2]

        while True:
            if defaultText:
                msg = "%s [%s]: " % (txt, defaultText)
            else:
                msg = "%s: " % txt
            answer = input(msg)
            if answer:
                return answer
            elif defaultText:
                return default

    if not contextName and not ask:
        cfg.displayContext()
        return

    if os.path.exists('CONARY'):
        state = ConaryStateFromFile('CONARY', repos)
    else:
        state = ConaryState()

    if not contextName:
        default = cfg.context
        contextName = _ask('Context name', default)

    context = cfg.getContext(contextName)

    if not ask:
        if not context:
            log.error("context %s does not exist", contextName)
            return
    elif context:
        log.error("context %s already exists", contextName)
        return
    else:
        # ask and not context
        print('* Creating new context %s' % contextName)
        context = cfg.setSection(contextName)
        conaryrc = _ask('File to store context definition in',
                        os.environ['HOME'] + '/.conaryrc')

        buildLabel = str(cfg.buildLabel)

        buildLabel = _ask('Build Label', buildLabel)
        context.configLine('buildLabel ' + buildLabel)

        installLabelPath = _ask('installLabelPath', buildLabel)
        context.configLine('installLabelPath ' + installLabelPath)

        flavor = _ask('installFlavor', 'use default', None)
        if flavor:
            context.configLine('flavor ' + flavor)

        name = _ask('contact name', 'use default (%s)' % cfg.name, None)
        if name:
            context.configLine('name ' + name)

        contact = _ask('contact info', 'use default (%s)' % cfg.contact,
                       None)

        if contact:
            context.configLine('contact ' + contact)

        f = open(conaryrc, 'a')
        f.write('\n\n[%s]\n' % contextName)
        f.write('# created by cvc context\n')
        context.display(f)

    state.setContext(contextName)
    state.write('CONARY')

def setFileFlags(repos, paths, text = False, binary = False):
    state = ConaryStateFromFile('CONARY', repos)
    sourceState = state.getSourceState()

    assert(not text or not binary)

    if text:
        isConfig = 1
    elif binary:
        isConfig = 0
    else:
        isConfig = None

    for path in paths:
        for (pathId, path, fileId, version) in sourceState.iterFileList():
            if path in paths:
                if isConfig is not None:
                    sourceState.fileIsConfig(pathId, set = isConfig)

    state.write('CONARY')

@api.developerApi
def refresh(repos, cfg, refreshPatterns=[], callback=None, dirName='.'):
    if not callback:
        callback = CheckinCallback()

    dirName = os.path.abspath(dirName)
    conaryState = ConaryStateFromFile(os.sep.join((dirName, 'CONARY')))
    state = conaryState.getSourceState()

    if len(refreshPatterns) == 1 and refreshPatterns[0] is None:
        refreshFilter = lambda x: True
    if len(refreshPatterns) == 0:
        refreshFilter = lambda x: True
    else:
        # User may pass wildcard ptterns
        patternFilter = _makeFilter(refreshPatterns)
        # Paths from the manifest must be exact filenames
        pathFilter = _makeFilter(state.getFileRefreshList(), exactPaths=True)
        refreshFilter = lambda x: patternFilter(x) or pathFilter(x)

    # if it's not being refreshed, don't download it
    skipPatterns = []
    for path in (x[1] for x in state.iterFileList()):
        if not refreshFilter(path):
            skipPatterns.append(path)

    skipFilter = _makeFilter(skipPatterns, exactPaths=True)

    troveName = state.getName()

    srcPkg = None
    if not isinstance(state.getVersion(), versions.NewVersion):
        srcPkg = repos.getTrove(troveName, state.getVersion(), deps.deps.Flavor())

    use.allowUnknownFlags(True)
    recipeClass = loadrecipe.RecipeLoaderFromSourceDirectory(state,
                                    cfg = cfg, repos = repos,
                                    branch = state.getBranch(),
                                    ignoreInstalled = True,
                                    parentDir = dirName,
                                    buildFlavor=cfg.buildFlavor).getRecipe()

    # fetch all the sources

    # setting the _trove to the last version of the source component
    # allows us to search that source component for files that are
    # not in the current directory or lookaside cache.
    recipeClass._trove = srcPkg

    # don't download sources for groups or filesets
    if not recipeClass.getType() in (recipe.RECIPE_TYPE_PACKAGE, recipe.RECIPE_TYPE_CAPSULE):
        raise errors.CvcError('Only package recipes can have files refreshed')

    lcache = lookaside.RepositoryCache(repos, refreshFilter, cfg=cfg)
    srcdirs = [ dirName,
                cfg.sourceSearchDir % {'pkgname': recipeClass.name} ]

    try:
        recipeObj = recipeClass(cfg, lcache, srcdirs, lightInstance=True)
    except builderrors.RecipeFileError as msg:
        raise errors.CvcError(msg)

    recipeObj.sourceVersion = state.getVersion()
    recipeObj.loadPolicy()
    level = log.getVerbosity()
    log.setVerbosity(log.INFO)
    if not 'abstractBaseClass' in recipeObj.__class__.__dict__ or not recipeObj.abstractBaseClass:
        if hasattr(recipeObj, 'setup'):
            cook._callSetup(cfg, recipeObj, recordCalls=False)
        else:
            raise errors.CvcError('Recipe requires setup() method')

    # setup must be called before lcache is populated
    recipeObj.populateLcache()

    # Get rid of the negative cache entries (CNY-3157)
    lcache.clearCacheDir(recipeObj.name, negative = True)
    try:
        recipeObj.fetchAllSources(refreshFilter = refreshFilter,
                                  skipFilter = skipFilter)
    except OSError as e:
        if e.errno == errno.ENOENT:
            raise errors.CvcError('Source file %s does not exist' %
                                  e.filename)
        else:
            raise errors.CvcError('Error accessing source file %s: %s' %
                                  (e.filename, e.strerror))

    log.setVerbosity(level)

    recipeFileName = state.getRecipeFileName()
    for (pathId, path, fileId, version) in state.iterFileList():
        if recipeFileName.endswith(path):
            continue
        if refreshFilter(path):
            if not state.fileIsAutoSource(pathId):
                log.warning('%s is not autosourced and cannot be refreshed' %
                            path)
                continue
            state.fileNeedsRefresh(pathId, True)

    conaryState.setSourceState(state)
    conaryState.write(os.sep.join((dirName, 'CONARY')))


@api.developerApi
def generateStatus(repos, dirName='.'):
    '''
    Create summary of changes regarding all files in a directory
    (the current directory by default) as a list of C{(I{status},
    I{filename})} tuples where C{I{status}} is a single character
    describing the status of the file C{I{filename}}:
     - C{?}: File not managed by Conary
     - C{A}: File added since last commit (or since package created if no commit)
     - C{M}: File modified since last commit
     - C{R}: File removed since last commit
    @rtype: list
    '''
    filtered = [ 'CONARY' ]
    # List of tuples (state, path)
    # state can be ?, A, M, R
    results = []

    dirNameLen = len(dirName)
    dirfiles = util.recurseDirectoryList(dirName, withDirs=False)
    dirfilesSet = set()
    for f in dirfiles:
        # Remove dirName/ prefix
        f = f[dirNameLen+1:]
        if f in filtered:
            # Special file
            continue
        dirfilesSet.add(f)

    state = ConaryStateFromFile(os.sep.join((dirName, "CONARY")),
                                repos).getSourceState()

    if state.getVersion() == versions.NewVersion():
        addedFilenames = set()
        for _, fileName, _, _ in state.iterFileList():
            dirfilesSet.discard(fileName)
            addedFilenames.add(fileName)
        results.extend([('?', x) for x in dirfilesSet])
        results.extend([('A', x) for x in addedFilenames])
        # Sort by file path
        results.sort(lambda x, y: cmp(x[1], y[1]))
        return results

    oldTrove = repos.getTrove(state.getName(), state.getVersion(), deps.deps.Flavor())

    result = update.buildLocalChanges(repos,
            [(state, oldTrove, versions.NewVersion(),
              update.UpdateFlags(ignoreUGids = True) )],
            forceSha1=True, ignoreAutoSource = True, root = dirName)
    result = localAutoSourceChanges(oldTrove, result)

    (changeSet, ((isDifferent, newState),)) = result

    troveChanges = changeSet.iterNewTroveList()
    troveCs = next(troveChanges)
    assert(util.assertIteratorAtEnd(troveChanges))

    fileList = [ (x[0], x[1], True, x[2], x[3]) for x in
troveCs.getNewFileList() ]
    fileList += [ (x[0], x[1], False, x[2], x[3]) for x in
                            troveCs.getChangedFileList() ]

    for (pathId, path, isNew, fileId, newVersion) in fileList:
        # autosource files aren't in this set, so discard not remove
        dirfilesSet.discard(path)

        if isNew:
            results.append(('A', path))
            continue

        # changed file
        if not path:
            path = oldTrove.getFile(pathId)[0]
        results.append(('M', path))
        continue

    for pathId in troveCs.getOldFileList():
        path = oldTrove.getFile(pathId)[0]
        results.append(('R', path))

    trackedFiles = {}
    # Add all the tracked files to a hash
    for iterr in state.iterFileList():
        trackedFiles[iterr[1]] = None

    # Eliminate the files that have not changed (the ones we track but are
    # still present in dirfilesSet)
    for k in set(dirfilesSet):
        if k in trackedFiles:
            dirfilesSet.discard(k)

    unknown = [ ('?', path) for path in dirfilesSet ]
    results[0:0] = unknown

    # Sort by file path
    results.sort(lambda x, y: cmp(x[1], y[1]))

    return results

def _showStat(results):
    'print out status lists as returned by C{generateStatus}'
    for fstat, path in results:
        print("%s  %s" % (fstat, path))
    return results

def stat_(repos):
    return _showStat(generateStatus(repos))


def localAutoSourceChanges(oldTrove, xxx_todo_changeme):
    # look for autosource files which have changed from upstream; we don't
    # use buildLocalChanges to do this because we don't want to download
    # autosource'd files which haven't changed; a side affect is that
    # changing, adding, or removing a url in a recipe won't show up here as a
    # change to an autosource files; only changes due to refresh will be
    # noticed
    (changeSet, ((isDifferent, newState),)) = xxx_todo_changeme
    for (pathId, path, fileId, version) in newState.iterFileList():
        if not newState.fileNeedsRefresh(pathId): continue
        assert(newState.fileIsAutoSource(pathId))
        assert(not newState.fileIsConfig(pathId))

        # we don't need the real change; we just make one up
        newState.updateFile(pathId, None, newState.getVersion(), '0' * 20)
        isDifferent = True

    d = newState.diff(oldTrove)[0]
    changeSet.newTrove(d)

    return (changeSet, ((isDifferent, newState),))

def factory(newFactory = None, targetDir=None):
    if targetDir:
        stateFile = os.path.join(targetDir, "CONARY")
    else:
        stateFile = "CONARY"
    conaryState = ConaryStateFromFile(stateFile)
    state = conaryState.getSourceState()

    if newFactory is None:
        if state.getFactory():
            print(state.getFactory())
        else:
            print("(none)")
    else:
        state.setFactory(newFactory)
        conaryState.write(stateFile)
