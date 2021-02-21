"""
psycopg3 connection pool
"""

# Copyright (C) 2021 The Psycopg Team

import time
import random
import logging
import threading
from abc import ABC, abstractmethod
from queue import Queue, Empty
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional, Tuple
from weakref import ref
from contextlib import contextmanager
from collections import deque

from . import errors as e
from .pq import TransactionStatus
from ._sched import Scheduler
from .connection import Connection

WORKER_TIMEOUT = 60.0

logger = logging.getLogger(__name__)


class PoolTimeout(e.OperationalError):
    pass


class PoolClosed(e.OperationalError):
    pass


class ConnectionPool:

    _num_pool = 0

    def __init__(
        self,
        conninfo: str = "",
        kwargs: Optional[Dict[str, Any]] = None,
        configure: Optional[Callable[[Connection], None]] = None,
        minconn: int = 4,
        maxconn: Optional[int] = None,
        name: Optional[str] = None,
        timeout: float = 30.0,
        max_idle: float = 10 * 60.0,
        reconnect_timeout: float = 5 * 60.0,
        reconnect_failed: Optional[Callable[["ConnectionPool"], None]] = None,
        num_workers: int = 3,
    ):
        if maxconn is None:
            maxconn = minconn
        if maxconn < minconn:
            raise ValueError(
                f"can't create {self.__class__.__name__}"
                f" with maxconn={maxconn} < minconn={minconn}"
            )
        if not name:
            self.__class__._num_pool += 1
            name = f"pool-{self._num_pool}"

        if num_workers < 1:
            # TODO: allow num_workers to be 0 - sync pool?
            raise ValueError("num_workers must be at least 1")

        self.conninfo = conninfo
        self.kwargs: Dict[str, Any] = kwargs or {}
        self._configure: Callable[[Connection], None]
        self._configure = configure or (lambda conn: None)
        self._reconnect_failed: Callable[["ConnectionPool"], None]
        self._reconnect_failed = reconnect_failed or (lambda pool: None)
        self.name = name
        self.minconn = minconn
        self.maxconn = maxconn
        self.timeout = timeout
        self.reconnect_timeout = reconnect_timeout
        self.max_idle = max_idle
        self.num_workers = num_workers

        self._nconns = minconn  # currently in the pool, out, being prepared
        self._pool: Deque[Tuple[Connection, float]] = deque()
        self._waiting: Deque["WaitingClient"] = deque()
        self._lock = threading.RLock()
        self._closed = False
        self.sched = Scheduler()

        self._wqueue: "Queue[MaintenanceTask]" = Queue()
        self._workers: List[threading.Thread] = []
        for i in range(num_workers):
            t = threading.Thread(target=self.worker, args=(self._wqueue,))
            t.daemon = True
            t.start()
            self._workers.append(t)

        self._sched_runner = threading.Thread(target=self.sched.run)
        self._sched_runner.daemon = True
        self._sched_runner.start()

        # Populate the pool with initial minconn connections
        event = threading.Event()
        for i in range(self._nconns):
            self.add_task(AddInitialConnection(self, event))

        # Wait for the pool to be full or throw an error
        if not event.wait(timeout=timeout):
            self.close()  # stop all the threads
            raise PoolTimeout(
                f"pool initialization incomplete after {timeout} sec"
            )

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__module__}.{self.__class__.__name__}"
            f" {self.name!r} at 0x{id(self):x}>"
        )

    @contextmanager
    def connection(
        self, timeout: Optional[float] = None
    ) -> Iterator[Connection]:
        """Context manager to obtain a connection from the pool.

        Returned the connection immediately if available, otherwise wait up to
        *timeout* or `self.timeout` and throw `PoolTimeout` if a
        connection is available in time.

        Upon context exit, return the connection to the pool. Apply the normal
        connection context behaviour (commit/rollback the transaction in case
        of success/error). If the connection is no more in working state
        replace it with a new one.
        """
        conn = self.getconn(timeout=timeout)
        try:
            with conn:
                yield conn
        finally:
            self.putconn(conn)

    def getconn(self, timeout: Optional[float] = None) -> Connection:
        """Obtain a contection from the pool.

        You should preferrably use `connection()`. Use this function only if
        it is not possible to use the connection as context manager.

        After using this function you *must* call a corresponding `putconn()`:
        failing to do so will deplete the pool. A depleted pool is a sad pool:
        you don't want a depleted pool.
        """
        logger.debug("connection requested to %r", self.name)
        # Critical section: decide here if there's a connection ready
        # or if the client needs to wait.
        with self._lock:
            if self._closed:
                raise PoolClosed(f"the pool {self.name!r} is closed")

            pos: Optional[WaitingClient] = None
            if self._pool:
                # Take a connection ready out of the pool
                conn = self._pool.pop()[0]
            else:
                # No connection available: put the client in the waiting queue
                pos = WaitingClient()
                self._waiting.append(pos)

                # If there is space for the pool to grow, let's do it
                if self._nconns < self.maxconn:
                    logger.debug("growing pool %r", self.name)
                    self._nconns += 1
                    self.add_task(AddConnection(self))

        # If we are in the waiting queue, wait to be assigned a connection
        # (outside the critical section, so only the waiting client is locked)
        if pos:
            if timeout is None:
                timeout = self.timeout
            conn = pos.wait(timeout=timeout)

        # Tell the connection it belongs to a pool to avoid closing on __exit__
        # Note that this property shouldn't be set while the connection is in
        # the pool, to avoid to create a reference loop.
        conn._pool = self
        logger.debug("connection given by %r", self.name)
        return conn

    def putconn(self, conn: Connection) -> None:
        """Return a connection to the loving hands of its pool.

        Use this function only paired with a `getconn()`. You don't need to use
        it if you use the much more comfortable `connection()` context manager.
        """
        # Quick check to discard the wrong connection
        pool = getattr(conn, "_pool", None)
        if pool is not self:
            if pool:
                msg = f"it comes from pool {pool.name!r}"
            else:
                msg = "it doesn't come from any pool"
            raise ValueError(
                f"can't return connection to pool {self.name!r}, {msg}: {conn}"
            )

        logger.debug("returning connection to %r", self.name)

        # If the pool is closed just close the connection instead of returning
        # it to the pool. For extra refcare remove the pool reference from it.
        if self._closed:
            conn._pool = None
            conn.close()
            return

        # Use a worker to perform eventual maintenance work in a separate thread
        self.add_task(ReturnConnection(self, conn))

    def _add_to_pool(self, conn: Connection) -> None:
        """
        Add a connection to the pool.

        The connection can be a fresh one or one already used in the pool.
        """
        # Remove the pool reference from the connection before returning it
        # to the state, to avoid to create a reference loop.
        # Also disable the warning for open connection in conn.__del__
        conn._pool = None

        self._reset_transaction_status(conn)
        if conn.pgconn.transaction_status == TransactionStatus.UNKNOWN:
            # Connection no more in working state: create a new one.
            logger.warning("discarding closed connection: %s", conn)
            self.add_task(AddConnection(self))
            return

        pos: Optional[WaitingClient] = None
        to_close: Optional[Connection] = None

        # Critical section: if there is a client waiting give it the connection
        # otherwise put it back into the pool.
        with self._lock:
            if self._waiting:
                # Extract the first client from the queue
                pos = self._waiting.popleft()
            else:
                now = time.monotonic()

                # No client waiting for a connection: put it back into the pool
                self._pool.append((conn, now))

                # Also check if it's time to shrink the pool
                if (
                    self._nconns > self.minconn
                    and now - self._pool[0][1] > self.max_idle
                ):
                    to_close, t0 = self._pool.popleft()
                    logger.debug(
                        "shrinking pool %r after connection unused for %s sec",
                        self.name,
                        now - t0,
                    )
                    self._nconns -= 1

        # If we found a client in queue, give it the connection and notify it
        if pos:
            pos.set(conn)

        if to_close:
            to_close.close()

    def _reset_transaction_status(self, conn: Connection) -> None:
        """
        Bring a connection to IDLE state or close it.
        """
        status = conn.pgconn.transaction_status
        if status == TransactionStatus.IDLE:
            return

        if status in (TransactionStatus.INTRANS, TransactionStatus.INERROR):
            # Connection returned with an active transaction
            logger.warning("rolling back returned connection: %s", conn)
            try:
                conn.rollback()
            except Exception as e:
                logger.warning(
                    "rollback failed: %s: %s. Discarding connection %s",
                    e.__class__.__name__,
                    e,
                    conn,
                )
                conn.close()

        elif status == TransactionStatus.ACTIVE:
            # Connection returned during an operation. Bad... just close it.
            logger.warning("closing returned connection: %s", conn)
            conn.close()

    @property
    def closed(self) -> bool:
        """`!True` if the pool is closed."""
        return self._closed

    def close(self) -> None:
        """Close the pool and make it unavailable to new clients.

        All the waiting and future client will fail to acquire a connection
        with a `PoolClosed` exception. Currently used connections will not be
        closed until returned to the pool.
        """
        with self._lock:
            self._closed = True

        # Now that the flag _closed is set, getconn will fail immediately,
        # putconn will just close the returned connection.

        # Stop the scheduler
        self.sched.enter(0, None)

        # Signal to eventual clients in the queue that business is closed.
        while self._waiting:
            pos = self._waiting.popleft()
            pos.fail(PoolClosed(f"the pool {self.name!r} is closed"))

        # Close the connections still in the pool
        while self._pool:
            conn = self._pool.pop()[0]
            conn.close()

        # Stop the worker threads
        for i in range(len(self._workers)):
            self.add_task(StopWorker(self))

    def add_task(self, task: "MaintenanceTask") -> None:
        """Add a task to the queue of tasts to perform."""
        self._wqueue.put(task)

    @classmethod
    def worker(cls, q: "Queue[MaintenanceTask]") -> None:
        """Runner to execute pending maintenance task.

        The function is designed to run as a separate thread.

        Block on the queue *q*, run a task received. Finish running if a
        StopWorker is received.
        """
        # Don't make all the workers time out at the same moment
        timeout = WORKER_TIMEOUT * (0.9 + 0.1 * random.random())
        while True:
            # Use a timeout to make the wait unterruptable
            try:
                task = q.get(timeout=timeout)
            except Empty:
                continue

            # Run the task. Make sure don't die in the attempt.
            try:
                task.run()
            except Exception as e:
                logger.warning(
                    "task run %s failed: %s: %s", task, e.__class__.__name__, e
                )

            if isinstance(task, StopWorker):
                return

    def _connect(self) -> Connection:
        """Return a new connection configured for the pool."""
        conn = Connection.connect(self.conninfo, **self.kwargs)
        self.configure(conn)
        conn._pool = self
        return conn

    def configure(self, conn: Connection) -> None:
        """Configure a connection after creation."""
        self._configure(conn)

    def reconnect_failed(self) -> None:
        """
        Called when reconnection failed for longer than `reconnect_timeout`.
        """
        self._reconnect_failed(self)


