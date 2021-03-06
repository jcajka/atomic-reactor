from __future__ import unicode_literals
import os
import smtplib
from collections import namedtuple

from flexmock import flexmock
import pytest
import six
import json

try:
    import koji
except ImportError:
    import inspect
    import sys

    # Find our mocked koji module
    import tests.koji as koji
    mock_koji_path = os.path.dirname(inspect.getfile(koji.ClientSession))
    if mock_koji_path not in sys.path:
        sys.path.append(os.path.dirname(mock_koji_path))

    # Now load it properly, the same way the plugin will
    del koji
    import koji


from atomic_reactor.plugin import PluginFailedException
from atomic_reactor.plugins.pre_check_and_set_rebuild import CheckAndSetRebuildPlugin
from atomic_reactor.plugins.exit_sendmail import SendMailPlugin, validate_address
from atomic_reactor.plugins.exit_store_metadata_in_osv3 import StoreMetadataInOSv3Plugin
from atomic_reactor.plugins.exit_koji_import import KojiImportPlugin
from atomic_reactor.plugins.exit_koji_promote import KojiPromotePlugin
from atomic_reactor.plugins.pre_reactor_config import (ReactorConfigPlugin,
                                                       WORKSPACE_CONF_KEY,
                                                       ReactorConfig)
from atomic_reactor import util
from osbs.api import OSBS
from osbs.exceptions import OsbsException
from tests.fixtures import reactor_config_map  # noqa
from smtplib import SMTPException

MS, MF = SendMailPlugin.MANUAL_SUCCESS, SendMailPlugin.MANUAL_FAIL
AS, AF = SendMailPlugin.AUTO_SUCCESS, SendMailPlugin.AUTO_FAIL
MC, AC = SendMailPlugin.MANUAL_CANCELED, SendMailPlugin.AUTO_CANCELED

MOCK_EMAIL_DOMAIN = "domain.com"
MOCK_KOJI_TASK_ID = 12345
MOCK_KOJI_BUILD_ID = 98765
MOCK_KOJI_PACKAGE_ID = 123
MOCK_KOJI_TAG_ID = 456
MOCK_KOJI_OWNER_ID = 789
MOCK_KOJI_OWNER_NAME = "foo"
MOCK_KOJI_OWNER_EMAIL = "foo@bar.com"
MOCK_KOJI_OWNER_GENERATED = "@".join([MOCK_KOJI_OWNER_NAME, MOCK_EMAIL_DOMAIN])
MOCK_KOJI_SUBMITTER_ID = 123456
MOCK_KOJI_SUBMITTER_NAME = "baz"
MOCK_KOJI_SUBMITTER_EMAIL = "baz@bar.com"
MOCK_KOJI_SUBMITTER_GENERATED = "@".join([MOCK_KOJI_SUBMITTER_NAME, MOCK_EMAIL_DOMAIN])
MOCK_ADDITIONAL_EMAIL = "spam@bar.com"

LogEntry = namedtuple('LogEntry', ['platform', 'line'])


class MockedClientSession(object):
    def __init__(self, hub, opts=None, has_kerberos=True):
        self.has_kerberos = has_kerberos

    def krb_login(self, principal=None, keytab=None, proxyuser=None):
        raise RuntimeError('No certificates provided')

    def ssl_login(self, cert=None, ca=None, serverca=None, proxyuser=None):
        return True

    def getBuild(self, build_id):
        assert build_id == MOCK_KOJI_BUILD_ID
        return {'package_id': MOCK_KOJI_PACKAGE_ID}

    def listTags(self, build_id):
        assert build_id == MOCK_KOJI_BUILD_ID
        return [{"id": MOCK_KOJI_TAG_ID}]

    def getPackageConfig(self, tag_id, package_id):
        assert tag_id == MOCK_KOJI_TAG_ID
        assert package_id == MOCK_KOJI_PACKAGE_ID
        return {"owner_id": MOCK_KOJI_OWNER_ID}

    def getUser(self, user_id):
        if user_id == MOCK_KOJI_OWNER_ID:
            if self.has_kerberos:
                return {"krb_principal": MOCK_KOJI_OWNER_EMAIL}
            else:
                return {"krb_principal": "",
                        "name": MOCK_KOJI_OWNER_NAME}

        elif user_id == MOCK_KOJI_SUBMITTER_ID:
            if self.has_kerberos:
                return {"krb_principal": MOCK_KOJI_SUBMITTER_EMAIL}
            else:
                return {"krb_principal": "",
                        "name": MOCK_KOJI_SUBMITTER_NAME}

        else:
            assert False, "Don't know user with id %s" % user_id

    def getTaskInfo(self, task_id):
        assert task_id == MOCK_KOJI_TASK_ID
        return {"owner": MOCK_KOJI_SUBMITTER_ID}

    def listTaskOutput(self, task_id):
        assert task_id == MOCK_KOJI_TASK_ID
        return ["openshift-final.log", "build.log"]


