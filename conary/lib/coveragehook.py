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
    Imports our modified version of coverage.py which has support for the COVERAGE_DIR
    directive.  If the COVERAGE_DIR environment variable is set, it will automatically
    start running coverage.  Forks will continue to run coverage, to a different pid.
"""
import atexit
import imp
import os
import signal
import traceback
import sys

from conary.lib import util

def install():
    """
        Starts the coverage tool if the coverage dir directive is set.
    """
    if os.environ.get('COVERAGE_DIR', None):
        _install()

def save():
    if 'coverage' in sys.modules:
        sys.modules['coverage'].the_coverage.save()
        _install()

def _save():
    sys.modules['coverage'].the_coverage.save()
    _install()

def _install():
    coverageLoc = os.environ.get('COVERAGE_PATH', None)
    if not coverageLoc:
        raise RuntimeError('cannot find coverage.py!')
    else:
        coverageLoc = coverageLoc + '/coverage.py'

    coverageDir = os.environ.get('COVERAGE_DIR', None)
    if not coverageDir:
        raise RuntimeError('COVERAGE_DIR must be set to a path for cache file')
    util.mkdirChain(coverageDir)

    if ('coverage' in sys.modules
        and (sys.modules['coverage'].__file__ == coverageLoc
             or sys.modules['coverage'].__file__ == coverageLoc + 'c')):
        coverage = sys.modules['coverage']
    else:
        coverage = imp.load_source('coverage', coverageLoc)
    the_coverage = coverage.the_coverage
    if hasattr(the_coverage, 'pid') and the_coverage.pid == os.getpid():
        _run(coverage)
        return
    elif hasattr(the_coverage, 'pid'):
        _reset(coverage)

    _installOsWrapper()
    _run(coverage)
    return

def _saveState(signal, f):
    save()
    os._exit(1)

def _run(coverage):
    signal.signal(signal.SIGUSR2, _saveState)
    atexit.register(coverage.the_coverage.save)
    coverage.the_coverage.start()

origOsFork = os.fork
origOsExit = os._exit
origExecArray = {}

for exectype in 'l', 'le', 'lp', 'lpe', 'v', 've', 'vp', 'vpe':
    fnName = 'exec' + exectype
    origExecArray[fnName] = getattr(os, fnName)

def _installOsWrapper():
    """
        wrap fork to automatically start a new coverage
        file with the forked pid.
    """
    global origOsFork
    global origOsExit
    def fork_wrapper():
        pid = origOsFork()
        if pid:
            return pid
        else:
            _reset(sys.modules['coverage'])
            _run(sys.modules['coverage'])
            return 0

    def exit_wrapper(*args):
        try:
            sys.modules['coverage'].the_coverage.save()
        except:
            print('Uncaught exception while saving coverage in exit_wrapper:')
            traceback.print_exc()
        origOsExit(*args)

    def exec_wrapper(fn):
        def _exec_wrapper(*args, **kw):
            sys.modules['coverage'].the_coverage.save()
            return fn(*args, **kw)
        return _exec_wrapper

    if os.fork is origOsFork:
        os.fork = fork_wrapper

    if os._exit is origOsExit:
        os._exit = exit_wrapper

    for fnName, origFn in origExecArray.items():
        curFn = getattr(os, fnName)
        if curFn is origFn:
            setattr(os, fnName, exec_wrapper(origFn))

def _reset(coverage):
    coverage.c.disable()
    coverage.c.clear()
    coverage.the_coverage = None
    coverage.the_coverage = coverage.coverage()


install()
