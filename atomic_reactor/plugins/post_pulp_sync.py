"""Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

Sync built image to pulp registry using Docker Registry HTTP API V2

Pulp authentication is via a key and certificate. Docker V2 registry
authentication is via a dockercfg file. Both of these sets of
credentials are stored in secrets which the builder service account is
allowed to mount:

$ oc secrets new pulp pulp.key=./pulp.key pulp.cer=./pulp.cer
secret/pulp
$ oc secrets add serviceaccount/builder secret/pulp --for=mount
$ oc secrets new-dockercfg registry-dockercfg [...]
secret/registry-dockercfg
$ oc secrets add serviceaccount/builder secret/registry-dockercfg --for=mount

In the BuildConfig for atomic-reactor, specify the secrets in the
strategy's 'secrets' array, specifying a mount path:

"secrets": [
  {
    "secretSource": {
      "name": "pulp"
    },
    "mountPath": "/var/run/secrets/pulp"
  },
  {
    "secretSource": {
      "name": "registry-dockercfg"
    },
    "mountPath": "/var/run/secrets/registry-dockercfg"
  }
]

In the configuration for this plugin, specify the same path for
pulp_secret_path:

"pulp_sync": {
  "pulp_registry_name": ...,
  ...
  "pulp_secret_path": "/var/run/secrets/pulp",
  "registry_secret_path": "/var/run/secrets/registry-dockercfg"
}

"""

from __future__ import print_function, unicode_literals

from atomic_reactor.plugin import PostBuildPlugin
from atomic_reactor.util import ImageName, Dockercfg
import dockpulp
import os
import re


# let's silence warnings from dockpulp: there is one warning for every
# request which may result in tens of messages: very annoying.
# with "module", it just prints one warning -- this should balance security
# and UX
from warnings import filterwarnings
filterwarnings("module")


class PulpSyncPlugin(PostBuildPlugin):
    key = 'pulp_sync'
    is_allowed_to_fail = False

    CER = 'pulp.cer'
    KEY = 'pulp.key'

    def __init__(self, tasker, workflow,
                 pulp_registry_name,
                 docker_registry,
                 delete_from_registry=False,
                 pulp_secret_path=None,
                 registry_secret_path=None,
                 insecure_registry=None,
                 dockpulp_loglevel=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param pulp_registry_name: str, name of pulp registry to use,
               specified in /etc/dockpulp.conf
        :param docker_registry: str, URL of docker registry to sync from
               including scheme e.g. https://registry.example.com
        :param delete_from_registry: bool, whether to delete the image
               from the docker v2 registry after sync
        :param pulp_secret_path: path to pulp.cer and pulp.key
        :param registry_secret_path: path to .dockercfg for the V2 registry
        :param insecure_registry: True if SSL validation should be skipped
        :param dockpulp_loglevel: int, logging level for dockpulp
        """
        # call parent constructor
        super(PulpSyncPlugin, self).__init__(tasker, workflow)
        self.pulp_registry_name = pulp_registry_name
        self.docker_registry = docker_registry
        self.pulp_secret_path = pulp_secret_path
        self.registry_secret_path = registry_secret_path
        self.insecure_registry = insecure_registry

        if dockpulp_loglevel is not None:
            logger = dockpulp.setup_logger(dockpulp.log)
            try:
                logger.setLevel(dockpulp_loglevel)
            except (ValueError, TypeError) as ex:
                self.log.error("Can't set provided log level %r: %r",
                               dockpulp_loglevel, ex)

        if delete_from_registry:
            self.log.error("will not delete from registry as instructed: "
                           "not implemented")

    def set_auth(self, pulp):
        path = self.pulp_secret_path
        if path is not None:
            self.log.info("using configured path %s for secrets", path)

            # Work out the pathnames for the certificate/key pair
            cer = os.path.join(path, self.CER)
            key = os.path.join(path, self.KEY)

            if not os.path.exists(cer):
                raise RuntimeError("Certificate does not exist")
            if not os.path.exists(key):
                raise RuntimeError("Key does not exist")

            # Tell dockpulp
            pulp.set_certs(cer, key)

    def get_dockercfg_credentials(self, docker_registry):
        """
        Read the .dockercfg file and return an empty dict, or else a dict
        with keys 'basic_auth_username' and 'basic_auth_password'.
        """
        if not self.registry_secret_path:
            return {}

        dockercfg = Dockercfg(self.registry_secret_path)
        registry_creds = dockercfg.get_credentials(docker_registry)
        if 'username' not in registry_creds:
            return {}

        return {
            'basic_auth_username': registry_creds['username'],
            'basic_auth_password': registry_creds['password'],
        }

    def run(self):
        pulp = dockpulp.Pulp(env=self.pulp_registry_name)
        self.set_auth(pulp)

        # We only want the hostname[:port]
        hostname_and_port = re.compile(r'^https?://([^/]*)/?.*')
        pulp_registry = hostname_and_port.sub(lambda m: m.groups()[0],
                                              pulp.registry)

        # Store the registry URI in the push configuration
        self.workflow.push_conf.add_pulp_registry(self.pulp_registry_name,
                                                  pulp_registry)

        self.log.info("syncing from docker V2 registry %s",
                      self.docker_registry)

        docker_registry = hostname_and_port.sub(lambda m: m.groups()[0],
                                                self.docker_registry)

        kwargs = self.get_dockercfg_credentials(docker_registry)
        if self.insecure_registry is not None:
            kwargs['ssl_validation'] = not self.insecure_registry

        images = []
        repos = {}  # pulp repo -> repo id
        for image in self.workflow.tag_conf.primary_images:
            if image.pulp_repo not in repos:
                self.log.info("syncing %s", image.pulp_repo)
                repoinfo = pulp.syncRepo(feed=self.docker_registry,
                                         upstream_name=image.repo,
                                         **kwargs)
                repos[image.pulp_repo] = repoinfo[0]['id']

            images.append(ImageName(registry=pulp_registry,
                                    repo=image.repo))

        self.log.info("publishing to crane")
        pulp.crane(list(repos.values()), wait=True)

        # Return the set of qualified repo names for this image
        return images
