import os
import re
import shutil
import sys
import tempfile
import time
import unittest
import urllib2
from random import randint
from textwrap import dedent

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO

try:
    import json
except ImportError:
    import simplejson as json

import nose

from rbtools.postreview import execute, load_config_files
from rbtools.postreview import APIError, GitClient, MercurialClient, \
                               RepositoryInfo, ReviewBoardServer, \
                               SvnRepositoryInfo
import rbtools.postreview


TEMPDIR_SUFFIX = '__' + __name__.replace('.', '_')


def is_exe_in_path(name):
    """Checks whether an executable is in the user's search path.

    This expects a name without any system-specific executable extension.
    It will append the proper extension as necessary. For example,
    use "myapp" and not "myapp.exe".

    This will return True if the app is in the path, or False otherwise.

    Taken from djblets.util.filesystem to avoid an extra dependency
    """

    if sys.platform == 'win32' and not name.endswith('.exe'):
        name += ".exe"

    for dir in os.environ['PATH'].split(os.pathsep):
        if os.path.exists(os.path.join(dir, name)):
            return True

    return False


def _get_tmpdir():
    return tempfile.mkdtemp(TEMPDIR_SUFFIX)


class MockHttpUnitTest(unittest.TestCase):
    deprecated_api = False

    def setUp(self):
        # Save the old http_get and http_post
        rbtools.postreview.options = OptionsStub()

        self.saved_http_get = ReviewBoardServer.http_get
        self.saved_http_post = ReviewBoardServer.http_post

        self.server = ReviewBoardServer('http://localhost:8080/',
                                        RepositoryInfo(), None)
        ReviewBoardServer.http_get = self._http_method
        ReviewBoardServer.http_post = self._http_method

        self.server.deprecated_api = self.deprecated_api
        self.http_response = {}

    def tearDown(self):
        ReviewBoardServer.http_get = self.saved_http_get
        ReviewBoardServer.http_post = self.saved_http_post

    def _http_method(self, path, *args, **kwargs):
        if isinstance(self.http_response, dict):
            http_response = self.http_response[path]
        else:
            http_response = self.http_response

        if isinstance(http_response, Exception):
            raise http_response
        else:
            return http_response


class OptionsStub(object):
    def __init__(self):
        self.debug = True
        self.guess_summary = False
        self.guess_description = False
        self.tracking = None
        self.username = None
        self.password = None
        self.repository_url = None


