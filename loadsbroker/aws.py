"""AWS Higher Level Abstractions

This module contains higher-level AWS abstractions to make working with
AWS instances and collections of instances easier and less error-prone.

:ref:`EC2instance` is responsible for maintaining information about
itself and updating its state when asked to. The executer passed in
must be capable of running functions that may block, ie a Greenlet or
ThreadPool executor.

:ref:`EC2Collection` is a group of instances for a given allocation
request. Collections should be passed back to the Pool when their use
is no longer required.

An EC2 Pool is responsible for allocating and dispersing
:ref:`EC2Instance`s and terminating idle instances.

The :ref:`EC2Pool` is responsible for tracking EC2 instances across
regions, allocating them for use by the broker, and terminating
excessively idle instances. It also can rebuild maps of existing
instances by querying AWS for appropriate instance types.

"""
import concurrent.futures
import time
from collections import defaultdict, namedtuple
from datetime import datetime, timedelta

from boto.ec2 import connect_to_region
from tornado import gen
from tornado.concurrent import Future
import tornado.ioloop

from loadsbroker.exceptions import LoadsException
from loadsbroker import logger


AWS_REGIONS = (
    "ap-northeast-1", "ap-southeast-1", "ap-southeast-2",
    "eu-west-1",
    "sa-east-1",
    "us-east-1", "us-west-1", "us-west-2"
)


# Initial blank list of AMI ID's that will map a region to a dict keyed by
# virtualization type of the appropriate AMI to use
AWS_AMI_IDS = {k: {} for k in AWS_REGIONS}


def populate_ami_ids(aws_access_key_id=None, aws_secret_access_key=None,
                     port=None, owner_id="595879546273"):
    """Populate all the AMI ID's with the latest CoreOS stable info.

    This is a longer blocking operation and should be done on startup.
    """
    # see https://github.com/boto/boto/issues/2617
    if port is not None:
        is_secure = port == 443
    else:
        is_secure = True

    # Spin up a temp thread pool to make this faster
    errors = []

    def get_amis(region):
        try:
            conn = connect_to_region(
                region,
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key,
                port=port, is_secure=is_secure)

            filters = {}
            if owner_id is not None:
                filters["owner-id"] = owner_id

            images = conn.get_all_images(filters=filters)

            # The last two highest sorted are the pvm and hvm instance id's
            images = sorted([x for x in images if "stable" in x.name],
                            key=lambda x: x.name)[-2:]

            AWS_AMI_IDS[region] = {x.virtualization_type: x for x in images}
        except Exception as exc:
            errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(len(AWS_REGIONS)) as pool:
        # Execute all regions in parallel.
        pool.map(get_amis, AWS_REGIONS)

    if len(errors) > 0:
        raise errors[0]


def get_ami(region, instance_type):
    """Returns the appropriate AMI to use for a given region + instance type

    HVM is always used except for instance types which cannot use it. Based
    on matrix here:

    http://aws.amazon.com/amazon-linux-ami/instance-type-matrix/

    .. note::

        :ref:`populate_ami_ids` must be called first to populate the available
        AMI's.

    """
    instances = AWS_AMI_IDS[region]

    inst_type = "hvm"
    if instance_type[:2] in ["m1", "m2", "c1", "t1"]:
        inst_type = "paravirtual"

    if inst_type not in instances:
        raise KeyError("Could not find instance type %r in %s" % (
            inst_type,
            list(instances.keys())))

    return instances[inst_type].id


def available_instance(instance):
    """Returns True if an instance is usable for allocation.

    Instances are only usable if they're running, or have been
    "pending" for less than 2 minutes. Instances pending more than
    2 minutes are likely perpetually stalled and will be reaped.

    :type instance: :ref:`instance.Instance`
    :returns: Whether the instance should be used for allocation.
    :rtype: bool

    """
    if instance.state == "running":
        return True

    if instance.state == "pending":
        oldest = datetime.today() - timedelta(minutes=2)
        launched = datetime.strptime(instance.launch_time,
                                     '%Y-%m-%dT%H:%M:%S.%fZ')
        if oldest < launched:
            return True

    return False


class ExtensionState:
    pass


class EC2Instance(namedtuple('EC2Instance', 'instance state')):
    """EC2Instance that holds the underlying EC2.Instance object and
    configurable plugin state."""


