import os
from functools import partial

import tornado.ioloop

from .db import Database, Run, RUNNING, TERMINATED
from .awsctrl import AWSController
from .dockerctrl import DockerDaemon


KEY_PATH = '/Users/tarek/.ssh/loads.pem'
USER_NAME = 'core'


class Broker(object):
    def __init__(self, io_loop):
        self.loop = io_loop
        self.aws = AWSController(io_loop=self.loop)
        self.db = Database('sqlite:////tmp/loads.db', echo=True)

    def get_runs(self):
        # XXX filters, batching
        runs = self.db.session().query(Run).all()
        return [run.json() for run in runs]

    def _cb(self, func, *args, **kw):
        self.loop.add_callback(func, *args, **kw)

    def _test(self, run, session, instances):
        run.status = RUNNING
        session.commit()

        # do something with the nodes
        for instance in instances:
            self._cb(self._run_instance, instance)

        # terminate them
        self._cb(self.aws.terminate_run, run.uuid)

        # mark the state in the DB

        def set_state(state):
            run.state = state
            session.commit()

        self._cb(set_state, TERMINATED)

    def _run_instance(self, instance):
        name = instance.tags['Name']
        # let's try to do something with it.
        # first a few checks via ssh
        print('working with %s' % name)
        print(self.aws.run_command(instance, 'ls -lah', KEY_PATH, USER_NAME))

        # port 2375 should be answering something. let's hook
        # it with our DockerDaemon class
        d = DockerDaemon(host='tcp://%s:2375' % instance.public_dns_name)

        # let's list the containers
        print(d.get_containers())


    def run_test(self, **options):
        user_data = options.pop('user_data')
        run = Run(**options)

        session = self.db.session()

        session.add(run)
        session.commit()
        callback = partial(self._test, run, session)
        self.aws.reserve(run.uuid, run.nodes, run.ami,
                            user_data=user_data,
                            callback=callback)

        return run.uuid