class GitClientTests(unittest.TestCase):
    TESTSERVER = "http://127.0.0.1:8080"

    def _gitcmd(self, command, env=None, split_lines=False,
                ignore_errors=False, extra_ignore_errors=(),
                translate_newlines=True, git_dir=None):
        if git_dir:
            full_command = ['git', '--git-dir=%s/.git' % git_dir]
        else:
            full_command = ['git']

        full_command.extend(command)

        return execute(full_command, env, split_lines, ignore_errors,
                       extra_ignore_errors, translate_newlines)

    def _git_add_file_commit(self, file, data, msg):
        """Add a file to a git repository with the content of data
        and commit with msg.
        """
        foo = open(file, 'w')
        foo.write(data)
        foo.close()
        self._gitcmd(['add', file])
        self._gitcmd(['commit', '-m', msg])

    def setUp(self):
        if not is_exe_in_path('git'):
            raise nose.SkipTest('git not found in path')

        self.orig_dir = os.getcwd()

        self.git_dir = _get_tmpdir()
        os.chdir(self.git_dir)
        self._gitcmd(['init'], git_dir=self.git_dir)
        foo = open(os.path.join(self.git_dir, 'foo.txt'), 'w')
        foo.write(FOO)
        foo.close()

        self._gitcmd(['add', 'foo.txt'])
        self._gitcmd(['commit', '-m', 'initial commit'])

        self.clone_dir = _get_tmpdir()
        os.rmdir(self.clone_dir)
        self._gitcmd(['clone', self.git_dir, self.clone_dir])
        self.client = GitClient()
        os.chdir(self.orig_dir)

        rbtools.postreview.user_config = {}
        rbtools.postreview.configs = []
        rbtools.postreview.options = OptionsStub()
        rbtools.postreview.options.parent_branch = None

    def tearDown(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.git_dir)
        shutil.rmtree(self.clone_dir)

    def test_get_repository_info_simple(self):
        """Test GitClient get_repository_info, simple case"""
        os.chdir(self.clone_dir)
        ri = self.client.get_repository_info()
        self.assert_(isinstance(ri, RepositoryInfo))
        self.assertEqual(ri.base_path, '')
        self.assertEqual(ri.path.rstrip("/.git"), self.git_dir)
        self.assertTrue(ri.supports_parent_diffs)
        self.assertFalse(ri.supports_changesets)

    def test_scan_for_server_simple(self):
        """Test GitClient scan_for_server, simple case"""
        os.chdir(self.clone_dir)
        ri = self.client.get_repository_info()

        server = self.client.scan_for_server(ri)
        self.assert_(server is None)

    def test_scan_for_server_reviewboardrc(self):
        "Test GitClient scan_for_server, .reviewboardrc case"""
        os.chdir(self.clone_dir)
        rc = open(os.path.join(self.clone_dir, '.reviewboardrc'), 'w')
        rc.write('REVIEWBOARD_URL = "%s"' % self.TESTSERVER)
        rc.close()
        rbtools.postreview.user_config = load_config_files(self.clone_dir)

        ri = self.client.get_repository_info()
        server = self.client.scan_for_server(ri)
        self.assertEqual(server, self.TESTSERVER)

    def test_scan_for_server_property(self):
        """Test GitClient scan_for_server using repo property"""
        os.chdir(self.clone_dir)
        self._gitcmd(['config', 'reviewboard.url', self.TESTSERVER])
        ri = self.client.get_repository_info()

        self.assertEqual(self.client.scan_for_server(ri), self.TESTSERVER)

    def test_diff_simple(self):
        """Test GitClient simple diff case"""
        diff = "diff --git a/foo.txt b/foo.txt\n" \
               "index 634b3e8ff85bada6f928841a9f2c505560840b3a..5e98e9540e1b741b5be24fcb33c40c1c8069c1fb 100644\n" \
               "--- a/foo.txt\n" \
               "+++ b/foo.txt\n" \
               "@@ -6,7 +6,4 @@ multa quoque et bello passus, dum conderet urbem,\n" \
               " inferretque deos Latio, genus unde Latinum,\n" \
               " Albanique patres, atque altae moenia Romae.\n" \
               " Musa, mihi causas memora, quo numine laeso,\n" \
               "-quidve dolens, regina deum tot volvere casus\n" \
               "-insignem pietate virum, tot adire labores\n" \
               "-impulerit. Tantaene animis caelestibus irae?\n" \
               " \n"

        os.chdir(self.clone_dir)
        self.client.get_repository_info()

        self._git_add_file_commit('foo.txt', FOO1, 'delete and modify stuff')

        self.assertEqual(self.client.diff(None), (diff, None))

    def test_diff_simple_multiple(self):
        """Test GitClient simple diff with multiple commits case"""
        diff = "diff --git a/foo.txt b/foo.txt\n" \
               "index 634b3e8ff85bada6f928841a9f2c505560840b3a..63036ed3fcafe870d567a14dd5884f4fed70126c 100644\n" \
               "--- a/foo.txt\n" \
               "+++ b/foo.txt\n" \
               "@@ -1,12 +1,11 @@\n" \
               " ARMA virumque cano, Troiae qui primus ab oris\n" \
               "+ARMA virumque cano, Troiae qui primus ab oris\n" \
               " Italiam, fato profugus, Laviniaque venit\n" \
               " litora, multum ille et terris iactatus et alto\n" \
               " vi superum saevae memorem Iunonis ob iram;\n" \
               "-multa quoque et bello passus, dum conderet urbem,\n" \
               "+dum conderet urbem,\n" \
               " inferretque deos Latio, genus unde Latinum,\n" \
               " Albanique patres, atque altae moenia Romae.\n" \
               "+Albanique patres, atque altae moenia Romae.\n" \
               " Musa, mihi causas memora, quo numine laeso,\n" \
               "-quidve dolens, regina deum tot volvere casus\n" \
               "-insignem pietate virum, tot adire labores\n" \
               "-impulerit. Tantaene animis caelestibus irae?\n" \
               " \n"

        os.chdir(self.clone_dir)
        self.client.get_repository_info()

        self._git_add_file_commit('foo.txt', FOO1, 'commit 1')
        self._git_add_file_commit('foo.txt', FOO2, 'commit 1')
        self._git_add_file_commit('foo.txt', FOO3, 'commit 1')

        self.assertEqual(self.client.diff(None), (diff, None))

    def test_diff_branch_diverge(self):
        """Test GitClient diff with divergent branches"""
        diff1 = "diff --git a/foo.txt b/foo.txt\n" \
                "index 634b3e8ff85bada6f928841a9f2c505560840b3a..e619c1387f5feb91f0ca83194650bfe4f6c2e347 100644\n" \
                "--- a/foo.txt\n" \
                "+++ b/foo.txt\n" \
                "@@ -1,4 +1,6 @@\n" \
                " ARMA virumque cano, Troiae qui primus ab oris\n" \
                "+ARMA virumque cano, Troiae qui primus ab oris\n" \
                "+ARMA virumque cano, Troiae qui primus ab oris\n" \
                " Italiam, fato profugus, Laviniaque venit\n" \
                " litora, multum ille et terris iactatus et alto\n" \
                " vi superum saevae memorem Iunonis ob iram;\n" \
                "@@ -6,7 +8,4 @@ multa quoque et bello passus, dum conderet urbem,\n" \
                " inferretque deos Latio, genus unde Latinum,\n" \
                " Albanique patres, atque altae moenia Romae.\n" \
                " Musa, mihi causas memora, quo numine laeso,\n" \
                "-quidve dolens, regina deum tot volvere casus\n" \
                "-insignem pietate virum, tot adire labores\n" \
                "-impulerit. Tantaene animis caelestibus irae?\n" \
                " \n"

        diff2 = "diff --git a/foo.txt b/foo.txt\n" \
                "index 634b3e8ff85bada6f928841a9f2c505560840b3a..5e98e9540e1b741b5be24fcb33c40c1c8069c1fb 100644\n" \
                "--- a/foo.txt\n" \
                "+++ b/foo.txt\n" \
                "@@ -6,7 +6,4 @@ multa quoque et bello passus, dum conderet urbem,\n" \
                " inferretque deos Latio, genus unde Latinum,\n" \
                " Albanique patres, atque altae moenia Romae.\n" \
                " Musa, mihi causas memora, quo numine laeso,\n" \
                "-quidve dolens, regina deum tot volvere casus\n" \
                "-insignem pietate virum, tot adire labores\n" \
                "-impulerit. Tantaene animis caelestibus irae?\n" \
                " \n"

        os.chdir(self.clone_dir)

        self._git_add_file_commit('foo.txt', FOO1, 'commit 1')

        self._gitcmd(['checkout', '-b', 'mybranch', '--track', 'origin/master'])
        self._git_add_file_commit('foo.txt', FOO2, 'commit 2')

        self.client.get_repository_info()
        self.assertEqual(self.client.diff(None), (diff1, None))

        self._gitcmd(['checkout', 'master'])
        self.client.get_repository_info()
        self.assertEqual(self.client.diff(None), (diff2, None))

    def test_diff_tracking_no_origin(self):
        """Test GitClient diff with a tracking branch, but no origin remote"""
        diff = "diff --git a/foo.txt b/foo.txt\n" \
               "index 634b3e8ff85bada6f928841a9f2c505560840b3a..5e98e9540e1b741b5be24fcb33c40c1c8069c1fb 100644\n" \
               "--- a/foo.txt\n" \
               "+++ b/foo.txt\n" \
               "@@ -6,7 +6,4 @@ multa quoque et bello passus, dum conderet urbem,\n" \
               " inferretque deos Latio, genus unde Latinum,\n" \
               " Albanique patres, atque altae moenia Romae.\n" \
               " Musa, mihi causas memora, quo numine laeso,\n" \
               "-quidve dolens, regina deum tot volvere casus\n" \
               "-insignem pietate virum, tot adire labores\n" \
               "-impulerit. Tantaene animis caelestibus irae?\n" \
               " \n"

        os.chdir(self.clone_dir)

        self._gitcmd(['remote', 'add', 'quux', self.git_dir])
        self._gitcmd(['fetch', 'quux'])
        self._gitcmd(['checkout', '-b', 'mybranch', '--track', 'quux/master'])
        self._git_add_file_commit('foo.txt', FOO1, 'delete and modify stuff')

        self.client.get_repository_info()

        self.assertEqual(self.client.diff(None), (diff, None))

    def test_diff_local_tracking(self):
        """Test GitClient diff with a local tracking branch"""
        diff = "diff --git a/foo.txt b/foo.txt\n" \
               "index 634b3e8ff85bada6f928841a9f2c505560840b3a..e619c1387f5feb91f0ca83194650bfe4f6c2e347 100644\n" \
               "--- a/foo.txt\n" \
               "+++ b/foo.txt\n" \
               "@@ -1,4 +1,6 @@\n" \
               " ARMA virumque cano, Troiae qui primus ab oris\n" \
               "+ARMA virumque cano, Troiae qui primus ab oris\n" \
               "+ARMA virumque cano, Troiae qui primus ab oris\n" \
               " Italiam, fato profugus, Laviniaque venit\n" \
               " litora, multum ille et terris iactatus et alto\n" \
               " vi superum saevae memorem Iunonis ob iram;\n" \
               "@@ -6,7 +8,4 @@ multa quoque et bello passus, dum conderet urbem,\n" \
               " inferretque deos Latio, genus unde Latinum,\n" \
               " Albanique patres, atque altae moenia Romae.\n" \
               " Musa, mihi causas memora, quo numine laeso,\n" \
               "-quidve dolens, regina deum tot volvere casus\n" \
               "-insignem pietate virum, tot adire labores\n" \
               "-impulerit. Tantaene animis caelestibus irae?\n" \
               " \n"

        os.chdir(self.clone_dir)

        self._git_add_file_commit('foo.txt', FOO1, 'commit 1')

        self._gitcmd(['checkout', '-b', 'mybranch', '--track', 'master'])
        self._git_add_file_commit('foo.txt', FOO2, 'commit 2')

        self.client.get_repository_info()
        self.assertEqual(self.client.diff(None), (diff, None))

    def test_diff_tracking_override(self):
        """Test GitClient diff with option override for tracking branch"""
        diff = "diff --git a/foo.txt b/foo.txt\n" \
               "index 634b3e8ff85bada6f928841a9f2c505560840b3a..5e98e9540e1b741b5be24fcb33c40c1c8069c1fb 100644\n" \
               "--- a/foo.txt\n" \
               "+++ b/foo.txt\n" \
               "@@ -6,7 +6,4 @@ multa quoque et bello passus, dum conderet urbem,\n" \
               " inferretque deos Latio, genus unde Latinum,\n" \
               " Albanique patres, atque altae moenia Romae.\n" \
               " Musa, mihi causas memora, quo numine laeso,\n" \
               "-quidve dolens, regina deum tot volvere casus\n" \
               "-insignem pietate virum, tot adire labores\n" \
               "-impulerit. Tantaene animis caelestibus irae?\n" \
               " \n"

        os.chdir(self.clone_dir)
        rbtools.postreview.options.tracking = 'origin/master'

        self._gitcmd(['remote', 'add', 'bad', self.git_dir])
        self._gitcmd(['fetch', 'bad'])
        self._gitcmd(['checkout', '-b', 'mybranch', '--track', 'bad/master'])

        self._git_add_file_commit('foo.txt', FOO1, 'commit 1')

        self.client.get_repository_info()
        self.assertEqual(self.client.diff(None), (diff, None))

    def test_diff_slash_tracking(self):
        """Test GitClient diff with tracking branch that has slash in its name"""
        diff = "diff --git a/foo.txt b/foo.txt\n" \
               "index 5e98e9540e1b741b5be24fcb33c40c1c8069c1fb..e619c1387f5feb91f0ca83194650bfe4f6c2e347 100644\n" \
               "--- a/foo.txt\n" \
               "+++ b/foo.txt\n" \
               "@@ -1,4 +1,6 @@\n" \
               " ARMA virumque cano, Troiae qui primus ab oris\n" \
               "+ARMA virumque cano, Troiae qui primus ab oris\n" \
               "+ARMA virumque cano, Troiae qui primus ab oris\n" \
               " Italiam, fato profugus, Laviniaque venit\n" \
               " litora, multum ille et terris iactatus et alto\n" \
               " vi superum saevae memorem Iunonis ob iram;\n"

        os.chdir(self.git_dir)
        self._gitcmd(['checkout', '-b', 'not-master'])
        self._git_add_file_commit('foo.txt', FOO1, 'commit 1')

        os.chdir(self.clone_dir)
        self._gitcmd(['fetch', 'origin'])
        self._gitcmd(['checkout', '-b', 'my/branch', '--track', 'origin/not-master'])
        self._git_add_file_commit('foo.txt', FOO2, 'commit 2')

        self.client.get_repository_info()
        self.assertEqual(self.client.diff(None), (diff, None))