class EC2Collection:
    """Create a collection to manage a set of instances.

    :type instances: list of :ref:`instance.Instance`

    """
    def __init__(self, run_id, uuid, conn, instances, io_loop=None):
        self.run_id = run_id
        self.uuid = uuid
        self.started = False
        self.finished = False
        self.conn = conn
        self._env_data = None
        self._command_args = None
        self._executer = concurrent.futures.ThreadPoolExecutor(len(instances))
        self._loop = io_loop or tornado.ioloop.IOLoop.instance()

        self.instances = []
        for inst in instances:
            self.instances.append(EC2Instance(inst, ExtensionState()))

    @gen.coroutine
    def wait(self, seconds):
        """Waits for ``seconds`` before resuming."""
        yield gen.Task(self._loop.add_timeout, time.time() + seconds)

    def execute(self, func, *args, **kwargs):
        """Execute a blocking function, return a future that will be
        called in the io loop.

        The blocking function will receive the underlying boto EC2
        instance object first, with the other args trailing.

        """
        fut = Future()

        def set_fut(future):
            exc = future.exception()
            if exc:
                fut.set_exception(exc)
            else:
                fut.set_result(future.result())

        def _throwback(fut):
            self._loop.add_callback(set_fut, fut)

        exc_fut = self._executer.submit(func, *args, **kwargs)
        exc_fut.add_done_callback(_throwback)
        return fut

    @gen.coroutine
    def map(self, func, *args, **kwargs):
        """Execute a blocking func with args/kwargs across all instances."""
        yield [self.execute(func, x, *args, **kwargs) for x in self.instances]

    def pending_instances(self):
        return [i for i in self.instances if i.instance.state == "pending"]

    def dead_instances(self):
        return [i for i in self.instances
                if i.instance.state not in ["pending", "running"]]

    def running_instances(self):
        return [i for i in self.instances if i.instance.state == "running"]

    @gen.coroutine
    def wait_for_running(self, interval=5, timeout=600):
        """Wait for all the instances to be running. Instances unable
        to load will be removed."""
        def update_state(inst):
            try:
                inst.instance.update()
            except Exception:
                # Updating state can fail, it happens
                pass

        end_time = time.time() + 600

        pending = self.pending_instances()

        while time.time() < end_time and pending:
            # Update the state of all the pending instances
            yield [self.execute(update_state, inst) for inst in pending]

            pending = self.pending_instances()

            # Wait if there's pending to check again
            if pending:
                yield self.wait(interval)

        # Remove everything that isn't running by now
        dead = self.dead_instances() + self.pending_instances()

        # Don't wait for the future that kills them
        logger.debug("Removing %d dead instances that wouldn't run.",
                     len(dead))
        self.remove_instances(dead)
        return True

    @gen.coroutine
    def remove_instances(self, ec2_instances):
        """Remove an instance entirely."""
        instances = [i.instance for i in ec2_instances]
        for inst in ec2_instances:
            self.instances.remove(inst)

        # Remove the tags
        yield self._executor.submit(
            self.conn.create_tags, [instances], {"RunId": "", "Uuid": ""})

        # Nuke them
        yield self._executor.submit(self.conn.terminate_instances, [instances])


