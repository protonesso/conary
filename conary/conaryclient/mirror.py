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

import copy
import itertools
import optparse
import os
import queue
import sys
import threading
import time
import traceback

from conary.conaryclient import callbacks as clientCallbacks
from conary.conaryclient import cmdline
from conary import conarycfg, callbacks, trove
from conary.lib import cfg, util, log
from conary.repository import errors, changeset, netclient
from conary.deps.deps import parseFlavor

class OptionError(Exception):
    def __init__(self, errcode, errmsg, *args):
        self.errcode = errcode
        self.errmsg = errmsg
        Exception.__init__(self, *args)

def parseArgs(argv):
    parser = optparse.OptionParser(version = '%prog 0.1')
    parser.add_option("--config-file", dest = "configFile",
                      help = "configuration file", metavar = "FILE")
    parser.add_option("--full-sig-sync", dest = "infoSync",
                      action = "store_true", default = False,
                      help = "deprecated: alias to --full-info-sync")
    parser.add_option("--full-info-sync", dest = "infoSync",
                      action = "store_true", default = False,
                      help = "replace all the trove signatures and metadata "
                      "in the target repository")
    parser.add_option("--fast-sync", dest = "fastSync",
                      action = "store_true", default = False,
                      help = "skip checking/mirroring of changed info records "
                             "for already mirrored troves")
    parser.add_option("--absolute", dest = "absolute",
                      action = "store_true", default = False,
                      help = "use only absolute changesets when mirroring content")
    parser.add_option("--full-trove-sync", dest = "sync", action = "store_true",
                      default = False,
                      help = "ignore the last-mirrored timestamp in the "
                             "target repository")
    parser.add_option("--check-sync", dest = "checkSync", action = "store_true",
                      default = False,
                      help = "only check if the source and target(s) are in sync")
    parser.add_option("--test", dest = "test", action = "store_true",
                      default = False,
                      help = "skip commiting changes to the target repository")
    parser.add_option("-v", "--verbose", dest = "verbose",
                      action = "store_true", default = False,
                      help = "display information on what is going on")

    (options, args) = parser.parse_args(argv)

    if options.configFile is None:
        raise OptionError(1, 'a mirror configuration must be provided')
    elif args:
        raise OptionError(1, 'unexpected arguments: %s' % " ".join(args))

    return options


class VerboseChangesetCallback(clientCallbacks.ChangesetCallback):

    def done(self):
        self.clearPrefix()
        self._message('\r')


class ChangesetCallback(callbacks.ChangesetCallback):

    def setPrefix(self, *args):
        pass
    def clearPrefix(self):
        pass


class MirrorConfigurationSection(cfg.ConfigSection):
    repositoryMap         =  conarycfg.CfgRepoMap
    user                  =  conarycfg.CfgUserInfo
    entitlement           =  conarycfg.CfgEntitlement


class MirrorFileConfiguration(cfg.SectionedConfigFile):
    host = cfg.CfgString
    entitlementDirectory = cfg.CfgPath
    labels = conarycfg.CfgInstallLabelPath
    matchTroves = cfg.CfgSignedRegExpList
    matchTroveSpecs = cfg.CfgSignedRegExpList

    recurseGroups = (cfg.CfgBool, False)
    uploadRateLimit = (conarycfg.CfgInt, 0,
            "Upload rate limit, in bytes per second")
    downloadRateLimit = (conarycfg.CfgInt, 0,
            "Download rate limit, in bytes per second")
    lockFile = cfg.CfgString
    useHiddenCommits = (cfg.CfgBool, True)
    absoluteChangesets = (cfg.CfgBool, False)
    includeSources = (cfg.CfgBool, False)
    splitNodes = (cfg.CfgBool, False,
            "Split jobs that would commit two versions of a trove at once. "
            "Needed for compatibility with older repositories.")
    noPGP = (cfg.CfgBool, False)

    _allowNewSections = True
    _defaultSectionType = MirrorConfigurationSection


# some sanity checks for the mirror configuration
def checkConfig(cfg):
    if not cfg.host:
        log.error("ERROR: cfg.host is not defined")
        raise RuntimeError("cfg.host is not defined")
    # make sure that each label belongs to the host we're mirroring
    for label in cfg.labels:
        if label.getHost() != cfg.host:
            log.error("ERROR: label %s is not on host %s", label, cfg.host)
            raise RuntimeError("label %s is not on host %s", label, cfg.host)


def _getMirrorClient(mirrorCfg, section):
    section = mirrorCfg.getSection(section)
    cfg = conarycfg.ConaryConfiguration(False)
    for name in ['repositoryMap', 'user', 'entitlement']:
        cfg[name] = section[name]
    for name in ['uploadRateLimit', 'downloadRateLimit', 'entitlementDirectory']:
        cfg[name] = mirrorCfg[name]
    return netclient.NetworkRepositoryClient(cfg=cfg)


