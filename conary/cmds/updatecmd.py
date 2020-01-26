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
import json
import os
import itertools
import sys
import threading
import urllib.request, urllib.error, urllib.parse

from conary import callbacks
from conary import conaryclient
from conary import display
from conary import errors
from conary import trove
from conary import trovetup
from conary import versions
from conary.deps import deps
from conary.lib import api
from conary.lib import log
from conary.lib import util
from conary.local import database
from conary.repository import changeset, filecontainer
from conary.conaryclient import cmdline, modelupdate
from conary.conaryclient.cmdline import parseTroveSpec

# FIXME client should instantiated once per execution of the command line
# conary client

class CriticalUpdateInfo(conaryclient.CriticalUpdateInfo):
    criticalTroveRegexps = ['conary:.*']

def locked(method):
    # this decorator used to be defined in UpdateCallback
    # The problem is you cannot subclass UpdateCallback and use the decorator
    # because python complains it is an unbound function.
    # And you can't define it as @staticmethod either, it would break the
    # decorated functions.
    # Somewhat related (staticmethod objects not callable) topic:
    # http://mail.python.org/pipermail/python-dev/2006-March/061948.html

    def wrapper(self, *args, **kwargs):
        self.lock.acquire()
        try:
            return method(self, *args, **kwargs)
        finally:
            self.lock.release()

    wrapper.__doc__ = method.__doc__
    wrapper.__name__ = method.__name__
    return wrapper

