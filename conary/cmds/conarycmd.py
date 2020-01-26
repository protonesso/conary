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
The conary main program.
"""

import sys
if sys.version_info < (2, 6):
    print("error: python 2.6 or later is required")
    sys.exit(1)

#stdlib
import optparse
import os
import errno

#conary
import conary
from conary import callbacks, command, conarycfg, constants
from conary import conaryclient
from conary.cmds import commit
from conary.cmds import cscmd
from conary.cmds import query
from conary.cmds import queryrep
from conary.cmds import rollbacks
from conary.cmds import search
from conary.cmds import showchangeset
from conary.cmds import updatecmd
from conary.cmds import verify
from conary.lib import cfgtypes,log, openpgpfile, options, util
from conary.local import database
from conary.conaryclient import cml
from conary.conaryclient import systemmodel
from conary.repository import trovesource

if __name__ == '__main__':
    sys.excepthook = util.genExcepthook(debug=True)
    sys.stdout = util.FileIgnoreEpipe(sys.stdout)

def openDatabase(root, path):
    return database.Database(root, path)

(NO_PARAM,  ONE_PARAM)  = (options.NO_PARAM, options.ONE_PARAM)
(OPT_PARAM, MULT_PARAM) = (options.OPT_PARAM, options.MULT_PARAM)
(NORMAL_HELP, VERBOSE_HELP)  = (options.NORMAL_HELP, options.VERBOSE_HELP)

_commands = []
def _register(cmd):
    _commands.append(cmd)


class _CallbackCommand(object):
    docs = {'json': (VERBOSE_HELP, 'Format output into json')}

    def addParameters(self, argDef):
        argDef[self.defaultGroup]['json'] = NO_PARAM

    def getCallback(self, cfg, argSet, *args, **kwargs):
        if cfg.quiet or 'quiet' in argSet:
            return callbacks.UpdateCallback()
        elif 'json' in argSet:
            return updatecmd.JsonUpdateCallback(cfg, *args, **kwargs)
        else:
            return updatecmd.UpdateCallback(cfg, *args, **kwargs)


class ConaryCommand(command.ConaryCommand):
    paramHelp = ''
    defaultGroup = 'Common Options'
    commandGroup = 'Miscellaneous Commands'
    ignoreConfigErrors = False

    def addConfigOptions(self, cfgMap, argDef):
        cfgMap['components'] = 'showComponents', NO_PARAM
        cfgMap['exclude-troves'] = 'excludeTroves', ONE_PARAM
        cfgMap['labels'] = 'showLabels', NO_PARAM,
        cfgMap['trust-threshold'] = 'trustThreshold', ONE_PARAM
        command.ConaryCommand.addConfigOptions(self, cfgMap, argDef)

class ChangeSetCommand(ConaryCommand, _CallbackCommand):
    commands = ['changeset', 'cs' ]
    help = 'Request a changeset from one or more Conary repositories'
    paramHelp = "<pkg>[=[<oldver>--]<newver>]+ <outfile>"
    description = """\
