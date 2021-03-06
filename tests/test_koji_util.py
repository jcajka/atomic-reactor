"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import absolute_import, print_function, unicode_literals
import time

from requests.exceptions import ConnectionError

try:
    import koji
except ImportError:
    import inspect
    import os
    import sys

    # Find our mocked koji module
    import tests.koji as koji
    mock_koji_path = os.path.dirname(inspect.getfile(koji.ClientSession))
    if mock_koji_path not in sys.path:
        sys.path.append(os.path.dirname(mock_koji_path))

    # Now load it properly, the same way the module we're testing will
    del koji
    import koji

from atomic_reactor.koji_util import (koji_login, create_koji_session,
                                      TaskWatcher, tag_koji_build)
from atomic_reactor import koji_util
from atomic_reactor.plugin import BuildCanceledException
from atomic_reactor.constants import HTTP_MAX_RETRIES
import flexmock
import pytest


class TestKojiLogin(object):
    @pytest.mark.parametrize('proxyuser', [None, 'proxy'])
    def test_koji_login_krb_keyring(self, proxyuser):
        session = flexmock()
        expectation = session.should_receive('krb_login').once().and_return(True)
        kwargs = {}
        if proxyuser is not None:
            expectation.with_args(proxyuser=proxyuser)
            kwargs['proxyuser'] = proxyuser
        else:
            expectation.with_args()

        koji_login(session, **kwargs)

    @pytest.mark.parametrize('proxyuser', [None, 'proxy'])
    def test_koji_login_krb_keytab(self, proxyuser):
        session = flexmock()
        expectation = session.should_receive('krb_login').once().and_return(True)
        principal = 'user'
        keytab = '/keytab'
        call_kwargs = {
            'krb_principal': principal,
            'krb_keytab': keytab,
        }
        exp_kwargs = {
            'principal': principal,
            'keytab': keytab,
        }
        if proxyuser is not None:
            call_kwargs['proxyuser'] = proxyuser
            exp_kwargs['proxyuser'] = proxyuser

        expectation.with_args(**exp_kwargs)
        koji_login(session, **call_kwargs)

    @pytest.mark.parametrize('proxyuser', [None, 'proxy'])
    @pytest.mark.parametrize('serverca', [True, False])
    def test_koji_login_ssl(self, tmpdir, proxyuser, serverca):
        session = flexmock()
        expectation = session.should_receive('ssl_login').once().and_return(True)
        call_kwargs = {
            'ssl_certs_dir': str(tmpdir),
        }
        exp_kwargs = {
            'cert': str(tmpdir.join('cert')),
            'ca': None,
        }

        if serverca:
            serverca = tmpdir.join('serverca')
            serverca.write('spam')
            exp_kwargs['serverca'] = str(serverca)

        if proxyuser:
            call_kwargs['proxyuser'] = proxyuser
            exp_kwargs['proxyuser'] = proxyuser

        expectation.with_args(**exp_kwargs)
        koji_login(session, **call_kwargs)