class MercurialTestBase(unittest.TestCase):

    def setUp(self):
        self._hg_env = {}

    def _hgcmd(self, command, split_lines=False,
                ignore_errors=False, extra_ignore_errors=(),
                translate_newlines=True, hg_dir=None):
        if hg_dir:
            full_command = ['hg', '--cwd', hg_dir]
        else:
            full_command = ['hg']

        # We're *not* doing `env = env or {}` here because
        # we want the caller to be able to *enable* reading
        # of user and system-level hgrc configuration.
        env = self._hg_env.copy()

        if not env:
            env = {
                'HGRCPATH': os.devnull,
                'HGPLAIN': '1',
            }

        full_command.extend(command)

        return execute(full_command, env, split_lines, ignore_errors,
                       extra_ignore_errors, translate_newlines)

    def _hg_add_file_commit(self, filename, data, msg):
        outfile = open(filename, 'w')
        outfile.write(data)
        outfile.close()
        self._hgcmd(['add', filename])
        self._hgcmd(['commit', '-m', msg])


class MercurialClientTests(MercurialTestBase):
    TESTSERVER = 'http://127.0.0.1:8080'
    CLONE_HGRC = dedent("""
    [paths]
    default = %(hg_dir)s
    cloned = %(clone_dir)s

    [reviewboard]
    url = %(test_server)s

    [diff]
    git = true
    """).rstrip()

    def setUp(self):
        MercurialTestBase.setUp(self)
        if not is_exe_in_path('hg'):
            raise nose.SkipTest('hg not found in path')

        self.orig_dir = os.getcwd()

        self.hg_dir = _get_tmpdir()
        os.chdir(self.hg_dir)
        self._hgcmd(['init'], hg_dir=self.hg_dir)
        foo = open(os.path.join(self.hg_dir, 'foo.txt'), 'w')
        foo.write(FOO)
        foo.close()

        self._hgcmd(['add', 'foo.txt'])
        self._hgcmd(['commit', '-m', 'initial commit'])

        self.clone_dir = _get_tmpdir()
        os.rmdir(self.clone_dir)
        self._hgcmd(['clone', self.hg_dir, self.clone_dir])
        os.chdir(self.clone_dir)
        self.client = MercurialClient()

        clone_hgrc = open(self.clone_hgrc_path, 'wb')
        clone_hgrc.write(self.CLONE_HGRC % {
            'hg_dir': self.hg_dir,
            'clone_dir': self.clone_dir,
            'test_server': self.TESTSERVER,
        })
        clone_hgrc.close()

        self.client.get_repository_info()
        rbtools.postreview.user_config = {}
        rbtools.postreview.options = OptionsStub()
        rbtools.postreview.options.parent_branch = None
        os.chdir(self.clone_dir)

    @property
    def clone_hgrc_path(self):
        return os.path.join(self.clone_dir, '.hg', 'hgrc')

    @property
    def hgrc_path(self):
        return os.path.join(self.hg_dir, '.hg', 'hgrc')

    def tearDown(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.hg_dir)
        shutil.rmtree(self.clone_dir)

    def testGetRepositoryInfoSimple(self):
        """Test MercurialClient get_repository_info, simple case"""
        ri = self.client.get_repository_info()

        self.assertTrue(isinstance(ri, RepositoryInfo))
        self.assertEqual('', ri.base_path)

        hgpath = ri.path

        if os.path.basename(hgpath) == '.hg':
            hgpath = os.path.dirname(hgpath)

        self.assertEqual(self.hg_dir, hgpath)
        self.assertTrue(ri.supports_parent_diffs)
        self.assertFalse(ri.supports_changesets)

    def testScanForServerSimple(self):
        """Test MercurialClient scan_for_server, simple case"""
        os.rename(self.clone_hgrc_path,
            os.path.join(self.clone_dir, '._disabled_hgrc'))

        self.client.hgrc = {}
        self.client._load_hgrc()
        ri = self.client.get_repository_info()

        server = self.client.scan_for_server(ri)
        self.assertTrue(server is None)

    def testScanForServerWhenPresentInHgrc(self):
        """Test MercurialClient scan_for_server when present in hgrc"""
        ri = self.client.get_repository_info()

        server = self.client.scan_for_server(ri)
        self.assertEqual(self.TESTSERVER, server)

    def testScanForServerReviewboardrc(self):
        """Test MercurialClient scan_for_server when in .reviewboardrc"""
        rc = open(os.path.join(self.clone_dir, '.reviewboardrc'), 'w')
        rc.write('REVIEWBOARD_URL = "%s"' % self.TESTSERVER)
        rc.close()

        ri = self.client.get_repository_info()
        server = self.client.scan_for_server(ri)
        self.assertEqual(self.TESTSERVER, server)

    def testDiffSimple(self):
        """Test MercurialClient diff, simple case"""
        self.client.get_repository_info()

        self._hg_add_file_commit('foo.txt', FOO1, 'delete and modify stuff')

        diff_result = self.client.diff(None)
        self.assertEqual((EXPECTED_HG_DIFF_0, None), diff_result)

    def testDiffSimpleMultiple(self):
        """Test MercurialClient diff with multiple commits"""
        self.client.get_repository_info()

        self._hg_add_file_commit('foo.txt', FOO1, 'commit 1')
        self._hg_add_file_commit('foo.txt', FOO2, 'commit 2')
        self._hg_add_file_commit('foo.txt', FOO3, 'commit 3')

        diff_result = self.client.diff(None)

        self.assertEqual((EXPECTED_HG_DIFF_1, None), diff_result)

    def testDiffBranchDiverge(self):
        """Test MercurialClient diff with diverged branch"""
        self._hg_add_file_commit('foo.txt', FOO1, 'commit 1')

        self._hgcmd(['branch', 'diverged'])
        self._hg_add_file_commit('foo.txt', FOO2, 'commit 2')
        self.client.get_repository_info()

        self.assertEqual((EXPECTED_HG_DIFF_2, None), self.client.diff(None))

        self._hgcmd(['update', '-C', 'default'])
        self.client.get_repository_info()

        self.assertEqual((EXPECTED_HG_DIFF_3, None), self.client.diff(None))