Creates a changeset with the specified troves and stores it in <outfile>"""
    commandGroup = 'Repository Access'
    docs = {'no-recurse' : (VERBOSE_HELP, 
                            "Don't include child troves in changeset")}
    hidden = True

    def addParameters(self, argDef):
        ConaryCommand.addParameters(self, argDef)
        _CallbackCommand.addParameters(self, argDef)
        argDef['no-recurse'] = NO_PARAM

    def runCommand(self, cfg, argSet, otherArgs):
        kwargs = {}

        callback = self.getCallback(cfg, argSet)
        if 'quiet' in argSet:
            del argSet['quiet']
        if 'json' in argSet:
            del argSet['json']
        kwargs['callback'] = callback

        kwargs['recurse'] = not('no-recurse' in argSet)
        if not kwargs['recurse']:
            del argSet['no-recurse']

        if len(otherArgs) < 4 or argSet:
            return self.usage()

        outFile = otherArgs[-1]
        del otherArgs[-1]

        cscmd.ChangeSetCommand(cfg, otherArgs[2:], outFile, **kwargs)
_register(ChangeSetCommand)

class CommitCommand(ConaryCommand):
    commands = ['commit']
    paramHelp = "<changeset>"
    help = 'Commit a changeset to a Conary repository'
    docs = {'target-branch' : ('commit to branch BRANCH', 'BRANCH')}
    commandGroup = 'Repository Access'
    hidden = True

    def addParameters(self, argDef):
        ConaryCommand.addParameters(self, argDef)
        argDef["target-branch"] = ONE_PARAM

    def runCommand(self, cfg, argSet, otherArgs):
        targetBranch = None
        if 'target-branch' in argSet:
            targetBranch  = argSet['target-branch']
            del argSet['target-branch']
        if len(otherArgs) < 3: return self.usage()
        for changeSet in otherArgs[2:]:
            commit.doCommit(cfg, changeSet, targetBranch)
_register(CommitCommand)

class EmergeCommand(ConaryCommand):
    commands = ['emerge']
    paramHelp = "<troveName>+"
    help = 'Build software from source and install it on the system'
    commandGroup = 'System Modification'
    hidden = True

    docs = {'no-deps' : 'Do not check to see if buildreqs are installed' }

    def addParameters(self, argDef):
        ConaryCommand.addParameters(self, argDef)
        argDef["no-deps"] = NO_PARAM

    def runCommand(self, cfg, argSet, otherArgs):
        # import this late to reduce the dependency set for
        # the main conary command in the common case.  This lets
        # conary run even if, for example, libelf is missing
        try:
            from conary.build import cook
        except ImportError:
            log.error('Conary build tools not installed - cannot build packages.  Please install conary-build')
            return 1
        try:
            import pwd
        except ImportError:
            log.error("emerge requires a POSIX operating system")
            return 1

        log.setVerbosity(log.INFO)

        if not os.getuid():
            # if we're running as root, switch to the emergeUser
            cookUser = cfg.emergeUser
            if not cookUser:
                log.error('Please set emergeUser config item before emerging')
                return 1
            try:
                info = pwd.getpwnam(cookUser)
            except KeyError:
                log.error("No user named '%s' to emerge with.  Please check "
                          "the 'emergeUser' config item." % cookUser)
                return 1
            if info.pw_dir == '/':
                log.error("The '%s' user's home directory is set to '/'.  "
                          "See http://wiki.rpath.com/wiki/Conary:Conversion for "
                          "assistance."
                          % cookUser)
                return 1
            if info.pw_uid == 0 or info.pw_gid == 0:
                log.error("The '%s' user has root privileges." % cookUser)
                return 1
            cookIds = (info.pw_uid, info.pw_gid)
            # reset the HOME environment variable, and reinterpolate
            # buildPath, lookaside, and logFile configuration variables
            # so that the correct home directory will be used.
            os.environ['HOME'] = info.pw_dir
            for cfgvar in ('buildPath', 'lookaside'):
                unexpanded = cfg[cfgvar]._getUnexpanded()
                cfg.configLine('%s %s' %(cfgvar, unexpanded))
            cfg.configLine('logFile ' +
                ':'.join(x._getUnexpanded() for x in cfg.logFile))
        else:
            cookIds = None

        ignoreDeps = argSet.pop('no-deps', False)
        if argSet: return self.usage()

        try:
            return cook.cookCommand(cfg, otherArgs[2:], False, {},
                                    emerge = True, cookIds=cookIds,
                                    ignoreDeps=ignoreDeps)
        except cook.CookError as msg:
            log.error(msg)
            return 1
_register(EmergeCommand)


class LocalChangeSetCommand(ConaryCommand):
    commands = ['localcs']
    paramHelp = "<pkgname>[=<version>][[flavor]] <outfile>"
    help = 'Create a changeset that represents local changes'
    hidden = True

    def runCommand(self, cfg, argSet, otherArgs):
        if len(otherArgs) != 4 and len(otherArgs) != 4:
            return self.usage()

        name = otherArgs[2]
        outFile = otherArgs[3]

        db = database.Database(cfg.root, cfg.dbPath)
        verify.LocalChangeSetCommand(db, cfg, name, changeSetPath = outFile)
_register(LocalChangeSetCommand)


class LocalCommitCommand(ConaryCommand):
    commands = ['localcommit']
    paramHelp = "<changeset>"
    help = 'Apply a changeset generated by localcs to the system'
    commandGroup = 'System Modification'
    hidden = True
    def runCommand(self, cfg, argSet, otherArgs):
        if len(otherArgs) < 3: return self.usage()
        db = database.Database(cfg.root, cfg.dbPath)
        for changeSet in otherArgs[2:]:
            commit.doLocalCommit(db, changeSet)
_register(LocalCommitCommand)


class PinUnpinCommand(ConaryCommand, _CallbackCommand):
    commands = ['pin', 'unpin']
    paramHelp = "<pkgname>[=<version>][[flavor]]+"
    hidden = True
    commandGroup = 'System Modification'

    docs = {
        'ignore-model'  : (VERBOSE_HELP,
                           'Do not use the system-model file, even if present'),
    }

    def addParameters(self, argDef):
        ConaryCommand.addParameters(self, argDef)
        _CallbackCommand.addParameters(self, argDef)
        d = {}
        d["ignore-model"] = NO_PARAM
        argDef['Update Options'] = d

    def runCommand(self, cfg, argSet, otherArgs):
        ignoreModel = argSet.pop('ignore-model', False)
        if argSet: return self.usage()

        pin = otherArgs[1] == 'pin'
        kwargs = {
            'pin': pin,
            'systemModel': False,
            'systemModelFile': None,
        }

        if not pin and not ignoreModel:
            # if system model is present, unpin implies sync
            systemModel = cml.CML(cfg)
            modelFile = systemmodel.SystemModelFile(systemModel)

            callback = self.getCallback(cfg, argSet, modelFile=modelFile)
            kwargs['callback'] = callback
            if modelFile.exists():
                kwargs['systemModel'] = systemModel
                kwargs['systemModelFile'] = modelFile

        updatecmd.changePins(cfg, otherArgs[2:], **kwargs)

class PinCommand(PinUnpinCommand):
    commands = ['pin']
    help = 'Pin software on the system'
_register(PinCommand)

class UnPinCommand(PinUnpinCommand):
    commands = ['unpin']
    help = 'Unpin software on the system'
_register(UnPinCommand)

class RemoveCommand(ConaryCommand):
    commands = ['remove', 'rm']
    paramHelp = "<path>+"
    help = 'Remove a file the system and Conary control'
    commandGroup = 'System Modification'
    hidden = True
    ignoreConfigErrors = True

    def runCommand(self, cfg, argSet, otherArgs):
        if len(otherArgs) < 3: return self.usage()
        if argSet: return self.usage()

        client = conaryclient.ConaryClient(cfg)
        db = client.db
        log.syslog.command()

        rc = 0

        pathList = []

        for path in otherArgs[2:]:
            if not path:
                log.error("remove cannot handle an empty path")
                return 1

            if path[0] == '/':
                fullPath = util.joinPaths(cfg.root, path)
            else:
                fullPath = os.path.realpath(path)
                if fullPath.startswith(cfg.root) and cfg.root != '/':
                    # fix up the path to be relative to the root dir
                    path = fullPath[len(cfg.root):]
                    if path[0] != '/':
                        # the root ended with a /
                        path = '/' + fullPath

            pathList.append((fullPath, path))

        try:
            db.removeFiles([ x[1] for x in pathList ])
        except conary.errors.DatabaseError as e:
            log.error(str(e))
            return 1
        except OSError as e:
            log.error("cannot remove %s: %s" % (fullPath, e.strerror))
            return 1

        log.syslog.commandComplete()
_register(RemoveCommand)

class RepairCommand(ConaryCommand):
    commands = ['repair']
    paramHelp = "<pkgname>[=<version>][[flavor]]*"
    help = ('Undo local changes to files owned by troves (EXPERIMENTAL)')
    commandGroup = 'System Modification'
    hidden = True
    ignoreConfigErrors = True

    def runCommand(self, cfg, argSet, otherArgs):
        if len(otherArgs) < 3: return self.usage()
        if argSet: return self.usage()

        client = conaryclient.ConaryClient(cfg)
        db = client.db
        log.syslog.command()

        troveList = []
        for item in otherArgs[2:]:
            name, ver, flv = updatecmd.parseTroveSpec(item)
            troves = client.db.findTrove(None, (name, ver, flv))
            troveList += troves

        try:
            db.repairTroves(client.getRepos(), troveList)
        except conary.errors.DatabaseError as e:
            log.error(str(e))
            return 1
        except OSError as e:
            log.error("cannot remove %s: %s" % (fullPath, e.strerror))
            return 1

        log.syslog.commandComplete()
_register(RepairCommand)

class RestoreCommand(ConaryCommand):
    commands = ['restore']
    paramHelp = "<pkgname>[=<version>][[flavor]]*"
    help = 'Restore files which have been removed from installed troves'
    commandGroup = 'System Modification'
    hidden = True
    ignoreConfigErrors = True

    def runCommand(self, cfg, argSet, otherArgs):
        if len(otherArgs) < 3: return self.usage()
        if argSet: return self.usage()

        client = conaryclient.ConaryClient(cfg)
        db = client.db
        log.syslog.command()

        troveList = []
        for item in otherArgs[2:]:
            name, ver, flv = updatecmd.parseTroveSpec(item)
            troves = client.db.findTrove(None, (name, ver, flv))
            troveList += troves

        try:
            db.restoreTroves(client.getRepos(), troveList)
        except conary.errors.DatabaseError as e:
            log.error(str(e))
            return 1
        except OSError as e:
            log.error("cannot remove %s: %s" % (fullPath, e.strerror))
            return 1

        log.syslog.commandComplete()
_register(RestoreCommand)

class RevertCommand(ConaryCommand):
    commands = ['revert']
    commandGroup = 'System Modification'
    help = 'Revert the journal from a failed operation.'
    hidden = True
    ignoreConfigErrors = True

    def runCommand(self, cfg, argSet, otherArgs):
        if argSet or len(otherArgs) > 2: return self.usage()
        updatecmd.revert(cfg)
_register(RevertCommand)

class ListRollbackCommand(ConaryCommand):
    commands = ['rblist']
    commandGroup = 'Information Display'
    help = 'List the rollbacks available in the rollback stack'
    ignoreConfigErrors = True

    def runCommand(self, cfg, argSet, otherArgs):
        if argSet or len(otherArgs) > 2: return self.usage()
        db = openDatabase(cfg.root, cfg.dbPath)
        rollbacks.listRollbacks(db, cfg)
_register(ListRollbackCommand)

class RollbackCommand(ConaryCommand, _CallbackCommand):
    commands = ['rollback', 'rb']
    help = "Roll back operations stored in the rollback stack"
    paramHelp = "[<num changes>|r.<rollback point>]"
    description = """\