class EC2Pool:
    """Initialize a pool for instance allocation and recycling.

    All instances allocated using this pool will be tagged as follows:

    Name
        loads-BROKER_ID
    Broker
        BROKER_ID
    Run (if this instance is currently associate with a Run)
        RUN_ID

    .. warning::

        This instance is **NOT SAFE FOR CONCURRENT USE BY THREADS**.

    """
    def __init__(self, broker_id, access_key=None, secret_key=None,
                 key_pair="loads", security="loads", max_idle=600,
                 user_data=None, io_loop=None, port=None,
                 owner_id="595879546273", use_filters=True):
        self.owner_id = owner_id
        self.use_filters = use_filters
        self.broker_id = broker_id
        self.access_key = access_key
        self.secret_key = secret_key
        self.max_idle = max_idle
        self.key_pair = key_pair
        self.security = security
        self.user_data = user_data
        self._instances = defaultdict(list)
        self._tag_filters = {"tag:Name": "loads-%s" % self.broker_id,
                             "tag:Project": "loads"}
        self._conns = {}
        self._recovered = {}
        self._executor = concurrent.futures.ThreadPoolExecutor(15)
        self._loop = io_loop or tornado.ioloop.IOLoop.instance()
        self.port = port
        # see https://github.com/boto/boto/issues/2617
        if port is not None:
            self.is_secure = port == 443
        else:
            self.is_secure = True

        # Asynchronously initialize ourself when the pool runs
        self._loop.add_future(
            self.initialize(),
            lambda x: logger.debug("Finished initializing. %s", x.result())
        )

        self.ready = Future()

    def shutdown(self):
        """Make sure we shutdown the executor.
        """
        self._executor.shutdown()

    def initialize(self):
        """Fully initialize the AWS pool and dependencies, recover existing
        instances, etc.

        :returns: A future that will require the loop running to retrieve.

        """
        logger.debug("Pulling CoreOS AMI info...")
        populate_ami_ids(self.access_key, self.secret_key, port=self.port,
                         owner_id=self.owner_id)
        return self._recover()

    def _initialized(self, future):
        # Run the result to ensure we raise an exception if any occurred
        future.result()
        logger.debug("Finished initializing.")
        self.ready.set_result(True)

    @gen.coroutine
    def _region_conn(self, region=None):
        if region in self._conns:
            return self._conns[region]

        # Setup a connection
        logger.debug("Requesting connection for region: %s", region)
        conn = yield self._executor.submit(
            connect_to_region, region,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            port=self.port, is_secure=self.is_secure)

        self._conns[region] = conn
        logger.debug("Returning connection for region: %s", region)
        return conn

    @gen.coroutine
    def _recover_region(self, region):
        """Recover all the instances in a region"""
        conn = yield self._region_conn(region)
        logger.debug("Requesting instances for %s", region)

        if self.use_filters:
            filters = self._tag_filters
        else:
            filters = {}

        instances = yield self._executor.submit(
            conn.get_only_instances,
            filters=filters)

        logger.debug("Finished requesting instances for %s", region)
        return instances

    @gen.coroutine
    def _recover(self):
        """Recover allocated instances from EC2."""
        recovered_instances = defaultdict(list)

        # Recover every region at once
        instancelist = yield [self._recover_region(x) for x in AWS_REGIONS]

        logger.debug("Found %s instances to recover.",
                     sum(map(len, instancelist)))

        for instances in instancelist:
            for instance in instances:
                tags = instance.tags

                # If this has been 'pending' too long, we put it in the main
                # instance pool for later reaping
                if not available_instance(instance):
                    self._instances[instance.region.name].append(instance)
                    continue

                if tags.get("RunId") and tags.get("Uuid"):
                    # Put allocated instances into a recovery pool separate
                    # from unallocated
                    inst_key = (tags["RunId"], tags["Uuid"])
                    recovered_instances[inst_key].append(instance)
                else:
                    self._instances[instance.region.name].append(instance)
        self._recovered = recovered_instances

    def _locate_recovered_instances(self, run_id, uuid):
        """Locates and removes existing allocated instances if any"""
        key = run_id, uuid

        if key not in self._recovered:
            # XXX do we want to raise here?
            return []

        instances = self._recovered[key]
        del self._recovered[key]
        return instances

    def _locate_existing_instances(self, count, inst_type, region):
        """Locates and removes existing available instances if any."""
        region_instances = self._instances[region]
        instances = []
        remaining = []

        for inst in region_instances:
            if available_instance(inst) and inst_type == inst.instance_type:
                    instances.append(inst)
            else:
                remaining.append(inst)

            if len(instances) > count:
                break

        # Determine how many were removed, and reconstruct the unallocated
        # instance list with the instances not used
        removed = len(instances) + len(remaining)
        self._instances[region] = region_instances[removed:] + remaining
        return instances

    @gen.coroutine
    def _allocate_instances(self, conn, count, inst_type, region):
        """Allocate a set of new instances and return them."""
        ami_id = get_ami(region, inst_type)
        reservations = yield self._executor.submit(
            conn.run_instances,
            ami_id, min_count=count, max_count=count,
            key_name=self.key_pair, security_groups=[self.security],
            user_data=self.user_data, instance_type=inst_type)

        return reservations.instances

    @gen.coroutine
    def request_instances(self, run_id, uuid, count=1, inst_type="t1.micro",
                          region="us-west-2"):
        """Allocate a collection of instances.

        :param run_id: Run ID for these instances
        :param uuid: UUID to use for this collection
        :param count: How many instances to allocate
        :param type: EC2 Instance type the instances should be
        :param region: EC2 region to allocate the instances in
        :returns: Collection of allocated instances
        :rtype: :ref:`EC2Collection`

        """
        if region not in AWS_REGIONS:
            raise LoadsException("Unknown region: %s" % region)

        # First attempt to recover instances for this run/uuid
        instances = self._locate_recovered_instances(run_id, uuid)
        remaining_count = count - len(instances)

        # Add any more remaining that should be used
        instances.extend(
            self._locate_existing_instances(remaining_count, inst_type, region)
        )

        conn = yield self._region_conn(region)

        # Determine if we should allocate more instances
        num = count - len(instances)
        if num > 0:
            new_instances = yield self._allocate_instances(
                conn, num, inst_type, region)
            logger.debug("Allocated instances: %s", new_instances)
            instances.extend(new_instances)

        # Tag all the instances
        if self.use_filters:
            yield self._executor.submit(
                conn.create_tags,
                [x.id for x in instances],
                {
                    "Name": "loads-%s" % self.broker_id,
                    "Project": "loads",
                    "RunId": run_id,
                    "Uuid": uuid
                }
            )
        return EC2Collection(run_id, uuid, conn, instances, self._loop)

    @gen.coroutine
    def release_instances(self, collection):
        """Return a collection of instances to the pool.

        :param collection: Collection to return
        :type collection: :ref:`EC2Collection`

        """
        region = collection.instances[0].instance.region.name
        instances = [x.instance for x in collection.instances]

        # De-tag the Run data on these instances
        conn = yield self._region_conn(region)

        if self.use_filters:
            yield self._executor.submit(
                conn.create_tags,
                [x.id for x in instances],
                {"RunId": "", "Uuid": ""})

        self._instances[region].extend(instances)

    @gen.coroutine
    def reap_instances(self):
        """Immediately reap all instances."""
        # Remove all the instances before yielding actions
        all_instances = self._instances
        self._instances = defaultdict(list)

        for region, instances in all_instances.items():
            conn = yield self._region_conn(region)

            # submit these instances for termination
            yield self._executor.submit(
                conn.terminate_instances,
                [x.id for x in instances])