class MercurialSubversionClientTests(MercurialTestBase):
    TESTSERVER = "http://127.0.0.1:8080"

    def __init__(self, *args, **kwargs):
        self._tmpbase = ''
        self.clone_dir = ''
        self.svn_repo = ''
        self.svn_checkout = ''
        self.client = None
        self._svnserve_pid = 0
        self._max_svnserve_pid_tries = 12
        self._svnserve_port = os.environ.get('SVNSERVE_PORT')
        self._required_exes = ('svnadmin', 'svnserve', 'svn')
        MercurialTestBase.__init__(self, *args, **kwargs)

    def setUp(self):
        MercurialTestBase.setUp(self)
        self._hg_env = {'FOO': 'BAR'}

        for exe in self._required_exes:
            if not is_exe_in_path(exe):
                raise nose.SkipTest('missing svn stuff!  giving up!')

        if not self._has_hgsubversion():
            raise nose.SkipTest('unable to use `hgsubversion` extension!  '
                                'giving up!')

        if not self._tmpbase:
            self._tmpbase = _get_tmpdir()

        self._create_svn_repo()
        self._fire_up_svnserve()
        self._fill_in_svn_repo()

        try:
            self._get_testing_clone()
        except (OSError, IOError):
            msg = 'could not clone from svn repo!  skipping...'
            raise nose.SkipTest(msg), None, sys.exc_info()[2]

        self._spin_up_client()
        self._stub_in_config_and_options()
        os.chdir(self.clone_dir)

    def _has_hgsubversion(self):
        output = self._hgcmd(['svn', '--help'],
                             ignore_errors=True, extra_ignore_errors=(255))

        return not re.search("unknown command ['\"]svn['\"]", output, re.I)

    def tearDown(self):
        shutil.rmtree(self.clone_dir)
        os.kill(self._svnserve_pid, 9)

        if self._tmpbase:
            shutil.rmtree(self._tmpbase)

    def _svn_add_file_commit(self, filename, data, msg):
        outfile = open(filename, 'w')
        outfile.write(data)
        outfile.close()
        execute(['svn', 'add', filename])
        execute(['svn', 'commit', '-m', msg])

    def _create_svn_repo(self):
        self.svn_repo = os.path.join(self._tmpbase, 'svnrepo')
        execute(['svnadmin', 'create', self.svn_repo])

    def _fire_up_svnserve(self):
        if not self._svnserve_port:
            self._svnserve_port = str(randint(30000, 40000))

        pid_file = os.path.join(self._tmpbase, 'svnserve.pid')
        execute(['svnserve', '--pid-file', pid_file, '-d',
                 '--listen-port', self._svnserve_port, '-r', self._tmpbase])

        for i in range(0, self._max_svnserve_pid_tries):
            try:
                self._svnserve_pid = int(open(pid_file).read().strip())
                return

            except (IOError, OSError):
                time.sleep(0.25)

        # This will re-raise the last exception, which will be either
        # IOError or OSError if the above fails and this branch is reached
        raise

    def _fill_in_svn_repo(self):
        self.svn_checkout = os.path.join(self._tmpbase, 'checkout.svn')
        execute(['svn', 'checkout', 'file://%s' % self.svn_repo,
                 self.svn_checkout])
        os.chdir(self.svn_checkout)

        for subtree in ('trunk', 'branches', 'tags'):
            execute(['svn', 'mkdir', subtree])

        execute(['svn', 'commit', '-m', 'filling in T/b/t'])
        os.chdir(os.path.join(self.svn_checkout, 'trunk'))

        for i, data in enumerate([FOO, FOO1, FOO2]):
            self._svn_add_file_commit('foo.txt', data, 'foo commit %s' % i)

    def _get_testing_clone(self):
        self.clone_dir = os.path.join(self._tmpbase, 'checkout.hg')
        self._hgcmd([
            'clone', 'svn://127.0.0.1:%s/svnrepo' % self._svnserve_port,
            self.clone_dir,
        ])

    def _spin_up_client(self):
        os.chdir(self.clone_dir)
        self.client = MercurialClient()

    def _stub_in_config_and_options(self):
        rbtools.postreview.user_config = {}
        rbtools.postreview.options = OptionsStub()
        rbtools.postreview.options.parent_branch = None

    def testGetRepositoryInfoSimple(self):
        """Test MercurialClient (+svn) get_repository_info, simple case"""
        ri = self.client.get_repository_info()

        self.assertEqual('svn', self.client._type)
        self.assertEqual('/trunk', ri.base_path)
        self.assertEqual('svn://127.0.0.1:%s/svnrepo' % self._svnserve_port,
                        ri.path)

    def testScanForServerSimple(self):
        """Test MercurialClient (+svn) scan_for_server, simple case"""
        ri = self.client.get_repository_info()
        server = self.client.scan_for_server(ri)

        self.assertTrue(server is None)

    def testScanForServerReviewboardrc(self):
        """Test MercurialClient (+svn) scan_for_server in .reviewboardrc"""
        rc_filename = os.path.join(self.clone_dir, '.reviewboardrc')
        rc = open(rc_filename, 'w')
        rc.write('REVIEWBOARD_URL = "%s"' % self.TESTSERVER)
        rc.close()

        ri = self.client.get_repository_info()
        server = self.client.scan_for_server(ri)

        self.assertEqual(self.TESTSERVER, server)

    def testScanForServerProperty(self):
        """Test MercurialClient (+svn) scan_for_server in svn property"""
        os.chdir(self.svn_checkout)
        execute(['svn', 'update'])
        execute(['svn', 'propset', 'reviewboard:url', self.TESTSERVER,
                 self.svn_checkout])
        execute(['svn', 'commit', '-m', 'adding reviewboard:url property'])

        os.chdir(self.clone_dir)
        self._hgcmd(['pull'])
        self._hgcmd(['update', '-C'])

        ri = self.client.get_repository_info()

        self.assertEqual(self.TESTSERVER, self.client.scan_for_server(ri))

    def testDiffSimple(self):
        """Test MercurialClient (+svn) diff, simple case"""
        self.client.get_repository_info()

        self._hg_add_file_commit('foo.txt', FOO4, 'edit 4')

        self.assertEqual(EXPECTED_HG_SVN_DIFF_0, self.client.diff(None)[0])

    def testDiffSimpleMultiple(self):
        """Test MercurialClient (+svn) diff with multiple commits"""
        self.client.get_repository_info()

        self._hg_add_file_commit('foo.txt', FOO4, 'edit 4')
        self._hg_add_file_commit('foo.txt', FOO5, 'edit 5')
        self._hg_add_file_commit('foo.txt', FOO6, 'edit 6')

        self.assertEqual(EXPECTED_HG_SVN_DIFF_1, self.client.diff(None)[0])