conary rollback 1       # Roll back the most recent change
conary rollback r.151   # Roll back a specific operation

See "conary rblist" for a list of changes to roll back.
The argument can be either a number of changes to rollback
("conary rollback 1" will rollback the most recent operation) or a
specific item in the rollback list ("conary rollback r.151" will
rollback operation #151 and all later operations)."""
    commandGroup = 'System Modification'

    docs = {
            'from-file'     : (VERBOSE_HELP, 'search changeset(s) (or directories) for capsule contents'),
            'just-db'       : (VERBOSE_HELP,
                          'Update db only - Do not modify rest of file system'),
        'replace-files' : (VERBOSE_HELP, 'Replace existing files if file conflict found '
                          '(equivalent to --replace-managed-files '
                          '--replace-modified-files --replace-unmanaged-files)'),
        'replace-managed-files':
            "Files changed in this rollback may replace files owned by other "
            "troves",
        'replace-modified-files':
            "Non-config files changed in this rollback may replace files which "
            "have been altered outside Conary",
        'replace-unmanaged-files':
            "Files changed in this rollback may replace files on the filesystem "
            "which are not owned by any trove",
            'no-scripts': ('Do not run trove scripts'),
            'tag-script': ('Output commands to run tag-script to PATH', 'PATH'),
            'abort-on-error' : ('Abort the rollback if any prerollback script '
                                'fails'),
           }

    def addParameters(self, argDef):
        ConaryCommand.addParameters(self, argDef)
        _CallbackCommand.addParameters(self, argDef)
        argDef["from-file"] = MULT_PARAM
        argDef["just-db"] = NO_PARAM
        argDef["replace-files"] = NO_PARAM
        argDef["replace-managed-files"] = NO_PARAM
        argDef["replace-modified-files"] = NO_PARAM
        argDef["replace-unmanaged-files"] = NO_PARAM
        argDef["info"] = NO_PARAM
        argDef["no-scripts"] = NO_PARAM
        argDef["tag-script"] = ONE_PARAM
        argDef["abort-on-error"] = NO_PARAM

    def runCommand(self, cfg, argSet, otherArgs):
        kwargs = {}
        kwargs['replaceManagedFiles'] = argSet.pop('replace-managed-files', False)
        kwargs['replaceModifiedFiles'] = argSet.pop('replace-modified-files', False)
        kwargs['replaceUnmanagedFiles'] = argSet.pop('replace-unmanaged-files', False)
        if argSet.pop('replace-files', False):
            kwargs['replaceManagedFiles'] = True
            kwargs['replaceModifiedFiles'] = True
            kwargs['replaceUnmanagedFiles'] = True

        kwargs['tagScript'] = argSet.pop('tag-script', None)
        kwargs['justDatabase'] = argSet.pop('just-db', False)
        kwargs['noScripts'] = argSet.pop('no-scripts', False)
        kwargs['showInfoOnly'] = argSet.pop('info', False)
        kwargs['abortOnError'] = argSet.pop('abort-on-error', False)
        kwargs['capsuleChangesets'] = argSet.pop('from-file', [])

        kwargs['callback'] = self.getCallback(cfg, argSet)
        if 'json' in argSet:
            del argSet['json']

        if argSet or len(otherArgs) != 3: return self.usage()
        db = openDatabase(cfg.root, cfg.dbPath)
        client = conaryclient.ConaryClient(cfg)
        client.applyRollback(otherArgs[2], **kwargs)
_register(RollbackCommand)

class RemoveRollbackCommand(ConaryCommand):
    commands = ['rmrollback', 'rmrb']
    paramHelp = "<rollback>"
    help = 'Remove old rollbacks'
    commandGroup = 'System Modification'
    ignoreConfigErrors = True
    hidden = True

    def addParameters(self, argDef):
        ConaryCommand.addParameters(self, argDef)

    def runCommand(self, cfg, argSet, otherArgs):
        if argSet or len(otherArgs) != 3: return self.usage()
        db = openDatabase(cfg.root, cfg.dbPath)
        rollbacks.removeRollbacks(db, otherArgs[2])
_register(RemoveRollbackCommand)

class _AbstractQueryCommand(ConaryCommand):

    docs = {'all-troves'    : '', # Meaning changes based on command
            'buildreqs'     : (VERBOSE_HELP,
                               'Display troves used to satisfy buildreqs'
                               ' of this trove'),
            'capsules'      : (VERBOSE_HELP,
                               'Display only encapsulated packages in file '
                               'lists'),
            'deps'          : 'Display trove dependencies',
            'file-deps'     : 'Display dependencies for individual files',
            'file-flavors'  : (VERBOSE_HELP,
                               'Display flavors for individual files'),
            'file-versions' : (VERBOSE_HELP, 'Display file versions'),
            'ids'           : (VERBOSE_HELP,
                               'Display path and file ids for files'),
            'info'          : 'Display detailed trove info',
            'ls'            : 'List files',
            'lsl'           : 'List file details like ls -l',
            'no-pristine'   : optparse.SUPPRESS_HELP,
            'no-recurse'    : (VERBOSE_HELP, 
                               'Force no recursive display of child troves'),
            'path'          : (VERBOSE_HELP, 'Display trove that owns PATH'),
            'recurse'       : (VERBOSE_HELP,
                              'Force recursive display of child troves'),
            'sha1s'         : (VERBOSE_HELP,
                              'Display checksums for original file contents'),
            'signatures'    : (VERBOSE_HELP,
                               'Display any signatures on this trove'),
            'tags'          : (VERBOSE_HELP, 'Display tags for files'),
            'troves'        : (VERBOSE_HELP, 
                               'Display one level of child troves'),
            'trove-flags'   : (VERBOSE_HELP, 'Display trove flags (see man page)'),
            'exact-flavors' : 'Return only troves that match the flavors specified exactly',
            'trove-headers' : (VERBOSE_HELP,
                               'Display current trove name when listing info'
                              ' from multiple troves'),
            'weak-refs'     : optparse.SUPPRESS_HELP,
            'what-provides' : (VERBOSE_HELP, 'Return information about what troves provide a dependency', 'DEP'),
            'what-requires' : (VERBOSE_HELP, 'Return information about what troves require a dependency', 'DEP')
            }




    def addParameters(self, argDef):
        ConaryCommand.addParameters(self, argDef)

        d = {}
        d["capsules"] = NO_PARAM
        d["ids"] = NO_PARAM
        d["ls"] = NO_PARAM
        d["lsl"] = NO_PARAM
        d["tags"] = NO_PARAM
        d["file-deps"] = NO_PARAM
        d["file-flavors"] = NO_PARAM
        d["file-versions"] = NO_PARAM
        d["sha1s"] = NO_PARAM
        argDef['File Display'] = d

        d = {}
        d["buildreqs"] = NO_PARAM
        d["deps"] = NO_PARAM
        d["info"] = '-i', NO_PARAM
        d["signatures"] = NO_PARAM
        d["trove-flags"] = NO_PARAM
        argDef['Info Display'] = d

        d = {}
        d["all-troves"] = NO_PARAM
        d["recurse"] = NO_PARAM
        d["no-recurse"] = NO_PARAM
        d["troves"] = NO_PARAM
        d["trove-headers"] = NO_PARAM
        d["weak-refs"] = NO_PARAM
        argDef['Child Display'] = d

        d = {}
        d["exact-flavors"] = NO_PARAM
        argDef['Trove Selection'] = d

    def runCommand(self, cfg, argSet, otherArgs):
        raise NotImplementedError

class QueryCommand(_AbstractQueryCommand):

    commands = [ 'query', 'q' ]
    paramHelp = "<pkgname>[=<version>][[flavor]]*"
    help = 'Query the local system database'
    docs = {'all-troves' : 'Display troves that are not installed'}
    commandGroup = 'Information Display'
    ignoreConfigErrors = True

    def addParameters(self, argDef):
        _AbstractQueryCommand.addParameters(self, argDef)
        d = {}
        d["no-pristine"] = NO_PARAM
        d["path"] = MULT_PARAM
        d["what-provides"] = MULT_PARAM
        argDef['Trove Selection'].update(d)

    def runCommand(self, cfg, argSet, otherArgs):
        kw = {}
        for opt in ('capsules', 'ls', 'lsl', 'ids', 'sha1s', 'tags',
                ('signatures', 'digSigs'), ('buildreqs', 'showBuildReqs'),
                ('file-deps', 'fileDeps'), 
                ('file-flavors', 'fileFlavors'), 
                ('file-versions', 'fileVersions'),
                ('all-troves', 'showAllTroves'),
                ('weak-refs', 'weakRefs'),
                ('troves', 'showTroves'),
                ('trove-headers', 'alwaysDisplayHeaders'),
                ('trove-flags', 'showTroveFlags'),
                ('deps', 'showDeps'), ('exact-flavors', 'exactFlavors'),
                'info'):
            if isinstance(opt, tuple):
                kw[opt[1]] = argSet.pop(opt[0], False)
            else:
                kw[opt] = argSet.pop(opt, False)
        kw['pathList'] = argSet.pop('path', [])
        kw['whatProvidesList'] = argSet.pop('what-provides', [])
        kw['pristine'] = not argSet.pop('no-pristine', False)
        kw['recurse'] = argSet.pop('recurse', None)
        if 'no-recurse' in argSet:
            kw['recurse'] = False
            del argSet['no-recurse']


        db = openDatabase(cfg.root, cfg.dbPath)

        if argSet: return self.usage()

        return query.displayTroves(db, cfg, otherArgs[2:], **kw)
_register(QueryCommand)


class RepQueryCommand(_AbstractQueryCommand):

    commands = [ 'repquery', 'rq' ]
    paramHelp = "<pkgname>[=<version>][[flavor]]*"
    help = 'Query one or more Conary repositories'
    commandGroup = 'Information Display'
    docs = {'affinity'    : ('Use branch and flavor affinity when searching'
                             ' for troves (behave like conary update)'),
            'all-troves'   : 'Display byDefault False troves',
            'all-flavors'  : 'Return all flavors of troves matching the query',
            'all-versions' : 'Return all versions of troves that'
                             ' match the query',
            'available-flavors' : ('Return all flavors that match system flavor'
                                  ' and the query (and affinity if specified)'),
            'best-flavors' : optparse.SUPPRESS_HELP, # default
            'latest-versions': optparse.SUPPRESS_HELP, # default
            'leaves'         : 'Return latest version for each unique flavor',
            'show-removed'   : 'Display troves that have been removed from the repos',
            'show-normal'    : 'Do not display branches that end in a redirect',
            'show-file'      : 'Display contents of a file',
            'build-log'      : 'Display log of the build for this package',
            #'show-redirects' : 'Display redirect troves'
            # turned off for now.
            }

    def addParameters(self, argDef):
        _AbstractQueryCommand.addParameters(self, argDef)
        d = {}
        d["affinity"] = NO_PARAM
        d["all-flavors"] = NO_PARAM
        d["all-versions"] = NO_PARAM
        d["available-flavors"] = NO_PARAM
        d["best-flavors"] = NO_PARAM
        d["build-log"] = NO_PARAM
        d["latest-versions"] = NO_PARAM
        d["leaves"] = NO_PARAM
        d["path"] = MULT_PARAM
        d["what-provides"] = MULT_PARAM
        d["show-removed"] = NO_PARAM
        d["show-normal"] = NO_PARAM
        d["show-file"] = ONE_PARAM
        #d["show-redirects"] = NO_PARAM
        argDef['Trove Selection'].update(d)

    def runCommand(self, cfg, argSet, otherArgs):
        versionOpts = [argSet.pop(x, False) for x in ['all-versions',
                                                      'latest-versions',
                                                      'leaves']]
        if versionOpts.count(True) > 1:
            log.error('Can only specify one of --all-versions,'
                      '--latest-versions, --leaves')
            sys.exit(1)
        if True in versionOpts:
            versionFilterIdx = versionOpts.index(True)
            versionFilter = [queryrep.VERSION_FILTER_ALL,
                             queryrep.VERSION_FILTER_LATEST,
                             queryrep.VERSION_FILTER_LEAVES][versionFilterIdx]
        else:
            versionFilter = queryrep.VERSION_FILTER_LATEST

        flavorOpts = [argSet.pop(x, False) for x in ['all-flavors',
                                                     'available-flavors',
                                                     'best-flavors',
                                                     'exact-flavors']]

        if flavorOpts.count(True) > 1:
            log.error('only one of --all-flavors, --available-flavors, '
                      ' --best-flavors, --exact-flavors may be'
                      ' specified')
            return 1
        if True in flavorOpts:
            flavorFilterIdx = flavorOpts.index(True)
            flavorFilter = [queryrep.FLAVOR_FILTER_ALL,
                            queryrep.FLAVOR_FILTER_AVAIL,
                            queryrep.FLAVOR_FILTER_BEST,
                            queryrep.FLAVOR_FILTER_EXACT][flavorFilterIdx]
        else: # default
            flavorFilter = queryrep.FLAVOR_FILTER_BEST

        kw = {}
        kw['filesToShow'] = []
        for opt in ('capsules', 'ls', 'lsl', 'ids', 'sha1s', 'tags',
                ('signatures', 'digSigs'), ('buildreqs', 'showBuildReqs'),
                ('build-log', 'showBuildLog'),
                ('file-deps', 'fileDeps'), 
                ('file-flavors', 'fileFlavors'), 
                ('file-versions', 'fileVersions'),
                ('all-troves', 'showAllTroves'),
                ('weak-refs', 'weakRefs'),
                ('troves', 'showTroves'),
                ('trove-flags', 'showTroveFlags'),
                ('trove-headers', 'alwaysDisplayHeaders'),
                ('affinity', 'useAffinity'),
                ('deps', 'showDeps'), 'info'):
            if isinstance(opt, tuple):
                kw[opt[1]] = argSet.pop(opt[0], False)
            else:
                kw[opt] = argSet.pop(opt, False)

        kw['recurse'] = argSet.pop('recurse', None)
        if 'no-recurse' in argSet:
            kw['recurse'] = False
            del argSet['no-recurse']
        whatProvidesList = argSet.pop('what-provides', [])

        pathList = argSet.pop('path', [])
        
        files = argSet.pop('show-file', None);
        if files:
            kw['filesToShow'].append(files)

        if kw['filesToShow'] and kw['showBuildLog']:
            print('error: can\'t use --build-log and --show-file together')
            return 1

        if argSet.pop('show-removed', False):
            argSet.pop('show-redirects', False)
            troveTypes = trovesource.TROVE_QUERY_ALL
        elif argSet.pop('show-normal', False):
            troveTypes = trovesource.TROVE_QUERY_NORMAL
        else:
            troveTypes = trovesource.TROVE_QUERY_PRESENT
        kw['troveTypes'] = troveTypes

        troveList = otherArgs[2:]
        queryrep.displayTroves(cfg, troveList, pathList, whatProvidesList,
                               versionFilter, flavorFilter, **kw)
_register(RepQueryCommand)


class RdiffCommand(_AbstractQueryCommand):

    # previous versions included support for show-empty, buildreqs options
    # which were removed when we moved to _AbstractQueryCommand
    commands = [ 'rdiff' ]
    paramHelp = "<pkgname>=<version1>[[flavor1]]--<version2>[[flavor]]"
    help = 'Display differences between troves in the repository'
    commandGroup = 'Information Display'
    docs = {'all-troves'         : 'display not-by-default troves', 
            'diff'               : 'show changes as a git style diff',
            'diff-binaries'      : 'include changed contents for binary files',
            'show-changes'       : ('display changes'
                                    ' that the relative changeset contains'),
           }
    hidden = True
    ignoreConfigErrors = True

    def addParameters(self, argDef):
        _AbstractQueryCommand.addParameters(self, argDef)
        
        argDef['Changeset Display'] = {"all":           NO_PARAM,
                                       "diff":          NO_PARAM,
                                       "diff-binaries": NO_PARAM,
                                       "show-changes":  NO_PARAM }

    def runCommand(self, cfg, argSet, otherArgs):
        kw = {}
        # FIXME: this overlaps with showchange
        for opt in ('capsules', 'ls', 'lsl', 'ids', 'sha1s', 'tags',
                ('signatures', 'digSigs'), ('buildreqs', 'showBuildReqs'),
                ('file-deps', 'fileDeps'), 
                ('file-flavors', 'fileFlavors'), 
                ('file-versions', 'fileVersions'),
                ('all-troves', 'showAllTroves'),
                ('weak-refs', 'weakRefs'),
                ('troves', 'showTroves'),
                ('trove-flags', 'showTroveFlags'),
                ('trove-headers', 'alwaysDisplayHeaders'),
                'deps', 'info', ('show-changes', 'showChanges'),
                ('exact-flavors', 'exactFlavors')):
            if isinstance(opt, tuple):
                kw[opt[1]] = argSet.pop(opt[0], False)
            else:
                kw[opt] = argSet.pop(opt, False)

        kw['recurse'] = argSet.pop('recurse', None)
        kw['asDiff'] = argSet.pop('diff', False)
        kw['diffBinaries'] = argSet.pop('diff-binaries', False)
        if 'no-recurse' in argSet:
            kw['recurse'] = False
            del argSet['no-recurse']

        if argSet or len(otherArgs) != 3:
            return self.usage()

        client = conaryclient.ConaryClient(cfg)
        db = database.Database(cfg.root, cfg.dbPath)
        if queryrep.rdiffCommand(cfg, client, db, otherArgs[2], **kw) == -1:
            return self.usage();

_register(RdiffCommand)

class ShowChangesetCommand(_AbstractQueryCommand):

    commands = [ "showcs", "scs" ]
    paramHelp = "<changeset> <pkgname>[=<version>][[flavor]]*"
    help = 'Display information about a changeset file'
    commandGroup = 'Information Display'
    docs = {'all'                : 'combine many common scs flags',
            'all-troves'         : 'display not-by-default troves', 
            'diff'               : 'show changes as a git style diff',
            'diff-binaries'      : 'include changed contents for binary files',
            'show-changes'       : ('(for relative changeset) display changes'
                                    ' that the relative changeset contains'),
            'recurse-repository' : (VERBOSE_HELP,
                                    'search the repository for troves not '
                                    'contained in the changeset when recursing')
           }
    ignoreConfigErrors = True

    def addParameters(self, argDef):
        _AbstractQueryCommand.addParameters(self, argDef)
        argDef['Changeset Display'] = {"all":           NO_PARAM,
                                       "diff":          NO_PARAM,
                                       "diff-binaries": NO_PARAM,
                                       "show-changes":  NO_PARAM }
        argDef['Child Display']["recurse-repository"] = NO_PARAM

    def runCommand(self, cfg, argSet, otherArgs):
        from conary.repository import changeset
        kw = {}
        # FIXME: organize
        for opt in ('capsules', 'ls', 'lsl', 'ids', 'sha1s', 'tags',
                ('signatures', 'digSigs'), ('buildreqs', 'showBuildReqs'),
                ('file-deps', 'fileDeps'), 
                ('file-flavors', 'fileFlavors'), 
                ('file-versions', 'fileVersions'),
                ('all-troves', 'showAllTroves'),
                ('weak-refs', 'weakRefs'),
                ('troves', 'showTroves'),
                ('trove-flags', 'showTroveFlags'),
                ('trove-headers', 'alwaysDisplayHeaders'),
                ('recurse-repository', 'recurseRepos'),
                'deps', 'info', 'all', ('show-changes', 'showChanges'),
                ('exact-flavors', 'exactFlavors')):
            if isinstance(opt, tuple):
                kw[opt[1]] = argSet.pop(opt[0], False)
            else:
                kw[opt] = argSet.pop(opt, False)


        kw['recurse'] = argSet.pop('recurse', None)
        kw['asDiff'] = argSet.pop('diff', None)
        kw['diffBinaries'] = argSet.pop('diff-binaries', False)
        if 'no-recurse' in argSet:
            kw['recurse'] = False
            del argSet['no-recurse']

        if argSet or len(otherArgs) < 3:
            self.usage()
            return 1

        cspath = otherArgs[2]
        component = None
        if len(otherArgs) > 3:
            component = otherArgs[3:]
        cs = changeset.ChangeSetFromFile(cspath)
        db = database.Database(cfg.root, cfg.dbPath)
        showchangeset.displayChangeSet(db, cs, component, cfg, **kw)
_register(ShowChangesetCommand)


class _UpdateCommand(ConaryCommand, _CallbackCommand):
    paramHelp = "[+][-]<pkgname>[=<version>][[flavor]]* <changeset>*"
    commandGroup = 'System Modification'

    docs = {
        'apply-critical'  : (VERBOSE_HELP, 
                             'apply any critical conary updates and then stop'),
        'disconnected'  : (VERBOSE_HELP,
                           'apply change without using the network connection'),
        'from-file'     : 'search changeset(s) for given troves',
        'just-db'       : (VERBOSE_HELP,
                          'Update db only - Do not modify rest of file system'),
        'keep-existing' : 'Install new troves as new, leaving existing troves.',
        'keep-required' : 'Do not erase troves which are needed by troves '
                          'which will remain installed',
        'exact-flavors' : 'Only match troves whose flavors match exactly',
        'info'          : 'Display what update would have done',
        'ignore-model'  : (VERBOSE_HELP,
                           'Do not use the system-model file, even if present'),
        'model'         : 'Display the new model that would have been applied',
        'model-graph'   : (VERBOSE_HELP,
                           'Write graph of model to specified file'),
        'model-trace'   : (VERBOSE_HELP,
                           'Display model actions involving specified troves'),
        'no-deps'       : 'Do not raise errors due to dependency failures',
        'no-recurse'    : (VERBOSE_HELP, 
                           'Do not install/erase children of specified trove'),
        'no-resolve'    : (VERBOSE_HELP,
                           'Do not attempt to solve dependency problems'),
        'no-conflict-check' : (VERBOSE_HELP,
                               'Ignore potential path conflicts'),
        'no-restart'    : optparse.SUPPRESS_HELP,
        'no-scripts'    : (VERBOSE_HELP,
                           'Do not run trove scripts'),
        'recurse'       : optparse.SUPPRESS_HELP,
        'replace-files' : (VERBOSE_HELP, 'Replace existing files if file conflict found '
                          '(equivalent to --replace-managed-files '
                          '--replace-modified-files --replace-unmanaged-files)'),
        'replace-config-files':
            "Config files changed in this update may replace files which have "
            "been altered outside Conary, instead of merging",
        'replace-managed-files':
            "Files changed in this update may replace files owned by other "
            "troves",
        'replace-modified-files':
            "Non-config files changed in this update may replace files which "
            "have been altered outside Conary",
        'replace-unmanaged-files':
            "Files changed in this update may replace files on the filesystem "
            "which are not owned by any trove",
        'resolve'       : 'Add troves to update to solve dependency problems',
        'restart'       : (VERBOSE_HELP,
                           'Restart after applying a critical update'),
        'restart-info'  : optparse.SUPPRESS_HELP,
        'sync-to-parents' : (VERBOSE_HELP,
                            'Install already referenced versions of troves'),
        'tag-script': ('Output commands to run tag-script to PATH', 'PATH'),
        'test'      : 'Run through whole update but do not modify system',
        'update-only' :(VERBOSE_HELP, 'Do not install any new troves'),
    }

    def addParameters(self, argDef):
        ConaryCommand.addParameters(self, argDef)
        _CallbackCommand.addParameters(self, argDef)
        d = {}
        d["apply-critical"] = NO_PARAM
        d["disconnected"] = NO_PARAM
        d["from-file"] = MULT_PARAM
        d["just-db"] = NO_PARAM
        d["keep-existing"] = NO_PARAM
        d["keep-journal"] = NO_PARAM            # don't document this
        d["keep-required"] = NO_PARAM
        d["info"] = '-i', NO_PARAM
        d["ignore-model"] = NO_PARAM
        d["model"] = NO_PARAM
        d["model-graph"] = ONE_PARAM
        d["model-trace"] = MULT_PARAM
        d["no-deps"] = NO_PARAM
        d["no-recurse"] = NO_PARAM
        d["no-resolve"] = NO_PARAM
        d["no-restart"] = NO_PARAM
        d["no-scripts"] = NO_PARAM
        d["no-conflict-check"] = NO_PARAM
        d["recurse"] = NO_PARAM
        d["replace-files"] = NO_PARAM
        d["replace-config-files"] = NO_PARAM
        d["replace-managed-files"] = NO_PARAM
        d["replace-modified-files"] = NO_PARAM
        d["replace-unmanaged-files"] = NO_PARAM
        d["resolve"] = NO_PARAM
        d["restart"] = NO_PARAM
        d["sync-to-parents"] = NO_PARAM
        d["tag-script"] = ONE_PARAM
        d["test"] = NO_PARAM

        d["restart-info"] = ONE_PARAM
        d["exact-flavors"] = NO_PARAM

        argDef['Update Options'] = d

    def runCommand(self, cfg, argSet, otherArgs):
        kwargs = { 'systemModel': False }
        model = cml.CML(cfg)
        modelFile = systemmodel.SystemModelFile(model)

        callback = self.getCallback(cfg, argSet, modelFile=modelFile)
        if 'quiet' in argSet:
            del argSet['quiet']
        if 'json' in argSet:
            del argSet['json']
        kwargs['callback'] = callback

        if 'resolve' in argSet:
            cfg.autoResolve = True
            del argSet['resolve']

        kwargs['noRestart'] = argSet.pop('no-restart',
                                         not argSet.pop('restart', False))
        if os.path.normpath(cfg.root) != '/':
            kwargs['noRestart'] = True

        if 'no-resolve' in argSet:
            cfg.autoResolve = False
            del argSet['no-resolve']
        if argSet.pop('keep-required', False):
            cfg.keepRequired = True

        kwargs['applyCriticalOnly'] = argSet.pop('apply-critical', False)
        kwargs['replaceManagedFiles'] = argSet.pop('replace-managed-files',
                                                   False)
        kwargs['replaceUnmanagedFiles'] = argSet.pop('replace-unmanaged-files',
                                                     False)
        kwargs['replaceModifiedFiles'] = argSet.pop('replace-modified-files',
                                                    False)
        kwargs['replaceModifiedConfigFiles'] = \
                        argSet.pop('replace-config-files', False)
        if argSet.pop('replace-files', False):
            kwargs['replaceManagedFiles'] = True
            kwargs['replaceUnmanagedFiles'] = True
            kwargs['replaceModifiedFiles'] = True
            kwargs['replaceModifiedConfigFiles'] = True

        kwargs['depCheck'] = not argSet.pop('no-deps', False)
        kwargs['disconnected'] = argSet.pop('disconnected', False)
        kwargs['fromFiles'] = argSet.pop('from-file', [])
        kwargs['recurse'] = not argSet.pop('no-recurse', False)
        kwargs['checkPathConflicts'] = \
                                not argSet.pop('no-conflict-check', False)
        kwargs['justDatabase'] = argSet.pop('just-db', False)
        kwargs['info'] = argSet.pop('info', False)
        ignoreModel = argSet.pop('ignore-model', False)
        kwargs['model'] = argSet.pop('model', False)
        kwargs['modelGraph'] = argSet.pop('model-graph', None)
        kwargs['modelTrace'] = argSet.pop('model-trace', None)
        kwargs['keepExisting'] = argSet.pop('keep-existing',
            otherArgs[1] == 'install') # install implies --keep-existing
        kwargs['keepJournal'] = argSet.pop('keep-journal', False)
        kwargs['tagScript'] = argSet.pop('tag-script', None)
        kwargs['noScripts'] = argSet.pop('no-scripts', False)
        kwargs['test'] = argSet.pop('test', False)
        kwargs['sync'] = argSet.pop('sync-to-parents', False)
        kwargs['updateOnly'] = argSet.pop('update-only', False)
        kwargs['restartInfo'] = argSet.pop('restart-info', None)
        kwargs['exactFlavors'] = argSet.pop('exact-flavors', False)

        kwargs['updateByDefault']    = otherArgs[1] != 'erase'
        kwargs['migrate']            = otherArgs[1] == 'migrate'

        #
        kwargs['syncChildren'] = False
        kwargs['syncUpdate'] = False
        if ignoreModel or not modelFile.exists():
            # this argument handling does not make sense for a modeled system
            kwargs.pop('model')
            if otherArgs[1] == 'sync':
                    if argSet.pop('current', False):
                        kwargs['syncChildren'] = True
                    else:
                        kwargs['syncUpdate'] = True
                    kwargs['removeNotByDefault'] = argSet.pop('full', False)
            elif otherArgs[1] == 'syncchildren':
                # backwards compatibility.  Don't remove this 
                kwargs['syncChildren'] = True
                kwargs['removeNotByDefault'] = argSet.pop('full', False)

        if kwargs['sync'] and kwargs['fromFiles']:
            log.error("Only one of --sync and --from-file may be used")
            return 1

        if argSet: return self.usage()

        if modelFile.exists() and not ignoreModel:
            if otherArgs[1] == 'sync' and len(otherArgs) > 2:
                log.error('The "sync" command cannot take trove arguments with a system model')
                return 1
            if (otherArgs[1] != 'sync' and modelFile.snapshotExists()
                and kwargs['restartInfo'] is None):
                log.error('The previous update was aborted; resume with "conary sync" or revert with "conary rollback 1"')
                return 1
            if otherArgs[1] == 'migrate':
                # Not entirely obvious what to do with the pre-existing
                # system model.  Keep search lines and remove all the
                # rest, replacing with the migrate arguments?  Remove
                # all search lines?  Add label-based search lines?
                # Add each of the items both as search (with version)
                # and install (without)?  If adding search lines, how
                # should they be ordered?
                log.error('The "migrate" command does not function with a system model')
                return 1
            if 'sync' in kwargs and kwargs['sync']:
                log.error('The --sync-to-parents argument cannot be used with a system model')
                return 1
            if otherArgs[1] == 'patch':
                kwargs['patchSpec'] = otherArgs[2:]
                otherArgs[2:] = []
            retval = updatecmd.doModelUpdate(cfg,
                model, modelFile, otherArgs[2:], **kwargs)
        elif len(otherArgs) >= 3:
            retval = updatecmd.doUpdate(cfg, otherArgs[2:], **kwargs)
        else:
            return self.usage()

        return retval

class UpdateCommand(_UpdateCommand):
    commands = [ "update" ]
    help = 'Update or install software on the system'
_register(UpdateCommand)


class InstallCommand(_UpdateCommand):
    commands = [ "install" ]
    help = 'Install software on the system'
_register(InstallCommand)


class PatchCommand(_UpdateCommand):
    commands = [ "patch" ]
    help = 'Patch software on the system'
    hidden = True
_register(PatchCommand)


class EraseCommand(_UpdateCommand):
    commands = [ "erase" ]
    help = 'Erase software from the system'

    def addParameters(self, argDef):
        # rename Update Options to Erase Options (CNY-1090)
        _UpdateCommand.addParameters(self, argDef)
        d = argDef['Erase Options'] = argDef.pop('Update Options')
        # --restart and --no-restart doesn't make sense in the erase context
        del d['no-restart']
        del d['restart']

_register(EraseCommand)


class SyncCommand(_UpdateCommand, _CallbackCommand):
    commands = ["syncchildren", "sync"]
    paramHelp = "<pkgname>[=<version>][[flavor]]* <changeset>*"
    help = 'Synchronize software on the system'
    hidden = True
    docs = {
            'current' : 'syncs child troves to this trove w/o updating <pkgname>',
            'full'    : 'also remove children that are non-default',
           }
    def addParameters(self, argDef):
        _UpdateCommand.addParameters(self, argDef)
        _CallbackCommand.addParameters(self, argDef)
        sync = argDef.pop('Update Options')
        sync["full"] = NO_PARAM
        sync["current"] = NO_PARAM
        sync["update-only"] = NO_PARAM
        argDef['Sync Options'] = sync

_register(SyncCommand)


class MigrateCommand(_UpdateCommand):

    commands = ["migrate"]
    paramHelp = "<pkgname>[=<version>][[flavor]]*"
    help = 'Migrate the system to a different group'
    hidden = False

    def addParameters(self, argDef):
        _UpdateCommand.addParameters(self, argDef)
        d = argDef.pop('Update Options')
        del d['keep-existing']
        del d['sync-to-parents']
        argDef['Migrate Options'] = d
_register(MigrateCommand)


class UpdateAllCommand(_UpdateCommand, _CallbackCommand):

    commands = ['updateall']
    paramHelp = ''
    docs = {'items': ('Display troves that conary updateall will update '
                      'to upgrade your system')}
    help = 'Update all the software on the system'

    def addParameters(self, argDef):
        ConaryCommand.addParameters(self, argDef)
        _CallbackCommand.addParameters(self, argDef)
        argDef["items"] = NO_PARAM
        argDef["info"] = '-i', NO_PARAM
        argDef["just-db"] = NO_PARAM
        argDef["keep-required"] = NO_PARAM
        argDef["ignore-model"] = NO_PARAM
        argDef["model"] = NO_PARAM
        argDef["model-graph"] = ONE_PARAM
        argDef["model-trace"] = MULT_PARAM
        argDef["no-conflict-check"] = NO_PARAM
        argDef["no-deps"] = NO_PARAM
        argDef["no-resolve"] = NO_PARAM
        argDef["no-restart"] = NO_PARAM
        argDef["replace-files"] = NO_PARAM
        argDef["replace-config-files"] = NO_PARAM
        argDef["replace-managed-files"] = NO_PARAM
        argDef["replace-modified-files"] = NO_PARAM
        argDef["replace-unmanaged-files"] = NO_PARAM
        argDef["resolve"] = NO_PARAM
        argDef["test"] = NO_PARAM
        argDef["restart"] = NO_PARAM
        argDef["restart-info"] = ONE_PARAM
        argDef["apply-critical"] = NO_PARAM

    def runCommand(self, cfg, argSet, otherArgs):
        kwargs = { 'systemModel': False }
        kwargs['restartInfo'] = argSet.pop('restart-info', None)
        kwargs['callback'] = self.getCallback(cfg, argSet)
        if 'json' in argSet:
            del argSet['json']

        model = cml.CML(cfg)
        modelFile = systemmodel.SystemModelFile(model)
        if modelFile.exists() and not argSet.pop('ignore-model', False):
            kwargs['systemModel'] = model
            kwargs['systemModelFile'] = modelFile
            if modelFile.snapshotExists() and kwargs['restartInfo'] is None:
                log.error('The previous update was aborted; resume with "conary sync" or revert with "conary rollback 1"')
                return 1

        kwargs['model'] = argSet.pop('model', False)
        kwargs['modelGraph'] = argSet.pop('model-graph', None)
        kwargs['modelTrace'] = argSet.pop('model-trace', None)

        kwargs['noRestart'] = argSet.pop('no-restart',
                                         not argSet.pop('restart', False))
        if os.path.normpath(cfg.root) != '/':
            kwargs['noRestart'] = True

        if 'info' in argSet:
            kwargs['info'] = True
            del argSet['info']

        if 'items' in argSet:
            kwargs['showItems'] = True
            del argSet['items']

        if 'no-deps' in argSet:
            kwargs['depCheck'] = False
            del argSet['no-deps']

        kwargs["replaceModifiedConfigFiles"] = argSet.pop('replace-config-files', False)
        kwargs["replaceManagedFiles"] = argSet.pop('replace-managed-files', False)
        kwargs["replaceModifiedFiles"] = argSet.pop('replace-modified-files', False)
        kwargs["replaceUnmanagedFiles"] = argSet.pop('replace-unmanaged-files', False)

        if 'replace-files' in argSet:
            del argSet['replace-files']
            kwargs["replaceFiles"] = \
            kwargs["replaceModifiedConfigFiles"] = \
            kwargs["replaceManagedFiles"] = \
            kwargs["replaceModifiedFiles"] = \
            kwargs["replaceUnmanagedFiles"] = True

        if argSet.pop('keep-required', False):
            cfg.keepRequired = True

        kwargs['justDatabase'] = argSet.pop('just-db', False)
        kwargs['applyCriticalOnly'] = argSet.pop('apply-critical', False)

        kwargs['checkPathConflicts'] = \
                                not argSet.pop('no-conflict-check', False)
        if 'no-resolve' in argSet:
            cfg.autoResolve = False
            del argSet['no-resolve']

        if 'resolve' in argSet:
            cfg.autoResolve = True
            del argSet['resolve']

        if 'test' in argSet:
            kwargs['test'] = argSet['test']
            del argSet['test']

        if argSet: return self.usage()

        if len(otherArgs) == 2:
            return updatecmd.updateAll(cfg, **kwargs)
        else:
            return self.usage()
_register(UpdateAllCommand)


class VerifyCommand(ConaryCommand):
    commands = ['verify']
    paramHelp = "<pkgname>[=<version>][[flavor]]*"
    help = 'Verify filesystem matches conary database'
    docs = {'all' : 'verify all troves on system',
            'changeset' : (VERBOSE_HELP,
                           'store the verify output in the named file' ),
            'diff' : 'represent changes as a git style diff',
            'diff-binaries'      : 'include changed contents for binary files',
            'hash' : "verify file contents even if the file's inode has "
                     "not changed",
            'new-files': "look for new files which have been added to the "
                         "system"}
    commandGroup = 'Information Display'
    ignoreConfigErrors = True
    cmdClass = verify.verify

    def addParameters(self, argDef):
        ConaryCommand.addParameters(self, argDef)
        argDef["all"] = NO_PARAM
        argDef["diff"] = NO_PARAM
        argDef["changeset"] = ONE_PARAM
        argDef["diff-binaries"] = NO_PARAM
        argDef["hash"] = NO_PARAM
        argDef["new-files"] = NO_PARAM

    def runCommand(self, cfg, argSet, otherArgs):
        client = conaryclient.ConaryClient(cfg)

        all = argSet.pop('all', False)
        csPath = argSet.pop('changeset', None)
        diff = argSet.pop('diff', False)
        diffBinaries = argSet.pop('diff-binaries', False)
        hash = argSet.pop('hash', False)
        newFiles = argSet.pop('new-files', False)

        if len(otherArgs) < 2 or argSet:
            return self.usage()
        troves = otherArgs[2:]
        if (not all and not troves) or (all and troves):
            return self.usage()

        verify.verify(troves, client.getDatabase(),
                      cfg, all=all, changesetPath=csPath,
                      forceHashCheck=hash, asDiff=diff,
                      diffBinaries = diffBinaries,
                      repos=client.getRepos(),
                      newFiles = newFiles)

_register(VerifyCommand)

class SearchCommand(ConaryCommand):
    commands = ['search']
    paramHelp = "<pkgname>[=<version>][[flavor]]*"
    help = 'Search the system model for available packages'
    docs = {}
    commandGroup = 'Information Display'
    ignoreConfigErrors = True
    cmdClass = verify.verify

    def runCommand(self, cfg, argSet, otherArgs):
        client = conaryclient.ConaryClient(cfg)
        search.search(client, otherArgs[2:])
_register(SearchCommand)

class ConaryMain(command.MainHandler):
    name = 'conary'
    abstractCommand = ConaryCommand
    configClass = conarycfg.ConaryConfiguration

    version = constants.version
    commandList = _commands
    hobbleShortOpts = True

    def usage(self, rc = 1, showAll=False):
        print('Conary Software Configuration Management System')
        if not showAll:
            print()
            print('Common Commands (use "conary help" for the full list)')
        return options.MainHandler.usage(self, rc, showAll=showAll)


    def main(self, argv, debuggerException, **kw):
        # override generic main handler so we can set the
        # "ignoreConfigErrors" flag

        # we have to call _getPreCommandOptions twice.  It's easier
        # this way so we can get rid of any extraneous options that
        # may precede the command name.
        if '--version' in argv:
            print(self.version)
            return

        dummyCfg = self.configClass(readConfigFiles=False)
        try:
            # This can raise an OptionError if the user does something
            # silly such as "conary --some-switch-that-doesnt-exist".
            # We have to handle it here. (CNY-3364)
            cmdArgv = self._getPreCommandOptions(argv, dummyCfg)[1]
        except options.OptionError as oe:
            self.usage()
            print(str(oe))
            return 1
        thisCommand = self.getCommand(cmdArgv, dummyCfg)
        self._ignoreConfigErrors = getattr(thisCommand, 'ignoreConfigErrors', False)
        return command.MainHandler.main(self, argv, debuggerException, **kw)

    def runCommand(self, thisCommand, cfg, argSet, args, debugAll=False):
        if not cfg.buildLabel and cfg.installLabelPath:
            cfg.buildLabel = cfg.installLabelPath[0]

        if cfg.installLabelPath:
            cfg.installLabel = cfg.installLabelPath[0]
        else:
            cfg.installLabel = None

        cfg.initializeFlavors()

        lsprof = False
        if 'lsprof' in argSet:
            import cProfile
            prof = cProfile.Profile()
            prof.enable()
            lsprof = True
            del argSet['lsprof']

        try:
            rv = options.MainHandler.runCommand(self, thisCommand, cfg,
                                                 argSet, args)
        finally:
            if lsprof:
                prof.disable()
                prof.dump_stats('conary.lsprof')
                prof.print_stats()

        if log.errorOccurred():
            return 1

def main(argv=sys.argv):
    try:
        debugAll = '--debug-all' in argv
        if debugAll:
            argv = argv[:]
            argv.remove('--debug-all')
            debuggerException = Exception
        else:
            debuggerException = conary.repository.errors.InternalConaryError

        conaryMain = ConaryMain()
        return conaryMain.main(argv, debuggerException, debugAll=debugAll)
    except IOError as e:
        # allow broken pipe to exit
        if e.errno != errno.EPIPE:
            raise
    except conary.errors.ReexecRequired as e:
        print(e)
        # a restart of this command has been requested for some 
        # reason.  Exit code will now be the exit code of the redirect.
        if e.execParams is not None:
            args = e.execParams
        else:
            args = sys.argv
        util.massCloseFileDescriptors(3, 252)
        os.execve(sys.argv[0], args, os.environ)
    except debuggerException:
        raise
    except cfgtypes.CfgError as e:
        if log.getVerbosity() == log.DEBUG:
            raise
        log.error(str(e))
    except (conary.repository.errors.ConaryError, 
            openpgpfile.PGPError) as e:
        if not str(e):
            raise
        else:
            print(str(e), file=sys.stderr)
    except:
        raise
    return 1

if __name__ == "__main__":
    sys.exit(main())