class UpdateCallback(callbacks.LineOutput, callbacks.UpdateCallback):

    def done(self):
        """
        @see: callbacks.UpdateCallback.done
        """
        self._message('')

    def _message(self, text):
        """
        Called when this callback object needs to output progress information.
        The information is written to stdout.

        @return: None
        """
        callbacks.LineOutput._message(self, text)

    def update(self):
        """
        Called by this callback object to update the status.  This method
        sanitizes text.  This method is not thread safe - obtain a lock before
        calling.

        @return: None
        """

        t = ""

        if self.updateText:
            t += self.updateText

        if self.csText:
            t = self.csText + ' '

        if t and len(t) < 76:
            t = t[:76]
            t += '...'

        self._message(t)

    @locked
    def updateMsg(self, text):
        """
        Called when the update thread has status updates.

        @param text: new status text
        @type text: string

        @return: None
        """
        self.updateText = text
        self.update()

    @locked
    def csMsg(self, text):
        """
        Called when the download thread has status updates.

        @param text: new status text
        @type text: string

        @return: None
        """

        self.csText = text
        self.update()

    def executingSystemModel(self):
        self.updateMsg("Processing system model")

    def loadingModelCache(self):
        self.updateMsg("Loading system model cache")

    def savingModelCache(self):
        self.updateMsg("Saving system model cache")

    def preparingChangeSet(self):
        """
        @see: callbacks.ChangesetCallback.preparingChangeSet
        """
        self.updateMsg("Preparing changeset request")

    def resolvingDependencies(self):
        """
        @see: callbacks.UpdateCallback.resolvingDependencies
        """
        self.updateMsg("Resolving dependencies")

    @locked
    def updateDone(self):
        """
        @see: callbacks.UpdateCallback.updateDone
        """
        self._message('')
        self.updateText = None

    @locked
    def _downloading(self, msg, got, rate, need):
        """
        Called by this callback object to handle different kinds of
        download-related progress information.  This method puts together
        download rate information.

        @param msg: status message
        @type msg: string
        @param got: number of bytes retrieved so far
        @type got: integer
        @param rate: bytes per second
        @type rate: integer
        @param need: number of bytes total to be retrieved
        @type need: integer
        @return: None
        """
        # This function acquires a lock just because it looks at self.csHunk
        # and self.updateText directly. Otherwise, self.csMsg will acquire the
        # lock (which is now reentrant)
        if got == need:
            self.csMsg(None)
        elif need != 0:
            if self.csHunk[1] < 2 or not self.updateText:
                self.csMsg("%s %dKB (%d%%) of %dKB at %dKB/sec"
                           % (msg, got/1024, (got*100)/need, need/1024, rate/1024))
            else:
                self.csMsg("%s %d of %d: %dKB (%d%%) of %dKB at %dKB/sec"
                           % ((msg,) + self.csHunk + \
                              (got/1024, (got*100)/need, need/1024, rate/1024)))
        else: # no idea how much we need, just keep on counting...
            self.csMsg("%s (got %dKB at %dKB/s so far)" % (msg, got/1024, rate/1024))

    def downloadingFileContents(self, got, need):
        """
        @see: callbacks.ChangesetCallback.downloadingFileContents
        """
        self._downloading('Downloading files for changeset', got, self.rate, need)

    def downloadingChangeSet(self, got, need):
        """
        @see: callbacks.ChangesetCallback.downloadingChangeSet
        """
        self._downloading('Downloading', got, self.rate, need)

    def requestingFileContents(self):
        """
        @see: callbacks.ChangesetCallback.requestingFileContents
        """
        if self.csHunk[1] < 2:
            self.csMsg("Requesting file contents")
        else:
            self.csMsg("Requesting file contents for changeset %d of %d" % self.csHunk)

    def requestingChangeSet(self):
        """
        @see: callbacks.ChangesetCallback.requestingChangeSet
        """
        if self.csHunk[1] < 2:
            self.csMsg("Requesting changeset")
        else:
            self.csMsg("Requesting changeset %d of %d" % self.csHunk)

    def creatingRollback(self):
        """
        @see: callbacks.UpdateCallback.creatingRollback
        """
        self.updateMsg("Creating rollback")

    def preparingUpdate(self, troveNum, troveCount):
        """
        @see: callbacks.UpdateCallback.preparingUpdate
        """
        self.updateMsg("Preparing update (%d of %d)" %
                      (troveNum, troveCount))

    @locked
    def restoreFiles(self, size, totalSize):
        """
        @see: callbacks.UpdateCallback.restoreFiles
        """
        # Locked, because we modify self.restored
        if totalSize != 0:
            self.restored += size
            self.updateMsg("Writing %dk of %dk (%d%%)"
                        % (self.restored / 1024 , totalSize / 1024,
                           (self.restored * 100) / totalSize))

    def removeFiles(self, fileNum, total):
        """
        @see: callbacks.UpdateCallback.removeFiles
        """
        if total != 0:
            self.updateMsg("Removing %d of %d (%d%%)"
                        % (fileNum , total, (fileNum * 100) / total))

    def creatingDatabaseTransaction(self, troveNum, troveCount):
        """
        @see: callbacks.UpdateCallback.creatingDatabaseTransaction
        """
        self.updateMsg("Creating database transaction (%d of %d)" %
                      (troveNum, troveCount))

    def updatingDatabase(self, step, stepNum, stepCount):
        if step == 'latest':
            self.updateMsg('Updating list of latest versions: (%d of %d)' %
                           (stepNum, stepCount))
        else:
            self.updateMsg('Updating database: (%d of %d)' %
                           (stepNum, stepCount))

    def runningPreTagHandlers(self):
        """
        @see: callbacks.UpdateCallback.runningPreTagHandlers
        """
        self.updateMsg("Running tag prescripts")

    def runningPostTagHandlers(self):
        """
        @see: callbacks.UpdateCallback.runningPostTagHandlers
        """
        self.updateMsg("Running tag post-scripts")

    def committingTransaction(self):
        """
        @see: callbacks.UpdateCallback.committingTransaction
        """
        self.updateMsg("Committing database transaction")

    @locked
    def setChangesetHunk(self, num, total):
        """
        @see: callbacks.ChangesetCallback.setChangesetHunk
        """
        self.csHunk = (num, total)

    @locked
    def setUpdateHunk(self, num, total):
        """
        @see: callbacks.UpdateCallback.setUpdateHunk
        """
        self.restored = 0
        self.updateHunk = (num, total)

    @locked
    def setUpdateJob(self, jobs):
        """
        @see: callbacks.UpdateCallback.setUpdateJob
        """
        self._message('')
        if self.updateHunk[1] < 2:
            self.out.write('Applying update job:\n')
        else:
            self.out.write('Applying update job %d of %d:\n' % self.updateHunk)
        # erase anything that is currently displayed
        self._message('')
        self.formatter.prepareJobs(jobs)
        for line in self.formatter.formatJobTups(jobs, indent='    '):
            self.out.write(line + '\n')

    @locked
    def tagHandlerOutput(self, tag, msg, stderr = False):
        """
        @see: callbacks.UpdateCallback.tagHandlerOutput
        """
        self._message('')
        self.out.write('[%s] %s\n' % (tag, msg))

    @locked
    def troveScriptOutput(self, typ, msg):
        """
        @see: callbacks.UpdateCallback.troveScriptOutput
        """
        self._message('')
        self.out.write("[%s] %s" % (typ, msg))

    @locked
    def troveScriptFailure(self, typ, errcode):
        """
        @see: callbacks.UpdateCallback.troveScriptFailure
        """
        self._message('')
        self.out.write("[%s] %s" % (typ, errcode))

    def capsuleSyncScan(self, capsuleType):
        self.updateMsg("Scanning for %s capsule changes" % capsuleType)

    def capsuleSyncCreate(self, capsuleType, name, num, total):
        self.updateMsg("Collecting modifications to %s database (%d of %d)" %
                (capsuleType, num, total))

    def capsuleSyncApply(self, added, removed):
        self._message('')
        self.out.write('Synchronizing database with capsule changes\n')

    def __init__(self, cfg=None, modelFile=None):
        """
        Initialize this callback object.
        @param cfg: Conary configuration
        @type cfg: A ConaryConfiguration object.
        @return: None
        """
        callbacks.UpdateCallback.__init__(self)
        if cfg:
            self.setTrustThreshold(cfg.trustThreshold)
        callbacks.LineOutput.__init__(self)
        self.restored = 0
        self.csHunk = (0, 0)
        self.updateHunk = (0, 0)
        self.csText = None
        self.updateText = None
        self.lock = threading.RLock()

        if cfg:
            fullVersions = cfg.fullVersions
            showFlavors = cfg.fullFlavors
            showLabels = cfg.showLabels
            baseFlavors = cfg.flavor
            showComponents = cfg.showComponents
            db = conaryclient.ConaryClient(cfg, modelFile=modelFile).db
        else:
            fullVersions = showFlavors = showLabels = db = baseFlavors = None
            showComponents = None

        self.formatter = display.JobTupFormatter(affinityDb=db)
        self.formatter.dcfg.setTroveDisplay(fullVersions=fullVersions,
                                            fullFlavors=showFlavors,
                                            showLabels=showLabels,
                                            baseFlavors=baseFlavors,
                                            showComponents=showComponents)
        self.formatter.dcfg.setJobDisplay(compressJobs=not showComponents)


