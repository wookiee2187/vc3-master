#!/usr/bin/env python


from vc3master.task import VC3Task
from vc3infoservice.infoclient import InfoConnectionFailure

from base64 import b64encode
import pluginmanager as pm
import traceback

import json
import os
import re
import subprocess

from novaclient import client as novaclient

class HandleHeadNodes(VC3Task):
    '''
    Plugin to manage the head nodes lifetime.
     
    '''

    def __init__(self, parent, config, section):
        super(HandleHeadNodes, self).__init__(parent, config, section)
        self.client = parent.client
        self.config = config

        nova_conf = {
                'version' : '2.0',
                'username' : self.config.get(section, 'username'),
                'password' : self.config.get(section, 'password'),
                'user_domain_name' : self.config.get(section, 'user_domain_name'),
                'project_domain_name' : self.config.get(section, 'project_domain_name'),
                'auth_url' : self.config.get(section, 'auth_url'),
                }

        self.nova = novaclient.Client( **nova_conf );

        self.node_image            = self.config.get(section, 'node_image')
        self.node_flavor           = self.config.get(section, 'node_flavor')
        self.node_user             = self.config.get(section, 'node_user')
        self.node_network_id       = self.config.get(section, 'node_network_id')
        self.node_private_key_file = os.path.expanduser(self.config.get(section, 'node_private_key_file'))
        self.node_public_key_name  = self.config.get(section, 'node_public_key_name')
        self.node_user_public_key_file = os.path.expanduser(self.config.get(section, 'node_user_public_key_file'))

        self.ansible_path       = os.path.expanduser(self.config.get(section, 'ansible_path'))
        self.ansible_playbook   = self.config.get(section, 'ansible_playbook')

        self.ansible_debug_file = os.path.expanduser(self.config.get(section, 'ansible_debug_file')) # temporary for debug, only works for one node at a time
        self.ansible_debug      = open(self.ansible_debug_file, 'a')

        groups = self.config.get(section, 'node_security_groups')
        self.node_security_groups = groups.split(',')

        self.initializers = {}

        self.log.debug("HandleHeadNodes VC3Task initialized.")

    def runtask(self):
        self.log.info("Running task %s" % self.section)
        self.log.debug("Polling master....")

        try:
            requests = self.client.listRequests()
            n = len(requests) if requests else 0
            self.log.debug("Processing %d requests" % n)
            if requests:
                for r in requests:
                    try:
                        self.process_request(r)
                    except Exception, e:
                        self.log.warning("Request %s had an exception (%s)", r.name, e)
                        self.log.debug(traceback.format_exc(None))
        except InfoConnectionFailure, e:
            self.log.warning("Could not read requests from infoservice. (%s)", e)

    def process_request(self, request):
        next_state  = None
        reason      = None

        self.log.debug("Processing headnode for '%s'", request.name)

        if request.headnode and request.headnode.has_key('state') and request.headnode['state'] == 'terminated':
            return

        if request.state == 'cleanup' or request.state == 'terminated':
            self.terminate_server(request)

        elif request.state == 'validated':

            if request.headnode is None:
                self.create_server(request)

            if request.headnode['state'] == 'booting' and self.check_if_online(request):
                self.initialize_server(request)

            if request.headnode['state'] == 'initializing' and self.check_if_done_init(request):
                self.report_running_server(request)

        else:
            return

        try:
            # hack to force info update (changing single fields does not trigger update)
            request.headnode = request.headnode
            self.client.storeRequest(request)
        except Exception, e:
            self.log.warning("Storing the new request state failed. (%s)", e)
            self.log.warning(traceback.format_exc(None))


    def terminate_server(self, request):
        try:
            if request.headnode['state'] == 'terminated':
                return

            if self.initializers[request.name]:
                try:
                    proc = self.initializers[request.name]
                    self.initializers[request.name] = None
                    proc.terminate()
                except Exception, e:
                    self.log.warning('Exception while killing initializer for %s: %s', request.name, e)

            server = self.nova.servers.find(name=request.name)
            self.log.debug('Teminating headnode at %s for request %s', request.headnode, request.name)
            server.delete()

        except Exception, e:
            self.log.warning('Could not find headnode for request %s (%s)', request.name, e)

        request.headnode['state'] = 'terminated'

    def create_server(self, request):
        request.headnode = {}

        server = self.boot_server(request)
        if not server:
            request.headnode['state'] = 'failure'
            raise('Could not boot headnode for request %s', request.name)

        self.log.debug('Waiting for headnode for request %s to come online', request.name)
        request.headnode['state'] = 'booting'


    def check_if_online(self, request):
        ip = self.__get_ip(request)

        if ip is None:
            return False

        try:
            subprocess.check_call([
                'ssh',
                '-o',
                'UserKnownHostsFile=/dev/null',
                '-o',
                'StrictHostKeyChecking=no',
                '-o',
                'ConnectTimeout=10',
                '-i',
                self.node_private_key_file,
                '-l',
                self.node_user,
                ip,
                '--',
                '/bin/date'])

            self.log.info('Headnode for %s running at %s', request.name, ip)

            return True
        except subprocess.CalledProcessError:
            return False

    def boot_server(self, request):
        try:
            server = self.nova.servers.find(name=request.name)
            self.log.info('Found headnode at %s for request %s', request.headnode, request.name)
            return server
        except Exception, e:
            pass

        self.log.info('Booting new headnode for request %s...', request.name)
        server = self.nova.servers.create(name = request.name, image = self.node_image, flavor = self.node_flavor, key_name = self.node_public_key_name, security_groups = self.node_security_groups, nics = [{'net-id' : self.node_network_id}])

        return server


    def initialize_server(self, request):

        # if we are already initializing this headnode
        if self.initializers.has_key(request.name):
            return

        self.log.info('Initializing new server at %s for request %s', request.headnode, request.name)

        request.headnode['state'] = 'initializing'
        request.headnode['ip']    = self.__get_ip(request)

        os.environ['ANSIBLE_HOST_KEY_CHECKING']='False'

        # key should come from an openstack allocation when the openstack
        # resource is defined.  for now, we take the key of the first
        # allocation first allocation of the request.
        allocation_name = request.allocations[0]
        allocation      = self.client.getAllocation(allocation_name)

        extra_vars  = {}
        extra_vars['request_name']         = request.name
        extra_vars['setup_user_name']      = self.node_user
        extra_vars['condor_password_file'] = self.condor_password_filename(request)

        extra_vars['production_keys'] = {}

        #members = project get members and keys... for now, fake it:
        members=['vc3username', 'vc3otherusername']
        tmpkey=self.client.decode(self.read_encoded(self.node_user_public_key_file))

        for member in members:
            extra_vars['production_keys'][member] = tmpkey

        # passing extra-vars as a command line argument for now. That won't
        # scale well, we want to write those vars to a file instead.
        pipe = subprocess.Popen(
                ['ansible-playbook',
                    self.ansible_playbook,
                    '--extra-vars',
                    json.dumps(extra_vars),
                    '--key-file',
                    self.node_private_key_file,
                    '--inventory',
                    request.headnode['ip'] + ',',
                    ],
                cwd = self.ansible_path,
                stdout=self.ansible_debug,
                stderr=self.ansible_debug,
                )
        self.initializers[request.name] = pipe

    def check_if_done_init(self, request):
        try:
            pipe = self.initializers[request.name]
            pipe.poll()

            self.ansible_debug.flush()

            if pipe.returncode is None:
                return False

            # the process is done when there is a returncode
            self.initializers[request.name] = None

            if pipe.returncode != 0:
                self.log.warning('Error when initializing headnode for request %s. Exit status: %d', request.name, pipe.returncode)
                request.headnode['state'] = 'failure'

            return True

        except Exception, e:
            self.log.warning('Error for headnode initializers for request %s (%s)', request.name, e)
            request.headnode['state'] = 'failure'

    def report_running_server(self, request):

        if request.headnode['state'] != 'initializing':
            return

        try:
            self.create_password_environment(request)
            request.headnode['state'] = 'running'
        except Exception, e:
            self.log.warning('Cound not read condor password file for request %s (%s)', request.name, e)
            request.headnode['state'] = 'failure'

    def create_password_environment(self, request):

        password_env_name = request.name + '.condor-password'
        password_contents = self.read_encoded(self.condor_password_filename(request))
        password_basename = os.path.basename(self.condor_password_filename(request))

        try:
            os.remove(self.condor_password_filename(request))
        except Exception, e:
            self.log.warning("Could not remove file: %s", self.condor_password_filename(request))

        self.log.debug('Creating condor password environment %s', password_env_name)

        try:
            env = self.client.defineEnvironment(password_env_name, request.owner, files = { password_basename : password_contents })
            self.client.storeEnvironment(env)

            request.headnode['condor_password_environment'] = password_env_name
            request.headnode['condor_password_filename']    = password_basename
        except Exception, e:
            self.log.error("Error when creating condor password environment %s: %s", password_env_name, e)
            self.log.debug(traceback.format_exc(None))

    def condor_password_filename(self, request):
        # file created by ansible
        return '/tmp/condor_password.' + request.name

    def read_encoded(self, filename):
        with open(filename, 'r') as f:
            contents = f.read()
            return self.client.encode(contents)



    def __get_ip(self, request):
        try:
            server = self.nova.servers.find(name=request.name)

            if server.status != 'ACTIVE':
                return None

        except Exception, e:
            self.log.warning('Could not find headnode for request %s (%s)', request.name, e)
            raise e

        try:
            for network in server.networks.keys():
                for ip in server.networks[network]:
                    if re.match('\d+\.\d+\.\d+\.\d+', ip):
                        return ip
        except Exception, e:
            self.log.warning("Could not find ip for request %s: %s", request.name, e)
            raise e

        return None