def mainWorkflow(cfg = None, callback=ChangesetCallback(),
                 test=False, sync=False, infoSync=False,
                 checkSync=False, fastSync=False):
    import fcntl
    if cfg.lockFile:
        try:
            log.debug('checking for lock file')
            lock = open(cfg.lockFile, 'w')
            fcntl.lockf(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except IOError:
            log.warning('lock held by another process, exiting')
            return

    # need to make sure we have a 'source' section
    if not cfg.hasSection('source'):
        log.debug("ERROR: mirror configuration file is missing a [source] section")
        raise RuntimeError("Mirror configuration file is missing a [source] section")
    sourceRepos = _getMirrorClient(cfg, 'source')

    # Optional reference repository
    if cfg.hasSection('reference'):
        refRepos = _getMirrorClient(cfg, 'reference')
    else:
        refRepos = sourceRepos

    # we need to build a target repo client for each of the "target*"
    # sections in the config file
    targets = []
    for name in cfg.iterSectionNames():
        if not name.startswith("target"):
            continue
        target = _getMirrorClient(cfg, name)
        target = TargetRepository(target, cfg, name, test=test)
        targets.append(target)
    # checkSync is a special operation...
    if checkSync:
        return checkSyncRepos(cfg, refRepos, targets)
    # we pass in the sync flag only the first time around, because after
    # that we need the targetRepos mark to advance accordingly after being
    # reset to -1
    callAgain = mirrorRepository(sourceRepos, targets, cfg,
                                 test = test, sync = sync,
                                 syncSigs = infoSync,
                                 callback = callback,
                                 fastSync = fastSync,
                                 referenceRepos=refRepos,
                                 )
    while callAgain:
        callAgain = mirrorRepository(sourceRepos, targets, cfg,
                                     test = test, callback = callback,
                                     fastSync = fastSync,
                                     referenceRepos=refRepos,
                                     )


def Main(argv=None):
    if argv is None:
        argv = argv=sys.argv[1:]
    try:
        options = parseArgs(argv)
    except OptionError as e:
        sys.stderr.write(e.errmsg)
        sys.stderr.write("\n")
        return e.errcode

    cfg = MirrorFileConfiguration()
    cfg.read(options.configFile, exception = True)
    callback = ChangesetCallback()
    if options.absolute:
        cfg.absoluteChangesets = True
    if options.verbose:
        log.setVerbosity(log.DEBUG)
        callback = VerboseChangesetCallback()
    if options.fastSync: # make --fast-sync imply --full-trove-sync
        options.sync = True
    try:
        mainWorkflow(cfg, callback, options.test,
                 sync = options.sync, infoSync = options.infoSync,
                 fastSync = options.fastSync, checkSync = options.checkSync)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        print('Terminating due to user interrupt', file=sys.stderr)
        sys.exit(1)


def groupTroves(troveList):
    # combine the troves into indisolvable groups based on their version and
    # flavor; it's assumed that adjacent troves with the same version/flavor
    # must be in a single commit
    grouping = {}
    for info in troveList:
        (n, v, f) = info[1]
        crtGrp = grouping.setdefault((v,f), [])
        crtGrp.append(info)
    grouping = list(grouping.values())
    # make sure the groups are sorted in ascending order of their mark
    def _groupsort(a, b):
        ret = cmp(a[0][0], b[0][0])
        if ret:
            return ret
        # if they have the same mark, sort the groups at the end
        ahasgrp = [x[1][1] for x in a if trove.troveIsGroup(x[1][0])]
        bhasgrp = [x[1][1] for x in b if trove.troveIsGroup(x[1][0])]
        if len(ahasgrp) > len(bhasgrp):
            return 1
        if len(bhasgrp) > len(ahasgrp):
            return -1
        return cmp(ahasgrp, bhasgrp)
    grouping.sort(_groupsort)
    return grouping

def buildJobList(src, target, groupList, absolute=False, splitNodes=True,
        jobSize=20):
    # Match each trove with something we already have; this is to mirror
    # using relative changesets, which is a lot more efficient than using
    # absolute ones.
    q = {}
    srcAvailable = {}
    for group in groupList:
        for mark, (name, version, flavor) in group:
            # force groups to always be transferred using absolute changesets
            if trove.troveIsGroup(name):
                continue
            srcAvailable[(name,version,flavor)] = True
            d = q.setdefault(name, {})
            l = d.setdefault(version.branch(), [])
            l.append(flavor)

    # check that the latestavailable versions from the target are
    # present on the source to be able to use relative changesets
    latestAvailable = {}
    if len(q):
        latestAvailable = target.getTroveLeavesByBranch(q)
        latestAvailable = dict(
                    (name, dict(
                        (version, set(flavors))
                        for (version, flavors) in versions.items()
                    )) for (name, versions) in latestAvailable.items())
    if len(latestAvailable):
        def _tol(d):
            for n, vd in d.items():
                for v, fl in vd.items():
                    for f in fl:
                        yield (n,v,f)
        ret = src.hasTroves(list(_tol(latestAvailable)), hidden=True)
        srcAvailable.update(ret)

    def _split():
        # Stop adding troves to this job and allow its troves to be used for
        # the next job's relative changesets.
        for mark, job in jobList[-1]:
            name = job[0]
            if trove.troveIsGroup(name):
                continue
            oldVersion, oldFlavor = job[1]
            newVersion, newFlavor = job[2]

            srcAvailable[(name, newVersion, newFlavor)] = True
            d = latestAvailable.setdefault(name, {})

            if oldVersion in d and oldVersion.branch() == newVersion.branch():
                # If the old version is on the same branch as the new one,
                # replace the old with the new. If it's on a different
                # branch, we'll track both.
                flavorList = d[oldVersion]
                flavorList.discard(oldFlavor)
                if not flavorList:
                    del d[oldVersion]

            flavorList = d.setdefault(newVersion, set())
            flavorList.add(newFlavor)
        if jobList[-1]:
            jobList.append([])

    # we'll keep latestAvailable in sync with what the target will look like
    # as the mirror progresses
    jobList = [[]]
    currentNodes = set()
    currentHost = None
    for group in groupList:
        # for each job find what it's relative to and build up a job list
        thisJob = []
        for mark, (name, version, flavor) in group:
            # name, version, versionDistance, flavorScore
            currentMatch = (None, None, None, None)
            if absolute or name not in latestAvailable:
                job = (name, (None, None), (version, flavor), True)
            else:
                d = latestAvailable[name]
                for repVersion, flavorList in d.items():
                    # the versions have to be on the same host to be
                    # able to generate relative changesets
                    if version.getHost() != repVersion.getHost():
                        continue
                    for repFlavor in flavorList:
                        if not srcAvailable.get((name, repVersion, repFlavor), False):
                            continue
                        score = flavor.score(repFlavor)
                        if score is False:
                            continue
                        if repVersion == version:
                            closeness = 100000
                        else:
                            closeness = version.closeness(repVersion)
                        if score < currentMatch[3]:
                            continue
                        elif score > currentMatch[3]:
                            currentMatch = (repVersion, repFlavor, closeness,
                                            score)
                        elif closeness < currentMatch[2]:
                            continue
                        else:
                            currentMatch = (repVersion, repFlavor, closeness,
                                            score)

                job = (name, (currentMatch[0], currentMatch[1]),
                              (version, flavor), currentMatch[0] is None)

            thisJob.append((mark, job))

        newNodes = set((x[1][0], x[1][2][0].branch()) for x in thisJob)
        newHosts = set(x[1][2][0].getHost() for x in thisJob)
        assert len(newHosts) == 1
        newHost = list(newHosts)[0]
        if (len(jobList[-1]) >= jobSize
                # Can't commit two versions of the same trove
                or (splitNodes and newNodes & currentNodes)
                # Can't commit troves on different hosts
                or currentHost not in (None, newHost)
                ):
            _split()
            currentNodes = set()
        jobList[-1].extend(thisJob)
        currentNodes.update(newNodes)
        currentHost = newHost

    if not jobList[-1]:
        jobList.pop()
    return jobList


recursedGroups = set()
def recurseTrove(sourceRepos, name, version, flavor,
                 callback = ChangesetCallback()):
    global recursedGroups
    assert(trove.troveIsGroup(name))
    # there's nothing much we can recurse from the source
    if name.endswith(":source"):
        return []
    # avoid grabbing the same group multiple times
    if (name, version, flavor) in recursedGroups:
        return []
    log.debug("recursing group trove: %s=%s[%s]" % (name, version, flavor))
    groupCs = sourceRepos.createChangeSet(
        [(name, (None, None), (version, flavor), True)],
        withFiles=False, withFileContents=False, recurse=False,
        callback = callback)
    recursedGroups.add((name, version, flavor))
    ret = []
    for troveCs in groupCs.iterNewTroveList():
        for name, ops in troveCs.iterChangedTroves(True, True):
            for oper, version, flavor, byDefault in ops:
                if oper != '-':
                    ret.append((name, version, flavor))
    return ret



def _toBraces(items):
    if len(items) > 1:
        return '{%s}' % (','.join(sorted(items)))
    else:
        return list(items)[0]


def formatTroveNames(names):
    """Group trove names by package and format them like a shell glob."""
    # Group names by package
    packages = {}
    for name in names:
        if ':' in name:
            package, component = name.split(':')
            component = ':' + component
        else:
            package, component = name, ''
        packages.setdefault(package, []).append(component)
    # If all the component sets are the same, collapse them.
    componentSets = set(tuple(x) for x in list(packages.values()))
    if len(componentSets) == 1:
        components = list(componentSets)[0]
        if len(components) > 1:
            prefix = _toBraces(packages)
            suffix = _toBraces(components)
            return prefix + suffix
    # Format the components for each package
    nameList = []
    for package, components in sorted(packages.items()):
        if len(components) == 1:
            # foo or foo:bar
            formatted = package + components[0]
        else:
            # foo and foo:bar
            components.sort()
            formatted = package + _toBraces(components)
        nameList.append(formatted)
    # Combine into one big set
    if len(nameList) == 1:
        return nameList[0]
    else:
        nameList.sort()
        return _toBraces(nameList)


def displayBundle(bundle):
    """Format a job bundle for display"""
    minMark = min([x[0] for x in bundle])
    # Group by version and flavor
    trovesByVF = {}
    for mark, (name, oldVF, newVF, absolute) in bundle:
        trovesByVF.setdefault((oldVF, newVF), set()).add(name)
    # Within each VF set, sort and fold the names and format for display.
    lines = []
    for (oldVF, newVF), names in list(trovesByVF.items()):
        allNames = formatTroveNames(names)
        # Add version and flavor info
        if oldVF[0]:
            if oldVF[1] != newVF[1]:
                oldInfo = '%s[%s]--' % oldVF
            else:
                oldInfo = '%s--' % (oldVF[0],)
        else:
            oldInfo = ''
        newInfo = '%s[%s]' % newVF
        lines.append(''.join((allNames, '=', oldInfo, newInfo)))
    lines.sort()
    lines.insert(0, '')
    lines.append('New mark: %.0f' % (minMark,))
    return "\n  ".join(lines)


# wrapper for displaying a simple jobList
def displayJobList(jobList):
    return displayBundle([(0, x) for x in jobList])

# mirroring stuff when we are running into PathIdConflict errors
def splitJobList(jobList, src, targetSet, hidden = False, callback = ChangesetCallback()):
    log.debug("Changeset Key conflict detected; splitting job further...")
    jobs = {}
    for job in jobList:
        name = job[0]
        if ':' in name:
            name = name.split(':')[0]
        l = jobs.setdefault(name, [])
        l.append(job)
    i = 0
    for smallJobList in jobs.values():
        (outFd, tmpName) = util.mkstemp()
        os.close(outFd)
        log.debug("jobsplit %d of %d %s" % (
            i + 1, len(jobs), displayBundle([(0,x) for x in smallJobList])))
        src.createChangeSetFile(smallJobList, tmpName, recurse = False,
                                callback = callback, mirrorMode = True)
        _parallel(targetSet, TargetRepository.commitChangeSetFile,
                tmpName, hidden=hidden, callback=callback)
        os.unlink(tmpName)
        callback.done()
        i += 1
    return


# filter a trove tuple based on cfg
def _filterTup(troveTup, cfg):
    (n, v, f) = troveTup
    troveSpec = cmdline.toTroveSpec(n, str(v), f)
    # filter by trovespec 
    if cfg.matchTroveSpecs and cfg.matchTroveSpecs.match(troveSpec) <= 0:
        return False
    # if we're matching troves
    if cfg.matchTroves and cfg.matchTroves.match(n) <= 0:
        return False
    # filter by host/label
    if v.getHost() != cfg.host:
        return False
    if cfg.labels and v.branch().label() not in cfg.labels:
        return False
    return True


# get all the trove info to be synced
def _getAllInfo(src, cfg):
    log.debug("resync all trove info from source. This will take a while...")
    # grab the full list of all the trove versions and flavors in the src
    troveDict = src.getTroveVersionList(cfg.host, { None : None })
    troveList = []
    # filter out the stuff we don't need
    for name, versionD in troveDict.items():
        for version, flavorList in versionD.items():
            for flavor in flavorList:
                tup = (name, version, flavor)
                troveList.append(tup)
    del troveDict
    # retrieve the sigs and the metadata records to sync over
    sigList = src.getTroveSigs(troveList)
    metaList = src.getTroveInfo(trove._TROVEINFO_TAG_METADATA, troveList)
    infoList = []
    for t, s, ti in zip(troveList, sigList, metaList):
        if ti is None:
            ti = trove.TroveInfo()
        ti.sigs.thaw(s)
        infoList.append((t, ti))
    return infoList

# while talking to older repos - get the new trove sigs
def _getNewSigs(src, cfg, mark):
    # talking to an old source server. We do the best and we get the sigs out
    sigList = src.getNewSigList(cfg.host, str(mark))
    log.debug("obtained %d changed trove sigs", len(sigList))
    sigList = [ x for x in sigList if _filterTup(x[1], cfg) ]
    log.debug("%d changed sigs after label and match filtering", len(sigList))
    # protection against duplicate items returned in the list by some servers
    sigList = list(set(sigList))
    sigList.sort(lambda a,b: cmp(a[0], b[0]))
    log.debug("downloading %d signatures from source repository", len(sigList))
    # XXX: we could also get the metadata in here, but getTroveInfo
    # would use a getChangeSet call against older repos, severely
    # impacting performance
    sigs = src.getTroveSigs([ x[1] for x in sigList ])
    # need to convert the sigs into TroveInfo instances
    def _sig2info(sig):
        ti = trove.TroveInfo()
        ti.sigs.thaw(sig)
        return ti
    sigs = [ _sig2info(s) for s in sigs]
    # we're gonna iterate repeatedely over the returned set, no itertools can do
    return [(m, t, ti) for (m,t),ti in zip(sigList, sigs) ]

# get the changed trove info entries for the troves comitted
def _getNewInfo(src, cfg, mark):
    # first, try the new getNewTroveInfo call
    labels = cfg.labels or []
    mark = str(int(mark)) # xmlrpc chokes on longs
    infoTypes = [trove._TROVEINFO_TAG_SIGS, trove._TROVEINFO_TAG_METADATA]
    try:
        infoList = src.getNewTroveInfo(cfg.host, mark, infoTypes, labels)
    except errors.InvalidServerVersion:
        # otherwise we mirror just the sigs...
        infoList = _getNewSigs(src, cfg, mark)
    return infoList


def _parallel_run(index, results, targets, classMethod, args, kwargs):
    try:
        target = targets[index]
        ret = (index, True, classMethod(target, *args, **kwargs))
    except Exception as err:
        ret = (index, False, (err, traceback.format_exc()))
    results.put(ret)


def _parallel(targets, classMethod, *args, **kwargs):
    """
    Map a method call across multiple targets concurrently
    """
    if len(targets) == 1:
        return [classMethod(targets[0], *args, **kwargs)]
    results = queue.Queue()
    threads = []
    for index in range(len(targets)):
        thread = threading.Thread(target=_parallel_run,
                args=(index, results, targets, classMethod, args, kwargs,))
        thread.start()
        threads.append(thread)

    for thread in threads:
        thread.join()
    ret = [None] * len(targets)
    last_error = None
    for thread in threads:
        index, ok, result = results.get()
        if ok:
            ret[index] = result
        else:
            last_error, trace = result
            log.error("Error updating target %s:\n%s",
                    targets[index].name, trace)
    if last_error is not None:
        raise last_error
    return ret


# mirror new trove info for troves we have already mirrored.
def mirrorTroveInfo(src, targets, mark, cfg, resync=False):
    if resync:
        log.debug("performing a full trove info sync")
        infoList = _getAllInfo(src, cfg)
        infoList = [(mark, t, ti) for t, ti in infoList ]
    else:
        log.debug("getting new trove info entries")
        infoList = _getNewInfo(src, cfg, mark)
    log.debug("obtained %d trove info records for mirroring", len(infoList))
    infoList = [(m,t,ti) for (m,t,ti) in infoList if _filterTup(t, cfg)]
    if not len(infoList):
        log.debug("no troveinfo records need to be mirrored")
        return 0
    log.debug("mirroring %d changed trove info records" % len(infoList))
    updateCount = sum(_parallel(targets,
        TargetRepository.setTroveInfo, infoList))
    return updateCount

# this mirrors all the troves marked as removed from the sourceRepos into the targetRepos
def mirrorRemoved(sourceRepos, targetRepos, troveSet, test = False, callback = ChangesetCallback()):
    if not troveSet:
        return 0
    log.debug("checking on %d removed troves", len(troveSet))
    # these removed troves better exist on the target
    present = targetRepos.hasTroves(list(troveSet))
    missing = [ x for x in troveSet if not present[x] ]
    # we can not have any "missing" troves while we mirror removals
    for t in missing:
        log.warning("Mirroring removed trove: valid trove not found on target: %s", t)
        troveSet.remove(t)
    # for the remaining removed troves, are any of them already mirrored?
    jobList = [ (name, (None, None), (version, flavor), True) for
                (name, version, flavor) in troveSet ]
    cs = targetRepos.createChangeSet(jobList, recurse=False, withFiles=False,
                                     withFileContents=False, callback=callback)
    for trvCs in cs.iterNewTroveList():
        if trvCs.getType() == trove.TROVE_TYPE_REMOVED:
            troveSet.remove(trvCs.getNewNameVersionFlavor())
    log.debug("mirroring %d removed troves", len(troveSet))
    if not troveSet:
        return 0
    jobList = [ (name, (None, None), (version, flavor), True) for
                (name, version, flavor) in troveSet ]
    log.debug("mirroring removed troves %s" % (displayJobList(jobList),))
    # grab the removed troves changeset
    cs = sourceRepos.createChangeSet(jobList, recurse = False,
                                     withFiles = False, withFileContents = False,
                                     callback = callback)
    log.debug("committing")
    targetRepos.commitChangeSet(cs, mirror = True, callback = callback)
    callback.done()
    return len(jobList)

# target repo class that helps dealing with testing mode
class TargetRepository:
    def __init__(self, repo, cfg, name = 'target', test=False):
        self.repo = repo
        self.test = test
        self.cfg = cfg
        self.mark = None
        self.name = name
        self.__gpg = {}
    def getMirrorMark(self):
        if self.mark is None:
            self.mark = self.repo.getMirrorMark(self.cfg.host)
        self.mark = str(int(self.mark))
        return int(self.mark)
    def setMirrorMark(self, mark):
        self.mark = str(int(mark))
        log.debug("%s setting mirror mark to %s", self.name, self.mark)
        if self.test:
            return
        self.repo.setMirrorMark(self.cfg.host, self.mark)
    def mirrorGPG(self, src, host):
        if self.cfg.noPGP:
            return
        if host in self.__gpg:
            return
        keyList = src.getNewPGPKeys(host, -1)
        self.__gpg[host] = keyList
        if not len(keyList):
            return
        log.debug("%s adding %d gpg keys", self.name, len(keyList))
        if self.test:
            return
        self.repo.addPGPKeyList(self.cfg.host, keyList)
    def setTroveInfo(self, infoList):
        log.debug("%s checking what troveinfo needs to be mirrored", self.name)
        # Items whose mark is the same as currentMark might not have their trove
        # available on the server (it might be coming as part of this mirror
        # run).
        inQuestion = [ x[1] for x in infoList if str(int(x[0])) >= self.mark ]
        present = self.repo.hasTroves(inQuestion, hidden=True)
        # filter out the not present troves which will get mirrored in
        # the current mirror run
        infoList = [ (t, ti) for (m, t, ti) in infoList if present.get(t, True) ]
        # avoid busy work for troveinfos which are empty
        infoList = [ (t, ti) for (t, ti) in infoList if len(ti.freeze()) > 0 ]
        if self.test:
            return 0
        try:
            self.repo.setTroveInfo(infoList)
        except errors.InvalidServerVersion: # to older servers we can only transport sigs
            infoList = [ (t, ti.sigs.freeze()) for t, ti in infoList ]
            # only send up the troves that actually have a signature change
            infoList = [ x for x in infoList if len(x[1]) > 0 ]
            log.debug("%s pushing %d trove sigs...", self.name, len(infoList))
            self.repo.setTroveSigs(infoList)
        else:
            log.debug("%s uploaded %d info records", self.name, len(infoList))
        return len(infoList)

    def addTroveList(self, tl):
        # Filter out troves which are already in the local repository. Since
        # the marks aren't distinct (they increase, but not monotonically), it's
        # possible that something new got committed with the same mark we
        # last updated to, so we have to look again at all of the troves in the
        # source repository with the last mark which made it into our target.
        present = self.repo.hasTroves([ x[1] for x in tl ], hidden = True)
        ret = [ x for x in tl if not present[x[1]] ]
        log.debug("%s found %d troves not present", self.name, len(ret))
        return ret
    def commitChangeSetFile(self, filename, hidden, callback):
        if self.test:
            return 0
        callback = copy.copy(callback)
        callback.setPrefix(self.name + ": ")
        t1 = time.time()
        ret = self.repo.commitChangeSetFile(filename, mirror=True, hidden=hidden,
                                            callback=callback)
        t2 = time.time()
        callback.done()
        hstr = ""
        if hidden: hstr = "hidden "
        log.debug("%s %scommit (%.2f sec)", self.name, hstr, t2-t1)
        return ret
    def presentHiddenTroves(self, newMark):
        log.debug("%s unhiding comitted troves", self.name)
        self.repo.presentHiddenTroves(self.cfg.host)
        self.setMirrorMark(newMark)

# split a troveList in changeset jobs
def buildBundles(sourceRepos, target, troveList, absolute=False,
        splitNodes=True):
    bundles = []
    log.debug("grouping %d troves based on version and flavor", len(troveList))
    groupList = groupTroves(troveList)
    log.debug("building grouped job list")
    bundles = buildJobList(sourceRepos, target.repo, groupList, absolute,
            splitNodes)
    return bundles

# return the new list of troves to process after filtering and sanity checks
def getTroveList(src, cfg, mark):
    # FIXME: getNewTroveList should accept and only return troves on
    # the labels we're interested in
    log.debug("looking for new troves")
    # make sure we always treat the mark as an integer
    troveList = [(int(m), (n,v,f), t) for m, (n,v,f), t in
                  src.getNewTroveList(cfg.host, str(mark))]
    if not len(troveList):
        # this should be the end - no more troves to look at
        log.debug("no new troves found")
        return (mark, [])
    # we need to protect ourselves from duplicate items in the troveList
    l = len(troveList)
    troveList = list(set(troveList))
    if len(troveList) < l:
        l = len(troveList)
        log.debug("after duplicate elimination %d troves are left", len(troveList))
    # if we filter out the entire list of troves we have been
    # returned, we need to tell the caller what was the highest mark
    # we had so it can continue asking for more
    maxMark = max([x[0] for x in troveList])
    # filter out troves on labels and parse through matchTroves
    troveList = [ x for x in troveList if _filterTup(x[1],cfg) ]
    if len(troveList) < l:
        l = len(troveList)
        log.debug("after label filtering and matchTroves %d troves are left", l)
        if not troveList:
            return (maxMark, [])
    # sort deterministically by mark, version, flavor, reverse name
    troveList.sort(lambda a,b: cmp(a[0], b[0]) or
                   cmp(a[1][1], b[1][1]) or
                   cmp(a[1][2], b[1][2]) or
                   cmp(b[1][0], a[1][0]) )
    log.debug("%d new troves returned", len(troveList))
    # We cut off the last troves that have the same flavor, version to
    # avoid committing an incomplete trove. This could happen if the
    # server side only listed some of a trove's components due to
    # server side limits on how many results it can return on each query
    lastIdx = len(troveList)-1
    # compare with the last one
    ml, (nl,vl,fl), tl = troveList[-1]
    while lastIdx >= 0:
        lastIdx -= 1
        m, (n,v,f), t = troveList[lastIdx]
        if v == vl and f == fl:
            continue
        lastIdx += 1
        break
    # the min mark of the troves we skip has to be higher than max
    # mark of troves we'll commit or otherwise we'll skip them for good...
    if lastIdx >= 0:
        firstMark = max([x[0] for x in troveList[:lastIdx]])
        lastMark = min([x[0] for x in troveList[lastIdx:]])
        if lastMark > firstMark:
            troveList = troveList[:lastIdx]
            log.debug("reduced new trove list to %d to avoid partial commits", len(troveList))
    # since we're returning at least on trove, the caller will make the next mark decision
    return (mark, troveList)

def _makeTargets(cfg, targetRepos, test = False):
    if not hasattr(targetRepos, '__iter__'):
        targetRepos = [ targetRepos ]
    targets = []
    for t in targetRepos:
        if isinstance(t, netclient.NetworkRepositoryClient):
            targets.append(TargetRepository(t, cfg, test=test))
        elif isinstance(t, TargetRepository):
            targets.append(t)
        else:
            raise RuntimeError("Can not handle unknown target repository type", t)
    return targets

# syncSigs really means "resync all info", but we keep the parameter
# name for compatibility reasons
def mirrorRepository(sourceRepos, targetRepos, cfg,
                     test = False, sync = False, syncSigs = False,
                     callback = ChangesetCallback(),
                     fastSync = False,
                     referenceRepos=None,
                     ):
    if referenceRepos is None:
        referenceRepos = sourceRepos
    checkConfig(cfg)
    targets = _makeTargets(cfg, targetRepos, test)
    log.debug("-" * 20 + " start loop " + "-" * 20)

    hidden = len(targets) > 1 or cfg.useHiddenCommits
    if hidden:
        log.debug("will use hidden commits to synchronize target mirrors")

    marks = _parallel(targets, TargetRepository.getMirrorMark)
    if sync:
        currentMark = -1
    else:
        # we use the oldest mark as a starting point (since we have to
        # get stuff from source for that oldest one anyway)
        currentMark = min(marks)
    log.debug("using common mirror mark %s", currentMark)
    # reset mirror mark to the lowest common denominator
    for t, mark in zip(targets, marks):
        if mark != currentMark:
            t.setMirrorMark(currentMark)
    # mirror gpg signatures from the src into the targets
    _parallel(targets, TargetRepository.mirrorGPG, referenceRepos, cfg.host)
    # mirror changed trove information for troves already mirrored
    if fastSync:
        updateCount = 0
        log.debug("skip trove info records sync because of fast-sync")
    else:
        updateCount = mirrorTroveInfo(referenceRepos, targets, currentMark,
                cfg, syncSigs)
    newMark, troveList = getTroveList(referenceRepos, cfg, currentMark)
    if not troveList:
        if newMark > currentMark: # something was returned, but filtered out
            _parallel(targets, TargetRepository.setMirrorMark, newMark)
            return -1 # call again
        return 0
    # prepare a new max mark to be used when we need to break out of a loop
    crtMaxMark = max(int(x[0]) for x in troveList)
    if currentMark > 0 and crtMaxMark == currentMark:
        # if we're hung on the current max then we need to
        # forcibly advance the mark in case we're stuck
        crtMaxMark += 1 # only used if we filter out all troves below
    initTLlen = len(troveList)

    # removed troves are a special blend - we keep them separate
    removedSet  = set([ x[1] for x in troveList if x[2] == trove.TROVE_TYPE_REMOVED ])
    troveList = [ (x[0], x[1]) for x in troveList if x[2] != trove.TROVE_TYPE_REMOVED ]

    # figure out if we need to recurse the group-troves
    if cfg.recurseGroups:
        # avoid adding duplicates
        troveSetList = set([x[1] for x in troveList])
        for mark, (name, version, flavor) in troveList:
            if trove.troveIsGroup(name):
                recTroves = recurseTrove(referenceRepos, name,
                        version, flavor, callback=callback)

                # add sources here:
                if cfg.includeSources:
                    troveInfo = referenceRepos.getTroveInfo(
                        trove._TROVEINFO_TAG_SOURCENAME, recTroves)
                    sourceComps = set()
                    for nvf, source in zip(recTroves, troveInfo):
                        sourceComps.add((source(), nvf[1].getSourceVersion(),
                                         parseFlavor('')))
                    recTroves.extend(sourceComps)

                # add the results at the end with the current mark
                for (n, v, f) in recTroves:
                    if (n, v, f) not in troveSetList:
                        troveList.append((mark, (n, v, f)))
                        troveSetList.add((n, v, f))
        log.debug("after group recursion %d troves are needed", len(troveList))
        # we need to make sure we mirror the GPG keys of any newly added troves
        newHosts = set([x[1].getHost() for x in troveSetList.union(removedSet)])
        for host in newHosts.difference(set([cfg.host])):
            _parallel(targets, TargetRepository.mirrorGPG,
                    referenceRepos, host)

    # we check which troves from the troveList are needed on each
    # target and we split the troveList into separate lists depending
    # on how many targets require each
    byTarget = {}
    targetSetList = []
    if len(troveList):
        byTrove = {}
        for i, target in enumerate(targets):
            for t in target.addTroveList(troveList):
                bt = byTrove.setdefault(t, set())
                bt.add(i)
        # invert the dict by target now
        for trv, ts in byTrove.items():
            targetSet = [ targets[i] for i in ts ]
            try:
                targetIdx = targetSetList.index(targetSet)
            except ValueError:
                targetSetList.append(targetSet)
                targetIdx = len(targetSetList)-1
            bt = byTarget.setdefault(targetIdx, [])
            bt.append(trv)
        del byTrove
    # if we were returned troves, but we filtered them all out, advance the
    # mark and signal "try again"
    if len(byTarget) == 0 and len(removedSet) == 0 and initTLlen:
        # we had troves and now we don't
        log.debug("no troves found for our label %s" % cfg.labels)
        _parallel(targets, TargetRepository.setMirrorMark, crtMaxMark)
        # try again
        return -1

    # now we get each section of the troveList for each targetSet. We
    # start off mirroring by those required by fewer targets, using
    # the assumption that those troves are what is required for the
    # targets to catch up to a common set
    if len(byTarget) > 1:
        log.debug("split %d troves into %d chunks by target", len(troveList), len(byTarget))
    # sort the targetSets by length
    targetSets = list(enumerate(targetSetList))
    targetSets.sort(lambda a,b: cmp(len(a[1]), len(b[1])))
    bundlesMark = 0
    for idx, targetSet in targetSets:
        troveList = byTarget[idx]
        if not troveList: # XXX: should not happen...
            continue
        log.debug("mirroring %d troves into %d targets", len(troveList), len(targetSet))
        # since these troves are required for all targets, we can use
        # the "first" one to build the relative changeset requests
        target = list(targetSet)[0]
        bundles = buildBundles(sourceRepos, target, troveList,
                cfg.absoluteChangesets, cfg.splitNodes)
        for i, bundle in enumerate(bundles):
            jobList = [ x[1] for x in bundle ]
            # XXX it's a shame we can't give a hint as to what server to use
            # to avoid having to open the changeset and read in bits of it
            if test:
                log.debug("test mode: not mirroring (%d of %d) %s" % (i + 1, len(bundles), jobList))
                updateCount += len(bundle)
                continue
            (outFd, tmpName) = util.mkstemp()
            os.close(outFd)
            log.debug("getting (%d of %d) %s" % (i + 1, len(bundles), displayBundle(bundle)))
            try:
                sourceRepos.createChangeSetFile(jobList, tmpName, recurse = False,
                                                callback = callback, mirrorMode = True)
            except changeset.ChangeSetKeyConflictError:
                splitJobList(jobList, sourceRepos, targetSet, hidden=hidden,
                             callback=callback)
            else:
                _parallel(targetSet, TargetRepository.commitChangeSetFile,
                        tmpName, hidden=hidden, callback=callback)
            try:
                os.unlink(tmpName)
            except OSError:
                pass
            callback.done()
        updateCount += len(bundle)
        # compute the max mark of the bundles we comitted
        mark = max([min([x[0] for x in bundle]) for bundle in bundles])
        if mark > bundlesMark:
            bundlesMark = mark
    else: # only when we're all done looping advance mark to the new max
        if bundlesMark == 0 or bundlesMark <= currentMark:
            bundlesMark = crtMaxMark # avoid repeating the same query...
        if hidden: # if we've hidden the last commits, show them now
            _parallel(targets, TargetRepository.presentHiddenTroves,
                    bundlesMark)
        else:
            _parallel(targets, TargetRepository.setMirrorMark, bundlesMark)
    # mirroring removed troves requires one by one processing
    for target in targets:
        copySet = removedSet.copy()
        updateCount += mirrorRemoved(referenceRepos, target.repo, copySet,
                                     test=test, callback=callback)
    # if this was a noop because the removed troves were already mirrored
    # we need to keep going
    if updateCount == 0 and len(removedSet):
        _parallel(targets, TargetRepository.setMirrorMark, crtMaxMark)
        return -1
    return updateCount

# check if the sourceRepos is in sync with targetRepos
def checkSyncRepos(config, sourceRepos, targetRepos):
    checkConfig(config)
    targets = _makeTargets(config, targetRepos)
    log.setVerbosity(log.DEBUG)

    # retrieve the set of troves from a give repository
    def _getTroveSet(config, repo):
        def _flatten(troveSpec):
            l = []
            for name, versionD in troveSpec.items():
                for version, flavorList in versionD.items():
                    l += [ (name, version, flavor) for flavor in flavorList ]
            return set(l)
        troveSpecs = {}
        if config.labels:
            d = troveSpecs.setdefault(None, {})
            for l in config.labels:
                d[l] = ''
            t = repo.getTroveVersionsByLabel(troveSpecs, troveTypes = netclient.TROVE_QUERY_ALL)
        else:
            troveSpecs = {None : None}
            t = repo.getTroveVersionList(config.host, troveSpecs,
                                         troveTypes = netclient.TROVE_QUERY_ALL)
        return _flatten(t)
    # compare source with each target
    def _compare(src, dst):
        srcName, srcSet = src
        dstName, dstSet = dst
        counter = 0
        for x in srcSet.difference(dstSet):
            log.debug(" - %s %s " % (srcName, x))
            counter += 1
        for x in dstSet.difference(srcSet):
            log.debug(" + %s %s" % (dstName, x))
            counter += 1
        return counter
    log.debug("Retrieving list of troves from source %s" % str(sourceRepos.c.map))
    sourceSet = _getTroveSet(config, sourceRepos)
    hasDiff = 0
    for target in targets:
        log.debug("Retrieving list of troves from %s %s" % (target.name, str(target.repo.c.map)))
        targetSet = _getTroveSet(config, target.repo)
        log.debug("Diffing source and %s" % target.name)
        hasDiff += _compare( ("source", sourceSet), (target.name, targetSet) )
    log.debug("Done")
    return hasDiff

if __name__ == '__main__':
    sys.exit(Main())