class SVNClientTests(unittest.TestCase):
    def test_relative_paths(self):
        """Testing SvnRepositoryInfo._get_relative_path"""
        info = SvnRepositoryInfo('http://svn.example.com/svn/', '/', '')
        self.assertEqual(info._get_relative_path('/foo', '/bar'), None)
        self.assertEqual(info._get_relative_path('/', '/trunk/myproject'),
                         None)
        self.assertEqual(info._get_relative_path('/trunk/myproject', '/'),
                         '/trunk/myproject')
        self.assertEqual(
            info._get_relative_path('/trunk/myproject', ''),
            '/trunk/myproject')
        self.assertEqual(
            info._get_relative_path('/trunk/myproject', '/trunk'),
            '/myproject')
        self.assertEqual(
            info._get_relative_path('/trunk/myproject', '/trunk/myproject'),
            '/')


class ApiTests(MockHttpUnitTest):
    def setUp(self):
        super(ApiTests, self).setUp()

        self.http_response = {
            'api/': json.dumps({
                'stat': 'ok',
                'links': {
                    'info': {
                        'href': 'api/info/',
                        'method': 'GET',
                    },
                },
            }),
        }

    def test_check_api_version_1_5_2_higher(self):
        """Testing checking the API version compatibility (RB >= 1.5.2)"""
        self.http_response.update(self._build_info_resource('1.5.2'))
        self.server.check_api_version()
        self.assertFalse(self.server.deprecated_api)

        self.http_response.update(self._build_info_resource('1.5.3alpha0'))
        self.server.check_api_version()
        self.assertFalse(self.server.deprecated_api)

    def test_check_api_version_1_5_1_lower(self):
        """Testing checking the API version compatibility (RB < 1.5.2)"""
        self.http_response.update(self._build_info_resource('1.5.1'))
        self.server.check_api_version()
        self.assertTrue(self.server.deprecated_api)

    def test_check_api_version_old_api(self):
        """Testing checking the API version compatibility (RB < 1.5.0)"""
        self.http_response = {
            'api/': APIError(404, 0),
        }

        self.server.check_api_version()
        self.assertTrue(self.server.deprecated_api)

    def _build_info_resource(self, package_version):
        return {
            'api/info/': json.dumps({
                'stat': 'ok',
                'info': {
                    'product': {
                        'package_version': package_version,
                    },
                },
            }),
        }