class JsonUpdateCallback(UpdateCallback):
    def __del__(self):
        pass

    def _message(self, msg):
        self.out.write('%s\n' % msg)

    def _capsuleSync(self, name, step, done=None, total=None, rate=None):
        step = max(step, 1)
        self.updateMsg(
            step_name=name, step=step, step_total=3, phase=1,
            phase_name="Capsule sync", done=done, total=total, rate=rate)

    def _calculateUpdate(self, name, step, done=None, total=None, rate=None):
        step = max(step, 1)
        self.updateMsg(
            step_name=name, step=step, step_total=4, phase=2,
            phase_name="Calculate update", done=done, total=total, rate=rate)

    def _applyUpdate(self, name, done=None, total=None, rate=None, jobs=None):
        step, step_total = self.updateHunk
        step = max(step, 1)
        step_total = max(step_total, 1)
        if jobs:
            self.updateMsg(
                step_name=name, step=step, step_total=step_total,
                phase=3, phase_name="Apply update", done=done, total=total,
                rate=rate, jobs=jobs)
        else:
            self.updateMsg(
                step_name=name, step=step, step_total=step_total,
                phase=3, phase_name="Apply update", done=done, total=total,
                rate=rate)

    def _applyUpdateCS(self, name, done=None, total=None, rate=None):
        step, step_total = self.updateHunk
        step = max(step, 1)
        step_total = max(step_total, 1)
        self.updateMsg(
            step_name=name, step=step, step_total=step_total,
            phase=3, phase_name="Apply update", done=done, total=total,
            rate=rate)

    def update(self):
        """
        Called by this callback object to udpate the status. This method
        convets dictionaries into json strings. This method is not thread safe
        - obtain a lock before calling.

        @return None
        """
        if self.updateText:
            t = self.updateText

        if self.csText:
            t = self.csText

        t['percent'] = None
        if t.get('done') is not None and t.get('total'):
            t['percent'] = (t['done'] * 100) / t['total']
        if t:
            self._message(json.dumps(t))

    @locked
    def updateMsg(self, *args, **kwargs):
        self.updateText = kwargs
        self.updateText['phase_total'] = 3
        if args:
            self.updateText['msg'] = args[0]
        self.update()

    @locked
    def csMsg(self, *args, **kwargs):
        self.csText = kwargs
        self.csText['phase_total'] = 3
        if args:
            if args[0] is None:
                self.csText = dict()
            else:
                self.csText['msg'] = args[0]
        self.update()

    def executingSystemModel(self):
        self._calculateUpdate("Processing system model", step=2)

    def loadingModelCache(self):
        self._calculateUpdate("Loading system model cache", step=1)

    def savingModelCache(self):
        self._calculateUpdate("Saving system model cache", step=4)

    def preparingChangeSet(self):
        self._applyUpdate("Preparing changeset request")

    def resolvingDependencies(self):
        self._calculateUpdate("Resolving dependencies", step=3)

    def creatingRollback(self):
        """
        @see: callbacks.UpdateCallback.creatingRollback
        """
        self._applyUpdate("Creating rollback")

    def preparingUpdate(self, troveNum, troveCount):
        """
        @see: callbacks.UpdateCallback.preparingUpdate
        """
        self._applyUpdate("Preparing update", done=troveNum, total=troveCount)

    @locked
    def restoreFiles(self, size, totalSize):
        """
        @see: callbacks.UpdateCallback.restoreFiles
        """
        # Locked, because we modify self.restored
        if totalSize != 0:
            self.restored += size
            self._applyUpdate("Restoring Files", done=self.restored / 1024,
                              total=totalSize / 1024)

    def removeFiles(self, fileNum, total):
        """
        @see: callbacks.UpdateCallback.removeFiles
        """
        if total != 0:
            self._applyUpdate("Removing Files", done=fileNum, total=total)

    def creatingDatabaseTransaction(self, troveNum, troveCount):
        """
        @see: callbacks.UpdateCallback.creatingDatabaseTransaction
        """
        self._applyUpdate("Creating database transaction", done=troveNum,
                          total=troveCount)

    def updatingDatabase(self, step, stepNum, stepCount):
        if step == 'latest':
            self._applyUpdate(
                'Updating list of latest versions',
                done=stepNum,
                total=stepCount,
                )
        else:
            self._applyUpdate(
                'Updating database', done=stepNum, total=stepCount)

    def runningPreTagHandlers(self):
        """
        @see: callbacks.UpdateCallback.runningPreTagHandlers
        """
        self._applyUpdate("Running tag prescripts")

    def runningPostTagHandlers(self):
        """
        @see: callbacks.UpdateCallback.runningPostTagHandlers
        """
        self._applyUpdate("Running tag post-scripts")

    def committingTransaction(self):
        """
        @see: callbacks.UpdateCallback.committingTransaction
        """
        self._applyUpdate("Committing database transaction")

    @locked
    def setUpdateJob(self, jobs):
        """
        @see: callbacks.UpdateCallback.setUpdateJob
        """
        jobs_collection = []
        self.formatter.prepareJobs(jobs)
        for line in self.formatter.formatJobTups(jobs):
            action, trove_spec = line.split(None, 1)
            jobs_collection.append(dict(action=action, trove=trove_spec))
        self._applyUpdate(
            'Applying update job',
            jobs=jobs_collection,
            )

    def capsuleSyncScan(self, capsuleType):
        self._capsuleSync(
            "Scanning for %s capsule changes" % capsuleType, step=1)

    def capsuleSyncCreate(self, capsuleType, name, num, total):
        self._capsuleSync(
            "Collecting modifications to %s database" % capsuleType,
            step=2, done=num, total=total)

    @locked
    def _downloading(self, msg, got, rate, need):
        """
        Called by this callback object to handle different kinds of
        download-related progress information.  This method puts together
        download rate information.

        @param msg: status message
        @type msg: string
        @param got: number of bytes retrieved so far
        @type got: integer
        @param rate: bytes per second
        @type rate: integer
        @param need: number of bytes total to be retrieved
        @type need: integer
        @return: None
        """
        # This function acquires a lock just because it looks at self.csHunk
        # and self.updateText directly. Otherwise, self.csMsg will acquire the
        # lock (which is now reentrant)
        if got == need:
            self.csMsg(None)
        elif need != 0:
            if self.csHunk[1] < 2 or not self.updateText:
                self._applyUpdateCS(msg, done=got / 1024, total=need / 1024,
                                    rate=rate / 1024)
            else:
                self._applyUpdateCS("%s %d of %d" % ((msg,) + self.csHunk),
                                    done=got / 1024, total=need / 1024,
                                    rate=rate / 1024)
        else:
            # no idea how much we need, just keep on counting...
            self._applyUpdateCS(msg, done=got / 1024, rate=rate / 1024)

    def downloadingFileContents(self, got, need):
        """
        @see: callbacks.ChangesetCallback.downloadingFileContents
        """
        self._applyUpdateCS('Downloading files for changeset', done=got,
                            rate=self.rate, total=need)

    def downloadingChangeSet(self, got, need):
        """
        @see: callbacks.ChangesetCallback.downloadingChangeSet
        """
        self._applyUpdateCS('Downloading', done=got, rate=self.rate,
                            total=need)

    def requestingFileContents(self):
        """
        @see: callbacks.ChangesetCallback.requestingFileContents
        """
        self._applyUpdateCS(
            "Requesting file contents for changeset",
            done=max(self.csHunk[0], 1),
            total=max(self.csHunk[1], 1),
            )

    def requestingChangeSet(self):
        """
        @see: callbacks.ChangesetCallback.requestingChangeSet
        """
        self._applyUpdateCS(
            "Requesting changeset",
            done=max(self.csHunk[0], 1),
            total=max(self.csHunk[1], 1),
            )

    @locked
    def troveScriptOutput(self, typ, msg):
        """
        @see: callbacks.UpdateCallback.troveScriptOutput
        """
        self._applyUpdate("[%s] %s" % (typ, msg))

    @locked
    def troveScriptFailure(self, typ, errcode):
        """
        @see: callbacks.UpdateCallback.troveScriptFailure
        """
        self._applyUpdate("[%s] %s" % (typ, errcode))

    def capsuleSyncApply(self, added, removed):
        self._capsuleSync('Synchronizing database with capsule changes',
                          step=3)

    def __init__(self, *args, **kwargs):
        UpdateCallback.__init__(self, *args, **kwargs)
        self.updateText = {}
        self.csText = {}


