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
Module used by recipes to modify the state of the installed C{%(destdir)s}.
for test suite preparation and fixing directory modes so that all the
subsequent directory policy can run.
"""
import os
import stat

#conary imports
from conary.lib import magic
from conary.lib import util
from conary.build import policy, recipe

class TestSuiteLinks(policy.Policy):
    """
    NAME
    ====
    B{C{r.TestSuiteLinks()}} - Indicate extra files to link into the test
    directory

    SYNOPSIS
    ========
    C{r.TestSuiteLinks([I{filterexp}] || [I{fileMap='<builddir_path>' : '<destdir_path>'}])}

    DESCRIPTION
    ===========
    The C{r.TestSuiteLinks()} class is called from within a Conary recipe to
    indicate extra files which should be linked into the test suite directory.

    Files specified by the filter expression can override standard exclusions,
    such as the document, man, info, or init directories.  Alternatively, a
    fileMap may be expressed. A fileMap is a dictionary each key is a path to
    a symlink that will be created in the test directory, and the value
    pointed to by that key is the path of a file installed in the destination
    directory that the symlink should point to.

    In order for C{r.TestSuiteLinks()} to be useful, you must indicate to
    Conary that you wish to create a test suite.  To create a test suite,
    you should use the C{r.TestSuite()} class as documented in
    conary.build.build.

    EXAMPLES
    ========
    C{r.TestSuiteLinks(fileMap={'client/mysqladmin': '/usr/bin/mysqladmin'})}

    The fileMap convention of C{r.TestSuiteLinks()} is used to indicate an
    extra test suite file comprised of the symbolic link C{client/mysqladmin}
    which in turn, points to the file C{/usr/bin/mysqladmin}.
    """
    bucket = policy.TESTSUITE
    requires = (
        ('TestSuiteFiles', policy.REQUIRED_SUBSEQUENT),
    )

    # build is an internal interface, used by r.TestSuite to indicate that
    # TestSuiteLinks should run
    keywords = { 'build': None,
                 'fileMap' : {}
                }
    invariantexceptions = [
                            '%(mandir)s/',
                            '%(infodir)s/',
                            '%(docdir)s/',
                            '%(initdir)s/',
                            #test dir itself as well
                            #as anything below it
                            '%(testdir)s',
                            '%(testdir)s/' ]

    buildTestSuite = None

    def updateArgs(self, *args, **keywords):
        # XXX add fileMap param, to map from builddir -> destdir files
        # could update automatically when we install a file?
        # but then we remove/move a file after installing,
        # we'd have to update map
        build = keywords.pop('build', None)
        if build is not None:
            self.buildTestSuite = build

        fileMap = keywords.pop('fileMap', None)
        if fileMap is not None:
            self.fileMap.update(fileMap)
        policy.Policy.updateArgs(self, *args, **keywords)

    def test(self):
        return not self.recipe.getType() == recipe.RECIPE_TYPE_CAPSULE

    def do(self):
        if not self.buildTestSuite:
            self.recipe.TestSuiteFiles(build=False)
            return

        # expand macros in fileMap
        newFileMap = {}
        for (buildfile, destfile) in self.fileMap.items():
            newFileMap[util.normpath(buildfile % self.macros)] = destfile % self.macros
        self.fileMap = newFileMap

        self.builddirfiles = {}
        self.builddirlinks = {}
        builddir = self.macros.builddir
        builddirlen = len(builddir)
        for root, dirs, files in os.walk(builddir):
            for file in files:
                realDir = root[builddirlen:]
                realPath = os.path.join(realDir, file)
                if realPath in self.fileMap:
                    continue
                fullpath = os.path.join(root, file)

                if os.path.islink(fullpath):
                    # symlink handling:
                    # change to absolute link and add to symlink list
                    contents = os.readlink(fullpath)
                    if contents[0] != '/':
                        contents = util.normpath(os.path.join(root, contents))[builddirlen:]
                    if contents not in self.builddirlinks:
                        self.builddirlinks[contents] = []
                    self.builddirlinks[contents].append(os.path.join(realDir, file))
                else:
                    # add to regular file list
                    if file not in self.builddirfiles:
                        self.builddirfiles[file] = []
                    self.builddirfiles[file].append(realDir)


        if self.buildTestSuite:
            for (buildfile, destfile) in self.fileMap.items():
                target = destfile
                link = util.normpath('%(destdir)s%(thistestdir)s/' % self.macros + buildfile)
                util.mkdirChain(os.path.dirname(link))
                os.symlink(target, link)

            self.recipe.TestSuiteFiles(build=True)
            self.recipe.TestSuiteFiles(builddirlinks=self.builddirlinks)
            policy.Policy.do(self)
        else:
            self.recipe.TestSuiteFiles(build=False)
            return


    def doFile(self, path):
        fullpath = self.macros.destdir + path
        if os.path.islink(fullpath):
            return

        fileName = os.path.basename(path)
        if fileName in self.builddirfiles:
            dirName = self.findRightFile(fullpath, fileName,
                         self.builddirfiles[fileName])
            if dirName is None:
                return

            # if destdir file eq to symlink in builddir
            if os.path.islink(self.macros.builddir + dirName + os.sep
                               + fileName):
                return

            testpath = ('%(destdir)s%(thistestdir)s' + os.sep + dirName
                         + os.sep + fileName) % self.macros
            util.mkdirChain(os.path.dirname(testpath))

            if os.path.islink(testpath):
                buildpath = ''.join((self.macros.builddir, dirName, os.sep, fileName))
                if self.betterLink(self.macros.destdir + path, testpath, buildpath):
                    util.remove(testpath)
                    os.symlink(util.normpath(path), testpath)
                else:
                    return
            else:
                os.symlink(util.normpath(path), testpath)

            # we've added a builddir file to the testdir,
            # see if there was a symlink in the builddir
            # that pointed to it
            builddirFile = os.path.join(dirName, fileName)
            if builddirFile in self.builddirlinks:
                for path in self.builddirlinks[builddirFile]:
                    linkpath = '%(destdir)s%(thistestdir)s' % self.macros + os.sep + path
                    if not os.path.islink(linkpath):
                        util.mkdirChain(os.path.dirname(linkpath))
                        os.symlink('%(thistestdir)s/' % self.macros + dirName
                                    + os.sep + fileName, linkpath)


    def betterLink(self, newpath, testpath, buildpath):
        """
        betterLink determines whether the destdir file
        I{newpath} is a better match than the destdir file pointed
        to by the current symlink at I{testpath} for the builddir
        file I{buildpath}.
        """
        # sample test: duplicates behavior in findRightfile
        #newsize = os.stat(newpath)[stat.ST_SIZE]
        #buildsize = os.stat(buildpath)[stat.ST_SIZE]
        #if(newsize == buildsize):
        #    return True
        #else:
        #    return False
        return False

    def findRightFile(self, fullpath, fileName, dirList):
        """
        Search for the best match in buildir for the the destdirfile
        fullPath.  Match I{fullpath} against %(builddir)/dir/fileName for each
        directory in I{dirList}
        """

        # XXX need to cache size/diff info
        exactMatchPossible = True
        builddir = self.macros.builddir
        builddirlen = len(builddir)
        size = os.stat(fullpath)[stat.ST_SIZE]

        fileList = [ ''.join([builddir, dir, os.sep, fileName]) for dir in dirList ]
        newFileList = [ file for file in fileList if size == os.stat(file)[stat.ST_SIZE] ]
        if len(newFileList) == 1:
            return os.path.dirname(newFileList[0])[builddirlen:]
        elif len(newFileList) > 1:
            # narrow down our search and continue looking
            fileList = newFileList
        else:
            exactMatchPossible = False

        # okay, there either at least two entries who passed,
        # or no entries who passed -- next heuristic

        if exactMatchPossible:
            # we found at least 2 that were the same size --
            # let's try for the more expensive diff
            # since this should be a very small set of files
            for file in fileList:
                fd = util.popen('diff %s %s' % (fullpath, file))
                results = fd.read()
                if results == "":
                    return os.path.dirname(file)[builddirlen:]

        # I give up: don't make the link because it's probably wrong
        # XXX other, missing tests: magic, close filenames, close sizes
        # destdirlen = len(self.macros.destdir)
        # self.warn('Could not determine which builddir file %s corresponds to for creating test component', fullpath[destdirlen:])
        return None


class TestSuiteFiles(policy.Policy):
    """
    NAME
    ====
    B{C{r.TestSuiteFiles}} - Indicate extra files to copy into the test directory

    SYNOPSIS
    ========
    C{r.TestSuiteFiles([filterexp])}

    DESCRIPTION
    ===========
    The C{r.TestSuiteFiles()} class is called from within a Conary recipe to
    indicate extra files which should be copied into the test suite directory.
    The filter expression is relative to the build directory, not the install
    directory as is the case with the rest of the destdir policies.

    Files included in the filterexp will be copied into the test directory,
    with their path relative to the build dir retained.  E.g. a file found
    at %(builddir)s/bin/foo will be copied to %(testdir)s/bin/foo.

    In order for C{r.TestSuiteFiles()} to be useful, you must indicate to
    Conary that you wish to create a test suite.  To create a test suite,
    you should use the C{r.TestSuite()} class as documented in
    conary.build.build.

    EXAMPLES
    ========
    C{r.TestSuiteFiles('MANIFEST')}

    The file C{MAINFEST} is marked to be copied into the package's test suite
    directory.
    """
    bucket = policy.TESTSUITE
    requires = (
        ('TestSuiteLinks', policy.REQUIRED_PRIOR),
    )

    filetree = policy.BUILDDIR
    buildTestSuite = None

    invariantexceptions = [ ( '.*', stat.S_IFDIR ), ]

    invariantinclusions = [ '.*[tT][eE][sS][tT].*',
                            'Makefile.*',
                            '.*\.exp', #used by dejagnu
                            '.*/config.*',
                            '.*/shconfig',
                            '.*/acconfig.*',
                            '.*/aclocal.*',
                            '.*\.la', ]

    keywords = { 'build': None,
                 'builddirlinks' : None}

    def test(self):
        return not self.recipe.getType() == recipe.RECIPE_TYPE_CAPSULE

    def do(self):
        if self.buildTestSuite is False:
            return
        policy.Policy.do(self)

    def updateArgs(self, *args, **keywords):
        """
        call as C{TestSuiteFiles(<inclusions>)}.
        @keyword build: If set to true, will create TestSuiteFiles even if a TestSuite command is not given.  If set to false, will not create TestSuiteFiles even if a TestSuite command is given.  Also turns on/off TestSuiteLinkx
        """
        build = keywords.pop('build', None)
        builddirlinks = keywords.pop('builddirlinks', None)
        if build is not None:
            self.buildTestSuite = build
        if builddirlinks is not None:
            self.builddirlinks = builddirlinks
        policy.Policy.updateArgs(self, *args, **keywords)

    def doFile(self, path):
        fullpath = self.macros.builddir + path
        testpath = self.macros.destdir + self.macros.thistestdir + path
        if not (os.path.islink(testpath) or os.path.exists(testpath)):
            if os.path.islink(fullpath):
                contents = os.readlink(fullpath)
                # only install symlinks by default if they point outside of the builddir
                if contents[0] == '/' and not contents.startswith(self.macros.builddir):
                    util.mkdirChain(os.path.dirname(testpath))
                    os.symlink(contents, testpath)
            elif os.path.isfile(fullpath):
                util.mkdirChain(os.path.dirname(testpath))
                util.copyfile(fullpath, testpath, verbose=False)
                self.replaceBuildPath(testpath)
                # finally, include any symlinks that point
                # to the file we just copied
                if path in self.builddirlinks:
                    for linkpath in self.builddirlinks[path]:
                        linkpath = '%(destdir)s%(thistestdir)s' % self.macros + linkpath
                        if not os.path.islink(linkpath):
                            util.mkdirChain(os.path.dirname(linkpath))
                            os.symlink(self.macros.thistestdir + path, linkpath)

    def replaceBuildPath(self, path):
        #Now remove references to builddir, but
        # we don't want to mess with binaries
        # XXX probbaly need a better check for binary status
        m = magic.magic(path, basedir='/')
        extension = path.split('.')[-1]
        if m and m.name != 'ltwrapper':
            return
        if extension in ('pyo', 'pyc'): # add as needed
            return
        util.execute(("sed -i -e 's|%%(builddir)s|%%(testdir)s/%%(name)s-%%(version)s|g' '%s'" % path) % self.macros, verbose=False)



class FixDirModes(policy.Policy):
    """
    NAME
    ====
    B{C{r.FixDirModes}} - Modifies directory permissions

    DESCRIPTION
    ===========
    The C{r.FixDirModes()} class modifies directory permissions which
    would otherwise prevent Conary from packaging C{%(destdir)s} as a
    non-root user.

    Any directories that do not have user read/write/execute must be
    fixed up first so that Conary can traverse the tree in following policy,
    packaging, and removing the tree after building.

    EXAMPLES
    ========
    This class is B{not} invoked from recipes directly.
    """
    # This policy must be run first so that other policies can be
    # counted on to search the full directory tree.
    bucket = policy.DESTDIR_PREPARATION
    processUnmodified = True
    requires = (
        ('setModes', policy.REQUIRED_SUBSEQUENT),
    )
    # call doFile for all directories that are not readable, writeable,
    # and executable for the user
    invariantinclusions = [ ('.*', stat.S_IFDIR) ]
    invariantexceptions = [ ('.*', 0o700) ]

    def test(self):
        # Capsule packages handle this separately
        return not self.recipe.getType() == recipe.RECIPE_TYPE_CAPSULE

    def doFile(self, path):
        fullpath = self.macros.destdir + path
        mode = os.lstat(fullpath)[stat.ST_MODE]
        if not self.recipe._getCapsulePathsForFile(path):
            self.recipe.setModes(path, userbits=(mode & 0o700))
        os.chmod(fullpath, mode | 0o700)
