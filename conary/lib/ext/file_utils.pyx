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
Collection of wrappers around file-related C functions.
"""

include "common.pxi"


cdef extern from "unistd.h" nogil:
    int getdtablesize()
    int lstat(char *path, stat *buf)
    int c_fchmod "fchmod"(int fd, int mode)
    int mkdir(char *pathname, int mode)
    int unlink(char *pathname)
    ssize_t c_pread "pread"(int fd, void *buf, size_t count, off_t offset)
    ssize_t c_pwrite "pwrite"(int fd, void *buf, size_t count, off_t offset)

cdef extern from "poll.h" nogil:
    cdef struct pollfd:
        int fd
        short events
        short revents
    int poll(pollfd *fds, int nfds, int timeout)
    int POLLIN, POLLPRI, POLLOUT, POLLNVAL

cdef extern from "Python.h":
    void *PyMem_Malloc(size_t)
    void PyMem_Free(void *)


def countOpenFileDescriptors():
    """Return a count of the number of open file descriptors."""
    cdef int maxfd, count, i, rc
    cdef pollfd *ufds

    maxfd = getdtablesize()
    ufds = <pollfd*>PyMem_Malloc(maxfd * sizeof(pollfd))
    if ufds == NULL:
        raise MemoryError

    with nogil:
        for i in range(maxfd):
            ufds[i].fd = i
            ufds[i].events = POLLIN | POLLPRI | POLLOUT

        # Loop until poll() succeeds without being interrupted by a signal
        while True:
            rc = poll(ufds, maxfd, 0)
            if rc >= 0 or errno != EINTR:
                break

    if rc < 0:
        PyMem_Free(ufds)
        PyErr_SetFromErrno(OSError)

    count = 0
    for i in range(maxfd):
        if ufds[i].revents != POLLNVAL:
            count += 1

    PyMem_Free(ufds)
    return count


def fchmod(fobj, int mode):
    """Change the permissions of an open file."""
    cdef int fd, rc
    fd = PyObject_AsFileDescriptor(fobj)
    with nogil:
        rc = c_fchmod(fd, mode)
    if rc == -1:
        PyErr_SetFromErrno(OSError)


def fopenIfExists(char *path, char *mode):
    """Open a file, or return C{None} if opening failed."""
    cdef FILE *fp

    with nogil:
        fp = fopen(path, mode)

    if fp == NULL:
        return None
    else:
        return PyFile_FromFd(fp, path, mode, fclose)


def lexists(char *path):
    """Return C{True} if C{path} exists."""
    cdef stat sb
    if lstat(path, &sb) == -1:
        if errno in (ENOENT, ENOTDIR, ENAMETOOLONG, EACCES):
            return False
        PyErr_SetFromErrnoWithFilename(OSError, path)
    return True


def massCloseFileDescriptors(int start, int count, int end):
    """Close file descriptors from C{start} to either C{end} or after C{count}
    unused descriptors have been encountered."""
    cdef int i, j, rc

    if count and end:
        raise ValueError("Exactly one of count and end must be zero.")

    rc = 0
    i = start
    j = count
    with nogil:
        while True:
            if count:
                # Stopping after a contiguous number of fds
                if j == 0:
                    break
            elif i == end:
                # Stopping at specific value
                break

            rc = close(i)
            if rc == 0:
                # Successful close -- reset contiguous counter
                j = count
            elif errno == EBADF:
                # FD was not in use
                j -= 1
            else:
                # Some other error
                break
            rc = 0
            i += 1

    if rc != 0:
        PyErr_SetFromErrno(OSError)


def mkdirIfMissing(char *path):
    """Make a directory at C{path} if it does not exist."""
    if mkdir(path, 0777) == -1:
        if errno == EEXIST:
            return False
        PyErr_SetFromErrnoWithFilename(OSError, path)
    return True


def pread(fobj, size_t count, off_t offset):
    """Read C{count} bytes at C{offset} in file C{fobj}."""
    cdef Py_ssize_t rc
    cdef char *data
    cdef int fd

    fd = PyObject_AsFileDescriptor(fobj)

    data = <char*>PyMem_Malloc(count)
    if data == NULL:
        raise MemoryError

    with nogil:
        rc = c_pread(fd, data, count, offset)

    if rc == -1:
        PyMem_Free(data)
        PyErr_SetFromErrno(OSError)

    ret = PyString_FromStringAndSize(data, rc)
    PyMem_Free(data)
    return ret


def pwrite(fobj, bytes data_p, off_t offset):
    """Write C{data} at C{offset} in file C{fobj}."""
    cdef Py_ssize_t rc
    cdef int fd
    cdef size_t count
    cdef char *data_b

    fd = PyObject_AsFileDescriptor(fobj)
    data_b = data_p
    count = len(data_p)

    with nogil:
        rc = c_pwrite(fd, data_b, count, offset)

    if rc == -1:
        PyErr_SetFromErrno(OSError)
    return rc


def removeIfExists(char *path):
    """Try to unlink C{path}, but don't fail if it doesn't exist."""
    if unlink(path):
        if errno in (ENOENT, ENAMETOOLONG):
            return False
        PyErr_SetFromErrnoWithFilename(OSError, path)
    return True