def displayChangedJobs(addedJobs, removedJobs, cfg):
    db = conaryclient.ConaryClient(cfg).db
    formatter = display.JobTupFormatter(affinityDb=db)
    formatter.dcfg.setTroveDisplay(fullVersions=cfg.fullVersions,
                                   fullFlavors=cfg.fullFlavors,
                                   showLabels=cfg.showLabels,
                                   baseFlavors=cfg.flavor,
                                   showComponents=cfg.showComponents)
    formatter.dcfg.setJobDisplay(compressJobs=not cfg.showComponents)
    formatter.prepareJobLists([removedJobs | addedJobs])

    if removedJobs:
        print('No longer part of job:')
        for line in formatter.formatJobTups(removedJobs, indent='    '):
            print(line)
    if addedJobs:
        print('Added to job:')
        for line in formatter.formatJobTups(addedJobs, indent='    '):
            print(line)

def displayUpdateInfo(updJob, cfg, noRestart=False):
    jobLists = updJob.getJobs()
    db = conaryclient.ConaryClient(cfg).db

    formatter = display.JobTupFormatter(affinityDb=db)
    formatter.dcfg.setTroveDisplay(fullVersions=cfg.fullVersions,
                                   fullFlavors=cfg.fullFlavors,
                                   showLabels=cfg.showLabels,
                                   baseFlavors=cfg.flavor,
                                   showComponents=cfg.showComponents)
    formatter.dcfg.setJobDisplay(compressJobs=not cfg.showComponents)
    formatter.prepareJobLists(jobLists)

    totalJobs = len(jobLists)
    for num, job in enumerate(jobLists):
        if totalJobs > 1:
            if num in updJob.getCriticalJobs():
                print('** ', end=' ')
            print('Job %d of %d:' % (num + 1, totalJobs))
        for line in formatter.formatJobTups(job, indent='    '):
            print(line)
    if updJob.getCriticalJobs() and not noRestart:
        criticalJobs = updJob.getCriticalJobs()
        if len(criticalJobs) > 1:
            jobPlural = 's'
        else:
            jobPlural = ''
        jobList = ', '.join([str(x + 1) for x in criticalJobs])
        print()
        print('** The update will restart itself after job%s %s and continue updating' % (jobPlural, jobList))
    return

