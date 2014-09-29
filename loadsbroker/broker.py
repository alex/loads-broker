"""Broker Orchestration

The Broker is responsible for:

* Coordinating runs
* Ensuring run transitions
* Providing a rudimentary public API for use by the CLI/Web clients

"""
import os
import time
from collections import namedtuple
from datetime import datetime
from functools import partial

from tornado import gen

from loadsbroker import logger, aws
from loadsbroker.api import _DEFAULTS
from loadsbroker.db import (
    Database,
    Run,
    Strategy,
    ContainerSet,
    RUNNING,
    TERMINATING,
    COMPLETED,
    status_to_text,
)


class Broker:
    def __init__(self, io_loop, sqluri, ssh_key, ssh_username, aws_port=None,
                 aws_owner_id="595879546273", aws_use_filters=True,
                 aws_access_key=None, aws_secret_key=None):
        self.loop = io_loop
        user_data = _DEFAULTS["user_data"]
        if user_data is not None and os.path.exists(user_data):
            with open(user_data) as f:
                user_data = f.read()

        self.pool = aws.EC2Pool("1234", user_data=user_data,
                                io_loop=self.loop, port=aws_port,
                                owner_id=aws_owner_id,
                                use_filters=aws_use_filters,
                                ssh_keyfile=ssh_key,
                                access_key=aws_access_key,
                                secret_key=aws_secret_key)

        self.db = Database(sqluri, echo=True)
        self.sqluri = sqluri
        self.ssh_key = ssh_key
        self.ssh_username = ssh_username

        # Run managers keyed by uuid
        self._runs = {}

    def shutdown(self):
        self.pool.shutdown()
        self._print_status()

    @gen.coroutine
    def _print_status(self):
        while True:
            if not len(self._runs):
                logger.debug("Status: No runs in progress.")
            for uuid, mgr in self._runs.items():
                run = mgr.run
                logger.debug("Run state for %s: %s - %s", run.uuid,
                             status_to_text(mgr.state), mgr.state_description)
            yield gen.Task(self.loop.add_timeout, time.time() + 10)

    def get_runs(self):
        # XXX filters, batching
        runs = self.db.session().query(Run).all()
        return [run.json() for run in runs]

    @gen.coroutine
    def _test(self, session, mgr, future):
        try:
            response = yield future
            logger.debug("Got response of: %s", response)
        except:
            logger.error("Got an exception", exc_info=True)

        # logger.debug("Reaping the pool")
        # yield self.pool.reap_instances()
        # logger.debug("Finished terminating.")

    def run_test(self, **options):
        session = self.db.session()
        curl = "https://s3.amazonaws.com/loads-images/simpletest-dev.tar.gz"

        strategy = session.query(Strategy).filter_by(name='strategic!').first()
        if not strategy:
            # the first thing to do is to create a container set and a strategy
            cs = ContainerSet(
                name='yeah',
                instance_count=1,
                container_name="bbangert/simpletest:dev",
                container_url=curl)
            strategy = Strategy(
                name='strategic!',
                container_sets=[cs])
            session.add(strategy)
            session.commit()

        # now we can start a new run
        mgr, future = RunManager.new_run(session, self.pool,
                                         self.loop, 'strategic!')

        callback = partial(self._test, session, mgr)
        future.add_done_callback(callback)
        self._runs[mgr.run.uuid] = mgr
        return mgr.run.uuid


class ContainerSetLink(namedtuple('ContainerSetLink',
                                  'running meta collection')):
    """Named tuple that links a EC2Collection to the metadata
    describing its container running info and the running db instance
    of it."""


