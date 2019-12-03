#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, unicode_literals
from logging import getLogger
from time import time as _time
import threading
import Queue
import heapq
from collections import deque

import xbmc

from . import utils, app, variables as v

LOG = getLogger('PLEX.threads')


class KillableThread(threading.Thread):
    def __init__(self, group=None, target=None, name=None, args=(), kwargs={}):
        self._canceled = False
        self._suspended = False
        self._is_not_suspended = threading.Event()
        self._is_not_suspended.set()
        self._suspension_reached = threading.Event()
        self._is_not_asleep = threading.Event()
        self._is_not_asleep.set()
        self.suspension_timeout = None
        super(KillableThread, self).__init__(group, target, name, args, kwargs)

    def should_cancel(self):
        """
        Returns True if the thread should be stopped immediately
        """
        return self._canceled or app.APP.stop_pkc

    def cancel(self):
        """
        Call from another thread to stop this current thread
        """
        self._canceled = True
        # Make sure thread is running in order to exit quickly
        self._is_not_suspended.set()
        self._is_not_asleep.set()

    def should_suspend(self):
        """
        Returns True if the current thread should be suspended immediately
        """
        return self._suspended

    def suspend(self, block=False, timeout=None):
        """
        Call from another thread to suspend the current thread. Provide a
        timeout [float] in seconds optionally. block=True will block the caller
        until the thread-to-be-suspended is indeed suspended
        Will wake a thread that is asleep!
        """
        self.suspension_timeout = timeout
        self._suspended = True
        self._is_not_suspended.clear()
        # Make sure thread wakes up in order to suspend
        self._is_not_asleep.set()
        if block:
            self._suspension_reached.wait()

    def resume(self):
        """
        Call from another thread to revive a suspended or asleep current thread
        back to life
        """
        self._suspended = False
        self._is_not_suspended.set()
        self._is_not_asleep.set()

    def wait_while_suspended(self):
        """
        Blocks until thread is not suspended anymore or the thread should
        exit or for a period of self.suspension_timeout (set by the caller of
        suspend())
        Returns the value of should_cancel()
        """
        self._suspension_reached.set()
        self._is_not_suspended.wait(self.suspension_timeout)
        self._suspension_reached.clear()
        return self.should_cancel()

    def is_suspended(self):
        """
        Check from another thread whether the current thread is suspended
        """
        return self._suspension_reached.is_set()

    def sleep(self, timeout):
        """
        Only call from the current thread in order to sleep for a period of
        timeout [float, seconds]. Will unblock immediately if thread should
        cancel (should_cancel()) or the thread should_suspend
        """
        self._is_not_asleep.clear()
        self._is_not_asleep.wait(timeout)
        self._is_not_asleep.set()

    def is_asleep(self):
        """
        Check from another thread whether the current thread is asleep
        """
        return not self._is_not_asleep.is_set()