@api.developerApi
def doUpdate(cfg, changeSpecs, **kwargs):
    callback = kwargs.get('callback', None)
    if not callback:
        callback = callbacks.UpdateCallback(trustThreshold=cfg.trustThreshold)
        kwargs['callback'] = callback
    else:
        callback.setTrustThreshold(cfg.trustThreshold)

    syncChildren = kwargs.get('syncChildren', False)
    syncUpdate = kwargs.pop('syncUpdate', False)
    restartInfo = kwargs.get('restartInfo', None)

    if syncChildren or syncUpdate:
        installMissing = True
    else:
        installMissing = False

    kwargs['installMissing'] = installMissing

    fromChangesets = []
    for path in kwargs.pop('fromFiles', []):
        cs = changeset.ChangeSetFromFile(path)
        fromChangesets.append(cs)

    kwargs['fromChangesets'] = fromChangesets

    # Look for items which look like files in the applyList and convert
    # them into fromChangesets w/ the primary sets
    for item in changeSpecs[:]:
        if os.access(item, os.R_OK):
            try:
                cs = changeset.ChangeSetFromFile(item)
            except:
                continue

            fromChangesets.append(cs)
            changeSpecs.remove(item)
            for troveTuple in cs.getPrimaryTroveList():
                changeSpecs.append(trovetup.TroveTuple(*troveTuple).asString())

    if kwargs.get('restartInfo', None):
        # We don't care about applyList, we will set it later
        applyList = None
    else:
        keepExisting = kwargs.get('keepExisting')
        updateByDefault = kwargs.get('updateByDefault', True)
        applyList = cmdline.parseChangeList(changeSpecs, keepExisting,
                                            updateByDefault,
                                            allowChangeSets=True)

    _updateTroves(cfg, applyList, **kwargs)
    # Clean up after ourselves
    if restartInfo:
        util.rmtree(restartInfo, ignore_errors=True)