class DeprecatedApiTests(MockHttpUnitTest):
    deprecated_api = True

    SAMPLE_ERROR_STR = json.dumps({
        'stat': 'fail',
        'err': {
            'code': 100,
            'msg': 'This is a test failure',
        }
    })

    def test_parse_get_error_http_200(self):
        self.http_response = self.SAMPLE_ERROR_STR

        try:
            self.server.api_get('/foo/')

            # Shouldn't be reached
            self._assert(False)
        except APIError, e:
            self.assertEqual(e.http_status, 200)
            self.assertEqual(e.error_code, 100)
            self.assertEqual(e.rsp['stat'], 'fail')
            self.assertEqual(str(e),
                             'This is a test failure (HTTP 200, API Error 100)')

    def test_parse_post_error_http_200(self):
        self.http_response = self.SAMPLE_ERROR_STR

        try:
            self.server.api_post('/foo/')

            # Shouldn't be reached
            self._assert(False)
        except APIError, e:
            self.assertEqual(e.http_status, 200)
            self.assertEqual(e.error_code, 100)
            self.assertEqual(e.rsp['stat'], 'fail')
            self.assertEqual(str(e),
                             'This is a test failure (HTTP 200, API Error 100)')

    def test_parse_get_error_http_400(self):
        self.http_response = self._make_http_error('/foo/', 400,
                                                   self.SAMPLE_ERROR_STR)

        try:
            self.server.api_get('/foo/')

            # Shouldn't be reached
            self._assert(False)
        except APIError, e:
            self.assertEqual(e.http_status, 400)
            self.assertEqual(e.error_code, 100)
            self.assertEqual(e.rsp['stat'], 'fail')
            self.assertEqual(str(e),
                             'This is a test failure (HTTP 400, API Error 100)')

    def test_parse_post_error_http_400(self):
        self.http_response = self._make_http_error('/foo/', 400,
                                                   self.SAMPLE_ERROR_STR)

        try:
            self.server.api_post('/foo/')

            # Shouldn't be reached
            self._assert(False)
        except APIError, e:
            self.assertEqual(e.http_status, 400)
            self.assertEqual(e.error_code, 100)
            self.assertEqual(e.rsp['stat'], 'fail')
            self.assertEqual(str(e),
                             'This is a test failure (HTTP 400, API Error 100)')

    def _make_http_error(self, url, code, body):
        return urllib2.HTTPError(url, code, body, {}, StringIO(body))


