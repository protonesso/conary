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


from testrunner import testhelp
import json
import os
import sys


from testrunner.testhelp import context
from testutils import mock

from conary_test import recipes
from conary_test import rephelp

from conary import conaryclient
from conary import constants
from conary.deps import deps
from conary.lib import util
from conary.local import database
from conary import errors
from conary.cmds import query
from conary.cmds import updatecmd
from conary.conaryclient import cml, update, systemmodel
from conary.repository import changeset

class UpdateCmdTest(rephelp.RepositoryHelper):
    def getModel(self, fileData=None):
        self.model = cml.CML(self.cfg)
        if fileData is not None:
            self.model.parse(fileData=fileData)
        self.modelFile = mock.MockInstance(systemmodel.SystemModelFile)
        self.modelFile._mock.set(model=self.model)
        snapFullName=self.cfg.root + '/etc/conary/system-model.next'
        fileFullName=self.cfg.root + '/etc/conary/system-model'
        util.mkdirChain(os.path.dirname(fileFullName))
        if fileData is not None:
            open(fileFullName, 'w').write(
                ''.join(fileData))
            self.modelFile._mock.set(filedata=fileData)
        else:
            open(fileFullName, 'w')
            self.modelFile._mock.set(filedata=[])
        self.modelFile._mock.enableMethod('read')
        self.modelFile._mock.enableMethod('exists')
        self.modelFile._mock.enableMethod('snapshotExists')
        self.modelFile._mock.set(snapFullName=snapFullName)
        self.modelFile._mock.set(fileFullName=fileFullName)

    def buildLocalChangesetFile(self, name, upVer, components, strongList=None):
        targetComponents = [ (comp, '%s/%s:%s.ccs' %(self.workDir, name, comp))
                             for comp in components ]
        csPath = '%s/%s-%s.ccs' %(self.workDir, name, upVer)
        version = 'local@local:COOK/%s-1-1' % upVer
        for comp, path in targetComponents:
            self.addComponent('%s:%s' %(name, comp),
                              version=version, changeSetFile=path)
        if strongList is None:
            strongList=[ ':%s' % x for x in components ]
            
        pkg = self.addCollection(name, strongList=strongList,
                version=version, changeSetFile=csPath)

        cs = changeset.ChangeSetFromFile(csPath)
        os.unlink(csPath)
        for _, path in targetComponents:
            cs.merge(changeset.ChangeSetFromFile(path))
            os.unlink(path)
        cs.setPrimaryTroveList( [ pkg.getNameVersionFlavor() ] )
        cs.writeToFile(csPath)
        return csPath


    def testTroveSpecError(self):
        err = self.assertRaises(conaryclient.cmdline.TroveSpecError,
                updatecmd.doUpdate, self.cfg, [ 'test[[~bootstrap]' ])
        self.assertEqual(str(err),
                'Error with spec "test[[~bootstrap]": bad flavor spec')

        err = self.assertRaises(conaryclient.cmdline.TroveSpecError,
                updatecmd.doUpdate, self.cfg, [ 'test[~bootstrap isx: x86]' ])
        self.assertEqual(str(err),
                'Error with spec "test[~bootstrap isx: x86]": bad flavor spec')

        err = self.assertRaises(conaryclient.cmdline.TroveSpecError,
                updatecmd.doUpdate, self.cfg, [ 'test=1.0-1-1=1.0-1-2' ])
        self.assertEqual(str(err),
                'Error with spec "test=1.0-1-1=1.0-1-2": Too many =\'s')
        nvf = conaryclient.cmdline.parseTroveSpec('test=test@rpl;1')
        self.assertEqual(nvf[0], 'test')
        self.assertEqual(nvf[1], 'test@rpl;1')

    def testDuplicates(self):
        trv = self.build(recipes.testTransientRecipe1, 'TransientRecipe1')
        updatecmd.doUpdate(self.cfg, [ 'testcase', 'testcase' ])
        updatecmd.doUpdate(self.cfg, [ 'testcase', 'testcase' ],
                           updateByDefault = False)

    def testPins(self):
        trv1 = self.build(recipes.testTransientRecipe1, 'TransientRecipe1')
        trv2 = self.build(recipes.testTransientRecipe3, 'TransientRecipe3')
        updatecmd.doUpdate(self.cfg, [ 'testcase=1.0', 'testcase=1.2' ])
        db = database.Database(self.rootDir, self.cfg.dbPath)
        self.assertEqual(
            db.trovesArePinned(sorted([x for x in db.iterAllTroves()])),
            [False, False, False, False])
        updatecmd.changePins(self.cfg, [ 'testcase' ])
        self.assertEqual(
            db.trovesArePinned(sorted([x for x in db.iterAllTroves()])),
            [True, True, True, True])
        updatecmd.changePins(self.cfg, [ 'testcase:runtime=1.0' ], pin = False)
        self.assertEqual(
            db.trovesArePinned(sorted([x for x in db.iterAllTroves()])),
            [True, True, False, True])
        updatecmd.changePins(self.cfg, [ 'testcase=1.2' ], pin = False)
        self.assertEqual(
            db.trovesArePinned(sorted([x for x in db.iterAllTroves()])),
            [True, False, False, False])

    @context('sysmodel')
    def testSysmodelPins(self):
        trv1 = self.build(recipes.testTransientRecipe1, 'TransientRecipe1')
        trv2 = self.build(recipes.testTransientRecipe3, 'TransientRecipe3')
        self.getModel()
        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
            [ 'testcase=localhost@rpl:linux/1.0',
              'testcase=localhost@rpl:linux/1.2' ])
        self.assertEqual(self.model.format(),
            'install testcase=localhost@rpl:linux/1.0'
                   ' testcase=localhost@rpl:linux/1.2\n')
        db = database.Database(self.rootDir, self.cfg.dbPath)
        self.assertEqual(
            db.trovesArePinned(sorted([x for x in db.iterAllTroves()])),
            [False, False, False, False])
        updatecmd.changePins(self.cfg, [ 'testcase' ])
        self.assertEqual(
            db.trovesArePinned(sorted([x for x in db.iterAllTroves()])),
            [True, True, True, True])

        # now remove 1.0 from the model
        self.model.parse(['install testcase=localhost@rpl:linux/1.2'])
        # and now unpin implies sync
        updatecmd.changePins(self.cfg, [ 'testcase=1.0' ], pin = False,
            systemModel=self.model, systemModelFile=self.modelFile)
        # unpinned 1.0 went away, leaving 1.2...
        self.assertEqual(
            set([str(x[1].trailingRevision()) for x in db.iterAllTroves()]),
            set(['1.2-1-1']))
        # ... which is pinned
        self.assertEqual(
            db.trovesArePinned(sorted([x for x in db.iterAllTroves()])),
            [True, True])

        # unpin 1.2
        updatecmd.changePins(self.cfg, [ 'testcase=1.2' ], pin = False,
            systemModel=self.model, systemModelFile=self.modelFile)
        # nothing removed by the sync
        self.assertEqual(
            set([str(x[1].trailingRevision()) for x in db.iterAllTroves()]),
            set(['1.2-1-1']))
        # but troves are unpined
        self.assertEqual(
            db.trovesArePinned(sorted([x for x in db.iterAllTroves()])),
            [False, False])

    def testUpdateFromReadOnlyChangeSet(self):
        # CNY-1681
        trv = self.addComponent('foo:runtime')
        csPath = self.workDir + '/foo.ccs'
        self.changeset(self.openRepository(), 'foo:runtime', csPath)
        os.chmod(csPath, 0o400)
        updatecmd.doUpdate(self.cfg, [ csPath ])

    @context('sysmodel')
    def testModelUpdateFromReadOnlyChangeSet(self):
        self.addComponent('foo:runtime')
        self.addCollection('foo', [ ':runtime' ])
        csPath = self.workDir + '/foo.ccs'
        self.changeset(self.openRepository(), 'foo', csPath)
        # make sure that we don't go to the repo for only a changeset
        self.stopRepository()
        os.chmod(csPath, 0o400)
        self.getModel()
        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
            [ csPath ])
        self.assertEqual(self.model.format(),
            'install foo=/localhost@rpl:linux/1.0-1-1[]\n')
        self.modelFile.closeSnapshot._mock.assertCalled()

    @context('sysmodel')
    def testModelUpdateNoPatchFromLocalCS(self):
        '''
        We absolutely shouldn't ever patch from a local label.
        '''
        csPath = self.workDir + '/foo.ccs'
        csPath1 = self.workDir + '/foo1.ccs'
        self.addComponent('foo:runtime', version='local@local:COOK/1-1-1',
            changeSetFile=csPath)
        pkg = self.addCollection('foo', strongList=[ ':runtime' ],
            version='local@local:COOK/1-1-1', changeSetFile=csPath1)

        cs = changeset.ChangeSetFromFile(csPath)
        os.unlink(csPath)
        cs.merge(changeset.ChangeSetFromFile(csPath1))
        cs.setPrimaryTroveList( [ pkg.getNameVersionFlavor() ] )
        cs.writeToFile(csPath)

        self.getModel()
        # Right now, there's no mechanism for parsing changeset files
        # at all for patch; if this is ever changes, then this might
        # change to a ConaryError, but it must raise an error of some
        # sort, never apply a patch from a local label.
        self.assertRaises(
            errors.TroveSpecsNotFound, updatecmd.doModelUpdate,
            self.cfg, self.model, self.modelFile,
            [ ], patchSpec=[ csPath ],
            keepExisting = False)

    @context('sysmodel')
    def testModelDepsAndLocalChangeset(self):
        csPath = self.workDir + '/foo.ccs'
        csPath1 = self.workDir + '/foo1.ccs'
        csPath2 = self.workDir + '/foo2.ccs'
        self.addComponent('foo:runtime', version='local@local:COOK/1-1-1',
            changeSetFile=csPath)
        self.addComponent('foo:optional', version='local@local:COOK/1-1-1',
            changeSetFile=csPath1,
            fileContents = [ '/foo-optional' ] )
        pkg = self.addCollection('foo', strongList=[ ':runtime' ],
            version='local@local:COOK/1-1-1', changeSetFile=csPath2)

        self.addComponent('bar:runtime=1.0',
                          requires = deps.parseDep('trove: foo:optional'))
        self.addCollection('bar=1.0', [ ':runtime' ])

        cs = changeset.ChangeSetFromFile(csPath)
        os.unlink(csPath)
        cs.merge(changeset.ChangeSetFromFile(csPath1))
        cs.merge(changeset.ChangeSetFromFile(csPath2))
        cs.setPrimaryTroveList( [ pkg.getNameVersionFlavor() ] )
        cs.writeToFile(csPath)

        self.getModel()
        self.captureOutput(updatecmd.doModelUpdate,
            self.cfg, self.model, self.modelFile,
            [ csPath, 'bar=%s' % self.cfg.buildLabel ], keepExisting = False)

        assert(os.path.exists(self.rootDir + '/foo-optional'))

        self.model = self.modelFile.model
        self.assertEqual(self.model.format(),
            'update bar=localhost@rpl:linux/1.0-1-1'
                  ' foo=/local@local:COOK/1-1-1[]\n')
        rc, text = self.captureOutput(updatecmd.updateAll,
            self.cfg, systemModel=self.model,
            systemModelFile=self.modelFile, model=True)
        self.assertEqual(text,
            'update bar=localhost@rpl:linux/1.0-1-1'
                  ' foo=/local@local:COOK/1-1-1[]\n')

    @context('sysmodel', 'redirect')
    def testModelUpdateLimitedLocalChangeSet(self):
        '''
        locally-cooked redirect or group changesets would, after being
        installed once, disappear and therefore cannot be represented
        in the model, so the next conary operation would fail.  So
        prevent them from being added in the first place.
        '''
        csPath = self.workDir + '/foo.ccs'
        csPath1 = self.workDir + '/foo1.ccs'
        csPathG = self.workDir + '/group.ccs'
        self.addComponent('foo:runtime',
            redirect=[],
            version='local@local:COOK/1-1-1',
            changeSetFile=csPath)
        pkg = self.addCollection('foo',
            redirect=[],
            strongList=[ ':runtime' ],
            version='local@local:COOK/1-1-1',
            changeSetFile=csPath1)

        cs = changeset.ChangeSetFromFile(csPath)
        os.unlink(csPath)
        cs.merge(changeset.ChangeSetFromFile(csPath1))
        cs.setPrimaryTroveList( [ pkg.getNameVersionFlavor() ] )
        cs.writeToFile(csPath)
        os.unlink(csPath1)

        self.addComponent('bar:runtime')
        self.addCollection('bar', strongList=[':runtime'])
        self.addCollection('group-foo',
            strongList=[ 'bar=/localhost@rpl:linux/1.0-1-1',
                         'bar:runtime=/localhost@rpl:linux/1.0-1-1' ],
            version='local@local:COOK/1-1-1',
            changeSetFile=csPathG)

        self.getModel()
        e = self.assertRaises(errors.ConaryError,
            updatecmd.doModelUpdate, self.cfg, self.model, self.modelFile,
            [ csPath, csPathG ], keepExisting = False)
        self.assertEqual(str(e),
            '\n'.join(('group and redirect changesets on a local label'
                       ' cannot be installed:',
                       '    %s contains local redirect:'
                       ' foo=/local@local:COOK/1-1-1[]' %csPath,
                       '    %s contains local group:'
                       ' group-foo=/local@local:COOK/1-1-1[]' %csPathG)))

    @context('sysmodel')
    def testModelUpdateFromLocalChangeSet(self):
        csPath = self.buildLocalChangesetFile('foo', 1, ['runtime', 'lib'])
        ncsPath = self.buildLocalChangesetFile('foo', 2, ['runtime', 'lib'])
        self.getModel()

        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
            [ csPath ], keepExisting = False)
        self.assertEqual(self.model.format(),
            'update foo=/local@local:COOK/1-1-1[]\n')
        self.modelFile.closeSnapshot._mock.assertCalled()
        # now, make sure that a sync later still works
        rc, text = self.captureOutput(updatecmd.doModelUpdate,
            self.cfg, self.model, self.modelFile, [])
        self.assertEqual(text, 'Update would not modify system\n')

        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
                                [ '-foo:lib' ])
        # do not find foo:lib in the modelcache, in order to be sure that
        # we are not depending on the modelcache
        os.remove(self.rootDir + '/var/lib/conarydb/modelcache')
        self.assertEqual(self.model.format(),
            'update foo=/local@local:COOK/1-1-1[]\n'
            'erase foo:lib\n')
        rc, text = self.captureOutput(updatecmd.doModelUpdate,
            self.cfg, self.model, self.modelFile, [])
        self.assertEqual(text, 'Update would not modify system\n')

        # Now, test updating multiple times, that simplification deletes
        # the previous reference.

        # update-update -> update
        self.resetRoot()
        self.getModel()
        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
            [ csPath ], keepExisting = False)
        self.assertEqual(self.model.format(),
            'update foo=/local@local:COOK/1-1-1[]\n')

        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
            [ ncsPath ], keepExisting = False)
        # simplification replaced the model, get the new copy
        self.model = self.modelFile.model
        # ensure that simplification removed the reference to the
        # now-missing 1-1-1 version
        self.assertEqual(self.model.format(),
            'update foo=/local@local:COOK/2-1-1[]\n')

        # install-update -> install
        self.resetRoot()
        self.getModel()
        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
            [ csPath ], keepExisting = True)
        self.assertEqual(self.model.format(),
            'install foo=/local@local:COOK/1-1-1[]\n')

        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
            [ ncsPath ], keepExisting = False)
        # simplification replaced the model, get the new copy
        self.model = self.modelFile.model
        # ensure that simplification removed the reference to the
        # now-missing 1-1-1 version
        self.assertEqual(self.model.format(),
            'install foo=/local@local:COOK/2-1-1[]\n')

    @testhelp.context('sysmodel')
    def testMissingLocalTroves(self):
        fooCS1 = self.buildLocalChangesetFile('foo', 1, ['runtime', 'lib'])
        ignCS1 = self.buildLocalChangesetFile('ign', 1, ['runtime'])
        fooCS2 = self.buildLocalChangesetFile('foo', 2, ['runtime', 'lib'])
        self.addComponent('test1:runtime', '1.0-1-1')
        self.addCollection('test1', '1.0-1-1', [ 'test1:runtime=1.0-1-1' ])

        self.getModel()
        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
            [ fooCS1, ignCS1 ], keepExisting = True)
        # parse the model to get the indices correct in the cml object
        self.getModel(fileData=self.modelFile.model.format().split("\n")[:-1])
        self.assertEqual(self.model.format(),
            'install foo=/local@local:COOK/1-1-1[]'
                   ' ign=/local@local:COOK/1-1-1[]\n')

        # disable simplification so that the model will still have
        # the orphaned foo=/local@local:COOK/1-1-1 in it
        self.mock(cml.CM, 'suggestSimplifications', mock.MockObject())
        cml.CM.suggestSimplifications._mock.setDefaultReturn(False)
        rc, text = self.captureOutput(updatecmd.doModelUpdate,
            self.cfg, self.model, self.modelFile,
            [ fooCS2, 'test1=localhost@rpl:linux/1.0-1-1' ],
            keepExisting = False)

        # ensure that mocking out simplification resulted in a bad model
        self.assertEqual(self.modelFile.model.format(),
            'install foo=/local@local:COOK/1-1-1[]'
                   ' ign=/local@local:COOK/1-1-1[]\n'
            'update test1=localhost@rpl:linux/1.0-1-1'
                  ' foo=/local@local:COOK/2-1-1[]\n')
        self.assertEqual(text,
                'Update would leave references to missing local troves:\n'
                    '\tfoo=/local@local:COOK/1-1-1[]\n')

    @context('sysmodel')
    def testModelUpdateLabelLatest(self):
        self.addQuickTestComponent('test1:runtime', '1.0-1-1')
        self.addQuickTestCollection('test1', '1.0-1-1',
                                    [ ('test1:runtime', '1.0-1-1') ])

        self.addQuickTestComponent('test2:runtime', '1.0-1-1', filePrimer=2)
        self.addQuickTestCollection('test2', '1.0-1-1',
                                    [ ('test2:runtime', '1.0-1-1') ])
        self.addQuickTestComponent('test2:runtime', '2.0-1-1', filePrimer=2)
        self.addQuickTestCollection('test2', '2.0-1-1',
                                    [ ('test2:runtime', '2.0-1-1') ])

        self.addQuickTestCollection('group-test', '1.0-1-1', [
                                      ('test1', '1.0-1-1'),
                                      ('test1:runtime', '1.0-1-1'),
                                      ('test2', '1.0-1-1'),
                                      ('test2:runtime', '1.0-1-1'),
                                    ])

        self.getModel()
        # to speed things up, do not actually run the update
        self.mock(updatecmd, '_updateTroves', mock.MockObject())

        # do not look up latest if no label specified
        self.model.parse(
            'search localhost@rpl:linux\n'
            'install group-test'.split('\n'))
        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
            [ 'test2' ], keepExisting = False)
        self.assertEqual(self.model.format(),
            'search localhost@rpl:linux\n'
            'install group-test\n'
            'update test2\n')

        # look up latest if only label specified
        self.model.parse(
            'search localhost@rpl:linux\n'
            'install group-test'.split('\n'))
        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
            [ 'test2=localhost@rpl:linux' ], keepExisting = False)
        self.assertEqual(self.model.format(),
            'search localhost@rpl:linux\n'
            'install group-test\n'
            'update test2=localhost@rpl:linux/2.0-1-1\n')

        # do not look up latest if full version specified
        self.model.parse(
            'search localhost@rpl:linux\n'
            'install group-test'.split('\n'))
        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
            [ 'test2=localhost@rpl:linux/1.0-1-1' ], keepExisting = False)
        self.assertEqual(self.model.format(),
            'search localhost@rpl:linux\n'
            'install group-test\n'
            'update test2=localhost@rpl:linux/1.0-1-1\n')

    @context('sysmodel')
    def testModelSimplification(self):
        self.addComponent('test1:runtime', '1.0-1-1')
        self.addCollection('test1', '1.0-1-1', [ 'test1:runtime=1.0-1-1' ])

        self.addComponent('test2:runtime', '1.0-1-1', filePrimer=2)
        self.addCollection('test2', '1.0-1-1', [ 'test2:runtime=1.0-1-1' ])
        self.addComponent('test2:runtime', '2.0-1-1', filePrimer=2)
        self.addCollection('test2', '2.0-1-1', [ 'test2:runtime=2.0-1-1' ])

        self.addComponent('test3:runtime', '1.0-1-1', filePrimer=3)
        self.addCollection('test3', '1.0-1-1', [ 'test3:runtime=1.0-1-1' ])
        self.addComponent('test3:runtime', '2.0-1-1', filePrimer=3)
        self.addCollection('test3', '2.0-1-1', [ 'test3:runtime=2.0-1-1' ])

        self.addCollection('group-test', '1.0-1-1', 
                           [ 'test1=1.0-1-1', 'test2=1.0-1-1' ],
                           weakRefList = [
                             ('test1:runtime=1.0-1-1', False),
                             ('test2:runtime=1.0-1-1', False),
                           ])

        self.getModel()
        # to speed things up, do not actually run the update
        self.mock(update.ClientUpdate, 'applyUpdateJob', mock.MockObject())
        update.ClientUpdate.applyUpdateJob._mock.setDefaultReturn(None)

        def runSimplification(initialModelText, updateArgs,
            preSimplificationText, postSimplificationText, keepExisting=False):
            self.model.parse(initialModelText.split('\n'))
            updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
                updateArgs, keepExisting = keepExisting)
            #self.assertEquals(self.model.format(), preSimplificationText)
            self.model = self.modelFile.model # get the saved model
            self.assertEqual(self.model.format(), postSimplificationText)

        # update a=1 update a=2 -> update a=2
        runSimplification(
            'search localhost@rpl:linux\n'
            'install group-test\n'
            'update test3=1.0-1-1',

            [ 'test3=2.0-1-1' ],

            'search localhost@rpl:linux\n'
            'install group-test\n'
            'update test3=1.0-1-1\n'
            'update test3=2.0-1-1\n',

            'search localhost@rpl:linux\n'
            'install group-test\n'
            'update test3=2.0-1-1\n')

        # but we don't simplify the pre-existing model
        runSimplification(
            'search localhost@rpl:linux\n'
            'install group-test\n'
            'update test3=2.0-1-1\n'
            'update test3=2.0-1-1',
            
            [ ],

            'search localhost@rpl:linux\n'
            'install group-test\n'
            'update test3=2.0-1-1\n'
            'update test3=2.0-1-1\n',

            'search localhost@rpl:linux\n'
            'install group-test\n'
            'update test3=2.0-1-1\n'
            'update test3=2.0-1-1\n')

        # install a=1 update a=2 -> install a=2
        runSimplification(
            'search localhost@rpl:linux\n'
            'install group-test\n'
            'install test3=1.0-1-1',

            [ 'test3=2.0-1-1' ],

            'search localhost@rpl:linux\n'
            'install group-test\n'
            'install test3=1.0-1-1\n'
            'update test3=2.0-1-1\n',

            'search localhost@rpl:linux\n'
            'install group-test\n'
            'install test3=2.0-1-1\n')

        # ... we don't simplify the pre-existing model
        runSimplification(
            'search localhost@rpl:linux\n'
            'install group-test\n'
            'install test3=1.0-1-1\n'
            'update test3',

            [ ],

            'search localhost@rpl:linux\n'
            'install group-test\n'
            'install test3=1.0-1-1\n'
            'update test3\n',

            'search localhost@rpl:linux\n'
            'install group-test\n'
            'install test3=1.0-1-1\n'
            'update test3\n'
            )

        # install a erase a -> ''
        runSimplification(
            'search localhost@rpl:linux\n'
            'install group-test\n'
            'install test3=1.0-1-1',

            [ '-test3' ],

            'search localhost@rpl:linux\n'
            'install group-test\n'
            'install test3=1.0-1-1\n'
            'erase test3\n',

            'search localhost@rpl:linux\n'
            'install group-test\n')

        # ... we don't simplify the pre-existing model
        runSimplification(
            'search localhost@rpl:linux\n'
            'install group-test\n'
            'install test3=1.0-1-1\n'
            'erase test3',

            [ ],

            'search localhost@rpl:linux\n'
            'install group-test\n'
            'install test3=1.0-1-1\n'
            'erase test3\n',

            'search localhost@rpl:linux\n'
            'install group-test\n'
            'install test3=1.0-1-1\n'
            'erase test3\n')

        # install a b erase a -> install b
        runSimplification(
            'search localhost@rpl:linux\n'
            'install group-test test3',

            [ '-test3' ],

            'search localhost@rpl:linux\n'
            'install group-test test3\n'
            'erase test3\n',

            'search localhost@rpl:linux\n'
            'install group-test\n')

        # install a (no-op, alread there) erase a -> erase a
        runSimplification(
            'search localhost@rpl:linux\n'
            'install group-test\n'
            'install test2', # test2 is in group-test

            [ '-test2' ],

            'search localhost@rpl:linux\n'
            'install group-test\n'
            'install test2\n'
            'erase test2\n',

            'search localhost@rpl:linux\n'
            'install group-test\n'
            'erase test2\n')

        # update a erase a -> erase a
        runSimplification(
            'search localhost@rpl:linux\n'
            'install group-test\n'
            'update test3',

            [ '-test3' ],

            'search localhost@rpl:linux\n'
            'install group-test\n'
            'update test3\n'
            'erase test3\n',

            'search localhost@rpl:linux\n'
            'install group-test\n')

        # update a b erase b -> update a
        runSimplification(
            'search localhost@rpl:linux\n'
            'update test1 test2',

            [ '-test2' ],

            'search localhost@rpl:linux\n'
            'update test1 test2\n'
            'erase test2\n',

            'search localhost@rpl:linux\n'
            'update test1\n')

        # install a install b erase a b -> ''
        runSimplification(
            'search localhost@rpl:linux\n'
            'install test1\n'
            'install test2\n'
            'install test3',

            [ '-test2', '-test3' ],

            'search localhost@rpl:linux\n'
            'install test1\n'
            'install test2\n'
            'install test3\n'
            'erase test2 test3\n',

            'search localhost@rpl:linux\n'
            'install test1\n')

        # install a b erase a -> install b
        runSimplification(
            'search localhost@rpl:linux\n'
            'install test1\n'
            'install test2 test3',

            [ '-test2' ],

            'search localhost@rpl:linux\n'
            'install test1\n'
            'install test2 test3\n'
            'erase test2\n',

            'search localhost@rpl:linux\n'
            'install test1\n'
            'install test3\n')

        # update a=newversion (relative to groups) erase a -> erase a
        runSimplification(
            'search localhost@rpl:linux\n'
            'install group-test\n'
            'update test2=2.0-1-1', # test2=1.0 is in group-test

            [ '-test2' ],

            'search localhost@rpl:linux\n'
            'install group-test\n'
            'update test2=2.0-1-1\n'
            'erase test2\n',

            'search localhost@rpl:linux\n'
            'install group-test\n'
            'erase test2\n')

        # make sure we search for items properly when there are multiple
        # occurrences
        runSimplification(
            'search localhost@rpl:linux\n'
            'install group-test\n'
            'erase test1\n'
            'update test1',

            [ '-test1' ],

            'search localhost@rpl:linux\n'
            'install group-test\n'
            'erase test1\n'
            'update test1\n'
            'erase test1\n',

            'search localhost@rpl:linux\n'
            'install group-test\n'
            'erase test1\n')

    def testUpdateInfo(self):
        # change version on same branch
        self.addQuickTestComponent('test1:runtime', '1.0-1-1')
        # change branch
        self.addQuickTestComponent('test2:runtime', '1.0-1-1', filePrimer=2)
        # change branch and part of flavor
        self.addQuickTestComponent('test3:runtime', '1.0-1-1', 
                                   'readline,krb,ssl', filePrimer=3)
        self.addQuickTestComponent('test3:runtime', '1.0-1-1', 
                                   '!krb,ssl', filePrimer=3)
        # delete
        self.addQuickTestComponent('test4:runtime', '1.0-1-1', filePrimer=4)

        self.addQuickTestCollection("group-test", "1.0-1-1",
                                    [ ("test1:runtime", "1.0-1-1"),
                                      ("test2:runtime", "1.0-1-1"),
                                      ("test3:runtime", "1.0-1-1", 
                                       'readline,krb,ssl'),
                                      ("test4:runtime", "1.0-1-1"),
                                    ])
        self.updatePkg(self.rootDir, 'group-test')

        # change version on same branch
        self.addQuickTestComponent('test1:runtime', '2.0-1-1')
        # change branch
        self.addQuickTestComponent('test2:runtime', 
                                '/localhost@rpl:branch/1.0-1-1', filePrimer=2)
        # change branch and part of flavor
        self.addQuickTestComponent('test3:runtime', 
                                   '/localhost@rpl:branch/1.0-1-1', 
                                   '!krb,ssl', filePrimer=3)
        # delete
        #self.addQuickTestComponent('test4:runtime', '1.0-1-1', filePrimer=4)

        # new
        self.addQuickTestComponent('test5:runtime', '1.0-1-1', filePrimer=5)
        # new branch
        self.addQuickTestComponent('test6:runtime', 
                                    '/localhost@rpl:branch/1.0-1-1', 
                                    filePrimer=6)

        self.addQuickTestCollection("group-test", "2.0-1-1",
                        [ ("test1:runtime", "2.0-1-1"),
                          ("test2:runtime", '/localhost@rpl:branch/1.0-1-1'),
                          ("test3:runtime", "1.0-1-1", '!krb,ssl'),
                          ("test5:runtime", "1.0-1-1"),
                          ("test6:runtime", "/localhost@rpl:branch/1.0-1-1"),
                                    ])
        rc, msg = self.captureOutput(self.updatePkg, self.rootDir, 
                                     ['group-test[!krb,ssl]'], info=True)
        assert(msg == '''\
Job 1 of 2:
    Update  test1:runtime (1.0-1-1 -> 2.0-1-1)
    Update  test2:runtime (/localhost@rpl:linux/1.0-1-1 -> /localhost@rpl:branch/1.0-1-1)
    Update  test3:runtime (1.0-1-1[krb,readline] -> 1.0-1-1[!krb])
    Erase   test4:runtime=1.0-1-1
    Install test5:runtime=1.0-1-1
    Install test6:runtime=1.0-1-1
Job 2 of 2:
    Update  group-test (1.0-1-1[krb,readline] -> 2.0-1-1[!krb])
''')

    def testCompressUpdateInfo(self):
        self.addComponent('test1:runtime', '1.0')
        self.addCollection('test1', '1.0', [':runtime'])
        self.addComponent('test1:runtime', '2.0', filePrimer=1)
        self.addCollection('test1', '2.0', [':runtime'])
        self.addComponent('test2:runtime', '1.0', filePrimer=2)
        self.addCollection('test2', '1.0', [':runtime'])
        self.addComponent('test3:runtime', '1.0', filePrimer=3)
        self.addCollection('test3', '1.0', [':runtime'])

        self.updatePkg(['test1=1.0', 'test3'])
        rc, msg = self.captureOutput(self.updatePkg,
                                     ['test1', 'test2', '-test3'], info=True)
        msg =  """\
Job 1 of 3:
    Update  test1 (1.0-1-1 -> 2.0-1-1)
    Update  test1:runtime (1.0-1-1 -> 2.0-1-1)
Job 2 of 3:
    Install test2=1.0-1-1
    Install test2:runtime=1.0-1-1
Job 3 of 3:
    Erase   test3=1.0-1-1
    Erase   test3:runtime=1.0-1-1
"""
        self.cfg.showComponents = True
        rc, msg = self.captureOutput(self.updatePkg,
                                     ['test1', 'test2', '-test3'], info=True)
        self.cfg.showComponents = False
        assert(msg == '''\
    Update  test1 (1.0-1-1 -> 2.0-1-1)
    Update  test1:runtime (1.0-1-1 -> 2.0-1-1)
    Install test2=1.0-1-1
    Install test2:runtime=1.0-1-1
    Erase   test3=1.0-1-1
    Erase   test3:runtime=1.0-1-1
''')


    def testPlusMinus(self):
        db = database.Database(self.rootDir, self.cfg.dbPath)
        self.addQuickTestComponent('test1:runtime', '1.0-1-1')
        updatecmd.doUpdate(self.cfg, [ '+test1:runtime' ],
                           updateByDefault = False)
        (rc, s) = self.captureOutput(query.displayTroves, db, self.cfg, [])
        updatecmd.doUpdate(self.cfg, [ '-test1:runtime' ],
                           updateByDefault = False)
        (rc, s) = self.captureOutput(query.displayTroves, db, self.cfg, [])

    @context('sysmodel')
    def testModelPlusMinus(self):
        # do not test or confuse lower layers here; we just want to
        # make sure that the system model is configured appropriately
        self.getModel()
        self.mock(updatecmd, '_updateTroves', mock.MockObject())

        self.assertEqual(self.model.modified(), False)
        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
            [ '+test1:runtime' ], updateByDefault = False)
        self.assertEqual(self.model.format(),
            'install test1:runtime\n')
        self.assertEqual(self.model.modified(), True)

        self.getModel()
        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
            [ '-test1:runtime' ], updateByDefault = False)
        self.assertEqual(self.model.format(),
            'erase test1:runtime\n')
        self.assertEqual(self.model.modified(), True)

        self.getModel()
        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
            [ 'test1:runtime' ], updateByDefault = False)
        self.assertEqual(self.model.format(),
            'erase test1:runtime\n')
        self.assertEqual(self.model.modified(), True)

        self.getModel()
        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
            [ '+test1:runtime' ])
        self.assertEqual(self.model.format(),
            'install test1:runtime\n')
        self.assertEqual(self.model.modified(), True)

        self.getModel()
        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
            [ '-test1:runtime' ])
        self.assertEqual(self.model.format(),
            'erase test1:runtime\n')
        self.assertEqual(self.model.modified(), True)

        self.getModel()
        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
            [ 'test1:runtime' ])
        self.assertEqual(self.model.format(),
            'install test1:runtime\n')
        self.assertEqual(self.model.modified(), True)

        self.getModel()
        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
            [ 'test1:runtime' ], keepExisting = False)
        self.assertEqual(self.model.format(),
            'update test1:runtime\n')
        self.assertEqual(self.model.modified(), True)

        # make sure directories don't trick cs file detection code into
        # raising an exception (CNY-3549)
        os.chdir(self.workDir)
        os.mkdir('test1:runtime')
        self.getModel()
        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
            [ 'test1:runtime' ])
        self.assertEqual(self.model.format(),
            'install test1:runtime\n')
        self.assertEqual(self.model.modified(), True)

    @context('sysmodel')
    def testModelPatch(self):
        # do not test or confuse lower layers here; we just want to
        # make sure that the system model is configured appropriately
        self.getModel()
        self.mock(updatecmd, '_updateTroves', mock.MockObject())

        self.assertEqual(self.model.modified(), False)
        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile, [],
            patchSpec=[ 'group-errata1', 'group-errata2' ],
            updateByDefault = False)
        self.assertEqual(self.model.format(),
            'patch group-errata1 group-errata2\n')
        self.assertEqual(self.model.modified(), True)

    @context('sysmodel')
    def testModelUpdatePrintModel(self):
        self.getModel()
        self.model.write = mock.MockObject()
        updatecmd.doModelUpdate(self.cfg, self.model, self.modelFile,
            [ 'test1:runtime' ], model=True)
        self.model.write._mock.assertCalled(sys.stdout)

    def testUpdateAll(self):
        db = database.Database(self.rootDir, self.cfg.dbPath)
        self.addQuickTestComponent('test1:runtime', '1.0-1-1')
        self.addQuickTestComponent('test1:runtime', '2.0-1-1')
        updatecmd.doUpdate(self.cfg, [ 'test1:runtime=1.0' ])
        # need to pass modelGraph explicitly somewhere, and the
        # initial bug report was with "conary updateall" so test here
        self.captureOutput(updatecmd.updateAll, self.cfg, modelGraph=None)
        (rc, s) = self.captureOutput(query.displayTroves, db, self.cfg, [])
        assert(s == 'test1:runtime=2.0-1-1\n')

    def testUpdateAllWithLocalNamespace(self):
        db = self.openDatabase()
        self.addDbComponent(db, 'test1:runtime', '/localhost@local:runtime/1.0-1-1')
        self.addDbComponent(db, 'test2:runtime', '/localhost@foo:runtime/1.0-1-1')
        rc, s = self.captureOutput(updatecmd.updateAll, self.cfg, showItems=True)
        self.assertEqual(s, 'test2:runtime\n')
        # same deal, but show labels too (CNY-3138)
        self.cfg.showLabels = True
        rc, s = self.captureOutput(updatecmd.updateAll, self.cfg, showItems=True)
        self.assertEqual(s, 'test2:runtime=localhost@foo:runtime/1.0-1-1\n')

        self.cfg.fullVersions = True
        rc, s = self.captureOutput(updatecmd.updateAll, self.cfg, showItems=True)
        self.assertEqual(s, 'test2:runtime=/localhost@foo:runtime/1.0-1-1\n')
        self.cfg.fullFlavors = True
        self.assertEqual(s, 'test2:runtime=/localhost@foo:runtime/1.0-1-1\n')

    @context('sysmodel')
    def testUpdateAllPrintModel(self):
        model = mock.MockObject()
        modelFile = mock.MockObject()
        rc = updatecmd.updateAll(self.cfg, systemModel=model,
            systemModelFile=modelFile, model=True)
        model.refreshVersionSnapshots._mock.assertCalled()
        model.write._mock.assertCalled(sys.stdout)

    @context('reexec', 'sysmodel')
    def testCriticalSysModelUpdates(self):
        'System model must implement critical updates'
        for v in '1', '2':
            self.addComponent('corecomp:runtime', v,
                             provides='trove:corecomp(%s)' % v)
            self.addComponent('conary:data', v, filePrimer=1)
            self.addComponent('conary:python', v, 
                          requires='trove:conary:data trove:corecomp:runtime',
                          provides='trove:conary:python(%s)' % v, filePrimer=2)
            self.addComponent('conary-build:python', v, 
                          requires='trove:conary:python(%s)' % v, filePrimer=3)
            self.addComponent('extra:runtime', v, filePrimer=4,
                              requires='trove:corecomp(%s)' % v)
            self.addCollection('conary', v, [':python', ':data'])
            self.addCollection('conary-build', v, [':python'])
            self.addCollection('corecomp', v, [':runtime'])
            self.addCollection('extra', v, [':runtime'])
            self.addCollection('group-dist', v,
                            ['conary', 'conary-build', 'corecomp', 'extra'])
        self.getModel(fileData=['search group-dist=/localhost@rpl:linux/1-1-1'])
        # First, test that we break the job up into a critical section
        rc, txt = self.captureOutput(updatecmd.doModelUpdate,
            self.cfg, self.model, self.modelFile,
            [ 'group-dist' ],
            info=True)
        self.assertEqual(txt, '''\
Job 1 of 4:
    Install conary:data=1-1-1
    Install corecomp:runtime=1-1-1
**  Job 2 of 4:
    Install conary:python=1-1-1
Job 3 of 4:
    Install conary=1-1-1
    Install conary-build(:python)=1-1-1
    Install corecomp=1-1-1
    Install extra(:runtime)=1-1-1
Job 4 of 4:
    Install group-dist=1-1-1

** The update will restart itself after job 2 and continue updating
''')
        self.assertEqual(self.model.format(),
            'search group-dist=/localhost@rpl:linux/1-1-1\n'
            'install group-dist\n')

        # Now actually run the update
        self.getModel(fileData=['search group-dist=/localhost@rpl:linux/1-1-1'])
        try:
            rc, txt = self.captureOutput(updatecmd.doModelUpdate,
                self.cfg, self.model, self.modelFile,
                [ 'group-dist' ])
            # Note: txt already tested in info case above, no need to retest
        except errors.ReexecRequired as e:
            db = self.openDatabase()
            self.assertEqual(sorted(db.iterAllTroveNames()),
                ['conary:data', 'conary:python', 'corecomp:runtime'])
            self.modelFile.closeSnapshot._mock.assertNotCalled()
            rc, txt = self.captureOutput(updatecmd.doModelUpdate,
                self.cfg, self.model, self.modelFile,
                [ ],
                restartInfo=e.data)
            self.assertEqual(txt, '')
            assert(sorted(db.iterAllTroveNames()) ==
                   ['conary', 'conary-build', 'conary-build:python',
                    'conary:data', 'conary:python',
                    'corecomp', 'corecomp:runtime',
                    'extra', 'extra:runtime',
                    'group-dist'])
        else:
            self.fail('did not get reexec request')

        # make sure that we haven't doubled the model
        self.assertEqual(self.model.format(),
            'search group-dist=/localhost@rpl:linux/1-1-1\n'
            'install group-dist\n')
        self.modelFile.closeSnapshot._mock.assertCalled()

        # now, test updateall roughly the same way
        try:
            rc, txt = self.captureOutput(updatecmd.updateAll, self.cfg,
                systemModel=self.model, systemModelFile=self.modelFile)
        except errors.ReexecRequired as e:
            self.assertEqual(txt, '')
            self.assertEqual(self.model.format(),
                'search group-dist=localhost@rpl:linux/2-1-1\n'
                'install group-dist\n')
            self.modelFile.closeSnapshot._mock.assertNotCalled()
            rc, txt = self.captureOutput(updatecmd.doModelUpdate,
                self.cfg, self.model, self.modelFile,
                [ ],
                restartInfo=e.data)
            self.assertEqual(txt, '')
        else:
            self.fail('did not get reexec request')

        # make sure that we haven't doubled the model
        self.assertEqual(self.model.format(),
            'search group-dist=localhost@rpl:linux/2-1-1\n'
            'install group-dist\n')
        self.modelFile.closeSnapshot._mock.assertCalled()


    @context('reexec')
    def testCriticalUpdates(self):
        updateScript = """\
#!/bin/sh
if [ -e %(root)s/myfile ]; then
    exit 1;
else
    touch %(root)s/myfile;
fi
"""  % dict(root=self.rootDir)
        myfile = os.path.join(self.rootDir, 'myfile')
        for v in '1', '2', '3':
            self.addComponent('corecomp:runtime', v,
                             provides='trove:corecomp(%s)' % v)
            self.addComponent('conary:data', v, filePrimer=1)
            self.addComponent('conary:python', v, 
                          requires='trove:conary:data trove:corecomp:runtime',
                          provides='trove:conary:python(%s)' % v, filePrimer=2)
            self.addComponent('conary-build:python', v, 
                          requires='trove:conary:python(%s)' % v, filePrimer=3)

            if v == '3':
                # add broken req for extra:runtime 3
                self.addComponent('extra:runtime', v, filePrimer=4,
                                  requires='trove:unknown:runtime')
            else:
                self.addComponent('extra:runtime', v, filePrimer=4,
                                  requires='trove:corecomp(%s)' % v)

            self.addCollection('conary', v, [':python', ':data'])
            self.addCollection('conary-build', v, [':python'])
            self.addCollection('corecomp', v, [':runtime'])
            self.addCollection('extra', v, [':runtime'])
            self.addCollection('group-dist', v,
                            ['conary', 'conary-build', 'corecomp', 'extra'],
                        preUpdateScript =
                        rephelp.TroveScript(script=updateScript),
                        postUpdateScript = "#!/bin/bash\n\nexit 0\n")
        rc, txt = self.captureOutput(self.updatePkg, 'group-dist=1',
                                     raiseError=True, info=True)
        assert(txt == '''\
Job 1 of 4:
    Install conary:data=1-1-1
    Install corecomp:runtime=1-1-1
**  Job 2 of 4:
    Install conary:python=1-1-1
Job 3 of 4:
    Install conary=1-1-1
    Install conary-build(:python)=1-1-1
    Install corecomp=1-1-1
    Install extra(:runtime)=1-1-1
Job 4 of 4:
    Install group-dist=1-1-1

** The update will restart itself after job 2 and continue updating
''')
        try:
            self.discardOutput(self.updatePkg, 'group-dist=1', raiseError=True)
        except errors.ReexecRequired as e:
            db = self.openDatabase()
            assert(sorted(db.iterAllTroveNames()) == ['conary:data', 'conary:python', 'corecomp:runtime'])
            self.discardOutput(self.updatePkg, 'group-dist=1', raiseError=True,
                               restartInfo=e.data)
        else:
            assert 0, 'did not get reexec request'
        self.assertFalse(os.path.exists(myfile),
            "preupdate script got executed on install")

        rc, txt = self.captureOutput(self.updatePkg, 'group-dist=2',
                                     raiseError=True, info=True)
        # this time conary-build:runtime is in the main update, 
        # but corecomp isn't (the new conary will work with either the
        # new or old corecomp)
        assert(txt == '''\
Job 1 of 4:
    Update  conary-build:python (1-1-1 -> 2-1-1)
    Update  conary:python (1-1-1 -> 2-1-1)
**  Job 2 of 4:
    Update  conary:data (1-1-1 -> 2-1-1)
Job 3 of 4:
    Update  conary (1-1-1 -> 2-1-1)
    Update  conary-build (1-1-1 -> 2-1-1)
    Update  corecomp(:runtime) (1-1-1 -> 2-1-1)
    Update  extra(:runtime) (1-1-1 -> 2-1-1)
Job 4 of 4:
    Update  group-dist (1-1-1 -> 2-1-1)

** The update will restart itself after job 2 and continue updating
''')
        try:
            self.mimicRoot()
            try:
                self.discardOutput(self.updatePkg, 'group-dist=2', 
                                   raiseError=True)
            except errors.ReexecRequired as e:
                self.assertTrue(os.path.exists(myfile),
                    "preupdate script was not executed")
                self.discardOutput(self.updatePkg, 'group-dist=2', 
                                   raiseError=True, restartInfo=e.data)
                self.assertTrue(os.path.exists(myfile),
                    "marker file mysteriously went away")
            else:
                assert 0, 'did not get reexec request'
        finally:
            self.realRoot()
        os.remove(myfile)

        groupDist3Error = '''\
The following dependencies could not be resolved:
    extra:runtime=3-1-1:
\ttrove: unknown:runtime'''
        groupDist3Critical =  '''\
    Update  conary-build:python (2-1-1 -> 3-1-1)
    Update  conary:data (2-1-1 -> 3-1-1)
    Update  conary:python (2-1-1 -> 3-1-1)
'''
        try:
            self.discardOutput(self.updatePkg, 'group-dist=3', 
                               raiseError=True, info=True)
        except errors.ConaryError as e:
            self.assertEqual(str(e), groupDist3Error)

        rc, txt = self.captureOutput(self.updatePkg, ['group-dist=3'],
                                     raiseError=True, info=True, 
                                     applyCriticalOnly=True)
        assert(txt == groupDist3Critical)

        self.assertFalse(os.path.exists(myfile),
            "preupdate script executed before critical update")
        # Hmm, arguably the preupdate script should have run _before_ any
        # subtrove of group-dist=3

        # We should not attempt to run the script for group-dist since we
        # don't have permissions to chroot
        file(myfile, "w")
        rc, txt = self.captureOutput(self.updatePkg, ['group-dist=3'],
                                     raiseError=True, applyCriticalOnly=True)
        self.assertTrue(os.path.exists(myfile),
            "marker file mysteriously went away")

        self.assertFalse(txt == 'warning: Not running script for group-dist due to insufficient permissions for chroot()\n')
        # Roll back the critical update
        self.rollback(self.rootDir, 4)

        # once more, with updateAll
        try:
            self.captureOutput(self.updateAll, info=True)
        except errors.ConaryError as e:
            assert(str(e)  == groupDist3Error)
        else:
            assert 0, 'dep error expected'
        rc, txt = self.captureOutput(self.updateAll, info=True,
                                     applyCriticalOnly=True)
        assert(txt == groupDist3Critical)

        # to make updateAll work, we'll create a new version w/ new conary
        # and old corecomp.
        self.addCollection('group-dist', '4',
                            [('conary', '3'),
                             ('conary-build', '3'),
                             ('corecomp', '1'),
                             ('extra', '1')])

        try:
            rc, txt = self.captureOutput(self.updateAll)
        except errors.ReexecRequired as e:
            self.captureOutput(self.updateAll, restartInfo=e.data)
            
        # instead we'll downgrade - not rollback because that doesn't
        # implement this functionality!
        try:
            self.discardOutput(self.updatePkg, 'group-dist=2', raiseError=True)
        except errors.ReexecRequired as e:
            jobSetPath = e.data + '/joblist'
            # muck with the list of jobs to apply - this will cause conary
            # to display a warning telling you the new conary resolved
            # deps differently.
            jobList = [ x for x in open(jobSetPath, 'r') 
                        if not x.startswith('corecomp')]
            open(jobSetPath, 'w').write(''.join(jobList))
            rc, txt = self.captureOutput(self.updatePkg, 'group-dist=1', 
                                         raiseError=True, 
                                         restartInfo=e.data, info=True,
                                         resolve=True)
            assert(txt == '''\
Job 1 of 2:
    Update  conary (3-1-1 -> 2-1-1)
    Update  conary-build (3-1-1 -> 2-1-1)
    Update  corecomp(:runtime) (1-1-1 -> 2-1-1)
    Update  extra(:runtime) (1-1-1 -> 2-1-1)
Job 2 of 2:
    Update  group-dist (4-1-1 -> 2-1-1)

NOTE: after critical updates were applied, the contents of the update were recalculated:

Added to job:
    Update  corecomp(:runtime) (1-1-1 -> 2-1-1)
''')
        else:
            assert(0)

    @context('reexec')
    def testCriticalUpdatesMultiplePreScripts(self):
        # The scenario:
        # * group-dist and group-foo have preupdate scripts
        # * group-dist has components that go in the critical update, and
        #   components that get installed after reexec
        # * group-foo has only components that get installed after reexec
        # We expect group-dist's preupdate script to be executed before the
        # critical update, and group-foo's after the reexec.
        updateScriptTmpl = """\
#!/bin/sh
if [ -e %(root)s/%(package)s ]; then
    exit 1;
else
    touch %(root)s/%(package)s;
fi
"""
        updateScriptGroupDist = updateScriptTmpl % dict(root=self.rootDir,
            package = 'dist')
        updateScriptGroupFoo = updateScriptTmpl % dict(root=self.rootDir,
            package = 'foo')
        myfileDist = os.path.join(self.rootDir, 'dist')
        myfileFoo = os.path.join(self.rootDir, 'foo')
        for v in '1', '2':
            self.addComponent('corecomp:runtime', v,
                             provides='trove:corecomp(%s)' % v)
            self.addComponent('conary:data', v, filePrimer=1)
            self.addComponent('conary:python', v, 
                          requires='trove:conary:data trove:corecomp:runtime',
                          provides='trove:conary:python(%s)' % v, filePrimer=2)
            self.addComponent('conary-build:python', v, 
                          requires='trove:conary:python(%s)' % v, filePrimer=3)

            self.addComponent('extra:runtime', v, filePrimer=4,
                              requires='trove:corecomp(%s)' % v)

            self.addComponent('foo:runtime', v, filePrimer=5)

            self.addCollection('conary', v, [':python', ':data'])
            self.addCollection('conary-build', v, [':python'])
            self.addCollection('corecomp', v, [':runtime'])
            self.addCollection('extra', v, [':runtime'])
            self.addCollection('foo', v, [':runtime'])
            self.addCollection('group-dist', v,
                            ['conary', 'conary-build', 'corecomp', 'extra'],
                        preUpdateScript =
                            rephelp.TroveScript(script=updateScriptGroupDist),
                        postUpdateScript = "#!/bin/bash\n\nexit 0\n")
            self.addCollection('group-foo', v,
                            ['foo'],
                        preUpdateScript =
                            rephelp.TroveScript(script=updateScriptGroupFoo),
                        postUpdateScript = "#!/bin/bash\n\nexit 0\n")
        groups0 = [ 'group-dist=1', 'group-foo=1' ]
        groups1 = [ 'group-dist=2', 'group-foo=2' ]
        rc, txt = self.captureOutput(self.updatePkg,
            groups0, raiseError=True, info=True)
        self.assertEqual(txt, '''\
Job 1 of 6:
    Install conary:data=1-1-1
    Install corecomp:runtime=1-1-1
**  Job 2 of 6:
    Install conary:python=1-1-1
Job 3 of 6:
    Install conary=1-1-1
    Install corecomp=1-1-1
    Install foo(:runtime)=1-1-1
Job 4 of 6:
    Install group-foo=1-1-1
Job 5 of 6:
    Install conary-build(:python)=1-1-1
    Install extra(:runtime)=1-1-1
Job 6 of 6:
    Install group-dist=1-1-1

** The update will restart itself after job 2 and continue updating
''')
        try:
            self.discardOutput(self.updatePkg, groups0, raiseError=True)
        except errors.ReexecRequired as e:
            db = self.openDatabase()
            assert(sorted(db.iterAllTroveNames()) == ['conary:data', 'conary:python', 'corecomp:runtime'])
            self.discardOutput(self.updatePkg, groups0, raiseError=True,
                               restartInfo=e.data)
        else:
            assert 0, 'did not get reexec request'
        self.assertFalse(os.path.exists(myfileFoo),
            "preupdate script got executed on install")
        self.assertFalse(os.path.exists(myfileDist),
            "preupdate script got executed on install")

        try:
            self.mimicRoot()
            try:
                self.discardOutput(self.updatePkg, groups1, raiseError=True)
            except errors.ReexecRequired as e:
                self.assertTrue(os.path.exists(myfileDist),
                    "preupdate script was not executed")
                self.assertFalse(os.path.exists(myfileFoo),
                    "preupdate script got executed for group-foo before critical updates")
                self.discardOutput(self.updatePkg, groups1, 
                                   raiseError=True, restartInfo=e.data)
                self.assertTrue(os.path.exists(myfileDist),
                    "marker file mysteriously went away")
                self.assertTrue(os.path.exists(myfileFoo),
                    "preupdate script for group-foo was not executed")
            else:
                assert 0, 'did not get reexec request'
        finally:
            self.realRoot()

    @context('reexec')
    def testCriticalUpdatesFlavorsWithPreScripts(self):
        # The scenario:
        # The group is flavored. This was breaking the deserializatiom from
        # the frozen critical update.
        # CNY-3216
        updateScriptTmpl = """
touch %(root)s/%(package)s;
"""
        flv = "ssl is: x86"

        updateScriptGroupDist = updateScriptTmpl % dict(root=self.rootDir,
            package = 'dist')
        for v in '1', '2':
            self.addComponent('corecomp:runtime', v, flavor = flv,
                filePrimer = 1)
            self.addComponent('corecomp:data', v, flavor = flv, filePrimer = 2)
            self.addCollection('corecomp', v, [
                (':runtime', v, flv),
                (':data', v, flv)], flavor = flv)
            self.addCollection('group-dist', v, [('corecomp', v, flv)],
                flavor = flv,
                preUpdateScript =
                    rephelp.TroveScript(script=updateScriptGroupDist))

        groups0 = 'group-dist=1'
        self.discardOutput(self.updatePkg, groups0, raiseError=True)

        criticalUpdateInfo = updatecmd.CriticalUpdateInfo()
        criticalUpdateInfo.setCriticalTroveRegexps(['corecomp:.*'])

        groups0 = 'group-dist=2'
        try:
            self.discardOutput(self.updatePkg, groups0, raiseError=True,
                criticalUpdateInfo = criticalUpdateInfo)
        except errors.ReexecRequired as e:
            self.discardOutput(self.updatePkg, groups0, raiseError=True,
                restartInfo=e.data,
                criticalUpdateInfo = criticalUpdateInfo)
        else:
            self.fail("Should have raised ReexecRequired")

    @context('reexec')
    def testSyncCriticalUpdate(self):
        for v in '1', '2':
            self.addComponent('corecomp:runtime', v,
                             provides='trove:corecomp(%s)' % v)
            self.addComponent('conary:data', v, filePrimer=1)
            self.addComponent('conary:python', v, 
                          requires='trove:conary:data trove:corecomp:runtime',
                          provides='trove:conary:python(%s)' % v, filePrimer=2)
            self.addComponent('conary-build:python', v, 
                          requires='trove:conary:python(%s)' % v, filePrimer=3)

            self.addComponent('extra:runtime', v, filePrimer=4,
                              requires='trove:corecomp(%s)' % v)

            self.addCollection('conary', v, [':python', ':data'])
            self.addCollection('conary-build', v, [':python'])
            self.addCollection('corecomp', v, [':runtime'])
            self.addCollection('extra', v, [':runtime'])
            self.addCollection('group-dist', v,
                            ['conary', 'conary-build', 'corecomp', 'extra'])
        try:
            self.discardOutput(self.updatePkg, 'group-dist=1', raiseError=True)
        except errors.ReexecRequired as e:
            self.discardOutput(self.updatePkg, 'group-dist=1', raiseError=True,
                                restartInfo=e.data)
        else:
            assert 0, 'did not get reeexec request'

        self.discardOutput(self.updatePkg, 'group-dist=2', recurse=False)
        try:
            self.discardOutput(self.updatePkg, 'group-dist', syncChildren=True,
                               raiseError=True)
        except errors.ReexecRequired as e:
            self.discardOutput(self.updatePkg, 'group-dist', syncChildren=True,
                               raiseError=True, restartInfo=e.data)

    @context('reexec')
    def testCriticalUpdateWithChangeSets(self):
        # create a changeset w/ one critical, one non-critical update in it
        # update from it w/ from-files and specifying it at the command line
        for v in '1',:
            self.addComponent('corecomp:runtime', v,
                             provides='trove:corecomp(%s)' % v)
            self.addComponent('conary:data', v, filePrimer=1)
            self.addComponent('conary:python', v, 
                          requires='trove:conary:data trove:corecomp:runtime',
                          provides='trove:conary:python(%s)' % v, filePrimer=2)
            self.addComponent('conary-build:python', v, 
                          requires='trove:conary:python(%s)' % v, filePrimer=3)

            self.addComponent('extra:runtime', v, filePrimer=4,
                              requires='trove:corecomp(%s)' % v)

            self.addCollection('conary', v, [':python', ':data'])
            self.addCollection('conary-build', v, [':python'])
            self.addCollection('corecomp', v, [':runtime'])
            self.addCollection('extra', v, [':runtime'])
            self.addCollection('group-dist', v,
                            ['conary', 'conary-build', 'corecomp', 'extra'])

        repos = self.openRepository()
        csPath = self.workDir + '/gd.ccs'
        self.changeset(repos, ['group-dist'], csPath)

        try:
            self.discardOutput(self.updatePkg, 'group-dist=1', raiseError=True, 
                               fromFiles=[csPath])
        except errors.ReexecRequired as e:
            self.updatePkg('group-dist=1', raiseError=True, restartInfo=e.data,
                           fromFiles=[csPath])
        else:
            assert 0, 'did not get reeexec request'
        self.resetRoot()

        try:
            self.discardOutput(self.updatePkg, [csPath], raiseError=True)
        except errors.ReexecRequired as e:
            self.updatePkg([csPath], raiseError=True, restartInfo=e.data)
        else:
            assert 0, 'did not get reeexec request'

    @context('reexec')
    def testCriticalUpdateVersionFileAndCleanup(self):
        groupTroveName = "group-dummy"
        for v in range(2):
            v = str(v + 1)

            self.addComponent('conary:runtime', v, filePrimer=1)
            self.addCollection('conary', v, [':runtime'])
            self.addComponent('extra:runtime', v, filePrimer=2)
            self.addCollection('extra', v, [':runtime'])
            self.addCollection(groupTroveName, v, ['extra', 'conary'])

        client = conaryclient.ConaryClient(self.cfg)

        vtrove = groupTroveName + "=1"
        try:
            self.discardOutput(self.updatePkg, vtrove, raiseError=True)
        except errors.ReexecRequired as e:
            # CNY-1034: a version file should be present
            miscdir = e.data + "misc"
            verfile = os.path.join(miscdir, '__version__')
            try:
                vf = open(verfile)
            except:
                self.fail("Unable to open version file")
            # Starting with Conary 1.1.29, the version file is also in the
            # restart directory
            nverfile = os.path.join(e.data, '__version__')
            self.assertTrue(os.path.exists(nverfile))
            self.assertEqual(vf.read(), open(nverfile).read())
            vf.seek(0)
            # First line should be "version XXX")
            verline = vf.readline().strip()
            # Get rid of white spaces (tabs etc)
            arr = [ x.strip() for x in verline.split() ]
            # filter out empty strings
            arr = [ x for x in arr if x ]
            self.assertEqual(arr, ['version', constants.version])

            # Add some extra files in the restart directory, make sure they
            # are ignored
            open(os.path.join(e.data, "file1.txt"), "w+").write("123\n")
            open(os.path.join(e.data, "blahccs"), "w+").write("123\n")
            open(os.path.join(e.data, "_blip"), "w+").write("123\n")

            # Do we have the invocation saved?
            jobInvocation = os.path.join(e.data, 'job-invocation')
            self.assertTrue(os.path.exists(jobInvocation))

            updJob = client.newUpdateJob()
            drep = updJob.loadInvocationInfo(jobInvocation)
            self.assertTrue(updJob.getItemList())
            self.assertTrue(updJob.getKeywordArguments())
            self.assertTrue('conaryVersion' in drep)

            self.updatePkg(vtrove, raiseError=True, restartInfo=e.data)
            # Did we clean up the restart info dir?
            self.assertFalse(os.path.isdir(e.data), "Did not clean up restart dir")
            # Did we clean up the misc directory?
            self.assertFalse(os.path.isdir(miscdir))
        else:
            self.fail('did not get reeexec request')

    @context('reexec')
    def testMigrateFromChangesetsDuplicateTroveFailure(self):
        # This was further refined into client/updatetest.py :
        # testMigrateFromChangesets
        # Fixing that one should make this test pass

        # Paths to the changesets we create
        changesets = []

        repos = self.openRepository()
        groupTroveName = "group-dummy"

        for v in range(2):
            v = str(v + 1)

            self.addComponent('corecomp:runtime', v,
                             provides='trove:corecomp(%s)' % v)
            self.addComponent('conary:data', v, filePrimer=1)
            self.addComponent('conary:python', v, 
                          requires='trove:conary:data trove:corecomp:runtime',
                          provides='trove:conary:python(%s)' % v, filePrimer=2)
            self.addComponent('conary-build:python', v, 
                          requires='trove:conary:python(%s)' % v, filePrimer=3)

            self.addComponent('extra:runtime', v, filePrimer=4,
                              requires='trove:corecomp(%s)' % v)

            self.addCollection('conary', v, [':python', ':data'])
            self.addCollection('conary-build', v, [':python'])
            self.addCollection('corecomp', v, [':runtime'])
            self.addCollection('extra', v, [':runtime'])
            self.addCollection(groupTroveName, v,
                            ['conary', 'conary-build', 'corecomp', 'extra'])

            repos = self.openRepository()
            csPath = self.workDir + '/gd%s.ccs' % v
            self.changeset(repos, [groupTroveName], csPath)
            changesets.append(csPath)

        vtrove = groupTroveName + "=1"
        try:
            self.discardOutput(self.updatePkg, vtrove, raiseError=True)
        except errors.ReexecRequired as e:
            self.updatePkg(vtrove, raiseError=True, restartInfo=e.data)
        else:
            assert 0, 'did not get reeexec request'

        vtrove = groupTroveName + "=2"
        # ensure that migrate now works in non-interactive mode (CNY-3361)
        self.cfg.interactive = False
        oldaskYn = conaryclient.cmdline.askYn
        try:
            conaryclient.cmdline.askYn = lambda *args, **kw: True
            try:
                self.discardOutput(self.updatePkg, vtrove, raiseError=True,
                               fromFiles=[changesets[1]], migrate=True)
                assert(0)
            except errors.ReexecRequired as e:
                pass
        finally:
            conaryclient.cmdline.askYn = oldaskYn

    @context('reexec')
    def testMigrateWithCriticalUpdate(self):
        groupTroveName = "group-dummy"
        for v in range(2):
            v = str(v + 1)

            self.addComponent('corecomp:runtime', v,
                             provides='trove:corecomp(%s)' % v)
            self.addComponent('conary:data', v, filePrimer=1)
            self.addComponent('conary:python', v, 
                          requires='trove:conary:data trove:corecomp:runtime',
                          provides='trove:conary:python(%s)' % v, filePrimer=2)
            self.addComponent('conary-build:python', v, 
                          requires='trove:conary:python(%s)' % v, filePrimer=3)

            self.addComponent('extra:runtime', v, filePrimer=4,
                              requires='trove:corecomp(%s)' % v)

            self.addCollection('conary', v, [':python', ':data'])
            self.addCollection('conary-build', v, [':python'])
            self.addCollection('corecomp', v, [':runtime'])
            self.addCollection('extra', v, [':runtime'])
            self.addCollection(groupTroveName, v,
                            ['conary', 'conary-build', 'corecomp', 'extra'])

        grp = groupTroveName + "=1"
        try:
            self.discardOutput(self.updatePkg, grp, raiseError=True)
        except errors.ReexecRequired as e:
            self.discardOutput(self.updatePkg, grp, raiseError=True,
                               restartInfo=e.data)
        else:
            assert 0, 'did not get reeexec request'

        # Set interactive mode
        self.cfg.interactive = True

        # Replace cmdline.askYn with a dummy function
        def askYn(prompt, default=None):
            return True
        oldaskYn = conaryclient.cmdline.askYn
        conaryclient.cmdline.askYn = askYn

        grp = groupTroveName + "=2"
        try:
            self.discardOutput(self.updatePkg, grp, raiseError=True,
                               migrate=True)
        except errors.ReexecRequired as e:
            # Should have changed from a migrate into a regular update
            self.assertEqual(e.execParams[1], 'update')
            try:
                self.discardOutput(self.updatePkg, grp, raiseError=True,
                                   migrate=False, restartInfo=e.data)
            except:
                # Clean up and re-raise
                conaryclient.cmdline.askYn = oldaskYn
                raise

        # Cleaning up
        conaryclient.cmdline.askYn = oldaskYn
        self.resetRoot()

    @context('reexec')
    def testLinkedFiles(self):
        # CNY-1115
        self.addComponent('foo:runtime', '1', '', 
            [ ('/foo/file1', 'contents1'), 
              ('/baz/file1', 'contents1')])
        self.addCollection('foo', '1', ['foo:runtime'])
        self.addComponent('bar:runtime', '1', '', 
            [ ('/bar/file1', 'contents1'),
              ('/bar/file1', 'contents1')])
        self.addCollection('bar', '1', ['bar:runtime'])
        self.addComponent('conary:runtime', '1', '', filePrimer=1)
        self.addCollection('conary', '1', ['conary:runtime'])

        pkgset = ['foo', 'bar', 'conary']
        try:
            self.discardOutput(self.updatePkg, pkgset, raiseError=True)
        except errors.ReexecRequired as e:
            self.discardOutput(self.updatePkg, pkgset, raiseError=True,
                               restartInfo=e.data)

        # One of the files changes from foo to bar
        # bar has an unsatisfied dependency
        self.addComponent('foo:runtime', '2', '', 
            [ ('/foo/file1', 'contents2'), ])
        self.addCollection('foo', '2', ['foo:runtime'])
        self.addComponent('bar:runtime', '2', '', 
            [ ('/bar/file1', 'contents2'),
              ('/bar/file1', 'contents2'),
              ('/baz/file1', 'contents1')], requires="trove: foobar")
        self.addCollection('bar', '2', ['bar:runtime'])
        self.addComponent('conary:runtime', '2', '', filePrimer=2)
        self.addCollection('conary', '2', ['conary:runtime'])

        # run with apply critical only
        self.discardOutput(self.updatePkg, pkgset, applyCriticalOnly=True)

    @context('reexec')
    def testCriticalUpdateFails(self):
        # CNY-1264
        repos = self.openRepository()
        colls = []
        for v in '1', '2':
            self.addComponent('corecomp:runtime', v,
                             provides='trove:corecomp(%s)' % v)
            self.addComponent('conary:data', v, filePrimer=1)

            self.addComponent('extra:runtime', v, filePrimer=4)

            self.addCollection('conary', v, [':data'])
            self.addCollection('corecomp', v, [':runtime'])
            self.addCollection('extra', v, [':runtime'])
            c = self.addCollection('group-dist', v, 
                ['conary', 'corecomp', 'extra'])
            colls.append(c)
        try:
            self.discardOutput(self.updatePkg, 'group-dist=1', raiseError=True)
        except errors.ReexecRequired as e:
            self.discardOutput(self.updatePkg, 'group-dist=1', raiseError=True,
                                restartInfo=e.data)
        else:
            assert 0, 'did not get reeexec request'

        def foo(slf, otherCs):
            raise changeset.PathIdsConflictError(
                'abcd' * 4,
                colls[0].diff(None)[0], (None, None), 
                colls[1].diff(None)[0], (None, None))

        job = [(colls[0].getName(),
                (colls[0].getVersion(), colls[0].getFlavor()),
                (colls[1].getVersion(), colls[1].getFlavor()),
                False,)]

        client = conaryclient.ConaryClient(self.cfg)
        updJob, suggMap = client.updateChangeSet(job, keepExisting=False,
                keepRequired=False,)

        callback = updatecmd.UpdateCallback()

        try:
            # Replace the merge function
            changeset.ReadOnlyChangeSet._saved_merge = changeset.ReadOnlyChangeSet.merge
            changeset.ReadOnlyChangeSet.merge = foo
            threaded = self.cfg.threaded
            self.cfg.threaded = True

            client = conaryclient.ConaryClient(self.cfg)
            client.setUpdateCallback(callback)
            self.assertRaises(changeset.PathIdsConflictError, 
                client.applyUpdate, updJob)
        finally:
            changeset.ReadOnlyChangeSet.merge = changeset.ReadOnlyChangeSet._saved_merge
            del changeset.ReadOnlyChangeSet._saved_merge
            self.cfg.threaded = threaded

    @context('reexec')
    def testCriticalUpdateFromChangesets(self):
        # CNY-1776 - make sure we write absolute paths in the changeset index
        # file
        repos = self.openRepository()
        client = conaryclient.ConaryClient(self.cfg)
        trvNames = ['foo', 'bar', 'corecomp']
        for idx, name in enumerate(trvNames):
            for ver in ['1', '2']:
                self.addComponent('%s:run' % name, ver, filePrimer=3*idx)
                self.addComponent('%s:walk' % name, ver, filePrimer=(3*idx+1))
                self.addCollection(name, ver, [':run', ':walk'])
                fname = "%s-%s.ccs" % (name, ver)
                self.changeset(repos, "%s=%s" % (name, ver),
                               os.path.join(self.workDir, fname))

        # First, install some version of the troves
        installs = ['foo=1', 'bar=1', 'corecomp=1']
        self.discardOutput(self.updatePkg, installs, raiseError=True)

        os.chdir(os.path.join(self.workDir, '..'))
        csList = [ changeset.ChangeSetFromFile(
                    client.lzCache.open(
                        os.path.join(
                            os.path.basename(self.workDir), '%s-2.ccs' % x)
                    ))
                    for x in ['foo', 'corecomp'] ]

        # update using changeset files
        updateInfo = conaryclient.CriticalUpdateInfo()
        updateInfo.setCriticalTroveRegexps(['corecomp:.*'])

        itemsList = [
            ('foo', (None, None), ('2', None), True),
            ('corecomp', (None, None), ('2', None), True),
        ]
        updJob = client.newUpdateJob()
        suggMap = client.prepareUpdateJob(updJob, itemsList, migrate=True,
            fromChangesets = csList, criticalUpdateInfo = updateInfo)

        restartInfo = client.applyUpdateJob(updJob)
        self.assertTrue(restartInfo)

        # Make sure we have the fromChangesets references in the critical
        # update info
        invocationInfoFile = os.path.join(restartInfo, "job-invocation")
        uj = client.newUpdateJob()
        uj.loadInvocationInfo(invocationInfoFile)
        fcs = uj.getFromChangesets()
        self.assertEqual(len(csList), len(fcs))

        os.chdir(self.workDir)

        updJob = client.newUpdateJob()
        # This should not fail
        suggMap = client.prepareUpdateJob(updJob, None,
                                          restartInfo=restartInfo)
        client.close()
        util.rmtree(restartInfo, ignore_errors = True)

    def testNoRestart(self):
        for v in ['1', '2']:
            self.addComponent("conary:runtime", v)
            self.addCollection("conary", v, [':runtime'])
        # Regular update, will restart
        try:
            self.captureOutput(self.updatePkg, self.rootDir,
                               'conary=1', raiseError=True)
        except errors.ReexecRequired as e:
            self.captureOutput(self.updatePkg, self.rootDir,
                               'conary=1', raiseError=True, restartInfo=e.data)
        else:
            self.fail("Expected a ReexecRequired")

        # No output, and no error
        ret, rets = self.captureOutput(self.updatePkg, self.rootDir,
            'conary=2', noRestart=True, raiseError=True)
        self.assertFalse(rets)

    def testThreadingOutput(self):

        class MyUpdateCallback(updatecmd.UpdateCallback):

            @updatecmd.locked
            def update(self):
                pass

            @updatecmd.locked
            def realUpdate(self):
                # Capture self.csText before and after execution
                val1 = self.csText
                # Signal the main thread it can go ahead and modify
                # self.csText
                self.q.put((None, None))
                import time; time.sleep(2)
                val2 = self.csText
                # Put the values in the queue
                self.q.put((val1, val2))

        import queue
        from threading import Thread

        q = queue.Queue(2)
        cb = MyUpdateCallback()
        cb.csMsg("ORIGINAL")
        cb.q = q

        cbThread = Thread(None, cb.realUpdate)
        cbThread.start()
        # Wait for thread to start - read first message
        val1, val2 = q.get(True, 1)
        self.assertEqual(val1, None)
        self.assertEqual(val2, None)

        # Modify cb.csText (csMsg will call the no-op update)
        othThread = Thread(None, cb.csMsg, args=("MODIFIED", ))
        othThread.start()

        try:
            # Block for 3 seconds
            val1, val2 = q.get(True, 3)
            self.assertEqual(val1, val2)
        finally:
            cbThread.join(5)
            if cbThread.isAlive():
                raise Exception("Thread did not finish")
            othThread.join(5)
            if othThread.isAlive():
                raise Exception("Thread did not finish")

    def testCmdlineDumpOnRestart(self):
        restartDir = os.path.join(self.workDir, "restart-dir")
        util.mkdirChain(restartDir)
        def mockPrepareUpdateJob(self, updJob, applyList, **kwargs):
            updJob.jobs = True # empty jobs are short-circuited
            return {}

        def mockApplyUpdateJob(*args, **kwargs):
            return restartDir

        argv = ["some", "command", "to", "run"]
        # Make copy of argv since doUpdate modifies it in place
        self.mock(sys, "argv", list(argv))

        self.mock(conaryclient.ConaryClient, 'prepareUpdateJob',
                  mockPrepareUpdateJob)
        self.mock(conaryclient.ConaryClient, 'applyUpdateJob',
                  mockApplyUpdateJob)

        self.assertRaises(errors.ReexecRequired,
            updatecmd.doUpdate, self.cfg, ["test:runtime"])
        cmdlinefile = os.path.join(restartDir, 'cmdline')
        self.assertTrue(os.path.exists(cmdlinefile))
        import xmlrpc.client
        params, _ = xmlrpc.client.loads(open(cmdlinefile).read())
        self.assertEqual(params[0], argv)

    def test_UpdateTroves(self):
        # CNY-2102
        kws = dict(replaceFiles = True,
                   callback = updatecmd.UpdateCallback(self.cfg))
        self.assertRaises(conaryclient.NoNewTrovesError, 
                              updatecmd._updateTroves, self.cfg, [], **kws)

    def testCallbackCreatingDatabaseTransaction(self):
        # CNY-2327

        # Create a callback that accumulates the output into a list
        output = []
        class MyCallback(updatecmd.UpdateCallback):
            def creatingDatabaseTransaction(xy, troveNum, troveCount):
                output.append((troveNum, troveCount))

        for v in ['1', '2']:
            self.addComponent('foo:run', v,
                fileContents = [ ('/usr/share/foo/%02d' % i,
                                  'c %s %02d\n' % (v, i)) for i in range(5) ])
            self.addComponent('bar:run', v,
                fileContents = [ ('/usr/share/bar/%02d' % i,
                                  'c %s %02d\n' % (v, i)) for i in range(5) ])

        self.updatePkg([ 'foo:run=1', 'bar:run=1' ])

        callback = MyCallback(self.cfg)
        self.discardOutput(self.updatePkg, [ 'foo:run=2', 'bar:run=2' ],
            callback = callback)
        self.assertEqual(output, [(1, 2), (2, 2)])

    def testUpdateAllReplaceFiles(self):
        self.addComponent('foo:runtime=1', 
                            [
                             # config file, handled by replaceModifiedConfig
                             # files
                             ('/etc/foo', 'contents\n'),
                             # non-config file, handled by replaceModifiedFiles
                             ('/nonconfig', 'contents\n')])
        self.addComponent('foo:runtime=2',
                          [ # config file, handled by replaceModifiedConfig
                            # files
                            ('/etc/foo', 'newcontents\n'),
                            # non-config file, handled by replaceModifiedFiles
                            ('/nonconfig', 'newcontents\n'),
                            # file that didn't exist before, handled
                            # by replaceUnmanagedFiles
                            ('/newfile', 'newcontents\n'),
                            ])
        self.updatePkg('foo:runtime=1')

        # we're using the "test" keyword here to ensure that no changes 
        # actually occur on disk

        kw = dict(test=True)
        # test one: change config contents
        self.writeFile(self.rootDir + '/etc/foo', 'othercontents\n')
        # update w/o replaceModifiedConfigFile fails
        self.discardOutput(self.updateAll)
        assert(os.path.exists(self.rootDir + '/etc/foo.conflicts'))
        self.resetRoot()
        self.updatePkg('foo:runtime=1')
        self.writeFile(self.rootDir + '/etc/foo', 'othercontents\n')
        self.discardOutput(self.updateAll, replaceModifiedConfigFiles=True)
        assert(not os.path.exists(self.rootDir + '/etc/foo.conflicts'))
        self.resetRoot()
        self.updatePkg('foo:runtime=1')

        # test two: try updating after changine non-config contents
        self.writeFile(self.rootDir + '/nonconfig', 'othercontents\n')
        # update w/o flags gets an error
        err = self.assertRaises(update.UpdateError, self.discardOutput,
                                self.updateAll, test=True) 
        assert(str(err) == '''\
changeset cannot be applied:
applying update would cause errors:
file contents conflict for %s/nonconfig''' % self.rootDir)
        # update with flags succeeds
        self.discardOutput(self.updateAll, replaceModifiedFiles=True, test=True)

        # test three: make a file at /newfile - this will conflict
        # with a file added in foo=2
        # fixed by replaceModifiedFiles
        self.writeFile(self.rootDir + '/newfile', 'othercontents\n')

        err = self.assertRaises(update.UpdateError, self.discardOutput,
                                self.updateAll, replaceModifiedFiles=True,
                                test=True)
        assert(str(err) == '''\
changeset cannot be applied:
applying update would cause errors:
%s/newfile is in the way of a newly created file in foo:runtime=/localhost@rpl:linux/2-1-1[]''' % self.rootDir)
        self.discardOutput(self.updateAll, replaceModifiedFiles=True,
                           replaceUnmanagedFiles=True, test=True)

        # Make /newfile a file managed by another package.
        # test four: make a file at /newfile owned by bar.
        # fixed by replaceManagedFiles
        self.addComponent('bar:runtime', [('/newfile', 'othercontents\n')])
        self.updatePkg('bar:runtime')
        err = self.assertRaises(update.UpdateError, self.discardOutput,
                                self.updateAll, replaceModifiedFiles=True,
                                replaceUnmanagedFiles=True, test=True)
        assert(str(err) == '''\
changeset cannot be applied:
applying update would cause errors:
%s/newfile conflicts with a file owned by bar:runtime=/localhost@rpl:linux/1.0-1-1[]''' % self.rootDir)
        self.discardOutput(self.updateAll, replaceModifiedFiles=True,
                           replaceManagedFiles=True, test=True)

        # replaceFiles does all options at once.
        self.discardOutput(self.updateAll, replaceFiles=True, test=True)


