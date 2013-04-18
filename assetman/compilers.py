from __future__ import absolute_import, with_statement

import base64
from collections import defaultdict
import hashlib
import logging
import mimetypes
import subprocess
import functools
import os
import re
import sys

# Find our project root, assuming this file lives in ./scripts/. We add that
# root dir to sys.path and use it as our working directory.
project_root = os.path.realpath(
    os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
os.chdir(project_root)

import assetman
from assetman.tools import make_static_path, get_static_pattern, make_output_path

def run_proc(cmd, stdin=None):
    """Runs the given cmd as a subprocess. If the exit code is non-zero, calls
    sys.exit with the exit code (aborting program). If stdin is given, it will
    be piped to the subprocess's stdin.

    The cmd should be a command suitable for passing to subprocess.call (ie, a
    list, usually).
    """
    popen_args = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if stdin is not None:
        popen_args['stdin'] = subprocess.PIPE
    proc = subprocess.Popen(cmd, **popen_args)
    out, err = proc.communicate(input=stdin)
    if proc.returncode != 0:
        raise CompileError(cmd, err)
    elif err:
        logging.warn('%s stderr:\n%s', cmd[0], err)
    return out

class CompileError(Exception):
    """Error encountered while compiling assets."""

class ParseError(Exception):
    """Error encountered while parsing templates."""

class DependencyError(Exception):
    """Invalid or missing dependency."""

class AssetCompiler(object):
    """A base class meant to be mixed in with `assetman.AssetManager`
    subclasses to provide support for compiling asset blocks.
    """

    # What do this compiler's {% assetman.include_* %} expressions look like?
    include_expr = None

    def __init__(self, *args, **kwargs): 
        super(AssetCompiler, self).__init__(*args, **kwargs)

    def compile(self, manifest, **kwargs):
        """Compiles the assets in this Assetman block. Returns compiled source
        code as a string. The given manifest is used to version static paths
        in the compiled source code.
        """
        logging.info('Compiling %s', self)
        return self.do_compile(**kwargs)

    def do_compile(self, **kwargs):
        raise NotImplementedError

    def needs_compile(self, cached_manifest, current_manifest):
        """Determines whether or not this asset block needs compilation by
        comparing the versions in the given manifests and checking the version
        on disk.
        """
        name_hash = self.get_hash()
        assert name_hash in current_manifest['blocks'], self
        content_hash = current_manifest['blocks'][name_hash]['version']
        if name_hash in cached_manifest['blocks']:
            if cached_manifest['blocks'][name_hash]['version'] == content_hash:
                compiled_path = self.get_compiled_path()
                if not os.path.exists(compiled_path):
                    logging.warn('Missing compiled asset %s from %s',
                                 compiled_path, self)
                    return True
                return False
            else:
                logging.warn('Contents of %s changed', self)
        else:
            logging.warn('New/unknown hash %s from %s', name_hash, self)
        return True

    def get_current_content_hash(self, manifest):
        """Gets the md5 hash for each of the files in this manager's list of
        assets.
        """
        h = hashlib.md5()
        for path in self.get_paths():
            assert path in manifest['assets']
            h.update(manifest['assets'][path]['version'])
        return h.hexdigest()

    def get_paths(self):
        """Returns a list of relative paths to the assets contained in this
        manager.
        """
        paths = map(functools.partial(make_static_path, self.settings['static_dir']), self.rel_urls)
        try:
            assert all(map(os.path.isfile, paths))
        except AssertionError:
            missing = [path for path in paths if not os.path.isfile(path)]
            raise DependencyError(self.src_path, ','.join(missing))
        return paths

    def get_compiled_path(self):
        """Creates the output filename for the compiled assets of the given
        manager.
        """
        return make_output_path(self.settings['static_dir'], self.get_compiled_name())


class JSCompiler(AssetCompiler, assetman.JSManager):

    include_expr = 'include_js'

    def do_compile(self, **kwargs):
        """We just hand each of the input paths to the closure compiler and
        let it go to work.
        """
        cmd = [
            'java', '-jar', self.settings.get("closure_compiler"),
            '--compilation_level', 'SIMPLE_OPTIMIZATIONS',
            ]
        for path in self.get_paths():
            cmd.extend(('--js', path))
        return run_proc(cmd)


class CSSCompiler(AssetCompiler, assetman.CSSManager):

    include_expr = 'include_css'

    def do_compile(self, **kwargs): 
        """Compiles CSS files using the YUI compressor. Since the compressor
        will only accept a single input file argument, we have to manually
        concat the CSS files in the batch and pipe them into the compressor.

        This also allows us to accept a css_input argument, so this function
        can be used by the compile_less function as well.
        """
        css_input = kwargs.get("css_input")
        if css_input is None:
            css_input = '\n'.join(
                open(path).read() for path in self.get_paths())
        if not kwargs.get("skip_inline_images"):
            css_input = self.inline_images(css_input)
        cmd = [
            'java', '-jar', self.settings.get("yui_compressor_path"),
            '--type', 'css', '--line-break', '160',
        ]
        return run_proc(cmd, stdin=css_input)

    def inline_images(self, css_src):
        """Here we will "inline" any images under a certain size threshold
        into the CSS in the form of "data:" URIs.

        IE 8 can't handle URLs longer than 32KB, so any image whose data URI
        is larger than that is skipped.
        """
        KB = 1024.0
        MAX_FILE_SIZE = 24 * KB # Largest size we consider for inlining
        MAX_DATA_URI_SIZE = 32 * KB # IE8's maximum URL size

        # We only want to replace asset references that show up inside of
        # `url()` rules (this avoids weird constructs like IE-specific filters
        # for transparent PNG support).
        base_pattern = get_static_pattern(self.settings.get('static_url_prefix'))
        pattern = r"""(url\(["']?)%s(["']?\))""" % base_pattern

        # Track duplicate images so that we can warn about them
        seen_assets = defaultdict(int)

        def replacer(match):
            before, url_prefix, rel_path, after = match.groups()
            path = make_static_path(rel_path)
            assert os.path.isfile(path), (path, str(self))
            if os.stat(path).st_size > MAX_FILE_SIZE:
                logging.debug('Not inlining %s (%.2fKB)', path, os.stat(path).st_size / KB)
                return match.group(0)
            else:
                encoded = base64.b64encode(open(path).read())
                mime, _ = mimetypes.guess_type(path)
                data_uri = 'data:%s;base64,%s' % (mime, encoded)
                if len(data_uri) >= MAX_DATA_URI_SIZE:
                    logging.debug('Not inlining %s (%.2fKB encoded)', path, len(data_uri) / KB)
                    return match.group(0)
                seen_assets['%s%s' % (url_prefix, rel_path)] += 1
                return ''.join([before, data_uri, after])

        result = re.sub(pattern, replacer, css_src)

        for url, count in seen_assets.iteritems():
            if count > 1:
                logging.warn('Inlined asset duplicated %dx: %s', count, url)

        return result


class LessCompiler(CSSCompiler, assetman.LessManager):

    include_expr = 'include_less'

    def do_compile(self, **kwargs):
        """Compiling less files is an ugly 2-step process, because the lessc
        compiler sucks.

        First, we have to run each of the given paths through lessc
        separately, capturing and concatenating the output. Then, we send all
        of the compiled CSS to the YUI compressor.
        """
        # First we "compile" the less files into CSS
        lessc = self.settings.get("lessc_path")
        outputs = [run_proc([lessc, path]) for path in self.get_paths()]
        return super(LessCompiler, self).do_compile(css_input='\n'.join(outputs))


class SassCompiler(CSSCompiler, assetman.SassManager):

    include_expr = 'include_sass'

    def do_compile(self, **kwargs):
        cmd = [
            self.settings.get("sass_compiler_path"),
            '--compass', '--trace', '-l',
        ] + self.rel_urls
        output = run_proc(cmd)
        return super(SassCompiler, self).do_compile(css_input=output)