class ProcessingQueue(Queue.Queue, object):
    """
    Queue of queues that processes a queue completely before moving on to the
    next queue. There's one queue per Section(). You need to initialize each
    section with
        ProcessingQueue().add_section(section)
    first.
    Put tuples (count, item) into this queue, with count being the respective
    position of the item in the queue, starting with 0 (zero).
    """
    def _init(self, maxsize):
        self.queue = deque()
        self._sections = deque()
        self._queues = deque()
        self._current_section = None
        self._current_queue = None
        self._counter = 0

    def _qsize(self):
        return self._current_queue._qsize() if self._current_queue else 0

    def total_size(self):
        """
        Return the approximate total size of all queues (not reliable!)
        """
        self.mutex.acquire()
        n = sum(q._qsize() for q in self._queues) if self._queues else 0
        self.mutex.release()
        return n

    def put(self, item, block=True, timeout=None):
        """Put an item into the queue.

        If optional args 'block' is true and 'timeout' is None (the default),
        block if necessary until a free slot is available. If 'timeout' is
        a non-negative number, it blocks at most 'timeout' seconds and raises
        the Full exception if no free slot was available within that time.
        Otherwise ('block' is false), put an item on the queue if a free slot
        is immediately available, else raise the Full exception ('timeout'
        is ignored in that case).
        """
        self.not_full.acquire()
        try:
            if self.maxsize > 0:
                if not block:
                    if self._qsize() == self.maxsize:
                        raise Queue.Full
                elif timeout is None:
                    while self._qsize() == self.maxsize:
                        self.not_full.wait()
                elif timeout < 0:
                    raise ValueError("'timeout' must be a non-negative number")
                else:
                    endtime = _time() + timeout
                    while self._qsize() == self.maxsize:
                        remaining = endtime - _time()
                        if remaining <= 0.0:
                            raise Queue.Full
                        self.not_full.wait(remaining)
            index = self._put(item)
            self.unfinished_tasks += 1
            if index == 0:
                # Only notify one waiting thread if this item is put into
                # current queue
                self.not_empty.notify()
        finally:
            self.not_full.release()

    def _put(self, item):
        """
        Returns the index of the section in whose subqueue we need to put the
        item into
        """
        for i, section in enumerate(self._sections):
            if item[1]['section'] == section:
                self._queues[i]._put(item)
                break
        else:
            raise RuntimeError('Could not find section for item %s' % item[1])
        return i

    def put_sentinel(self, section):
        """
        Adds a new empty section as a sentinel. Call with an empty Section()
        object.
        Once the get()-method returns None, you've received the sentinel and
        you've thus exhausted the queue
        """
        self.not_full.acquire()
        try:
            section.number_of_items = 1
            section.failed_items = 0
            self._add_section(section)
            # Add the actual sentinel to the queue we just added
            self._queues[-1]._put((None, None))
            self.unfinished_tasks += 1
            if self.unfinished_tasks == 1:
                # queue was already exhausted!
                self.not_empty.notify()
        finally:
            self.not_full.release()

    def add_section(self, section):
        self.mutex.acquire()
        try:
            self._add_section(section)
        finally:
            self.mutex.release()

    def _add_section(self, section):
        self._sections.append(section)
        self._queues.append(
            OrderedQueue(self.mutex) if section.plex_type == v.PLEX_TYPE_ALBUM
            else Queue.Queue())
        if self._current_section is None:
            self._switch_queues()

    def _init_next_section(self):
        self._sections.popleft()
        self._queues.popleft()
        self._counter = 0
        self._switch_queues()

    def _switch_queues(self):
        self._current_section = self._sections[0] if self._sections else None
        self._current_queue = self._queues[0] if self._queues else None

    def get(self, block=True, timeout=None):
        """Remove and return an item from the queue.

        If optional args 'block' is true and 'timeout' is None (the default),
        block if necessary until an item is available. If 'timeout' is
        a non-negative number, it blocks at most 'timeout' seconds and raises
        the Empty exception if no item was available within that time.
        Otherwise ('block' is false), return an item if one is immediately
        available, else raise the Empty exception ('timeout' is ignored
        in that case).
        """
        self.not_empty.acquire()
        try:
            if not block:
                if not self._qsize():
                    raise Queue.Empty
            elif timeout is None:
                while not self._qsize():
                    self.not_empty.wait()
            elif timeout < 0:
                raise ValueError("'timeout' must be a non-negative number")
            else:
                endtime = _time() + timeout
                while not self._qsize():
                    remaining = endtime - _time()
                    if remaining <= 0.0:
                        raise Queue.Empty
                    self.not_empty.wait(remaining)
            item = self._get()
            self.not_full.notify()
            return item
        finally:
            self.not_empty.release()

    def _get(self):
        item = self._current_queue._get()
        self._counter += 1
        if self._counter == self._current_section.number_of_items - self._current_section.failed_items:
            self._init_next_section()
        return item[1]


class OrderedQueue(Queue.PriorityQueue, object):
    """
    Queue that enforces an order on the items it returns. An item you push
    onto the queue must be a tuple
        (index, item)
    where index=-1 is the item that will be returned first. The Queue will block
    until index=-1, 0, 1, 2, 3, ... is then made available
    """
    def __init__(self, lock, maxsize=0):
        super(OrderedQueue, self).__init__(maxsize)
        self.next_index = 0
        self.not_next_item = threading.Condition(lock)

    def _put(self, item, heappush=heapq.heappush):
        heappush(self.queue, item)
        if item[0] == self.next_index:
            self.not_next_item.notify()

    def _get(self, heappop=heapq.heappop):
        """
        Careful - will block!
        """
        while self.queue[0][0] != self.next_index:
            self.not_next_item.wait()
        self.next_index += 1
        return heappop(self.queue)


class Tasks(list):
    def add(self, task):
        for t in self:
            if not t.isValid():
                self.remove(t)

        if isinstance(task, list):
            self += task
        else:
            self.append(task)

    def cancel(self):
        while self:
            self.pop().cancel()


class Task(object):
    def __init__(self, priority=None):
        self.priority = priority
        self._canceled = False
        self.finished = False

    def __cmp__(self, other):
        return self.priority - other.priority

    def start(self):
        BGThreader.addTask(self)

    def _run(self):
        self.run()
        self.finished = True

    def run(self):
        raise NotImplementedError

    def cancel(self):
        self._canceled = True

    def should_cancel(self):
        return self._canceled or xbmc.abortRequested

    def isValid(self):
        return not self.finished and not self._canceled


class FunctionAsTask(Task):
    def __init__(self, function, callback, *args, **kwargs):
        self._function = function
        self._callback = callback
        self._args = args
        self._kwargs = kwargs
        super(FunctionAsTask, self).__init__()

    def run(self):
        result = self._function(*self._args, **self._kwargs)
        if self._callback:
            self._callback(result)