class JsonUpdateCallbackTest(testhelp.TestCase):
    json_fields = ("phase_name", "percent", "rate", "step", "done", "jobs",
                   "step_name", "phase", "total", "phase_total", "step_total",)

    def validateJson(self, **kwargs):
        if not self.out.write._mock.calls:
            if kwargs:
                self.fail("No calls but expected: '%s'" % kwargs)
            return

        args, keywords = self.out.write._mock.calls[-1]
        self.out.write._mock.calls.remove((args, keywords))
        if keywords:
            self.fail("got keywords in call")

        for key, value in json.loads(args[0]).items():
            self.assertTrue(key in self.json_fields)
            if key in kwargs:
                self.assertEqual((key, value), (key, kwargs[key]))

    def setUp(self):
        testhelp.TestCase.setUp(self)
        self.callback = updatecmd.JsonUpdateCallback()
        mock.mock(self.callback, 'out')
        self.out = self.callback.out

    def test_message(self):
        self.callback._message('foo')
        self.out.write._mock.assertCalled('foo\n')

    def test_capsuleSync(self):
        self.callback._capsuleSync('foo', 1)
        self.validateJson(phase_name="Capsule sync", step=1, step_name="foo",
                          phase=1, phase_total=3, step_total=3)

    def test_applyUpdate(self):
        self.callback._applyUpdate('foo')
        self.validateJson(phase_name="Apply update", step=1, step_name="foo",
                          phase=3, phase_total=3, step_total=1)

        self.callback.updateHunk = (0, 0)
        self.callback._applyUpdate('foo')
        self.validateJson(phase_name="Apply update", step=1, step_name="foo",
                          phase=3, phase_total=3, step_total=1)

        self.callback.updateHunk = (1, 3)
        self.callback._applyUpdate('foo')
        self.validateJson(phase_name="Apply update", step=1, step_name="foo",
                          phase=3, phase_total=3, step_total=3)

    def test_applyUpdateCS(self):
        self.callback._applyUpdateCS('foo')
        self.validateJson(phase_name="Apply update", step=1, step_name="foo",
                          phase=3, phase_total=3, step_total=1)

        self.callback.updateHunk = (0, 0)
        self.callback._applyUpdateCS('foo')
        self.validateJson(phase_name="Apply update", step=1, step_name="foo",
                          phase=3, phase_total=3, step_total=1)

        self.callback.updateHunk = (1, 3)
        self.callback._applyUpdateCS('foo')
        self.validateJson(phase_name="Apply update", step=1, step_name="foo",
                          phase=3, phase_total=3, step_total=3)

    def testUpdate(self):
        '''Test update method's json output'''
        # we always override percent with calcuated values
        self.callback.updateText = dict(percent=100)
        self.callback.update()
        self.validateJson(percent=None)

        # don't calculate percent if done or total is not set
        self.callback.updateText = dict(done=10)
        self.callback.update()
        self.validateJson(percent=None, done=10)

        self.callback.updateText = dict(total=10)
        self.callback.update()
        self.validateJson(percent=None, total=10)

        # Don't calculate percent if total is 0 (divide by zero)
        self.callback.updateText = dict(total=0, done=5)
        self.callback.update()
        self.validateJson(percent=None, total=0, done=5)

        # calculate percent
        self.callback.updateText = dict(total=10, done=5)
        self.callback.update()
        self.validateJson(percent=50, total=10, done=5)

        self.callback.updateText = dict(total=10, done=0)
        self.callback.update()
        self.validateJson(percent=0, total=10, done=0)

        # csText overrides updateText
        self.callback.csText = dict(percent=20)
        self.callback.update()
        self.validateJson(percent=None)

    def testCallbackMethods(self):
        self.callback.executingSystemModel()
        self.validateJson(step_name="Processing system model", step=2)

        self.callback.loadingModelCache()
        self.validateJson(step_name="Loading system model cache", step=1)

        self.callback.savingModelCache()
        self.validateJson(step_name="Saving system model cache", step=4)

        self.callback.preparingChangeSet()
        self.validateJson(step_name="Preparing changeset request")

        self.callback.resolvingDependencies()
        self.validateJson(step_name="Resolving dependencies", step=3)

        self.callback.creatingRollback()
        self.validateJson(step_name="Creating rollback")

        self.callback.preparingUpdate(1, 2)
        self.validateJson(step_name="Preparing update", done=1, total=2,
                          percent=50)

        self.callback.restoreFiles(1024, 0)
        self.validateJson()

        self.callback.restoreFiles(1024, 10240)
        self.validateJson(step_name="Restoring Files", done=1, total=10,
                          percent=10)

        self.callback.removeFiles(1, 0)
        self.validateJson()

        self.callback.removeFiles(1, 1)
        self.validateJson(step_name="Removing Files", done=1, total=1,
                          percent=100)

        self.callback.creatingDatabaseTransaction(1, 1)
        self.validateJson(step_name="Creating database transaction", done=1,
                          total=1, percent=100)

        self.callback.updatingDatabase('latest', 1, 2)
        self.validateJson(step_name="Updating list of latest versions", done=1,
                          total=2, percent=50)

        self.callback.updatingDatabase('other', 1, 2)
        self.validateJson(step_name="Updating database", done=1,
                          total=2, percent=50)

        self.callback.runningPreTagHandlers()
        self.validateJson(step_name="Running tag prescripts")

        self.callback.runningPostTagHandlers()
        self.validateJson(step_name="Running tag post-scripts")

        self.callback.committingTransaction()
        self.validateJson(step_name="Committing database transaction")

        mock.mock(self.callback, 'formatter')
        self.callback.formatter.formatJobTups._mock.setReturn((
                'install foo',
                'update bar',
                ), ['foo', 'bar'])
        self.callback.setUpdateJob(['foo', 'bar'])
        self.validateJson(step_name='Applying update job', jobs=[
                dict(action='install', trove='foo'),
                dict(action='update', trove='bar'),
                ])

        self.callback.capsuleSyncScan('rpm')
        self.validateJson(step_name="Scanning for rpm capsule changes", step=1)

        self.callback.capsuleSyncCreate('rpm', 'foo', 1, 2)
        self.validateJson(step_name="Collecting modifications to rpm database",
                          step=2, done=1, total=2, percent=50)

        self.callback.rate = 100
        self.callback.downloadingFileContents(1024, 2048)
        self.validateJson(step_name="Downloading files for changeset",
                          done=1024, total=2048, rate=100, percent=50)

        self.callback.downloadingChangeSet(1024, 2048)
        self.validateJson(setp_name="Downloading", done=1024, total=2048,
                          rate=100, percent=50)

        self.callback.csHunk = (0, 0)
        self.callback.requestingFileContents()
        self.validateJson(step_name="Requesting file contents for changeset",
                          done=1, total=1, percent=100)

        self.callback.csHunk = (1, 2)
        self.callback.requestingFileContents()
        self.validateJson(step_name="Requesting file contents for changeset",
                          done=1, total=2, percent=50)

        self.callback.csHunk = (0, 0)
        self.callback.requestingChangeSet()
        self.validateJson(step_name="Requesting changeset",
                          done=1, total=1, percent=100)

        self.callback.csHunk = (1, 2)
        self.callback.requestingChangeSet()
        self.validateJson(step_name="Requesting changeset",
                          done=1, total=2, percent=50)

        self.callback.troveScriptOutput('post', 'foo')
        self.validateJson(step_name="[post] foo")

        self.callback.troveScriptFailure('post', 'foo')
        self.validateJson(step_name="[post] foo")

        self.callback.capsuleSyncApply(1, 2)
        self.validateJson(
            step_name='Synchronizing database with capsule changes', step=3)