class TestCreateKojiSession(object):
    def test_create_simple_session(self):
        url = 'https://example.com'
        session = flexmock()

        (flexmock(koji_util.koji).should_receive('ClientSession').with_args(
            url, opts={'krb_rdns': False}).and_return(session))
        assert create_koji_session(url)._wrapped_session == session

    @pytest.mark.parametrize(('ssl_session'), [
        (True, False),
    ])
    def test_create_authenticated_session(self, tmpdir, ssl_session):
        url = 'https://example.com'
        args = {}

        session = flexmock()
        if ssl_session:
            args['ssl_certs_dir'] = str(tmpdir)
            session.should_receive('ssl_login').once().and_return(True)
        else:
            session.should_receive('krb_login').once().and_return(True)

        (flexmock(koji_util.koji).should_receive('ClientSession').with_args(
            url, opts={'krb_rdns': False}).and_return(session))
        assert create_koji_session(url, args)._wrapped_session == session

    @pytest.mark.parametrize(('ssl_session'), [
        (True, False),
    ])
    def test_fail_authenticated_session(self, tmpdir, ssl_session):
        url = 'https://example.com'
        args = {}

        session = flexmock()
        if ssl_session:
            args['ssl_certs_dir'] = str(tmpdir)
            session.should_receive('ssl_login').once().and_return(False)
        else:
            session.should_receive('krb_login').once().and_return(False)

        (flexmock(koji_util.koji).should_receive('ClientSession').with_args(
            url, opts={'krb_rdns': False}).and_return(session))
        with pytest.raises(RuntimeError):
            create_koji_session(url, args)

    @pytest.mark.parametrize(('auth_type', 'auth_args'), [
        ('ssl_login', {'ssl_certs_dir': 'value'}),
        ('krb_login', {}),
    ])
    @pytest.mark.parametrize(('error_type', 'should_recover', 'attempts'), [
        (ConnectionError, True, HTTP_MAX_RETRIES - 1),
        (ConnectionError, False, HTTP_MAX_RETRIES),
        (KeyError, False, 1),
    ])
    def test_create_session_failures(self, tmpdir, auth_type, auth_args,
                                     error_type, should_recover, attempts):
        url = 'https://example.com'
        if auth_args.get('ssl_certs_dir', None) == 'value':
            auth_args['ssl_certs_dir'] = str(tmpdir)

        session = flexmock(_value="test_value")
        (flexmock(koji_util.koji).should_receive('ClientSession').with_args(
            url, opts={'krb_rdns': False}).and_return(session))

        flexmock(time).should_receive('sleep').and_return(None)

        if should_recover:
            (session.should_receive(auth_type)
                .and_raise(error_type)
                .and_raise(error_type)
                .and_return(True))
            test_session = create_koji_session(url, auth_args)
            assert test_session._wrapped_session == session
            assert test_session._value == "test_value"
        else:
            session.should_receive(auth_type).and_raise(error_type).times(attempts)
            with pytest.raises(error_type):
                create_koji_session(url, auth_args)


class TestStreamTaskOutput(object):
    def test_output_as_generator(self):
        contents = 'this is the simulated file contents'

        session = flexmock()
        expectation = session.should_receive('downloadTaskOutput')

        for chunk in contents:
            expectation = expectation.and_return(chunk)
        # Empty content to simulate end of stream.
        expectation.and_return('')

        streamer = koji_util.stream_task_output(session, 123, 'file.ext')
        assert ''.join(list(streamer)) == contents


class TestTaskWatcher(object):
    @pytest.mark.parametrize(('finished', 'info', 'exp_state', 'exp_failed'), [
        ([False, False, True],
         {'state': koji.TASK_STATES['CANCELED']},
         'CANCELED', True),

        ([False, True],
         {'state': koji.TASK_STATES['FAILED']},
         'FAILED', True),

        ([True],
         {'state': koji.TASK_STATES['CLOSED']},
         'CLOSED', False),
    ])
    def test_wait(self, finished, info, exp_state, exp_failed):
        session = flexmock()
        task_id = 1234
        task_finished = (session.should_receive('taskFinished')
                         .with_args(task_id))
        for finished_value in finished:
            task_finished = task_finished.and_return(finished_value)

        (session.should_receive('getTaskInfo')
            .with_args(task_id, request=True)
            .once()
            .and_return(info))

        task = TaskWatcher(session, task_id, poll_interval=0)
        assert task.wait() == exp_state
        assert task.failed() == exp_failed

    def test_cancel(self):
        session = flexmock()
        task_id = 1234
        (session
            .should_receive('taskFinished')
            .with_args(task_id)
            .and_raise(BuildCanceledException))

        task = TaskWatcher(session, task_id, poll_interval=0)
        with pytest.raises(BuildCanceledException):
            task.wait()

        assert task.failed()


class TestTagKojiBuild(object):
    @pytest.mark.parametrize(('task_state', 'failure'), (
        ('CLOSED', False),
        ('CANCELED', True),
        ('FAILED', True),
    ))
    def test_tagging(self, task_state, failure):
        session = flexmock()
        task_id = 9876
        build_id = 1234
        target_name = 'target'
        tag_name = 'images-candidate'
        target_info = {'dest_tag_name': tag_name}
        task_info = {'state': koji.TASK_STATES[task_state]}

        (session
            .should_receive('getBuildTarget')
            .with_args(target_name)
            .and_return(target_info))
        (session
            .should_receive('tagBuild')
            .with_args(tag_name, build_id)
            .and_return(task_id))
        (session
            .should_receive('taskFinished')
            .with_args(task_id)
            .and_return(True))
        (session
            .should_receive('getTaskInfo')
            .with_args(task_id, request=True)
            .and_return(task_info))

        if failure:
            with pytest.raises(RuntimeError):
                tag_koji_build(session, build_id, target_name)
        else:
            build_tag = tag_koji_build(session, build_id, target_name)
            assert build_tag == tag_name
