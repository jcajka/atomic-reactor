import os

import pytest

from atomic_reactor.source import (Source, GitSource, PathSource, get_source_instance_for)
import atomic_reactor.source
from jsonschema import ValidationError

from tests.constants import DOCKERFILE_GIT, DOCKERFILE_OK_PATH, SOURCE_CONFIG_ERROR_PATH
from tests.util import requires_internet
import flexmock


class TestSource(object):
    def test_creates_tmpdir_if_not_passed(self):
        s = Source('git', 'foo')
        assert os.path.exists(s.tmpdir)


@requires_internet
class TestGitSource(object):
    def test_checks_out_repo(self):
        gs = GitSource('git', DOCKERFILE_GIT)
        assert os.path.exists(os.path.join(gs.path, '.git'))
        assert os.path.basename(gs.path) == 'docker-hello-world'
        assert gs.commit_id is not None
        assert len(gs.commit_id) == 40  # current git hashes are this long

        previous_commit_id = gs.commit_id
        gs.reset('HEAD~2')  # Go back two commits
        assert gs.commit_id is not None
        assert gs.commit_id != previous_commit_id
        assert len(gs.commit_id) == 40  # current git hashes are this long


class TestPathSource(object):
    def test_copies_target_dir(self, tmpdir):
        tmpdir.ensure('foo', 'bar', 'Dockerfile')
        ps = PathSource('path', 'file://' + os.path.join(str(tmpdir), 'foo'))
        path = ps.path
        assert os.path.isfile(os.path.join(path, 'bar', 'Dockerfile'))
        # make sure these are the same even on second access to ps.path/ps.get(),
        #  since second (and any subsequent) access does a bit different thing than the first one
        assert ps.get() == path


class TestGetSourceInstanceFor(object):
    @pytest.mark.parametrize('source, expected', [
        ({'provider': 'git', 'uri': 'foo'}, GitSource),
        ({'provider': 'path', 'uri': 'foo'}, PathSource),
    ])
    def test_recognizes_correct_provider(self, source, expected):
        assert isinstance(get_source_instance_for(source), expected)

    @pytest.mark.parametrize('source, error', [
        ({'provider': 'xxx', 'uri': 'foo'}, 'unknown source provider "xxx"'),
        ({'provider': 'git'}, '"source" must contain "uri" key'),
        ({'uri': 'path'}, '"source" must contain "provider" key'),
        (None, '"source" must be a dict'),
    ])
    def test_errors(self, source, error):
        with pytest.raises(ValueError) as ex:
            get_source_instance_for(source)

        assert str(ex.value) == error

    def test_retrieves_source_config_file(self):
        s = get_source_instance_for({'provider': 'path', 'uri': DOCKERFILE_OK_PATH})
        assert s.config
        assert s.config.image_build_method == 'imagebuilder'

    def test_sourceconfig_bad_build_method(self, monkeypatch):
        s = get_source_instance_for({'provider': 'path', 'uri': DOCKERFILE_OK_PATH})
        flexmock(atomic_reactor.source, CONTAINER_BUILD_METHODS=[])
        with pytest.raises(AssertionError):
            s.config

    def test_broken_source_config_file(self):
        s = get_source_instance_for({'provider': 'path', 'uri': SOURCE_CONFIG_ERROR_PATH})
        with pytest.raises(ValidationError):
            s.config
