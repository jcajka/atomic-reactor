"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


Reads input from OpenShift v3
"""
import json
import os

from atomic_reactor.plugin import InputPlugin
from atomic_reactor.util import get_build_json, read_yaml
from osbs.utils import RegistryURI


class OSv3InputPlugin(InputPlugin):
    key = "osv3"

    def __init__(self, **kwargs):
        """
        constructor
        """
        # call parent constructor
        super(OSv3InputPlugin, self).__init__(**kwargs)

    def get_plugins_with_user_params(self, build_json, user_params):
        #  get the reactor config map and derive an osbs instance from it

        from osbs.api import OSBS
        from osbs.conf import Configuration

        # make sure the input json is valid
        read_yaml(user_params, 'schemas/user_params.json')
        reactor_config_override = json.loads(user_params).get('reactor_config_override')
        if reactor_config_override:
            read_yaml(json.dumps(reactor_config_override), 'schemas/config.json')

        osbs_conf = Configuration(build_json_dir=json.loads(user_params).get('build_json_dir'))
        osbs = OSBS(osbs_conf, osbs_conf)
        return osbs.render_plugins_configuration(user_params)

    def get_value(self, name, default=None):
        return self.reactor_env.get(name, default)

    def remove_plugin(self, phase, target_plugin, reason):
        if phase in self.plugins_json:
            for index, plugin in enumerate(self.plugins_json[phase]):
                if plugin['name'] == target_plugin:
                    self.log.info('%s: removing %s from phase %s', reason, target_plugin, phase)
                    del self.plugins_json[phase][index]
                    break

    def remove_koji_plugins(self):
        koji_map = self.get_value('koji', {})
        if not koji_map.get('hub_url'):
            # bump_release is removed in PluginsConfiguration if no release value
            self.remove_plugin('prebuild_plugins', 'bump_release', 'no koji hub available')
            # inject_parent_image is removed in PluginsConfiguration if no parent image
            self.remove_plugin('prebuild_plugins', 'inject_parent_image', 'no koji hub available')
            self.remove_plugin('prebuild_plugins', 'koji_parent', 'no koji hub available')
            self.remove_plugin('postbuild_plugins', 'koji_upload', 'no koji hub available')
            self.remove_plugin('exit_plugins', 'koji_promote', 'no koji hub available')
            self.remove_plugin('exit_plugins', 'koji_import', 'no koji hub available')
            self.remove_plugin('exit_plugins', 'koji_tag_build', 'no koji hub available')
            # root and hub are required, so this check is probably redundant
            if not koji_map.get('root_url'):
                self.remove_plugin('prebuild_plugins', 'fetch_maven_artifacts',
                                   'no koji root available')

    def remove_pulp_plugins(self):
        phases = ('postbuild_plugins', 'exit_plugins')
        pulp_registry = self.get_value('pulp')
        koji_hub = self.get_value('koji', {}).get('hub_url')
        for phase in phases:
            if not (pulp_registry and koji_hub):
                self.remove_plugin(phase, 'pulp_pull', 'no pulp or koji available')

        if not pulp_registry:
            self.remove_plugin('postbuild_plugins', 'pulp_push', 'no pulp available')
            self.remove_plugin('postbuild_plugins', 'pulp_sync', 'no pulp available')
            self.remove_plugin('postbuild_plugins', 'pulp_tag', 'no pulp available')
            self.remove_plugin('exit_plugins', 'delete_from_registry', 'no pulp available')
            self.remove_plugin('exit_plugins', 'pulp_publish', 'no pulp available')
        else:
            docker_registry = None
            all_registries = self.get_value('registries', {})

            versions = self.get_value('content_versions', ['v1', 'v2'])

            for registry in all_registries:
                reguri = RegistryURI(registry.get('url'))
                if reguri.version == 'v2':
                    # First specified v2 registry is the one we'll tell pulp
                    # to sync from. Keep the http prefix -- pulp wants it.
                    docker_registry = registry
                    break

            if 'v1' not in versions:
                self.remove_plugin('postbuild_plugins', 'pulp_push', 'v1 content not enabled')

            if docker_registry:
                source_registry_str = self.get_value('source_registry', {}).get('url')
                perform_delete = (source_registry_str is None or
                                  RegistryURI(source_registry_str).uri != reguri.uri)
                if not perform_delete:
                    self.remove_plugin('exit_plugins', 'delete_from_registry', 'no delete needed')
            else:
                self.remove_plugin('postbuild_plugins', 'pulp_sync', 'no V2 pulp available')
                self.remove_plugin('exit_plugins', 'delete_from_registry', 'no V2 pulp available')

    def remove_plugins_without_parameters(self):
        """
        This used to be handled in BuildRequest, but with REACTOR_CONFIG, osbs-client doesn't have
        enough information.
        """

        # Compatibility code for dockerfile_content plugin
        self.remove_plugin('prebuild_plugins', 'dockerfile_content',
                           'dockerfile_content is deprecated, please remove from config')
        if not self.reactor_env:
            return
        self.remove_koji_plugins()
        self.remove_pulp_plugins()
        if not self.get_value('odcs'):
            self.remove_plugin('prebuild_plugins', 'resolve_composes', 'no odcs available')
        if not self.get_value('smtp'):
            self.remove_plugin('exit_plugins', 'sendmail', 'no mailhost available')
        if not self.get_value('sources_command'):
            self.remove_plugin('prebuild_plugins', 'distgit_fetch_artefacts', 'no sources command')

    def run(self):
        """
        each plugin has to implement this method -- it is used to run the plugin actually

        response from plugin is kept and used in json result response
        """
        user_params = None
        build_json = get_build_json()
        git_url = os.environ['SOURCE_URI']
        git_ref = os.environ.get('SOURCE_REF', None)
        image = os.environ['OUTPUT_IMAGE']
        self.target_registry = os.environ.get('OUTPUT_REGISTRY', None)
        self.reactor_env = None

        try:
            user_params = os.environ['USER_PARAMS']
            self.plugins_json = self.get_plugins_with_user_params(build_json, user_params)
            # if we get the USER_PARAMS, we'd better get the REACTOR_CONFIG too
            reactor_config_map = os.environ['REACTOR_CONFIG']
            self.reactor_env = read_yaml(reactor_config_map, 'schemas/config.json')
        except KeyError:
            try:
                self.plugins_json = os.environ['ATOMIC_REACTOR_PLUGINS']
            except KeyError:
                raise RuntimeError("No plugin configuration found!")

        self.plugins_json = json.loads(self.plugins_json)

        input_json = {
            'source': {
                'provider': 'git',
                'uri': git_url,
                'provider_params': {'git_commit': git_ref}
            },
            'image': image,
            'openshift_build_selflink': build_json.get('metadata', {}).get('selfLink', None)
        }
        input_json.update(self.plugins_json)

        self.log.debug("build json: %s", input_json)

        self.remove_plugins_without_parameters()
        # make sure the final json is valid
        read_yaml(json.dumps(self.plugins_json), 'schemas/plugins.json')

        return input_json

    @classmethod
    def is_autousable(cls):
        return 'BUILD' in os.environ and 'SOURCE_URI' in os.environ and 'OUTPUT_IMAGE' in os.environ