FOO = """\
ARMA virumque cano, Troiae qui primus ab oris
Italiam, fato profugus, Laviniaque venit
litora, multum ille et terris iactatus et alto
vi superum saevae memorem Iunonis ob iram;
multa quoque et bello passus, dum conderet urbem,
inferretque deos Latio, genus unde Latinum,
Albanique patres, atque altae moenia Romae.
Musa, mihi causas memora, quo numine laeso,
quidve dolens, regina deum tot volvere casus
insignem pietate virum, tot adire labores
impulerit. Tantaene animis caelestibus irae?

"""

FOO1 = """\
ARMA virumque cano, Troiae qui primus ab oris
Italiam, fato profugus, Laviniaque venit
litora, multum ille et terris iactatus et alto
vi superum saevae memorem Iunonis ob iram;
multa quoque et bello passus, dum conderet urbem,
inferretque deos Latio, genus unde Latinum,
Albanique patres, atque altae moenia Romae.
Musa, mihi causas memora, quo numine laeso,

"""

FOO2 = """\
ARMA virumque cano, Troiae qui primus ab oris
ARMA virumque cano, Troiae qui primus ab oris
ARMA virumque cano, Troiae qui primus ab oris
Italiam, fato profugus, Laviniaque venit
litora, multum ille et terris iactatus et alto
vi superum saevae memorem Iunonis ob iram;
multa quoque et bello passus, dum conderet urbem,
inferretque deos Latio, genus unde Latinum,
Albanique patres, atque altae moenia Romae.
Musa, mihi causas memora, quo numine laeso,

"""

FOO3 = """\
ARMA virumque cano, Troiae qui primus ab oris
ARMA virumque cano, Troiae qui primus ab oris
Italiam, fato profugus, Laviniaque venit
litora, multum ille et terris iactatus et alto
vi superum saevae memorem Iunonis ob iram;
dum conderet urbem,
inferretque deos Latio, genus unde Latinum,
Albanique patres, atque altae moenia Romae.
Albanique patres, atque altae moenia Romae.
Musa, mihi causas memora, quo numine laeso,

"""

