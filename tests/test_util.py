"""
Copyright (c) 2015 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

import json
import logging
import os
import tempfile
import pytest
import requests
import responses
from requests.exceptions import ConnectionError
import subprocess
import time
from collections import namedtuple

from tempfile import mkdtemp
from textwrap import dedent
from flexmock import flexmock

from collections import OrderedDict
import docker
import yaml
from atomic_reactor.build import BuildResult
from atomic_reactor.constants import (IMAGE_TYPE_DOCKER_ARCHIVE, IMAGE_TYPE_OCI, IMAGE_TYPE_OCI_TAR)
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.util import (ImageName, wait_for_command, clone_git_repo,
                                 LazyGit, figure_out_build_file,
                                 render_yum_repo, process_substitutions,
                                 get_checksums, print_version_of_tools,
                                 get_version_of_tools,
                                 human_size, CommandResult,
                                 registry_hostname, Dockercfg, RegistrySession,
                                 get_manifest_digests, ManifestDigest,
                                 get_manifest_list,
                                 get_build_json, is_scratch_build, is_isolated_build, df_parser,
                                 are_plugins_in_order, LabelFormatter,
                                 guess_manifest_media_type,
                                 get_manifest_media_type,
                                 get_manifest_media_version,
                                 get_primary_images,
                                 get_image_upload_filename,
                                 split_module_spec, ModuleSpec,
                                 read_yaml, read_yaml_from_file_path, OSBSLogs,
                                 get_platforms_in_limits, get_orchestrator_platforms)
from atomic_reactor import util
from tests.constants import (DOCKERFILE_GIT, DOCKERFILE_SHA1,
                             INPUT_IMAGE, MOCK, MOCK_SOURCE,
                             REACTOR_CONFIG_MAP)
from atomic_reactor.constants import INSPECT_CONFIG, PLUGIN_BUILD_ORCHESTRATE_KEY

from tests.util import requires_internet

if MOCK:
    from tests.docker_mock import mock_docker
    from tests.retry_mock import mock_get_retry_session

TEST_DATA = {
    "repository.com/image-name": ImageName(registry="repository.com", repo="image-name"),
    "repository.com/prefix/image-name:1": ImageName(registry="repository.com",
                                                    namespace="prefix",
                                                    repo="image-name", tag="1"),
    "repository.com/prefix/image-name@sha256:12345": ImageName(registry="repository.com",
                                                               namespace="prefix",
                                                               repo="image-name",
                                                               tag="sha256:12345"),
    "repository.com/prefix/image-name": ImageName(registry="repository.com",
                                                  namespace="prefix",
                                                  repo="image-name"),
    "image-name": ImageName(repo="image-name"),

    "registry:5000/image-name:latest": ImageName(registry="registry:5000",
                                                 repo="image-name", tag="latest"),
    "registry:5000/image-name@sha256:12345": ImageName(registry="registry:5000",
                                                       repo="image-name", tag="sha256:12345"),
    "registry:5000/image-name": ImageName(registry="registry:5000", repo="image-name"),

    "fedora:20": ImageName(repo="fedora", tag="20"),
    "fedora@sha256:12345": ImageName(repo="fedora", tag="sha256:12345"),

    "prefix/image-name:1": ImageName(namespace="prefix", repo="image-name", tag="1"),
    "prefix/image-name@sha256:12345": ImageName(namespace="prefix", repo="image-name",
                                                tag="sha256:12345"),

    "library/fedora:20": ImageName(namespace="library", repo="fedora", tag="20"),
    "library/fedora@sha256:12345": ImageName(namespace="library", repo="fedora",
                                             tag="sha256:12345"),
}


class CustomTestException(Exception):
    """
    Custom Exception used to prematurely end function call
    """
    pass


def test_image_name_parse():
    for inp, parsed in TEST_DATA.items():
        assert ImageName.parse(inp) == parsed


def test_image_name_format():
    for expected, image_name in TEST_DATA.items():
        assert image_name.to_str() == expected


def test_image_name_comparison():
    # make sure that both "==" and "!=" are implemented right on both Python major releases
    i1 = ImageName(registry='foo.com', namespace='spam', repo='bar', tag='1')
    i2 = ImageName(registry='foo.com', namespace='spam', repo='bar', tag='1')
    assert i1 == i2
    assert not i1 != i2

    i2 = ImageName(registry='foo.com', namespace='spam', repo='bar', tag='2')
    assert not i1 == i2
    assert i1 != i2


def test_wait_for_command():
    if MOCK:
        mock_docker()

    d = docker.APIClient()
    logs_gen = d.pull(INPUT_IMAGE, decode=True, stream=True)
    assert wait_for_command(logs_gen) is not None


@requires_internet
def test_clone_git_repo(tmpdir):
    tmpdir_path = str(tmpdir.realpath())
    commit_id = clone_git_repo(DOCKERFILE_GIT, tmpdir_path)
    assert commit_id is not None
    assert len(commit_id) == 40  # current git hashes are this long
    assert os.path.isdir(os.path.join(tmpdir_path, '.git'))


class TestCommandResult(object):
    @pytest.mark.parametrize(('item', 'expected'), [
        ({"stream": "Step 0 : FROM ebbc51b7dfa5bcd993a[...]"},
         "Step 0 : FROM ebbc51b7dfa5bcd993a[...]"),

        ('this is not valid JSON',
         'this is not valid JSON'),
    ])
    def test_parse_item(self, item, expected):
        cr = CommandResult()
        cr.parse_item(item)
        assert cr.logs == [expected]


@requires_internet
def test_clone_git_repo_by_sha1(tmpdir):
    tmpdir_path = str(tmpdir.realpath())
    commit_id = clone_git_repo(DOCKERFILE_GIT, tmpdir_path, commit=DOCKERFILE_SHA1)
    assert commit_id is not None
    assert commit_id == DOCKERFILE_SHA1
    assert len(commit_id) == 40  # current git hashes are this long
    assert os.path.isdir(os.path.join(tmpdir_path, '.git'))


BUILD_FILE_CONTENTS_DOCKER = {
    "Dockerfile": "",
    "container.yaml": "",
    "subdir/Dockerfile": ""
}

BUILD_FILE_CONTENTS_FLATPAK = {
    "container.yaml": "flatpak: {}\n",
    "subdir/container.yaml": "flatpak: {}\n"
}

BUILD_FILE_CONTENTS_BROKEN = {
    "dummy": "",
    "subdir/container.yaml": ""
}


@pytest.mark.parametrize('contents,local_path,expected_path,expected_exception', [
    (BUILD_FILE_CONTENTS_DOCKER, None, "Dockerfile", None),
    (BUILD_FILE_CONTENTS_DOCKER, "subdir", "subdir/Dockerfile", None),
    (BUILD_FILE_CONTENTS_DOCKER, "subdir/Dockerfile", "subdir/Dockerfile", None),
    (BUILD_FILE_CONTENTS_FLATPAK, None, "container.yaml", None),
    (BUILD_FILE_CONTENTS_FLATPAK, "subdir", "subdir/container.yaml", None),
    (BUILD_FILE_CONTENTS_FLATPAK, "subdir/container.yaml", "subdir/container.yaml", None),
    (BUILD_FILE_CONTENTS_BROKEN, None, None, "doesn't exist"),
    (BUILD_FILE_CONTENTS_BROKEN, "subdir", None, "no accompanying Dockerfile"),
    (BUILD_FILE_CONTENTS_BROKEN, "nonexist_subdir", None, "doesn't exist"),
])
def test_figure_out_build_file(tmpdir, contents, local_path, expected_path, expected_exception):
    tmpdir_path = str(tmpdir.realpath())
    for path, path_contents in contents.items():
        fullpath = os.path.join(tmpdir_path, path)
        d = os.path.dirname(fullpath)
        if not os.path.exists(d):
            os.makedirs(d)
        with open(fullpath, "w") as f:
            f.write(path_contents)

    if expected_exception is None:
        path, dir = figure_out_build_file(tmpdir_path, local_path=local_path)
        assert path == os.path.join(tmpdir_path, expected_path)
        assert os.path.isfile(path)
        assert os.path.isdir(dir)
    else:
        with pytest.raises(Exception) as e:
            figure_out_build_file(tmpdir_path, local_path=local_path)
        print(e)
        assert expected_exception in str(e)


@requires_internet
def test_lazy_git():
    lazy_git = LazyGit(git_url=DOCKERFILE_GIT)
    with lazy_git:
        assert lazy_git.git_path is not None
        assert lazy_git.commit_id is not None
        assert len(lazy_git.commit_id) == 40  # current git hashes are this long

        previous_commit_id = lazy_git.commit_id
        lazy_git.reset('HEAD~2')  # Go back two commits
        assert lazy_git.commit_id is not None
        assert lazy_git.commit_id != previous_commit_id
        assert len(lazy_git.commit_id) == 40  # current git hashes are this long


@requires_internet
def test_lazy_git_with_tmpdir(tmpdir):
    t = str(tmpdir.realpath())
    lazy_git = LazyGit(git_url=DOCKERFILE_GIT, tmpdir=t)
    assert lazy_git._tmpdir == t
    assert lazy_git.git_path is not None
    assert lazy_git.commit_id is not None
    assert len(lazy_git.commit_id) == 40  # current git hashes are this long


def test_render_yum_repo_unicode():
    yum_repo = OrderedDict((
        ("name", "asd"),
        ("baseurl", "http://example.com/$basearch/test.repo"),
        ("enabled", "1"),
        ("gpgcheck", "0"),
    ))
    rendered_repo = render_yum_repo(yum_repo)
    assert rendered_repo == """\