def doModelUpdate(cfg, sysmodel, modelFile, otherArgs, **kwargs):
    kwargs['systemModel'] = sysmodel
    kwargs['systemModelFile'] = modelFile
    kwargs['loadTroveCache'] = True
    kwargs.setdefault('updateByDefault', True) # erase is not default case
    kwargs.setdefault('model', False)
    kwargs.setdefault('keepExisting', True) # prefer "install" to "update"
    restartInfo = kwargs.get('restartInfo', None)
    patchArgs = kwargs.pop('patchSpec', None)
    fromChangesets = []
    applyList = []

    callback = kwargs.get('callback', None)
    if not callback:
        callback = callbacks.UpdateCallback(trustThreshold=cfg.trustThreshold)
        kwargs['callback'] = callback
    else:
        callback.setTrustThreshold(cfg.trustThreshold)

    if restartInfo is None:
        addArgs = [x[1:] for x in otherArgs if x.startswith('+')]
        rmArgs = [x[1:] for x in otherArgs if x.startswith('-')]
        defArgs = [x for x in otherArgs
                    if not (x.startswith('+') or x.startswith('-'))]

        # find any default arguments that represent changesets to
        # install/update
        for defArg in list(defArgs):
            if kwargs['updateByDefault'] and os.path.isfile(defArg):
                try:
                    cs = changeset.ChangeSetFromFile(defArg)
                    fromChangesets.append((cs, defArg))
                    defArgs.remove(defArg)
                except filecontainer.BadContainer:
                    # not a changeset, must be a trove name
                    pass

        if kwargs['updateByDefault']:
            addArgs += defArgs
        else:
            rmArgs += defArgs

        if rmArgs:
            sysmodel.appendOpByName('erase', text=rmArgs)

        updateName = { False: 'update',
                       True: 'install' }[kwargs['keepExisting']]

        branchArgs = {}
        for index, spec in enumerate(addArgs):
            try:
                troveSpec = trovetup.TroveSpec(spec)
                version = versions.Label(troveSpec.version)
                branchArgs[troveSpec] = index
            except:
                # Any exception is a parse failure in one of the
                # two steps, and so we do not convert that argument
                pass
       
        if branchArgs:
            client = conaryclient.ConaryClient(cfg)
            repos = client.getRepos()
            foundTroves = repos.findTroves(cfg.installLabelPath,
                                           list(branchArgs.keys()),
                                           defaultFlavor = cfg.flavor)
            for troveSpec in foundTroves:
                index = branchArgs[troveSpec]
                foundTrove = foundTroves[troveSpec][0]
                addArgs[index] = addArgs[index].replace(
                    troveSpec.version,
                    '%s/%s' %(foundTrove[1].trailingLabel(),
                              foundTrove[1].trailingRevision()))

        disallowedChangesets = []
        for cs, argName in fromChangesets:
            for troveTuple in cs.getPrimaryTroveList():
                # group and redirect changesets will break the model the
                # next time it is run, so prevent them from getting in
                # the model in the first place
                if troveTuple[1].isOnLocalHost():
                    if troveTuple[0].startswith('group-'):
                        disallowedChangesets.append((argName, 'group',
                            trovetup.TroveTuple(*troveTuple).asString()))
                        continue
                    trvCs = cs.getNewTroveVersion(*troveTuple)
                    if trvCs.getType() == trove.TROVE_TYPE_REDIRECT:
                        disallowedChangesets.append((argName, 'redirect',
                            trovetup.TroveTuple(*troveTuple).asString()))
                        continue

                addArgs.append(
                    trovetup.TroveTuple(*troveTuple).asString())

        if disallowedChangesets:
            raise errors.ConaryError(
                'group and redirect changesets on a local label'
                ' cannot be installed:\n    ' + '\n    '.join(
                    '%s contains local %s: %s' % x
                    for x in disallowedChangesets))

        if addArgs:
            sysmodel.appendOpByName(updateName, text=addArgs)

        if patchArgs:
            sysmodel.appendOpByName('patch', text=patchArgs)


        kwargs['fromChangesets'] = [x[0] for x in fromChangesets]

        if kwargs.pop('model'):
            sysmodel.write(sys.stdout)
            sys.stdout.flush()
            return None

        keepExisting = kwargs.get('keepExisting')
        updateByDefault = kwargs.get('updateByDefault', True)
        applyList = cmdline.parseChangeList([], keepExisting,
                                            updateByDefault,
                                            allowChangeSets=True)

    else:
        # In the restart case, applyList == [] which says "sync to model"
        pass
        
    _updateTroves(cfg, applyList, **kwargs)
    # Clean up after ourselves
    if restartInfo:
        util.rmtree(restartInfo, ignore_errors=True)