class MockedPathInfo(object):
    def __init__(self, topdir=None):
        self.topdir = topdir

    def work(self):
        return "{}/work".format(self.topdir)

    def taskrelpath(self, task_id):
        assert task_id == MOCK_KOJI_TASK_ID
        return "tasks/%s" % task_id


DEFAULT_ANNOTATIONS = {
    'repositories': {
        'unique': ['foo/bar:baz'],
        'primary': ['foo/bar:spam'],
    }
}


def mock_store_metadata_results(workflow, annotations=DEFAULT_ANNOTATIONS):
    result = {}
    if annotations:
        result['annotations'] = {key: json.dumps(value) for key, value in annotations.items()}
    workflow.exit_results[StoreMetadataInOSv3Plugin.key] = result


@pytest.mark.parametrize(('address', 'valid'), [
    ('me@example.com', True),
    ('me1@example.com', True),
    ('me+@example.com', True),
    ('me_@example.com', True),
    ('me-@example.com', True),
    ('me.me@example.com', True),
    ('me@www-1.example.com', True),
    (None, None),
    ('', None),
    ('invalid', None),
    ('me@example', None),
    ('me@@example.com', None),
    ('me/me@example.com', None),
    ('1me@example.com', None),
    ('me@www/example.com', None),
    ('me@www_example.com', None),
    ('me@www+example.com', None),
])
def test_valid_address(address, valid):
    assert validate_address(address) == valid