class RunManager:
    """Manages the life-cycle of a load run.

    """
    def __init__(self, db_session, pool, io_loop, run):
        self.run = run
        self._db_session = db_session
        self._pool = pool
        self._loop = io_loop
        self._set_links = []
        self.abort = False
        self.state_description = ""
        # XXX see what should be this time
        self.sleep_time = .1

    @classmethod
    def new_run(cls, db_session, pool, io_loop, strategy_name):
        """Create a new run manager for the given strategy name

        This creates a new run for this strategy and initializes it.

        :param db_session: SQLAlchemy database session
        :param pool: AWS EC2Pool instance to allocate from
        :param io_loop: A tornado io loop
        :param strategy_name: The strategy name to use for this run

        :returns: New RunManager in the process of being initialized,
                  along with a future tracking the run.

        """
        # Create the run for this manager
        run = Run.new_run(db_session, strategy_name)
        db_session.add(run)
        db_session.commit()

        run_manager = cls(db_session, pool, io_loop, run)
        future = run_manager.start()
        return run_manager, future

    @classmethod
    def recover_run(cls, run_uuid):
        """Given a run uuid, fully reconstruct the run manager state"""
        pass

    @property
    def uuid(self):
        return self.run.uuid

    @property
    def state(self):
        return self.run.state

    @gen.coroutine
    def _get_container_sets(self):
        """Request all the container sets instances needed from the pool

        This is a separate method as both the recover run and new run
        will need to run this identically.

        """
        csets = self.run.strategy.container_sets
        collections = yield [
            self._pool.request_instances(self.run.uuid, c.uuid,
                                         count=c.instance_count,
                                         inst_type=c.instance_type,
                                         region=c.instance_region)
            for c in csets]

        try:
            # Setup the collection lookup info
            coll_by_uuid = {x.uuid: x for x in csets}
            running_by_uuid = {x.container_set.uuid: x
                               for x in self.run.running_container_sets}
            for coll in collections:
                meta = coll_by_uuid[coll.uuid]
                running = running_by_uuid[coll.uuid]
                setlink = ContainerSetLink(running, meta, coll)
                self._set_links.append(setlink)

                # Setup the container info
                coll.set_container(meta.container_name,
                                   meta.container_url,
                                   env_data=meta.environment_data,
                                   command_args=meta.additional_command_args)
        except Exception:
            # Ensure we return collections if something bad happened
            logger.error("Got an exception in runner, returning instances",
                         exc_info=True)

            try:
                yield [self._pool.return_instances(x) for x in collections]
            except:
                logger.error("Wat? Got an error returning instances.",
                             exc_info=True)

            # Clear out the setlinks to make sure they aren't cleaned up
            # again
            self._set_links = []

    @gen.coroutine
    def cleanup(self):
        # Ensure we always release the collections we used
        logger.debug("Returning collections")
        try:
            yield [self._pool.return_instances(x.collection)
                   for x in self._set_links]
        except Exception:
            logger.error("Embarassing, error returning instances.",
                         exc_info=True)

        self._set_links = []

    @gen.coroutine
    def start(self):
        """Fully manage a complete run

        This doesn't return until the run is complete. A reference
        should be held so that the run state can be checked on as
        needed while this is running. This method chains to all the
        individual portions of a run.

        """
        try:
            # Initialize the run
            yield self._initialize()

            # Start and manage the run
            yield self._run()

            # Terminate the run
            yield self._shutdown()
        finally:
            yield self.cleanup()

        return True

    @gen.coroutine
    def _initialize(self):
        # Initialize all the collections, this needs to always be done
        # just in case we're recovering
        yield self._get_container_sets()

        # Skip if we're running
        if self.state == RUNNING:
            return

        # Wait for docker on all the collections to come up
        self.state_description = "Waiting for docker"
        yield [x.collection.wait_for_docker() for x in self._set_links]

        # Pull the appropriate container for every collection
        self.state_description = "Pulling container images"
        yield [x.collection.load_container() for x in self._set_links]
        self.state_description = ""

        self.run.state = RUNNING
        self.run.started_at = datetime.utcnow()
        self._db_session.commit()

    @gen.coroutine
    def _run(self):
        # Skip if we're not running
        if self.state != RUNNING:
            return

        # We're not done until every collection has been run, and is now
        # done
        while True:
            if self.abort:
                break

            # First, only consider collections not completed
            running_collections = [x for x in self._set_links
                                   if not x.running.completed_at]

            # See which are done, and ensure they get shut-down
            dones = yield [self.collection_is_done(x)
                           for x in running_collections]

            # Send shutdown to all finished collections, we're not going
            # to wait on these futures, they will save when they complete
            for done, setlink in zip(dones, self._set_links):
                if not done:
                    continue

                def save_completed(fut):
                    setlink.running.completed_at = datetime.utcnow()
                    self._db_session.commit()
                    try:
                        fut.result()
                    except:
                        logger.error("Exception in shutdown.", exc_info=True)

                future = setlink.collection.shutdown()
                future.add_done_callback(save_completed)

            # If they're all done and have all been started (ie, maybe there
            # were gaps in the container sets on purpose)
            all_started = all([x.collection.started
                               for x in running_collections])
            if all(dones) and all_started:
                break

            # Not every collection has been started, check to see which
            # ones should be started and start them, then sleep
            starts = yield [self.collection_should_start(x)
                            for x in self._set_links]

            for start, setlink in zip(starts, self._set_links):
                if not start:
                    continue

                def save_started(fut):
                    setlink.running.started_at = datetime.utcnow()
                    self._db_session.commit()
                    try:
                        fut.result()
                    except:
                        logger.error("Exception starting.", exc_info=True)

                future = setlink.collection.start()
                future.add_done_callback(save_started)

            # Now we sleep for a bit
            yield gen.Task(self._loop.add_timeout, time.time() +
                           self.sleep_time)

        # We're done running, time to terminate
        self.run.state = TERMINATING
        self.run.completed_at = datetime.utcnow()
        self._db_session.commit()

    @gen.coroutine
    def _shutdown(self):
        # If we aren't terminating, we shouldn't have been called
        if self.state != TERMINATING:
            return

        # Tell all the collections to shutdown
        yield [x.collection.shutdown() for x in self._set_links]
        self.run.state = COMPLETED
        self._db_session.commit()

    @gen.coroutine
    def collection_is_done(self, setlink):
        """Given a ContainerSetLink, determine
        if the collection has finished or should be terminated."""
        # If the collection has no instances running the container, its done
        instances_running = yield setlink.collection.is_running()
        if not instances_running:
            return True

        # If we haven't been started, we can't be done
        if not setlink.running.started_at:
            return False

        # Otherwise return whether we should be stopped
        return setlink.running.should_stop()

    @gen.coroutine
    def collection_should_start(self, setlink):
        """Given a ContainerSetLink, determine
        if the collection should be started."""
        # If the collection is already running, this is a moot point since
        # we can't start it again
        if setlink.collection.started:
            return False

        # If we've waited longer than the delay
        return setlink.running.should_start()