def _updateTroves(cfg, applyList, **kwargs):
    # Take out the apply-related keyword arguments
    applyDefaults = dict(
                        replaceFiles = False,
                        replaceManagedFiles = False,
                        replaceUnmanagedFiles = False,
                        replaceModifiedFiles = False,
                        replaceModifiedConfigFiles = False,
                        tagScript = None,
                        justDatabase = False,
                        skipCapsuleOps = False,
                        info = False,
                        keepJournal = False,
                        noRestart = False,
                        noScripts = False,
    )
    applyKwargs = {}
    for k in applyDefaults:
        if k in kwargs:
            applyKwargs[k] = kwargs.pop(k)

    callback = kwargs.pop('callback')
    loadTroveCache = kwargs.pop('loadTroveCache', False)
    applyKwargs['test'] = kwargs.get('test', False)
    applyKwargs['localRollbacks'] = cfg.localRollbacks
    applyKwargs['autoPinList'] = cfg.pinTroves

    model = kwargs.pop('systemModel', None)
    modelFile = kwargs.pop('systemModelFile', None)
    modelGraph = kwargs.pop('modelGraph', None)
    modelTrace = kwargs.pop('modelTrace', None)

    noRestart = applyKwargs.get('noRestart', False)

    client = conaryclient.ConaryClient(cfg, modelFile=modelFile)
    client.setUpdateCallback(callback)
    if kwargs.pop('disconnected', False):
        client.disconnectRepos()
    migrate = kwargs.get('migrate', False)
    # even though we no longer differentiate forceMigrate, we still
    # remove it from kwargs to avoid confusing prepareUpdateJob
    kwargs.pop('forceMigrate', False)
    restartInfo = kwargs.get('restartInfo', None)

    # Initialize the critical update set
    applyCriticalOnly = kwargs.get('applyCriticalOnly', False)
    if kwargs.get('criticalUpdateInfo') is not None:
        kwargs['criticalUpdateInfo'].criticalOnly = applyCriticalOnly
    else:
        kwargs['criticalUpdateInfo'] = CriticalUpdateInfo(applyCriticalOnly)

    info = applyKwargs.pop('info', False)

    # Rename depCheck to resolveDeps
    depCheck = kwargs.pop('depCheck', True)
    kwargs['resolveDeps'] = depCheck

    if not info:
        client.checkWriteableRoot()

    # Unfortunately there's no easy way to make 'test' or 'info' mode work
    # with capsule sync, doubly so because it influences the decisions made
    # later on about what troves to update. So this will always really
    # apply, but the good news is that it never modifies the system outside
    # of the Conary DB.
    client.syncCapsuleDatabase(callback, makePins=True)

    updJob = client.newUpdateJob()

    try:
        if model:
            changeSetList = kwargs.get('fromChangesets', [])
            criticalUpdates = kwargs.get('criticalUpdateInfo', None)

            tc = modelupdate.CMLTroveCache(client.getDatabase(),
                                                   client.getRepos(),
                                                   callback = callback,
                                                   changeSetList =
                                                        changeSetList)
            tcPath = cfg.root + cfg.dbPath + '/modelcache'
            if loadTroveCache:
                if os.path.exists(tcPath):
                    log.info("loading %s", tcPath)
                    callback.loadingModelCache()
                    tc.load(tcPath)
            ts = client.cmlGraph(model, changeSetList = changeSetList)
            if modelGraph is not None:
                ts.g.generateDotFile(modelGraph)
            suggMap = client._updateFromTroveSetGraph(updJob, ts, tc,
                                        fromChangesets = changeSetList,
                                        criticalUpdateInfo = criticalUpdates,
                                        callback = callback)
            if modelTrace is not None:
                ts.g.trace([ parseTroveSpec(x) for x in modelTrace ] )

            finalModel = copy.deepcopy(model)
            if model.suggestSimplifications(tc, ts.g):
                log.info("possible system model simplifications found")
                ts2 = client.cmlGraph(model, changeSetList = changeSetList)
                updJob2 = client.newUpdateJob()
                try:
                    suggMap2 = client._updateFromTroveSetGraph(updJob2, ts2,
                                        tc,
                                        fromChangesets = changeSetList,
                                        criticalUpdateInfo = criticalUpdates)
                except errors.TroveNotFound:
                    log.info("bad model generated; bailing")
                else:
                    if (suggMap == suggMap2 and
                        updJob.getJobs() == updJob2.getJobs()):
                        log.info("simplified model verfied; using it instead")
                        ts = ts2
                        finalModel = model
                        updJob = updJob2
                        suggMap = suggMap2
                    else:
                        log.info("simplified model changed result; ignoring")

            model = finalModel
            modelFile.model = finalModel

            if tc.cacheModified():
                log.info("saving %s", tcPath)
                callback.savingModelCache()
                tc.save(tcPath)
                callback.done()
        else:
            suggMap = client.prepareUpdateJob(updJob, applyList, **kwargs)
    except:
        callback.done()
        client.close()
        raise

    if info:
        callback.done()
        displayUpdateInfo(updJob, cfg, noRestart=noRestart)
        if restartInfo and not model:
            callback.done()
            newJobs = set(itertools.chain(*updJob.getJobs()))
            oldJobs = set(updJob.getItemList())
            addedJobs = newJobs - oldJobs
            removedJobs = oldJobs - newJobs
            if addedJobs or removedJobs:
                print()
                print('NOTE: after critical updates were applied, the contents of the update were recalculated:')
                print()
                displayChangedJobs(addedJobs, removedJobs, cfg)
        updJob.close()
        client.close()
        return

    if model:
        missingLocalTroves = model.getMissingLocalTroves(tc, ts)
        if missingLocalTroves:
            print('Update would leave references to missing local troves:')
            for troveTup in missingLocalTroves:
                if not isinstance(troveTup, trovetup.TroveTuple):
                    troveTup = trovetup.TroveTuple(troveTup)
                print("\t" + str(troveTup))
            client.close()
            return

    if suggMap:
        callback.done()
        dcfg = display.DisplayConfig()
        dcfg.setTroveDisplay(fullFlavors = cfg.fullFlavors,
                             fullVersions = cfg.fullVersions,
                             showLabels = cfg.showLabels)
        formatter = display.TroveTupFormatter(dcfg)

        print("Including extra troves to resolve dependencies:")
        print("   ", end=' ')

        items = sorted(set(formatter.formatNVF(*x)
                       for x in itertools.chain(*iter(suggMap.values()))))
        print(" ".join(items))

    askInteractive = cfg.interactive
    if restartInfo:
        callback.done()
        newJobs = set(itertools.chain(*updJob.getJobs()))
        oldJobs = set(updJob.getItemList())
        addedJobs = newJobs - oldJobs
        removedJobs = oldJobs - newJobs

        if not model and addedJobs or removedJobs:
            print('NOTE: after critical updates were applied, the contents of the update were recalculated:')
            displayChangedJobs(addedJobs, removedJobs, cfg)
        else:
            askInteractive = False

    if not updJob.jobs:
        # Nothing to do
        print('Update would not modify system')
        if model and not kwargs.get('test'):
            # Make sure 'conary sync' clears model.next even if nothing needs
            # to be done.
            modelFile.closeSnapshot()
        updJob.close()
        client.close()
        return

    elif askInteractive:
        print('The following updates will be performed:')
        displayUpdateInfo(updJob, cfg, noRestart=noRestart)

    if migrate and cfg.interactive:
        print ('Migrate erases all troves not referenced in the groups'
               ' specified.')

    if askInteractive:
        if migrate:
            style = 'migrate'
        else:
            style = 'update'
        okay = cmdline.askYn('continue with %s? [Y/n]' % style, default=True)
        if not okay:
            updJob.close()
            client.close()
            return

    if not noRestart and updJob.getCriticalJobs():
        print("Performing critical system updates, will then restart update.")
    try:
        restartDir = client.applyUpdateJob(updJob, **applyKwargs)
    finally:
        updJob.close()
        client.close()

    if restartDir:
        params = sys.argv

        # Write command line to disk
        import xmlrpc.client
        cmdlinefile = open(os.path.join(restartDir, 'cmdline'), "w")
        cmdlinefile.write(xmlrpc.client.dumps((params, ), methodresponse = True))
        cmdlinefile.close()

        # CNY-980: we should have the whole script of changes to perform in
        # the restart directory (in the job list); if in migrate mode, re-exec
        # as regular update
        if migrate and 'migrate' in params:
            params[params.index('migrate')] = 'update'

        params.extend(['--restart-info=%s' % restartDir])
        client.close()
        raise errors.ReexecRequired(
                'Critical update completed, rerunning command...', params,
                restartDir)
    else:
        if (not kwargs.get('test', False)) and model:
            modelFile.closeSnapshot()