class TestSendMailPlugin(object):
    def test_fails_with_unknown_states(self, reactor_config_map):  # noqa
        class WF(object):
            exit_results = {}
            plugin_workspace = {}

        workflow = WF()
        if reactor_config_map:
            smtp_map = {
                'from_address': 'foo@bar.com',
                'host': 'smtp.spam.com',
            }
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig({'version': 1, 'smtp': smtp_map})

        p = SendMailPlugin(None, workflow,
                           smtp_host='smtp.bar.com', from_address='foo@bar.com',
                           send_on=['unknown_state', MS])
        with pytest.raises(PluginFailedException) as e:
            p.run()
        assert str(e.value) == 'Unknown state(s) "unknown_state" for sendmail plugin'

    @pytest.mark.parametrize('rebuild, success, auto_canceled, manual_canceled, send_on, expected', [  # noqa
        # make sure that right combinations only succeed for the specific state
        (False, True, False, False, [MS], True),
        (False, True, False, True, [MS], True),
        (False, True, False, False, [MF, AS, AF, AC], False),
        (False, True, False, True, [MF, AS, AF, AC], False),
        (False, False, False, False, [MF], True),
        (False, False, False, True, [MF], True),
        (False, False, False, False, [MS, AS, AF, AC], False),
        (False, False, False, True, [MS, AS, AF, AC], False),
        (False, False, True, True, [MC], True),
        (False, True, True, True, [MC], True),
        (False, True, False, True, [MC], True),
        (False, True, False, False, [MC], False),
        (True, True, False, False, [AS], True),
        (True, True, False, False, [MS, MF, AF, AC], False),
        (True, False, False, False, [AF], True),
        (True, False, False, False, [MS, MF, AS, AC], False),
        (True, False, True, True, [AC], True),
        # auto_fail would also give us True in this case
        (True, False, True, True, [MS, MF, AS], False),
        # also make sure that a random combination of more plugins works ok
        (True, False, False, False, [AF, MS], True)
    ])
    def test_should_send(self, rebuild, success, auto_canceled, manual_canceled, send_on, expected,
                         reactor_config_map):
        class WF(object):
            exit_results = {
                KojiPromotePlugin.key: MOCK_KOJI_BUILD_ID
            }
            plugin_workspace = {}

        kwargs = {
            'smtp_host': 'smtp.bar.com',
            'from_address': 'foo@bar.com',
            'send_on': send_on,
        }

        workflow = WF()
        if reactor_config_map:
            smtp_map = {
                'from_address': 'foo@bar.com',
                'host': 'smtp.spam.com',
            }
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig({'version': 1, 'smtp': smtp_map})

        p = SendMailPlugin(None, workflow, **kwargs)
        assert p._should_send(rebuild, success, auto_canceled, manual_canceled) == expected

    @pytest.mark.parametrize(('additional_addresses', 'expected_receivers'), [  # noqa:F811
        ('', None),
        ([], None),
        ([''], []),
        (['', ''], []),
        (['not/me@example.com'], []),
        (['me@example.com'], ['me@example.com']),
        (['me@example.com', 'me@example.com'], ['me@example.com']),
        (['me@example.com', '', 'me@example.com'], ['me@example.com']),
        (['not/me@example.com', 'me@example.com'], ['me@example.com']),
        (['me@example.com', 'us@example.com'], ['me@example.com', 'us@example.com']),
        (['not/me@example.com', '', 'me@example.com', 'us@example.com'],
         ['me@example.com', 'us@example.com']),
    ])
    def test_get_receiver_list(self, monkeypatch, additional_addresses, expected_receivers,
                               reactor_config_map):
        class TagConf(object):
            unique_images = []

        class WF(object):
            image = util.ImageName.parse('foo/bar:baz')
            openshift_build_selflink = '/builds/blablabla'
            build_process_failed = False
            autorebuild_canceled = False
            build_canceled = False
            tag_conf = TagConf()
            exit_results = {
                KojiPromotePlugin.key: MOCK_KOJI_BUILD_ID
            }
            prebuild_results = {}
            plugin_workspace = {}

        monkeypatch.setenv("BUILD", json.dumps({
            'metadata': {
                'labels': {
                    'koji-task-id': MOCK_KOJI_TASK_ID,
                },
                'name': {},
            }
        }))

        session = MockedClientSession('', has_kerberos=True)
        pathinfo = MockedPathInfo('https://koji')

        flexmock(koji, ClientSession=lambda hub, opts: session, PathInfo=pathinfo)
        kwargs = {
            'url': 'https://something.com',
            'smtp_host': 'smtp.bar.com',
            'from_address': 'foo@bar.com',
            'koji_root': 'https://koji/',
            'additional_addresses': additional_addresses
        }

        workflow = WF()

        if reactor_config_map:
            openshift_map = {'url': 'https://something.com'}
            koji_map = {
                'hub_url': None,
                'root_url': 'https://koji/',
                'auth': {
                    'ssl_certs_dir': '/certs',
                    'proxyuser': None,
                    'krb_principal': None,
                    'krb_keytab_path': None
                }
            }
            smtp_map = {
                'from_address': 'foo@bar.com',
                'host': 'smtp.bar.com',
                'send_to_submitter': False,
                'send_to_pkg_owner': False,
                'additional_addresses': additional_addresses
            }
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig({'version': 1, 'smtp': smtp_map, 'koji': koji_map,
                               'openshift': openshift_map})

        p = SendMailPlugin(None, workflow, **kwargs)
        if expected_receivers is not None:
            assert sorted(expected_receivers) == sorted(p._get_receivers_list())
        else:
            with pytest.raises(RuntimeError) as ex:
                p._get_receivers_list()
                assert str(ex) == 'No recipients found'

    @pytest.mark.parametrize('success', (True, False))
    @pytest.mark.parametrize(('has_store_metadata_results', 'annotations', 'has_repositories',
                              'expect_error'), [
        (True, True, True, False),
        (True, True, False, True),
        (True, False, False, True),
        (False, False, False, True)
    ])
    @pytest.mark.parametrize('koji_integration', (True, False))
    @pytest.mark.parametrize(('autorebuild', 'auto_cancel', 'manual_cancel',
                              'to_koji_submitter', 'has_koji_logs'), [
        (True, False, False, True, True),
        (True, True, False, True, True),
        (True, False, True, True, True),
        (True, False, False, True, False),
        (True, True, False, True, False),
        (True, False, True, True, False),
        (False, False, False, True, True),
        (False, True, False, True, True),
        (False, False, True, True, True),
        (False, False, False, True, False),
        (False, True, False, True, False),
        (False, False, True, True, False),
        (True, False, False, False, True),
        (True, True, False, False, True),
        (True, False, True, False, True),
        (True, False, False, False, False),
        (True, True, False, False, False),
        (True, False, True, False, False),
        (False, False, False, False, True),
        (False, True, False, False, True),
        (False, False, True, False, True),
        (False, False, False, False, False),
        (False, True, False, False, False),
        (False, False, True, False, False),
    ])
    def test_render_mail(self, monkeypatch, autorebuild, auto_cancel,
                         manual_cancel, to_koji_submitter, has_koji_logs,
                         koji_integration, success, reactor_config_map,
                         has_store_metadata_results, annotations, has_repositories,
                         expect_error):
        log_url_cases = {
            # (koji_integration,autorebuild,success)
            (False, False, False): False,
            (False, False, True): False,
            (False, True, False): False,  # Included as attachment
            (False, True, True): False,
            (True, False, False): True,
            (True, False, True): True,
            (True, True, False): False,   # Included as attachment
            (True, True, True): False,    # Logs in Koji Build
        }

        class TagConf(object):
            unique_images = []

        class WF(object):
            image = util.ImageName.parse('foo/bar:baz')
            openshift_build_selflink = '/builds/blablabla'
            build_process_failed = False
            autorebuild_canceled = auto_cancel
            build_canceled = manual_cancel
            tag_conf = TagConf()
            exit_results = {
                KojiPromotePlugin.key: MOCK_KOJI_BUILD_ID
            }
            prebuild_results = {}
            plugin_workspace = {}

        monkeypatch.setenv("BUILD", json.dumps({
            'metadata': {
                'labels': {
                    'koji-task-id': MOCK_KOJI_TASK_ID,
                },
                'name': {},
            }
        }))

        session = MockedClientSession('', has_kerberos=True)
        pathinfo = MockedPathInfo('https://koji')
        if not has_koji_logs:
            (flexmock(pathinfo)
                .should_receive('work')
                .and_raise(RuntimeError, "xyz"))

        fake_logs = [LogEntry(None, 'orchestrator'),
                     LogEntry(None, 'orchestrator line 2'),
                     LogEntry('x86_64', 'Hurray for bacon: \u2017'),
                     LogEntry('x86_64', 'line 2')]
        flexmock(OSBS).should_receive('get_orchestrator_build_logs').and_return(fake_logs)

        flexmock(koji, ClientSession=lambda hub, opts: session, PathInfo=pathinfo)
        kwargs = {
            'url': 'https://something.com',
            'smtp_host': 'smtp.bar.com',
            'from_address': 'foo@bar.com',
            'to_koji_submitter': to_koji_submitter,
            'to_koji_pkgowner': False,
            'koji_hub': '/' if koji_integration else None,
            'koji_root': 'https://koji/',
            'koji_proxyuser': None,
            'koji_ssl_certs_dir': '/certs',
            'koji_krb_principal': None,
            'koji_krb_keytab': None
        }

        workflow = WF()
        if has_store_metadata_results:
            if annotations:
                if has_repositories:
                    mock_store_metadata_results(workflow)
                else:
                    mock_store_metadata_results(workflow, {'repositories': {}})
            else:
                mock_store_metadata_results(workflow, None)

        if reactor_config_map:
            openshift_map = {'url': 'https://something.com'}
            koji_map = {
                'hub_url': '/' if koji_integration else None,
                'root_url': 'https://koji/',
                'auth': {
                    'ssl_certs_dir': '/certs',
                    'proxyuser': None,
                    'krb_principal': None,
                    'krb_keytab_path': None
                }
            }
            smtp_map = {
                'from_address': 'foo@bar.com',
                'host': 'smtp.bar.com',
                'send_to_submitter': to_koji_submitter,
                'send_to_pkg_owner': False,
            }
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig({'version': 1, 'smtp': smtp_map, 'koji': koji_map,
                               'openshift': openshift_map})

        p = SendMailPlugin(None, workflow, **kwargs)

        # Submitter is updated in _get_receivers_list
        try:
            p._get_receivers_list()
        except RuntimeError as ex:
            # Only valid exception is a RuntimeError when there are no
            # recipients available
            assert str(ex) == 'No recipients found'

        if expect_error:
            with pytest.raises(ValueError):
                p._render_mail(autorebuild, success, auto_cancel, manual_cancel)
            return

        subject, body, logs = p._render_mail(autorebuild, success,
                                             auto_cancel, manual_cancel)

        if auto_cancel or manual_cancel:
            status = 'Canceled'
            assert not logs
        elif success:
            status = 'Succeeded'
            assert not logs
        else:
            status = 'Failed'
            # Full logs are only generated on a failed autorebuild
            assert autorebuild == bool(logs)

        exp_subject = '%s building image foo/bar' % status
        exp_body = [
            'Image Name: foo/bar',
            'Repositories: ',
            '    foo/bar:baz',
            '    foo/bar:spam',
            'Status: ' + status,
            'Submitted by: ',
        ]
        if autorebuild:
            exp_body[-1] += '<autorebuild>'
        elif koji_integration and to_koji_submitter:
            exp_body[-1] += MOCK_KOJI_SUBMITTER_EMAIL
        else:
            exp_body[-1] += SendMailPlugin.DEFAULT_SUBMITTER

        if log_url_cases[(koji_integration, autorebuild, success)]:
            if has_koji_logs:
                exp_body.append("Logs: https://koji/work/tasks/12345")
            else:
                exp_body.append("Logs: "
                                "https://something.com/builds/blablabla/log")

        assert subject == exp_subject
        assert body == '\n'.join(exp_body)

    @pytest.mark.parametrize('error_type', [
        TypeError,
        OsbsException, 'unable to get build logs from OSBS',
    ])
    def test_failed_logs(self, monkeypatch, error_type, reactor_config_map):  # noqa
        # just test a random combination of the method inputs and hope it's ok for other
        #   combinations
        class TagConf(object):
            unique_images = []

        class WF(object):
            image = util.ImageName.parse('foo/bar:baz')
            openshift_build_selflink = '/builds/blablabla'
            build_process_failed = True
            autorebuild_canceled = False
            build_canceled = False
            tag_conf = TagConf()
            exit_results = {
                KojiPromotePlugin.key: MOCK_KOJI_BUILD_ID
            }
            prebuild_results = {}
            plugin_workspace = {}

        monkeypatch.setenv("BUILD", json.dumps({
            'metadata': {
                'labels': {
                    'koji-task-id': MOCK_KOJI_TASK_ID,
                },
                'name': {},
            }
        }))

        session = MockedClientSession('', has_kerberos=True)
        pathinfo = MockedPathInfo('https://koji')

        flexmock(OSBS).should_receive('get_orchestrator_build_logs').and_raise(error_type)

        flexmock(koji, ClientSession=lambda hub, opts: session, PathInfo=pathinfo)
        kwargs = {
            'url': 'https://something.com',
            'smtp_host': 'smtp.bar.com',
            'from_address': 'foo@bar.com',
            'to_koji_submitter': True,
            'to_koji_pkgowner': False,
            'koji_hub': '/',
            'koji_root': 'https://koji/',
            'koji_proxyuser': None,
            'koji_ssl_certs_dir': '/certs',
            'koji_krb_principal': None,
            'koji_krb_keytab': None
        }

        workflow = WF()
        mock_store_metadata_results(workflow)

        if reactor_config_map:
            smtp_map = {
                'from_address': 'foo@bar.com',
                'host': 'smtp.bar.com',
                'send_to_submitter': True,
                'send_to_pkg_owner': False,
            }
            koji_map = {
                'hub_url': '/',
                'root_url': 'https://koji/',
                'auth': {'ssl_certs_dir': '/certs'}
            }
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig({'version': 1,
                               'openshift': {'url': 'https://something.com'},
                               'smtp': smtp_map,
                               'koji': koji_map})

        p = SendMailPlugin(None, workflow, **kwargs)
        subject, body, fail_logs = p._render_mail(True, False, False, False)
        assert not fail_logs

    @pytest.mark.parametrize(
        'has_koji_config, has_addit_address, to_koji_submitter, to_koji_pkgowner, expected_receivers', [  # noqa
            (True, True, True, True,
                [MOCK_ADDITIONAL_EMAIL, MOCK_KOJI_OWNER_EMAIL, MOCK_KOJI_SUBMITTER_EMAIL]),
            (True, False, True, True, [MOCK_KOJI_OWNER_EMAIL, MOCK_KOJI_SUBMITTER_EMAIL]),
            (True, False, True, False, [MOCK_KOJI_SUBMITTER_EMAIL]),
            (True, False, False, True, [MOCK_KOJI_OWNER_EMAIL]),
            (True, True, False, False, [MOCK_ADDITIONAL_EMAIL]),
            (True, False, False, False, []),
            (False, False, False, False, []),
            (False, True, False, True, [MOCK_ADDITIONAL_EMAIL]),
            (False, True, True, False, [MOCK_ADDITIONAL_EMAIL]),
        ])
    @pytest.mark.parametrize('use_import', [
        (True, False)
    ])
    def test_recepients_from_koji(self, monkeypatch,
                                  has_addit_address,
                                  has_koji_config, to_koji_submitter, to_koji_pkgowner,
                                  expected_receivers, use_import, reactor_config_map):
        class TagConf(object):
            unique_images = []

        class WF(object):
            image = util.ImageName.parse('foo/bar:baz')
            openshift_build_selflink = '/builds/blablabla'
            build_process_failed = False
            tag_conf = TagConf()
            plugin_workspace = {}
            if use_import:
                exit_results = {
                    KojiImportPlugin.key: MOCK_KOJI_BUILD_ID,
                }
            else:
                exit_results = {
                    KojiPromotePlugin.key: MOCK_KOJI_BUILD_ID,
                }
            prebuild_results = {}

        monkeypatch.setenv("BUILD", json.dumps({
            'metadata': {
                'labels': {
                    'koji-task-id': MOCK_KOJI_TASK_ID,
                },
                'name': {},
            }
        }))

        session = MockedClientSession('', has_kerberos=True)
        flexmock(koji, ClientSession=lambda hub, opts: session, PathInfo=MockedPathInfo)

        kwargs = {
            'url': 'https://something.com',
            'smtp_host': 'smtp.bar.com',
            'from_address': 'foo@bar.com',
            'to_koji_submitter': to_koji_submitter,
            'to_koji_pkgowner': to_koji_pkgowner,
            'email_domain': MOCK_EMAIL_DOMAIN
        }
        smtp_map = {
            'from_address': 'foo@bar.com',
            'host': 'smtp.bar.com',
            'send_to_submitter': to_koji_submitter,
            'send_to_pkg_owner': to_koji_pkgowner,
            'domain': MOCK_EMAIL_DOMAIN,
        }
        if has_addit_address:
            kwargs['additional_addresses'] = [MOCK_ADDITIONAL_EMAIL]
            smtp_map['additional_addresses'] = [MOCK_ADDITIONAL_EMAIL]

        koji_map = None
        if has_koji_config:
            kwargs['koji_hub'] = '/'
            kwargs['koji_proxyuser'] = None
            kwargs['koji_ssl_certs_dir'] = '/certs'
            kwargs['koji_krb_principal'] = None
            kwargs['koji_krb_keytab'] = None
            koji_map = {
                'hub_url': '/',
                'root_url': '',
                'auth': {
                    'ssl_certs_dir': '/certs',
                }
            }

        workflow = WF()
        if reactor_config_map:
            openshift_map = {'url': 'https://something.com'}
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
            full_config = {
                'version': 1,
                'smtp': smtp_map,
                'openshift': openshift_map,
            }
            if koji_map:
                full_config['koji'] = koji_map
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig(full_config)

        p = SendMailPlugin(None, workflow, **kwargs)

        if not expected_receivers:
            with pytest.raises(RuntimeError):
                p._get_receivers_list()
        else:
            receivers = p._get_receivers_list()
            assert sorted(receivers) == sorted(expected_receivers)

    @pytest.mark.parametrize('has_kerberos, expected_receivers', [
        (True, [MOCK_KOJI_OWNER_EMAIL, MOCK_KOJI_SUBMITTER_EMAIL]),
        (False, [MOCK_KOJI_OWNER_GENERATED, MOCK_KOJI_SUBMITTER_GENERATED])])
    def test_generated_email(self, monkeypatch, has_kerberos, expected_receivers,
                             reactor_config_map):
        class TagConf(object):
            unique_images = []

        class WF(object):
            image = util.ImageName.parse('foo/bar:baz')
            openshift_build_selflink = '/builds/blablabla'
            build_process_failed = False
            tag_conf = TagConf()
            exit_results = {
                KojiPromotePlugin.key: MOCK_KOJI_BUILD_ID
            }
            plugin_workspace = {}

        monkeypatch.setenv("BUILD", json.dumps({
            'metadata': {
                'labels': {
                    'koji-task-id': MOCK_KOJI_TASK_ID,
                },
                'name': {},
            }
        }))

        session = MockedClientSession('', has_kerberos=has_kerberos)
        flexmock(koji, ClientSession=lambda hub, opts: session, PathInfo=MockedPathInfo)

        kwargs = {
            'url': 'https://something.com',
            'smtp_host': 'smtp.bar.com',
            'from_address': 'foo@bar.com',
            'to_koji_submitter': True,
            'to_koji_pkgowner': True,
            'email_domain': MOCK_EMAIL_DOMAIN,
            'koji_hub': '/',
            'koji_proxyuser': None,
            'koji_ssl_certs_dir': '/certs',
            'koji_krb_principal': None,
            'koji_krb_keytab': None
        }

        workflow = WF()
        if reactor_config_map:
            openshift_map = {'url': 'https://something.com'}
            koji_map = {
                'hub_url': '/',
                'root_url': '',
                'auth': {
                    'ssl_certs_dir': '/certs',
                }
            }
            smtp_map = {
                'from_address': 'foo@bar.com',
                'host': 'smtp.bar.com',
                'send_to_submitter': True,
                'send_to_pkg_owner': True,
                'domain': MOCK_EMAIL_DOMAIN,
            }
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig({'version': 1, 'smtp': smtp_map, 'koji': koji_map,
                               'openshift': openshift_map})

        p = SendMailPlugin(None, workflow, **kwargs)
        receivers = p._get_receivers_list()
        assert sorted(receivers) == sorted(expected_receivers)

        if has_kerberos:
            assert p.submitter == MOCK_KOJI_SUBMITTER_EMAIL
        else:
            assert p.submitter == MOCK_KOJI_SUBMITTER_GENERATED

    @pytest.mark.parametrize('exception_location, expected_receivers', [
        ('koji_connection', []),
        ('submitter', [MOCK_KOJI_OWNER_EMAIL]),
        ('empty_submitter', [MOCK_KOJI_OWNER_EMAIL]),
        ('owner', [MOCK_KOJI_SUBMITTER_EMAIL]),
        ('empty_owner', [MOCK_KOJI_SUBMITTER_EMAIL]),
        ('empty_email_domain', [])])
    def test_koji_recepients_exception(self, monkeypatch, exception_location, expected_receivers,
                                       reactor_config_map):
        class TagConf(object):
            unique_images = []

        if exception_location == 'empty_owner':
            koji_build_id = None
        else:
            koji_build_id = MOCK_KOJI_BUILD_ID

        if exception_location == 'empty_submitter':
            koji_task_id = None
        else:
            koji_task_id = MOCK_KOJI_TASK_ID

        class WF(object):
            image = util.ImageName.parse('foo/bar:baz')
            openshift_build_selflink = '/builds/blablabla'
            build_process_failed = False
            tag_conf = TagConf()
            exit_results = {
                KojiPromotePlugin.key: koji_build_id
            }
            plugin_workspace = {}

        monkeypatch.setenv("BUILD", json.dumps({
            'metadata': {
                'labels': {
                    'koji-task-id': koji_task_id,
                },
                'name': {},
            }
        }))

        has_kerberos = exception_location != 'empty_email_domain'
        session = MockedClientSession('', has_kerberos=has_kerberos)
        if exception_location == 'koji_connection':
            (flexmock(session)
                .should_receive('ssl_login')
                .and_raise(RuntimeError, "xyz"))
        elif exception_location == 'submitter':
            (flexmock(session)
                .should_receive('getTaskInfo')
                .and_raise(RuntimeError, "xyz"))
        elif exception_location == 'owner':
            (flexmock(session)
                .should_receive('getPackageConfig')
                .and_raise(RuntimeError, "xyz"))

        flexmock(koji, ClientSession=lambda hub, opts: session, PathInfo=MockedPathInfo)

        kwargs = {
            'url': 'https://something.com',
            'smtp_host': 'smtp.bar.com',
            'from_address': 'foo@bar.com',
            'to_koji_submitter': True,
            'to_koji_pkgowner': True,
            'koji_hub': '/',
            'koji_proxyuser': None,
            'koji_ssl_certs_dir': '/certs',
            'koji_krb_principal': None,
            'koji_krb_keytab': None
        }
        smtp_map = {
            'from_address': 'foo@bar.com',
            'host': 'smtp.bar.com',
            'send_to_submitter': True,
            'send_to_pkg_owner': True,
        }
        if exception_location != 'empty_email_domain':
            kwargs['email_domain'] = MOCK_EMAIL_DOMAIN
            smtp_map['domain'] = MOCK_EMAIL_DOMAIN

        workflow = WF()
        if reactor_config_map:
            openshift_map = {'url': 'https://something.com'}
            koji_map = {
                'hub_url': '/',
                'root_url': '',
                'auth': {
                    'ssl_certs_dir': '/certs',
                }
            }
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig({'version': 1, 'smtp': smtp_map, 'koji': koji_map,
                               'openshift': openshift_map})

        p = SendMailPlugin(None, workflow, **kwargs)
        if not expected_receivers:
            with pytest.raises(RuntimeError):
                p._get_receivers_list()
        else:
            receivers = p._get_receivers_list()
            assert sorted(receivers) == sorted(expected_receivers)

    @pytest.mark.parametrize('throws_exception', [False, True])
    def test_send_mail(self, throws_exception, reactor_config_map):
        class WF(object):
            exit_results = {}
            plugin_workspace = {}

        workflow = WF()
        if reactor_config_map:
            smtp_map = {
                'from_address': 'foo@bar.com',
                'host': 'smtp.bar.com',
            }
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig({'version': 1, 'smtp': smtp_map})

        p = SendMailPlugin(None, workflow, from_address='foo@bar.com', smtp_host='smtp.spam.com')

        class SMTP(object):
            def sendmail(self, from_addr, to, msg):
                pass

            def quit(self):
                pass

        smtp_inst = SMTP()
        flexmock(smtplib).should_receive('SMTP').and_return(smtp_inst)
        sendmail_chain = (flexmock(smtp_inst).should_receive('sendmail').
                          with_args('foo@bar.com', ['spam@spam.com'], str))
        if throws_exception:
            sendmail_chain.and_raise(smtplib.SMTPException, "foo")
        flexmock(smtp_inst).should_receive('quit')

        if throws_exception:
            with pytest.raises(SMTPException) as e:
                p._send_mail(['spam@spam.com'], 'subject', 'body')
            assert str(e.value) == 'foo'
        else:
            p._send_mail(['spam@spam.com'], 'subject', 'body')

    def test_run_ok(self, reactor_config_map):  # noqa
        class TagConf(object):
            unique_images = []

        class WF(object):
            autorebuild_canceled = False
            build_canceled = False
            prebuild_results = {CheckAndSetRebuildPlugin.key: True}
            image = util.ImageName.parse('repo/name')
            build_process_failed = True
            tag_conf = TagConf()
            exit_results = {}
            plugin_workspace = {}

        receivers = ['foo@bar.com', 'x@y.com']

        workflow = WF()
        mock_store_metadata_results(workflow)

        if reactor_config_map:
            smtp_map = {
                'from_address': 'foo@bar.com',
                'host': 'smtp.bar.com',
            }
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig({'version': 1, 'smtp': smtp_map})

        p = SendMailPlugin(None, workflow,
                           from_address='foo@bar.com', smtp_host='smtp.spam.com',
                           send_on=[AF])

        (flexmock(p).should_receive('_should_send')
         .with_args(True, False, False, False).and_return(True))
        flexmock(p).should_receive('_get_receivers_list').and_return(receivers)
        flexmock(p).should_receive('_fetch_log_files').and_return(None)
        flexmock(p).should_receive('_send_mail').with_args(receivers,
                                                           six.text_type, six.text_type, None)

        p.run()

    def test_run_ok_and_send(self, monkeypatch, reactor_config_map):  # noqa
        class TagConf(object):
            unique_images = []

        class WF(object):
            autorebuild_canceled = False
            build_canceled = False
            prebuild_results = {CheckAndSetRebuildPlugin.key: True}
            image = util.ImageName.parse('repo/name')
            build_process_failed = True
            tag_conf = TagConf()
            exit_results = {}
            plugin_workspace = {}

        class SMTP(object):
            def sendmail(self, from_addr, to, msg):
                pass

            def quit(self):
                pass

        monkeypatch.setenv("BUILD", json.dumps({
            'metadata': {
                'labels': {
                    'koji-task-id': MOCK_KOJI_TASK_ID,
                },
                'name': {},
            }
        }))

        workflow = WF()
        if reactor_config_map:
            smtp_map = {
                'from_address': 'foo@bar.com',
                'host': 'smtp.spam.com',
            }
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig({'version': 1, 'smtp': smtp_map})

        receivers = ['foo@bar.com', 'x@y.com']
        fake_logs = [LogEntry(None, 'orchestrator'),
                     LogEntry(None, 'orchestrator line 2'),
                     LogEntry('x86_64', 'Hurray for bacon: \u2017'),
                     LogEntry('x86_64', 'line 2')]
        p = SendMailPlugin(None, workflow,
                           from_address='foo@bar.com', smtp_host='smtp.spam.com',
                           send_on=[AF])

        (flexmock(p).should_receive('_should_send')
            .with_args(True, False, False, False).and_return(True))
        flexmock(p).should_receive('_get_receivers_list').and_return(receivers)
        flexmock(OSBS).should_receive('get_orchestrator_build_logs').and_return(fake_logs)
        flexmock(p).should_receive('_get_image_name_and_repos').and_return(('foobar',
                                                                           ['foo/bar:baz',
                                                                            'foo/bar:spam']))

        smtp_inst = SMTP()
        flexmock(smtplib).should_receive('SMTP').and_return(smtp_inst)
        p.run()

    def test_run_fails_to_obtain_receivers(self, reactor_config_map):  # noqa
        class TagConf(object):
            unique_images = []

        class WF(object):
            autorebuild_canceled = False
            build_canceled = False
            prebuild_results = {CheckAndSetRebuildPlugin.key: True}
            image = util.ImageName.parse('repo/name')
            build_process_failed = True
            tag_conf = TagConf()
            exit_results = {}
            plugin_workspace = {}

        error_addresses = ['error@address.com']
        workflow = WF()
        mock_store_metadata_results(workflow)

        if reactor_config_map:
            smtp_map = {
                'from_address': 'foo@bar.com',
                'host': 'smtp.bar.com',
                'error_addresses': ['error@address.com'],
            }
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig({'version': 1, 'smtp': smtp_map})

        p = SendMailPlugin(None, workflow,
                           from_address='foo@bar.com', smtp_host='smtp.spam.com',
                           send_on=[AF], error_addresses=error_addresses)

        (flexmock(p).should_receive('_should_send')
            .with_args(True, False, False, False).and_return(True))
        flexmock(p).should_receive('_get_receivers_list').and_raise(RuntimeError())
        flexmock(p).should_receive('_fetch_log_files').and_return(None)
        flexmock(p).should_receive('_get_image_name_and_repos').and_return(('foobar',
                                                                           ['foo/bar:baz',
                                                                            'foo/bar:spam']))
        flexmock(p).should_receive('_send_mail').with_args(error_addresses,
                                                           six.text_type, six.text_type, None)

        p.run()

    def test_run_invalid_receivers(self, caplog, reactor_config_map):  # noqa
        class TagConf(object):
            unique_images = []

        class WF(object):
            autorebuild_canceled = False
            build_canceled = False
            prebuild_results = {CheckAndSetRebuildPlugin.key: True}
            image = util.ImageName.parse('repo/name')
            build_process_failed = True
            tag_conf = TagConf()
            exit_results = {}
            plugin_workspace = {}

        error_addresses = ['error@address.com']
        workflow = WF()
        mock_store_metadata_results(workflow)

        if reactor_config_map:
            smtp_map = {
                'from_address': 'foo@bar.com',
                'host': 'smtp.bar.com',
                'error_addresses': ['error@address.com'],
            }
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig({'version': 1, 'smtp': smtp_map})

        p = SendMailPlugin(None, workflow,
                           from_address='foo@bar.com', smtp_host='smtp.spam.com',
                           send_on=[AF], error_addresses=error_addresses)

        (flexmock(p).should_receive('_should_send')
            .with_args(True, False, False, False).and_return(True))
        flexmock(p).should_receive('_get_receivers_list').and_return([])
        flexmock(p).should_receive('_fetch_log_files').and_return(None)
        flexmock(p).should_receive('_get_image_name_and_repos').and_return(('foobar',
                                                                           ['foo/bar:baz',
                                                                            'foo/bar:spam']))
        p.run()
        assert 'no valid addresses in requested addresses. Doing nothing' in caplog.text()

    def test_run_does_nothing_if_conditions_not_met(self, reactor_config_map):  # noqa
        class WF(object):
            autorebuild_canceled = False
            build_canceled = False
            prebuild_results = {CheckAndSetRebuildPlugin.key: True}
            image = util.ImageName.parse('repo/name')
            build_process_failed = True
            exit_results = {}
            plugin_workspace = {}

        workflow = WF()
        if reactor_config_map:
            smtp_map = {
                'from_address': 'foo@bar.com',
                'host': 'smtp.spam.com',
            }
            workflow.plugin_workspace[ReactorConfigPlugin.key] = {}
            workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] =\
                ReactorConfig({'version': 1, 'smtp': smtp_map})

        p = SendMailPlugin(None, workflow,
                           from_address='foo@bar.com', smtp_host='smtp.spam.com',
                           send_on=[MS])

        (flexmock(p).should_receive('_should_send')
            .with_args(True, False, False, False).and_return(False))
        flexmock(p).should_receive('_get_receivers_list').times(0)
        flexmock(p).should_receive('_send_mail').times(0)

        p.run()