class WaitingClient:
    """An position in a queue for a client waiting for a connection."""

    __slots__ = ("event", "conn", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.conn: Connection
        self.error: Optional[Exception] = None

    def wait(self, timeout: float) -> Connection:
        """Wait for the event to be set and return the connection."""
        if not self.event.wait(timeout):
            raise PoolTimeout(f"couldn't get a connection after {timeout} sec")
        if self.error:
            raise self.error
        return self.conn

    def set(self, conn: Connection) -> None:
        """Signal the client waiting that a connection is ready."""
        self.conn = conn
        self.event.set()

    def fail(self, error: Exception) -> None:
        """Signal the client that, alas, they won't have a connection today."""
        self.error = error
        self.event.set()


class MaintenanceTask(ABC):
    """A task run asynchronously to maintain the pool state."""

    def __init__(self, pool: ConnectionPool):
        self.pool = ref(pool)
        logger.debug("task created: %s", self)

    def __repr__(self) -> str:
        pool = self.pool()
        name = repr(pool.name) if pool else "<pool is gone>"
        return f"<{self.__class__.__name__} {name} at 0x{id(self):x}>"

    def run(self) -> None:
        pool = self.pool()
        if not pool:
            # Pool has been deleted. Quietly discard operation.
            return

        logger.debug("task running: %s", self)
        self._run(pool)

    @abstractmethod
    def _run(self, pool: ConnectionPool) -> None:
        ...


class StopWorker(MaintenanceTask):
    """Signal the maintenance thread to terminate."""

    def _run(self, pool: ConnectionPool) -> None:
        pass


class AddInitialConnection(MaintenanceTask):
    """Add a new connection into to the pool.

    If the desired number of connections is reached notify the event.
    """

    def __init__(self, pool: ConnectionPool, event: threading.Event):
        super().__init__(pool)
        self.event = event

    def _run(self, pool: ConnectionPool) -> None:
        conn = pool._connect()
        pool._add_to_pool(conn)
        if len(pool._pool) >= pool._nconns:
            self.event.set()


class AddConnection(MaintenanceTask):
    INITIAL_DELAY = 1.0
    DELAY_JITTER = 0.1
    DELAY_BACKOFF = 2.0

    def __init__(self, pool: ConnectionPool):
        super().__init__(pool)
        self.delay = 0.0
        self.give_up_at = 0.0

    def _run(self, pool: ConnectionPool) -> None:
        try:
            conn = pool._connect()
        except Exception as e:
            logger.warning(f"error reconnecting in {pool.name!r}: {e}")
            self._handle_error(pool)
        else:
            pool._add_to_pool(conn)

    def _handle_error(self, pool: ConnectionPool) -> None:
        """Called after a connection failure.

        Calculate the new time for a new reconnection attempt and schedule a
        retry in the future. If too many attempts were performed, give up, by
        decreasing the pool connection number and calling
        `pool.reconnect_failed()`.
        """
        now = time.monotonic()
        if self.give_up_at and now >= self.give_up_at:
            logger.warning(
                "reconnection attempt in pool %r failed after %s sec",
                pool.name,
                pool.reconnect_timeout,
            )
            with pool._lock:
                pool._nconns -= 1
            pool.reconnect_failed()
            return

        # Calculate how long to wait for a new connection attempt
        if self.delay == 0.0:
            self.give_up_at = now + pool.reconnect_timeout
            # +/- 10% of the initial delay
            jitter = self.INITIAL_DELAY * (
                (2.0 * self.DELAY_JITTER * random.random()) - self.DELAY_JITTER
            )
            self.delay = self.INITIAL_DELAY + jitter
        else:
            self.delay *= self.DELAY_BACKOFF

        # Schedule a run of self.retry() some time in the future
        if now + self.delay < self.give_up_at:
            pool.sched.enter(self.delay, self.retry)
        else:
            pool.sched.enterabs(self.give_up_at, self.retry)

    def retry(self) -> None:
        pool = self.pool()
        if not pool:
            return

        pool.add_task(self)


class ReturnConnection(MaintenanceTask):
    """Clean up and return a connection to the pool."""

    def __init__(self, pool: ConnectionPool, conn: Connection):
        super().__init__(pool)
        self.conn = conn

    def _run(self, pool: ConnectionPool) -> None:
        pool._add_to_pool(self.conn)