class MutablePriorityQueue(Queue.PriorityQueue):
    def _get(self, heappop=heapq.heappop):
            self.queue.sort()
            return heappop(self.queue)

    def lowest(self):
        """Return the lowest priority item in the queue (not reliable!)."""
        self.mutex.acquire()
        try:
            lowest = self.queue and min(self.queue) or None
        except Exception:
            lowest = None
            utils.ERROR()
        finally:
            self.mutex.release()
        return lowest


class BackgroundWorker(object):
    def __init__(self, queue, name=None):
        self._queue = queue
        self.name = name
        self._thread = None
        self._abort = False
        self._task = None

    @staticmethod
    def _runTask(task):
        if task._canceled:
            return
        try:
            task._run()
        except Exception:
            utils.ERROR()

    def abort(self):
        self._abort = True
        return self

    def aborted(self):
        return self._abort or xbmc.abortRequested

    def start(self):
        if self._thread and self._thread.isAlive():
            return

        self._thread = KillableThread(target=self._queueLoop, name='BACKGROUND-WORKER({0})'.format(self.name))
        self._thread.start()

    def _queueLoop(self):
        if self._queue.empty():
            return

        LOG.debug('(%s): Active', self.name)
        try:
            while not self.aborted():
                self._task = self._queue.get_nowait()
                self._runTask(self._task)
                self._queue.task_done()
                self._task = None
        except Queue.Empty:
            LOG.debug('(%s): Idle', self.name)

    def shutdown(self):
        self.abort()

        if self._task:
            self._task.cancel()

        if self._thread and self._thread.isAlive():
            LOG.debug('thread (%s): Waiting...', self.name)
            self._thread.join()
            LOG.debug('thread (%s): Done', self.name)

    def working(self):
        return self._thread and self._thread.isAlive()


class NonstoppingBackgroundWorker(BackgroundWorker):
    def __init__(self, queue, name=None):
        self._working = False
        super(NonstoppingBackgroundWorker, self).__init__(queue, name)

    def _queueLoop(self):
        while not self.aborted():
            try:
                self._task = self._queue.get_nowait()
                self._working = True
                self._runTask(self._task)
                self._working = False
                self._queue.task_done()
                self._task = None
            except Queue.Empty:
                app.APP.monitor.waitForAbort(0.05)

    def working(self):
        return self._working


class BackgroundThreader:
    def __init__(self, name=None, worker=BackgroundWorker, worker_count=6):
        self.name = name
        self._queue = MutablePriorityQueue()
        self._abort = False
        self.priority = -1
        self.workers = [worker(self._queue, 'queue.{0}:worker.{1}'.format(self.name, x)) for x in range(worker_count)]

    def _nextPriority(self):
        self.priority += 1
        return self.priority

    def abort(self):
        self._abort = True
        for w in self.workers:
            w.abort()
        return self

    def aborted(self):
        return self._abort or xbmc.abortRequested

    def shutdown(self):
        self.abort()

        for w in self.workers:
            w.shutdown()

    def addTask(self, task):
        task.priority = self._nextPriority()
        self._queue.put(task)
        self.startWorkers()

    def addTasks(self, tasks):
        for t in tasks:
            t.priority = self._nextPriority()
            self._queue.put(t)

        self.startWorkers()

    def addTasksToFront(self, tasks):
        lowest = self.getLowestPrority()
        if lowest is None:
            return self.addTasks(tasks)

        p = lowest - len(tasks)
        for t in tasks:
            t.priority = p
            self._queue.put(t)
            p += 1

        self.startWorkers()

    def startWorkers(self):
        for w in self.workers:
            w.start()

    def working(self):
        return not self._queue.empty() or self.hasTask()

    def hasTask(self):
        return any([w.working() for w in self.workers])

    def getLowestPrority(self):
        lowest = self._queue.lowest()
        if not lowest:
            return None

        return lowest.priority

    def moveToFront(self, qitem):
        lowest = self.getLowestPrority()
        if lowest is None:
            return

        qitem.priority = lowest - 1


class ThreaderManager:
    def __init__(self, worker=BackgroundWorker, worker_count=6):
        self.index = 0
        self.abandoned = []
        self._workerhandler = worker
        self.threader = BackgroundThreader(name=str(self.index),
                                           worker=worker,
                                           worker_count=worker_count)

    def __getattr__(self, name):
        return getattr(self.threader, name)

    def reset(self):
        if self.threader._queue.empty() and not self.threader.hasTask():
            return

        self.index += 1
        self.abandoned.append(self.threader.abort())
        self.threader = BackgroundThreader(name=str(self.index),
                                           worker=self._workerhandler)

    def shutdown(self):
        self.threader.shutdown()
        for a in self.abandoned:
            a.shutdown()


BGThreader = ThreaderManager()