[asd]
name=asd
baseurl=http://example.com/\$basearch/test.repo
enabled=1
gpgcheck=0
"""


@pytest.mark.parametrize('dct, subst, expected', [
    ({'foo': 'bar'}, ['foo=spam'], {'foo': 'spam'}),
    ({'foo': 'bar'}, ['baz=spam'], {'foo': 'bar', 'baz': 'spam'}),
    ({'foo': 'bar'}, ['foo.bar=spam'], {'foo': {'bar': 'spam'}}),
    ({'foo': 'bar'}, ['spam.spam=spam'], {'foo': 'bar', 'spam': {'spam': 'spam'}}),


    ({'x_plugins': [{'name': 'a', 'args': {'b': 'c'}}]}, {'x_plugins.a.b': 'd'},
        {'x_plugins': [{'name': 'a', 'args': {'b': 'd'}}]}),
    # substituting plugins doesn't add new params
    ({'x_plugins': [{'name': 'a', 'args': {'b': 'c'}}]}, {'x_plugins.a.c': 'd'},
        {'x_plugins': [{'name': 'a', 'args': {'b': 'c'}}]}),
    ({'x_plugins': [{'name': 'a', 'args': {'b': 'c'}}]}, {'x_plugins.X': 'd'},
        ValueError()),
])
def test_process_substitutions(dct, subst, expected):
    if isinstance(expected, Exception):
        with pytest.raises(type(expected)):
            process_substitutions(dct, subst)
    else:
        process_substitutions(dct, subst)
        assert dct == expected


@pytest.mark.parametrize('content, algorithms, expected', [
    (b'abc', ['md5', 'sha256'],
     {'md5sum': '900150983cd24fb0d6963f7d28e17f72',
      'sha256sum': 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'}),
    (b'abc', ['md5'], {'md5sum': '900150983cd24fb0d6963f7d28e17f72'}),
    (b'abc', [], {})
])
def test_get_hexdigests(tmpdir, content, algorithms, expected):
    with tempfile.NamedTemporaryFile(dir=str(tmpdir)) as tmpfile:
        tmpfile.write(content)
        tmpfile.flush()

        checksums = get_checksums(tmpfile.name, algorithms)
        assert checksums == expected


@pytest.mark.parametrize('path, image_type, expected', [
    ('foo.tar', IMAGE_TYPE_DOCKER_ARCHIVE, 'docker-image-XXX.x86_64.tar'),
    ('foo.tar.gz', IMAGE_TYPE_DOCKER_ARCHIVE, 'docker-image-XXX.x86_64.tar.gz'),
    ('foo.tar.gz', IMAGE_TYPE_OCI_TAR, 'oci-image-XXX.x86_64.tar.gz'),
    ('foo', IMAGE_TYPE_OCI, None),
])
def test_get_image_upload_filename(path, image_type, expected):
    metadata = {
        'path': path,
        'type': image_type,
    }
    if expected is None:
        with pytest.raises(ValueError):
            get_image_upload_filename(metadata, 'XXX', 'x86_64')
    else:
        assert get_image_upload_filename(metadata, 'XXX', 'x86_64') == expected


def test_get_versions_of_tools():
    response = get_version_of_tools()
    assert isinstance(response, list)
    for t in response:
        assert t["name"]
        assert t["version"]


def test_print_versions_of_tools():
    print_version_of_tools()


@pytest.mark.parametrize('size_input,expected', [
    (0, "0.00 B"),
    (1, "1.00 B"),
    (-1, "-1.00 B"),
    (1536, "1.50 KiB"),
    (-1024, "-1.00 KiB"),
    (204800, "200.00 KiB"),
    (6983516, "6.66 MiB"),
    (14355928186, "13.37 GiB"),
    (135734710448947, "123.45 TiB"),
    (1180579814801204129310965, "999.99 ZiB"),
    (1074589982539051580812825722, "888.88 YiB"),
    (4223769947617154742438477168, "3493.82 YiB"),
    (-4223769947617154742438477168, "-3493.82 YiB"),
])
def test_human_size(size_input, expected):
    assert human_size(size_input) == expected


@pytest.mark.parametrize(('registry', 'expected'), [
    ('example.com', 'example.com'),
    # things that don't look like URIs are left untouched
    ('example.com/foo', 'example.com/foo'),
    ('http://example.com', 'example.com'),
    ('http://example.com:5000', 'example.com:5000'),
    ('https://example.com:5000', 'example.com:5000'),
    ('https://example.com/foo', 'example.com')
])
def test_registry_hostname(registry, expected):
    assert registry_hostname(registry) == expected


@pytest.mark.parametrize(('in_config', 'lookup', 'expected'), [
    ('example.com', 'example.com', True),
    ('example.com', 'https://example.com/v2', True),
    ('https://example.com/v2', 'https://example.com/v2', True),
    ('example.com', 'https://example.com/v2', True),
    ('example.com', 'notexample.com', False),
])
def test_dockercfg(tmpdir, in_config, lookup, expected):
    temp_dir = mkdtemp(dir=str(tmpdir))
    with open(os.path.join(temp_dir, '.dockercfg'), 'w+') as dockerconfig:
        dockerconfig.write(json.dumps({
            in_config: {
                'username': 'john.doe', 'password': 'letmein'
            }
        }))
    creds = Dockercfg(temp_dir).get_credentials(lookup)
    found = creds.get('username') == 'john.doe' and creds.get('password') == 'letmein'

    assert found == expected


@pytest.mark.parametrize(('registry', 'insecure'), [
    ('https://example.com', False),
    ('example.com', True),
    ('example.com', False),
])
@pytest.mark.parametrize(('method', 'responses_method'), [
    (RegistrySession.get, responses.GET),
    (RegistrySession.head, responses.HEAD),
    (RegistrySession.put, responses.PUT),
    (RegistrySession.delete, responses.DELETE),
])
@responses.activate
def test_registry_session(tmpdir, registry, insecure, method, responses_method):
    temp_dir = mkdtemp(dir=str(tmpdir))
    with open(os.path.join(temp_dir, '.dockercfg'), 'w+') as dockerconfig:
        dockerconfig.write(json.dumps({
            registry_hostname(registry): {
                'username': 'john.doe', 'password': 'letmein'
            }
        }))
    session = RegistrySession(registry, insecure=insecure, dockercfg_path=temp_dir)

    path = '/v2/test/image/manifests/latest'
    if registry.startswith('http'):
        url = registry + path
    elif insecure:
        https_url = 'https://' + registry + path
        responses.add(responses_method, https_url, body=ConnectionError())
        url = 'http://' + registry + path
    else:
        url = 'https://' + registry + path

    def request_callback(request, all_headers=True):
        assert request.headers.get('Authorization') is not None
        return (200, {}, 'A-OK')

    responses.add_callback(responses_method, url, request_callback)

    res = method(session, path)
    assert res.text == 'A-OK'


@pytest.mark.parametrize(('version', 'expected'), [
    ('v1', 'application/vnd.docker.distribution.manifest.v1+json'),
    ('v2', 'application/vnd.docker.distribution.manifest.v2+json'),
    ('v2_list', 'application/vnd.docker.distribution.manifest.list.v2+json'),
])
def test_get_manifest_media_type(version, expected):
    assert get_manifest_media_type(version) == expected


def test_get_manifest_media_type_unknown():
    with pytest.raises(RuntimeError):
        assert get_manifest_media_type('no_such_version')


@pytest.mark.parametrize(('content', 'media_type'), [
    (b'{', None),
    (b'{}', None),
    (b'{"\xff', None),
    (b'{"schemaVersion": 1}',
     'application/vnd.docker.distribution.manifest.v1+json'),
    (b'{"schemaVersion": 2}',
     None),
    (b'{"mediaType": "application/vnd.docker.distribution.manifest.v2+json"}',
     'application/vnd.docker.distribution.manifest.v2+json'),
    (b'{"mediaType": "application/vnd.oci.image.manifest.v1"}',
     'application/vnd.oci.image.manifest.v1'),
])
def test_guess_manifest_media_type(content, media_type):
    assert guess_manifest_media_type(content) == media_type


@pytest.mark.parametrize('insecure', [
    True,
    False,
])
@pytest.mark.parametrize('versions,require_digest', [
    (('v1', 'v2', 'v2_list'), True),
    (('v1', 'v2', 'v2_list'), False),
    (('v1',), False),
    (('v1',), True),
    (('v2',), False),
    (('v2',), True),
    (tuple(), False),
    (tuple(), True),
    (None, False),
    (None, True),
    (('v2_list',), True),
    (('v2_list',), False),
])
@pytest.mark.parametrize('creds', [
    ('user1', 'pass'),
    (None, 'pass'),
    ('user1', None),
    None,
])
@pytest.mark.parametrize('image,registry,path', [
    ('not-used.com/spam:latest', 'localhost.com',
     '/v2/spam/manifests/latest'),

    ('not-used.com/food/spam:latest', 'http://localhost.com',
     '/v2/food/spam/manifests/latest'),

    ('not-used.com/spam', 'https://localhost.com',
     '/v2/spam/manifests/latest'),
])
@responses.activate
def test_get_manifest_digests(tmpdir, caplog, image, registry, insecure, creds,
                              versions, require_digest, path):
    kwargs = {}

    image = ImageName.parse(image)
    kwargs['image'] = image

    if creds:
        temp_dir = mkdtemp(dir=str(tmpdir))
        with open(os.path.join(temp_dir, '.dockercfg'), 'w+') as dockerconfig:
            dockerconfig.write(json.dumps({
                registry: {
                    'username': creds[0], 'password': creds[1]
                }
            }))
        kwargs['dockercfg_path'] = temp_dir

    kwargs['registry'] = registry

    if insecure is not None:
        kwargs['insecure'] = insecure

    if versions is not None:
        kwargs['versions'] = versions

    kwargs['require_digest'] = require_digest

    def request_callback(request, all_headers=True):
        if creds and creds[0] and creds[1]:
            assert request.headers['Authorization']

        media_type = request.headers['Accept']
        if media_type.endswith('list.v2+json'):
            digest = 'v2_list-digest'
        elif media_type.endswith('v2+json'):
            digest = 'v2-digest'
        elif media_type.endswith('v1+json'):
            digest = 'v1-digest'
        else:
            raise ValueError('Unexpected media type {}'.format(media_type))

        media_type_prefix = media_type.split('+')[0]
        if all_headers:
            headers = {
                'Content-Type': '{}+jsonish'.format(media_type_prefix),
            }
            if not media_type.endswith('list.v2+json'):
                headers['Docker-Content-Digest'] = digest
        else:
            headers = {}
        return (200, headers, '')

    if registry.startswith('http'):
        url = registry + path
    else:
        # In the insecure case, we should try the https URL, and when that produces
        # an error, fall back to http
        if insecure:
            https_url = 'https://' + registry + path
            responses.add(responses.GET, https_url, body=ConnectionError())
            url = 'http://' + registry + path
        else:
            url = 'https://' + registry + path
    responses.add_callback(responses.GET, url, callback=request_callback)

    expected_versions = versions
    if versions is None:
        # Test default versions value
        expected_versions = ('v1', 'v2')

    expected_result = dict(
        (version, '{}-digest'.format(version))
        for version in expected_versions)
    if versions and 'v2_list' in versions:
        expected_result['v2_list'] = True

    # Only capture errors, since we want to be sure none are reported
    with caplog.atLevel(logging.ERROR):
        if expected_versions:
            actual_digests = get_manifest_digests(**kwargs)

            # Check the expected versions are found
            assert actual_digests.v1 == expected_result.get('v1')
            assert actual_digests.v2 == expected_result.get('v2')
            if 'v2_list' in expected_result:
                assert actual_digests.v2_list == expected_result.get('v2_list')
        elif require_digest:
            # When require_digest is set but there is no digest
            # available (no expected_versions), expect a RuntimeError
            with pytest.raises(RuntimeError):
                get_manifest_digests(**kwargs)
        else:
            get_manifest_digests(**kwargs)

    # there should be no errors reported
    assert not caplog.records()


@pytest.mark.parametrize('has_content_type_header', [
    True, False
])
@pytest.mark.parametrize('has_content_digest', [
    True, False
])
@pytest.mark.parametrize('manifest_type,can_convert_v2_v1', [
    ('v1', False),
    ('v2', True),
    ('v2', False),
    ('oci', False),
    ('oci_index', False),
])
def test_get_manifest_digests_missing(tmpdir, has_content_type_header, has_content_digest,
                                      manifest_type, can_convert_v2_v1):
    kwargs = {}

    image = ImageName.parse('example.com/spam:latest')
    kwargs['image'] = image

    kwargs['registry'] = 'https://example.com'

    expected_url = 'https://example.com/v2/spam/manifests/latest'

    mock_get_retry_session()

    def custom_get(url, headers, **kwargs):
        assert url == expected_url

        media_type = headers['Accept']
        media_type_prefix = media_type.split('+')[0]

        assert media_type.endswith('+json')

        # Attempt to simulate how a docker registry behaves:
        #  * If the stored digest is v1, return it
        #  * If the stored digest is v2, and v2 is requested, return it
        #  * If the stored digest is v2, and v1 is requested, try
        #    to convert and return v1 or an error.
        if manifest_type == 'v1':
            digest = 'v1-digest'
            media_type_prefix = 'application/vnd.docker.distribution.manifest.v1'
        elif manifest_type == 'v2':
            if media_type_prefix == 'application/vnd.docker.distribution.manifest.v2':
                digest = 'v2-digest'
            else:
                if not can_convert_v2_v1:
                    response_json = {"errors": [{"code": "MANIFEST_INVALID"}]}
                    response = requests.Response()
                    flexmock(response,
                             status_code=400,
                             content=json.dumps(response_json).encode("utf-8"),
                             headers=headers)

                    return response

                digest = 'v1-converted-digest'
                media_type_prefix = 'application/vnd.docker.distribution.manifest.v1'
        elif manifest_type == 'oci':
            if media_type_prefix == 'application/vnd.oci.image.manifest.v1':
                digest = 'oci-digest'
            else:
                headers = {}
                response_json = {"errors": [{"code": "MANIFEST_UNKNOWN"}]}
                response = requests.Response()
                flexmock(response,
                         status_code=requests.codes.not_found,
                         content=json.dumps(response_json).encode("utf-8"),
                         headers=headers)

                return response
        elif manifest_type == 'oci_index':
            if media_type_prefix == 'application/vnd.oci.image.index.v1':
                digest = 'oci-index-digest'
            else:
                headers = {}
                response_json = {"errors": [{"code": "MANIFEST_UNKNOWN"}]}
                response = requests.Response()
                flexmock(response,
                         status_code=requests.codes.not_found,
                         content=json.dumps(response_json).encode("utf-8"),
                         headers=headers)

                return response

        headers = {}
        if has_content_type_header:
            headers['Content-Type'] = '{}+jsonish'.format(media_type_prefix)
        if has_content_digest:
            headers['Docker-Content-Digest'] = digest

        if media_type_prefix == 'application/vnd.docker.distribution.manifest.v1':
            response_json = {'schemaVersion': 1}
        else:
            response_json = {'schemaVersion': 2,
                             'mediaType': media_type_prefix + '+json'}

        response = requests.Response()
        flexmock(response,
                 status_code=200,
                 content=json.dumps(response_json).encode("utf-8"),
                 headers=headers)

        return response

    (flexmock(requests.Session)
        .should_receive('get')
        .replace_with(custom_get))

    actual_digests = get_manifest_digests(**kwargs)
    if manifest_type == 'v1':
        if has_content_digest:
            assert actual_digests.v1 == 'v1-digest'
        else:
            assert actual_digests.v1 is True
        assert actual_digests.v2 is None
        assert actual_digests.oci is None
        assert actual_digests.oci_index is None
    elif manifest_type == 'v2':
        if can_convert_v2_v1:
            if has_content_digest:
                assert actual_digests.v1 == 'v1-converted-digest'
            else:
                assert actual_digests.v1 is True
        else:
            assert actual_digests.v1 is None
        if has_content_digest:
            assert actual_digests.v2 == 'v2-digest'
        else:
            assert actual_digests.v2 is True
        assert actual_digests.oci is None
        assert actual_digests.oci_index is None
    elif manifest_type == 'oci':
        assert actual_digests.v1 is None
        assert actual_digests.v2 is None
        if has_content_digest:
            assert actual_digests.oci == 'oci-digest'
        else:
            assert actual_digests.oci is True
        assert actual_digests.oci_index is None
    elif manifest_type == 'oci_index':
        assert actual_digests.v1 is None
        assert actual_digests.v2 is None
        assert actual_digests.oci is None
        if has_content_digest:
            assert actual_digests.oci_index == 'oci-index-digest'
        else:
            assert actual_digests.oci_index is True


@responses.activate
def test_get_manifest_digests_connection_error(tmpdir):
    # Test that our code to handle falling back from https to http
    # doesn't do anything unexpected when a connection can't be
    # made at all.
    kwargs = {}
    kwargs['image'] = ImageName.parse('example.com/spam:latest')
    kwargs['registry'] = 'https://example.com'

    url = 'https://example.com/v2/spam/manifests/latest'
    responses.add(responses.GET, url, body=ConnectionError())

    with pytest.raises(ConnectionError):
        get_manifest_digests(**kwargs)


@pytest.mark.parametrize('namespace,repo,explicit,expected', [
    ('foo', 'bar', False, 'foo/bar'),
    ('foo', 'bar', True, 'foo/bar'),
    (None, 'bar', False, 'bar'),
    (None, 'bar', True, 'library/bar'),
])
def test_image_name_get_repo(namespace, repo, explicit, expected):
    image = ImageName(namespace=namespace, repo=repo)
    assert image.get_repo(explicit) == expected


@pytest.mark.parametrize('v1,v2,v2_list,oci,oci_index,default', [
    ('v1-digest', 'v2-digest', None, None, None, 'v2-digest'),
    ('v1-digest', None, None, None, None, 'v1-digest'),
    (None, 'v2-digest', None, None, None, 'v2-digest'),
    (None, 'v2-digest', None, None, None, 'v2-digest'),
    (None, None, None, 'oci-digest', None, 'oci-digest'),
    (None, None, None, None, 'oci-index-digest', 'oci-index-digest'),
    (None, 'v2-digest', None, 'oci-digest', None, 'oci-digest'),
    ('v1-digest', 'v2-digest', 'v2-list-digest', 'oci-digest', 'oci-index-digest',
     'v2-list-digest'),
    (None, 'v2-digest', 'v2-list-digest', 'oci-digest', None, 'v2-list-digest'),
    ('v1-digest', None, 'v2-list-digest', 'oci-digest', None, 'v2-list-digest'),
    ('v1-digest', 'v2-digest', 'v2-list-digest', None, None, 'v2-list-digest'),
    (None, None, None, 'oci-digest', 'oci-index-digest', 'oci-index-digest'),
    (None, None, None, None, None, None),
])
def test_manifest_digest(v1, v2, v2_list, oci, oci_index, default):
    md = ManifestDigest(v1=v1, v2=v2, v2_list=v2_list, oci=oci, oci_index=oci_index)
    assert md.v1 == v1
    assert md.v2 == v2
    assert md.v2_list == v2_list
    assert md.oci == oci
    assert md.default == default
    with pytest.raises(AttributeError):
        assert md.no_such_version


def test_get_manifest_media_version_unknown():
    with pytest.raises(RuntimeError):
        assert get_manifest_media_version(ManifestDigest())


@pytest.mark.parametrize('environ,expected', [
    ({'BUILD': '{"foo": "bar"}'}, {'foo': 'bar'}),
    ({}, False),
])
def test_get_build_json(environ, expected):
    flexmock(os, environ=environ)

    if expected:
        assert get_build_json() == {'foo': 'bar'}
    else:
        with pytest.raises(KeyError):
            get_build_json()


@pytest.mark.parametrize('build_json,scratch', [
    ({'metadata': {'labels': {'scratch': True}}}, True),
    ({'metadata': {'labels': {'scratch': False}}}, False),
    ({'metadata': {'labels': {}}}, False),
    ({'metadata': {}}, None),
    ({}, None),
])
def test_is_scratch_build(build_json, scratch):
    flexmock(util).should_receive('get_build_json').and_return(build_json)
    if scratch is None:
        with pytest.raises(KeyError):
            is_scratch_build()
    else:
        assert is_scratch_build() == scratch


@pytest.mark.parametrize(('build_json', 'isolated'), [
    ({'metadata': {'labels': {'isolated': True}}}, True),
    ({'metadata': {'labels': {'isolated': False}}}, False),
    ({'metadata': {'labels': {}}}, False),
    ({'metadata': {}}, None),
    ({}, None),
])
def test_is_isolated_build(build_json, isolated):
    flexmock(util).should_receive('get_build_json').and_return(build_json)
    if isolated is None:
        with pytest.raises(KeyError):
            is_isolated_build()
    else:
        assert is_isolated_build() == isolated


@pytest.mark.parametrize(('inputs', 'results'), [
    (None, None),
    ([], None),
    ([{'name': 'Bogus', 'args': {'platforms': ['spam']}}], None),
    ([{'name': 'Bogus', 'args': {'platforms': ['spam']}},
      {'name': PLUGIN_BUILD_ORCHESTRATE_KEY, 'args': {'platforms': ['ppce64le', 'arm64']}}],
     ['arm64', 'ppce64le'])
])
def test_get_orchestrator_platforms(inputs, results):
    class MockWorkflow(object):
        def __init__(self, inputs):
            self.buildstep_plugins_conf = inputs

    workflow = MockWorkflow(inputs)
    if results:
        assert sorted(get_orchestrator_platforms(workflow)) == sorted(results)
    else:
        assert get_orchestrator_platforms(workflow) == results


def test_df_parser(tmpdir):
    tmpdir_path = str(tmpdir.realpath())
    df = df_parser(tmpdir_path)
    df.lines = [
        "FROM fedora\n",
        "ENV foo=\"bar\"\n",
        "LABEL label=\"foobar barfoo\"\n"
    ]

    assert len(df.envs) == 1
    assert df.envs.get('foo') == 'bar'
    assert len(df.labels) == 1
    assert df.labels.get('label') == 'foobar barfoo'


def test_df_parser_parent_env_arg(tmpdir):
    p_env = {
        "test_env": "first"
    }
    df_content = dedent("""\
        FROM fedora
        ENV foo=bar
        LABEL label="foobar $test_env"
        """)
    df = df_parser(str(tmpdir), parent_env=p_env)
    df.content = df_content
    assert df.labels.get('label') == 'foobar first'


@pytest.mark.parametrize('env_arg', [
    {"test_env": "first"},
    ['test_env=first'],
    ['test_env='],
    ['test_env=--option=first --option=second'],
    ['test_env_first'],
])
def test_df_parser_parent_env_wf(tmpdir, caplog, env_arg):
    df_content = dedent("""\
        FROM fedora
        ENV foo=bar
        LABEL label="foobar $test_env"
        """)
    env_conf = {INSPECT_CONFIG: {"Env": env_arg}}
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')
    flexmock(workflow, base_image_inspect=env_conf)
    df = df_parser(str(tmpdir), workflow=workflow)
    df.content = df_content

    if isinstance(env_arg, list) and ('=' not in env_arg[0]):
        expected_log_message = "Unable to parse all of Parent Config ENV"
        assert expected_log_message in [l.getMessage() for l in caplog.records()]
    elif isinstance(env_arg, dict):
        assert df.labels.get('label') == ('foobar ' + env_arg['test_env'])
    else:
        assert df.labels.get('label') == 'foobar ' + env_arg[0].split('=', 1)[1]


@pytest.mark.parametrize(('available', 'requested', 'result'), (
    (['spam', 'bacon', 'eggs'], ['spam'], True),
    (['spam', 'bacon', 'eggs'], ['spam', 'bacon'], True),
    (['spam', 'bacon', 'eggs'], ['spam', 'bacon', 'eggs'], True),
    (['spam', 'bacon', 'eggs'], ['spam', 'eggs'], True),
    (['spam', 'bacon', 'eggs'], ['eggs', 'spam'], False),
    (['spam', 'bacon', 'eggs'], ['spam', 'eggs', 'bacon'], False),
    (['spam', 'bacon', 'eggs'], ['sausage'], False),
))
def test_are_plugins_in_order(available, requested, result):
    assert are_plugins_in_order([{'name': plugin} for plugin in available],
                                *requested) == result


@pytest.mark.parametrize(('test_string', 'labels', 'expected'), [
    ('', {}, ''),
    ('', {'version': 'cat'}, ''),
    ('dog', {'version': 'cat'}, 'dog'),
    ('dog', {}, 'dog'),
    ('{version}', {'version': 'cat'}, 'cat'),
    ('dog-{version}', {'version': 'cat'}, 'dog-cat'),
    ('{version}', {}, None),
    ('{Version}', {'version': 'cat'}, None),
])
def test_label_formatter(labels, test_string, expected):
    if expected is not None:
        assert expected == LabelFormatter().vformat(test_string, [], labels)
    else:
        with pytest.raises(KeyError):
            LabelFormatter().vformat(test_string, [], labels)


@pytest.mark.parametrize(('tag_conf', 'tag_annotation', 'expected'), (
    (['spam', 'bacon'], [], ['spam', 'bacon']),
    ([], ['spam', 'bacon'], ['spam', 'bacon']),
    (['spam', 'bacon'], ['ignored', 'scorned'], ['spam', 'bacon']),
))
def test_get_primary_images(tag_conf, tag_annotation, expected):
    template_image = ImageName.parse('registry.example.com/fedora')
    workflow = DockerBuildWorkflow(MOCK_SOURCE, 'test-image')

    for tag in tag_conf:
        image_name = ImageName.parse(str(template_image))
        image_name.tag = tag
        workflow.tag_conf.add_primary_image(str(image_name))

    annotations = {}
    for tag in tag_annotation:
        annotations.setdefault('repositories', {}).setdefault('primary', [])
        image_name = ImageName.parse(str(template_image))
        image_name.tag = tag

        annotations['repositories']['primary'].append(str(image_name))

    build_result = BuildResult(annotations=annotations, image_id='foo')
    workflow.build_result = build_result

    actual = get_primary_images(workflow)
    assert len(actual) == len(expected)
    for index, primary_image in enumerate(actual):
        assert primary_image.registry == template_image.registry
        assert primary_image.namespace == template_image.namespace
        assert primary_image.repo == template_image.repo

        assert primary_image.tag == expected[index]


@pytest.mark.parametrize('retry_times', [0, 1, 2, 3])
@pytest.mark.parametrize('raise_exc', [True, False])
def test_clone_git_repo_retry(tmpdir, retry_times, raise_exc):
    tmpdir_path = str(tmpdir.realpath())
    (flexmock(time)
        .should_receive('sleep')
        .and_return(None))

    if raise_exc:
        (flexmock(subprocess)
            .should_receive('check_output')
            .times(retry_times + 1)
            .and_raise(subprocess.CalledProcessError, 1, "git clone", output="error"))

    else:
        (flexmock(subprocess)
            .should_receive('check_output')
            .once()
            .and_return(True))

        (flexmock(subprocess)
            .should_receive('check_call')
            .once()
            .and_raise(CustomTestException))

    exception = subprocess.CalledProcessError if raise_exc else CustomTestException
    with pytest.raises(exception):
        clone_git_repo(DOCKERFILE_GIT, tmpdir_path, retry_times=retry_times)


@pytest.mark.parametrize(('module', 'should_raise', 'expected'), [
    ('eog', True, None),
    ('eog:f26', False, ModuleSpec('eog', 'f26')),
    ('eog:f26/default', False, ModuleSpec('eog', 'f26', profile='default')),
    ('eog-f26', False, ModuleSpec('eog', 'f26')),
    ('eog:f26:20170629213428', False, ModuleSpec('eog', 'f26', '20170629213428')),
    ('eog:f26:20170629213428/default', False,
     ModuleSpec('eog', 'f26', '20170629213428', profile='default')),
    ('eog-f26-20170629213428', False, ModuleSpec('eog', 'f26', '20170629213428')),
    ('a-b-c-20176291342855', False, ModuleSpec('a-b', 'c', '20176291342855')),
    ('a-b-c-d', False, ModuleSpec('a-b-c', 'd')),
    ('a:b:c:d', True, None),
])
def test_split_module_spec(module, should_raise, expected):
    if should_raise:
        with pytest.raises(RuntimeError):
            split_module_spec(module)
    else:
        assert split_module_spec(module) == expected


@pytest.mark.parametrize(('as_str', 'as_str_no_profile'), [
    ('a:b', 'a:b'),
    ('a:b/p', 'a:b'),
    ('a:b:c', 'a:b:c'),
    ('a:b:c/p', 'a:b:c'),
])
def test_module_spec_to_str(as_str, as_str_no_profile):
    spec = split_module_spec(as_str)
    assert spec.to_str() == as_str
    assert spec.to_str(include_profile=False) == as_str_no_profile


@pytest.mark.parametrize('from_file', [True, False])
@pytest.mark.parametrize('config', [
    ("""\
      version: 1
      clusters:
        ignored:
        - name: foo
          max_concurrent_builds: 2
        platform:
        - name: one
          max_concurrent_builds: 4
        - name: two
          max_concurrent_builds: 8
          enabled: true
        - name: three
          max_concurrent_builds: 16
          enabled: false
    """),
    REACTOR_CONFIG_MAP,
])
def test_read_yaml_file_or_yaml(tmpdir, from_file, config):
    expected = yaml.safe_load(config)

    if from_file:
        config_path = os.path.join(str(tmpdir), 'config.yaml')
        with open(config_path, 'w') as fp:
            fp.write(config)
        output = read_yaml_from_file_path(config_path, 'schemas/config.json')
    else:
        output = read_yaml(config, 'schemas/config.json')

    assert output == expected


LogEntry = namedtuple('LogEntry', ['platform', 'line'])


def test_osbs_logs_get_log_files(tmpdir):
    class OSBS(object):
        def get_orchestrator_build_logs(self, build_id):
            logs = [LogEntry(None, 'orchestrator'),
                    LogEntry('x86_64', 'Hurray for bacon: \u2017'),
                    LogEntry('x86_64', 'line 2')]
            return logs

    metadata = {
        'x86_64.log': {
            'checksum': 'c2487bf0142ea344df8b36990b0186be',
            'checksum_type': 'md5',
            'filename': 'x86_64.log',
            'filesize': 29
        },
        'orchestrator.log': {
            'checksum': 'ac9ed4cc35a9a77264ca3a0fb81be117',
            'checksum_type': 'md5',
            'filename': 'orchestrator.log',
            'filesize': 13
        }
    }

    logger = flexmock()
    flexmock(logger).should_receive('error')
    osbs_logs = OSBSLogs(logger)
    osbs = OSBS()
    output = osbs_logs.get_log_files(osbs, 1)
    for entry in output:
        assert entry[1] == metadata[entry[1]['filename']]


@pytest.mark.parametrize('insecure', [
    True,
    False,
])
@pytest.mark.parametrize('creds', [
    ('user1', 'pass'),
    (None, 'pass'),
    ('user1', None),
    None,
])
@pytest.mark.parametrize('image,registry,path', [
    ('not-used.com/spam:latest', 'localhost.com',
     '/v2/spam/manifests/latest'),

    ('not-used.com/food/spam:latest', 'http://localhost.com',
     '/v2/food/spam/manifests/latest'),

    ('not-used.com/spam', 'https://localhost.com',
     '/v2/spam/manifests/latest'),
])
@responses.activate
def test_get_manifest_list(tmpdir, image, registry, insecure, creds, path):
    kwargs = {}

    image = ImageName.parse(image)
    kwargs['image'] = image

    if creds:
        temp_dir = mkdtemp(dir=str(tmpdir))
        with open(os.path.join(temp_dir, '.dockercfg'), 'w+') as dockerconfig:
            dockerconfig.write(json.dumps({
                registry: {
                    'username': creds[0], 'password': creds[1]
                }
            }))
        kwargs['dockercfg_path'] = temp_dir

    kwargs['registry'] = registry

    if insecure is not None:
        kwargs['insecure'] = insecure

    def request_callback(request, all_headers=True):
        if creds and creds[0] and creds[1]:
            assert request.headers['Authorization']

        media_type = request.headers['Accept']
        if media_type.endswith('list.v2+json'):
            digest = 'v2_list-digest'
        elif media_type.endswith('v2+json'):
            digest = 'v2-digest'
        elif media_type.endswith('v1+json'):
            digest = 'v1-digest'
        else:
            raise ValueError('Unexpected media type {}'.format(media_type))

        media_type_prefix = media_type.split('+')[0]
        if all_headers:
            headers = {
                'Content-Type': '{}+jsonish'.format(media_type_prefix),
            }
            if not media_type.endswith('list.v2+json'):
                headers['Docker-Content-Digest'] = digest
        else:
            headers = {}
        return (200, headers, '')

    if registry.startswith('http'):
        url = registry + path
    else:
        # In the insecure case, we should try the https URL, and when that produces
        # an error, fall back to http
        if insecure:
            https_url = 'https://' + registry + path
            responses.add(responses.GET, https_url, body=ConnectionError())
            url = 'http://' + registry + path
        else:
            url = 'https://' + registry + path
    responses.add_callback(responses.GET, url, callback=request_callback)

    manifest_list = get_manifest_list(**kwargs)
    assert manifest_list


@pytest.mark.parametrize(('valid'), [
    True,
    False
])
@pytest.mark.parametrize(('platforms', 'platform_exclude', 'platform_only', 'result'), [
    (['x86_64', 'ppc64le'], '', 'ppc64le', ['ppc64le']),
    (['x86_64', 'spam', 'bacon', 'toast', 'ppc64le'], ['spam', 'bacon', 'eggs', 'toast'], '',
     ['x86_64', 'ppc64le']),
    (['ppc64le', 'spam', 'bacon', 'toast'], ['spam', 'bacon', 'eggs', 'toast'], 'ppc64le',
     ['ppc64le']),
    (['x86_64', 'bacon', 'toast'], 'toast', ['x86_64', 'ppc64le'], ['x86_64']),
    (['x86_64', 'toast'], 'toast', 'x86_64', ['x86_64']),
    (['x86_64', 'spam', 'bacon', 'toast'], ['spam', 'bacon', 'eggs', 'toast'], ['x86_64',
                                                                                'ppc64le'],
     ['x86_64']),
    (['x86_64', 'ppc64le'], '', '', ['x86_64', 'ppc64le']),
    (['x86_64', 'ppc64le'], 'x86_64', 'x86_64', []),
])
def test_get_platforms_in_limits(tmpdir, platforms, platform_exclude, platform_only, result,
                                 valid, caplog):
    class MockSource(object):
        def __init__(self, build_dir):
            self.build_dir = build_dir

        def get_build_file_path(self):
            return self.build_dir, self.build_dir

    class MockWorkflow(object):
        def __init__(self, build_dir):
            self.source = MockSource(build_dir)

    platforms_dict = {}
    if platform_exclude:
        platforms_dict['platforms'] = {}
        platforms_dict['platforms']['not'] = platform_exclude
        if not isinstance(platform_exclude, list):
            platform_exclude = [platform_exclude]
    if platform_only:
        if 'platforms' not in platforms_dict:
            platforms_dict['platforms'] = {}
        platforms_dict['platforms']['only'] = platform_only
        if not isinstance(platform_only, list):
            platform_only = [platform_only]

    with open(os.path.join(str(tmpdir), 'container.yaml'), 'w') as f:
        f.write(yaml.safe_dump(platforms_dict))
        f.flush()
    if valid and platforms:
        workflow = MockWorkflow(str(tmpdir))
        final_platforms = get_platforms_in_limits(workflow, platforms)
        if platform_exclude and platform_exclude == platform_only:
            assert 'only and not platforms are the same' in caplog.text()
        assert final_platforms == set(result)
    elif valid:
        workflow = MockWorkflow(str(tmpdir))
        final_platforms = get_platforms_in_limits(workflow, platforms)
        assert final_platforms is None
    else:
        workflow = MockWorkflow('bad_dir')
        final_platforms = get_platforms_in_limits(workflow, platforms)
        assert final_platforms == set(platforms)