class UpdateAllFormatter(object):
    def formatNVF(self, name, version, flavor):
        if version and (flavor is not None) and not flavor.isEmpty():
            return "'%s=%s[%s]'" % (name, version.asString(), deps.formatFlavor(flavor))
        if (flavor is not None) and not flavor.isEmpty():
            return "'%s[%s]'" % (name, deps.formatFlavor(flavor))
        if version:
            return "%s=%s" % (name, version.asString())
        return name

def updateAll(cfg, **kwargs):
    showItems = kwargs.pop('showItems', False)
    restartInfo = kwargs.get('restartInfo', None)
    migrate = kwargs.pop('migrate', False)
    modelArg = kwargs.pop('model', False)
    modelFile = kwargs.get('systemModelFile', None)
    model = kwargs.get('systemModel', None)
    infoArg = kwargs.get('info', False)

    if model and modelFile and modelFile.exists() and restartInfo is None:
        model.refreshVersionSnapshots()
        if modelArg:
            model.write(sys.stdout)
            sys.stdout.flush()
            return None

    kwargs['installMissing'] = kwargs['removeNotByDefault'] = migrate
    if 'callback' not in kwargs or not kwargs.get('callback'):
        kwargs['callback'] = UpdateCallback(cfg)
    # load trove cache only if --info provided
    kwargs['loadTroveCache'] = infoArg

    client = conaryclient.ConaryClient(cfg)
    # We want to be careful not to break the old style display, for whoever
    # might have a parser for that output.
    withLongDisplay = (cfg.fullFlavors or cfg.fullVersions or cfg.showLabels)
    formatter = UpdateAllFormatter()
    if restartInfo or (model and modelFile and modelFile.exists()):
        updateItems = []
        applyList = None
    else:
        if showItems and withLongDisplay:
            updateItems = client.getUpdateItemList()
            dcfg = display.DisplayConfig()
            dcfg.setTroveDisplay(fullFlavors = cfg.fullFlavors,
                                 fullVersions = cfg.fullVersions,
                                 showLabels = cfg.showLabels)
            formatter = display.TroveTupFormatter(dcfg)
        else:
            updateItems = client.fullUpdateItemList()
            applyList = [ (x[0], (None, None), x[1:], True) for x in updateItems ]

    if showItems:
        for (name, version, flavor) in sorted(updateItems, key=lambda x:x[0]):
            print(formatter.formatNVF(name, version, flavor))
        return

    _updateTroves(cfg, applyList, **kwargs)
    # Clean up after ourselves
    if restartInfo:
        util.rmtree(restartInfo, ignore_errors=True)

def changePins(cfg, troveStrList, pin = True,
               systemModel = None, systemModelFile = None,
               callback = None):
    client = conaryclient.ConaryClient(cfg)
    client.checkWriteableRoot()
    troveList = []
    for item in troveStrList:
        name, ver, flv = parseTroveSpec(item)
        troves = client.db.findTrove(None, (name, ver, flv))
        troveList += troves

    client.pinTroves(troveList, pin = pin)

    if systemModel and systemModelFile and not pin:
        doModelUpdate(cfg, systemModel, systemModelFile, [], callback=callback)


def revert(cfg):
    conaryclient.ConaryClient.revertJournal(cfg)