FOO4 = """\
Italiam, fato profugus, Laviniaque venit
litora, multum ille et terris iactatus et alto
vi superum saevae memorem Iunonis ob iram;
dum conderet urbem,





inferretque deos Latio, genus unde Latinum,
Albanique patres, atque altae moenia Romae.
Musa, mihi causas memora, quo numine laeso,

"""

FOO5 = """\
litora, multum ille et terris iactatus et alto
Italiam, fato profugus, Laviniaque venit
vi superum saevae memorem Iunonis ob iram;
dum conderet urbem,
Albanique patres, atque altae moenia Romae.
Albanique patres, atque altae moenia Romae.
Musa, mihi causas memora, quo numine laeso,
inferretque deos Latio, genus unde Latinum,

ARMA virumque cano, Troiae qui primus ab oris
ARMA virumque cano, Troiae qui primus ab oris
"""

FOO6 = """\
ARMA virumque cano, Troiae qui primus ab oris
ARMA virumque cano, Troiae qui primus ab oris
Italiam, fato profugus, Laviniaque venit
litora, multum ille et terris iactatus et alto
vi superum saevae memorem Iunonis ob iram;
dum conderet urbem, inferretque deos Latio, genus
unde Latinum, Albanique patres, atque altae
moenia Romae. Albanique patres, atque altae
moenia Romae. Musa, mihi causas memora, quo numine laeso,

"""

EXPECTED_HG_DIFF_0 = """\
diff --git a/foo.txt b/foo.txt
--- a/foo.txt
+++ b/foo.txt
@@ -6,7 +6,4 @@
 inferretque deos Latio, genus unde Latinum,
 Albanique patres, atque altae moenia Romae.
 Musa, mihi causas memora, quo numine laeso,
-quidve dolens, regina deum tot volvere casus
-insignem pietate virum, tot adire labores
-impulerit. Tantaene animis caelestibus irae?
 
"""

EXPECTED_HG_DIFF_1 = """\
diff --git a/foo.txt b/foo.txt
--- a/foo.txt
+++ b/foo.txt
@@ -1,12 +1,11 @@
+ARMA virumque cano, Troiae qui primus ab oris
 ARMA virumque cano, Troiae qui primus ab oris
 Italiam, fato profugus, Laviniaque venit
 litora, multum ille et terris iactatus et alto
 vi superum saevae memorem Iunonis ob iram;
-multa quoque et bello passus, dum conderet urbem,
+dum conderet urbem,
 inferretque deos Latio, genus unde Latinum,
 Albanique patres, atque altae moenia Romae.
+Albanique patres, atque altae moenia Romae.
 Musa, mihi causas memora, quo numine laeso,
-quidve dolens, regina deum tot volvere casus
-insignem pietate virum, tot adire labores
-impulerit. Tantaene animis caelestibus irae?
 
"""

EXPECTED_HG_DIFF_2 = """\
diff --git a/foo.txt b/foo.txt
--- a/foo.txt
+++ b/foo.txt
@@ -1,3 +1,5 @@
+ARMA virumque cano, Troiae qui primus ab oris
+ARMA virumque cano, Troiae qui primus ab oris
 ARMA virumque cano, Troiae qui primus ab oris
 Italiam, fato profugus, Laviniaque venit
 litora, multum ille et terris iactatus et alto
"""

EXPECTED_HG_DIFF_3 = """\
diff --git a/foo.txt b/foo.txt
--- a/foo.txt
+++ b/foo.txt
@@ -6,7 +6,4 @@
 inferretque deos Latio, genus unde Latinum,
 Albanique patres, atque altae moenia Romae.
 Musa, mihi causas memora, quo numine laeso,
-quidve dolens, regina deum tot volvere casus
-insignem pietate virum, tot adire labores
-impulerit. Tantaene animis caelestibus irae?
 
"""

EXPECTED_HG_SVN_DIFF_0 = """\
Index: foo.txt
===================================================================
--- foo.txt\t(revision 4)
+++ foo.txt\t(working copy)
@@ -1,4 +1,1 @@
-ARMA virumque cano, Troiae qui primus ab oris
-ARMA virumque cano, Troiae qui primus ab oris
-ARMA virumque cano, Troiae qui primus ab oris
 Italiam, fato profugus, Laviniaque venit
@@ -6,3 +3,8 @@
 vi superum saevae memorem Iunonis ob iram;
-multa quoque et bello passus, dum conderet urbem,
+dum conderet urbem,
+
+
+
+
+
 inferretque deos Latio, genus unde Latinum,
"""

EXPECTED_HG_SVN_DIFF_1 = """\
Index: foo.txt
===================================================================
--- foo.txt\t(revision 4)
+++ foo.txt\t(working copy)
@@ -1,2 +1,1 @@
-ARMA virumque cano, Troiae qui primus ab oris
 ARMA virumque cano, Troiae qui primus ab oris
@@ -6,6 +5,6 @@
 vi superum saevae memorem Iunonis ob iram;
-multa quoque et bello passus, dum conderet urbem,
-inferretque deos Latio, genus unde Latinum,
-Albanique patres, atque altae moenia Romae.
-Musa, mihi causas memora, quo numine laeso,
+dum conderet urbem, inferretque deos Latio, genus
+unde Latinum, Albanique patres, atque altae
+moenia Romae. Albanique patres, atque altae
+moenia Romae. Musa, mihi causas memora, quo numine laeso,
 
"""
