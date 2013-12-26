# -*- coding: utf-8 -*-
"""
This module provides some utils for easier creation of Wapiti_ templates
and data files.

.. _Wapiti: http://wapiti.limsi.fr/

The idea is to train models with command-line wapiti utility
using templates and training files prepared with WapitiFeatureEncoder,
and then apply this model from Python using WapitiChunker class.
"""

from __future__ import absolute_import
import os
import re
import shlex
import tempfile
from sklearn.base import BaseEstimator, TransformerMixin
from .utils import get_combined_keys, tostr, run_command


class WapitiChunker(BaseEstimator):
    """
    Class for tagging using pre-built Wapiti models.
    """
    def __init__(self, model, feature_encoder, feature_extractor):
        """

        Parameters
        ----------
        model: wapiti.Model
            Loaded pre-built Wapiti model.

        feature_encoder: WapitiFeatureEncoder
            Encoder instance that was used for model building.

        feature_extractor: HtmlFeaturesExtractor
            Extractor instance that was used for converting HTML into features

        """
        self.model = model
        self.feature_encoder = feature_encoder
        self.feature_extractor = feature_extractor

    def transform(self, X, encoding=None):
        """
        Return a list of (text, ner_label) pairs for HTML document X.
        """
        feature_dicts, feature_lines = self._prepare_features(X, encoding)
        labels = self._get_labels(feature_lines)
        tokens = [f['token'] for f in feature_dicts]
        assert len(tokens) == len(labels), (len(tokens), len(labels), tokens, labels)

        le = self.feature_extractor.label_encoder
        return [
            (" ".join(ner_tokens), label)
            for ner_tokens, label in le.group(zip(tokens, labels))
        ]

    def _prepare_features(self, X, encoding=None):
        feature_dicts, _ = self.feature_extractor.fit_transform(X, encoding=encoding)
        feature_lines = self.feature_encoder.transform(feature_dicts)
        return feature_dicts, feature_lines

    def _get_labels(self, feature_lines):
        lines_joined = "\n".join(feature_lines)
        out_lines = self.model.label_sequence(lines_joined).split('\n')
        return [line.rsplit()[-1] for line in out_lines if line.strip()]


class WapitiFeatureEncoder(BaseEstimator, TransformerMixin):

    def __init__(self, move_to_front=('token',)):
        self.move_to_front = tuple(move_to_front)
        self.feature_names_ = None
        self.vocabulary_ = None

    def fit(self, X, y=None):
        """
        X should be a list of lists of dicts with features;
        It can be obtained using HtmlFeaturesExtractor.
        """
        return self.partial_fit(X)

    def partial_fit(self, X, y=None):
        keys = set(self.feature_names_ or set())
        move_to_front = set(self.move_to_front)

        for feature_dicts in X:
            keys = (keys | get_combined_keys(feature_dicts)) - move_to_front

        self.feature_names_ = self.move_to_front + tuple(keys)
        self.vocabulary_ = dict((f, i) for i, f in enumerate(self.feature_names_))
        return self

    def transform_single(self, feature_dicts):
        """
        Transform a sequence of dicts ``feature_dicts``
        to a list of Wapiti data file lines.
        """
        lines = []
        for dct in feature_dicts:
            line = ' '.join(tostr(dct.get(key)) for key in self.feature_names_)
            lines.append(line)
        return lines

    def transform(self, X):
        return [self.transform_single(feature_dicts) for feature_dicts in X]

    def prepare_template(self, template):
        r"""
        Prepare Wapiti template by replacing feature names with feature
        column indices inside ``%x[row,col]`` macros. Indices are compatible
        with :meth:`WapitiFeatureEncoder.transform` output.

            >>> we = WapitiFeatureEncoder(['token', 'tag'])
            >>> seq_features = [{'token': 'the', 'tag': 'DT'}, {'token': 'dog', 'tag': 'NN'}]
            >>> we.fit([seq_features])
            WapitiFeatureEncoder(move_to_front=('token', 'tag'))
            >>> we.prepare_template('*:Pos-1 L=%x[-1, tag]\n*:Suf-2 X=%m[ 0,token,".?.?$"]')
            '*:Pos-1 L=%x[-1,1]\n*:Suf-2 X=%m[0,0,".?.?$"]'

        Check these links for more info about template format:

        * http://wapiti.limsi.fr/manual.html
        * http://crfpp.googlecode.com/svn/trunk/doc/index.html#templ

        """
        return prepare_wapiti_template(template, self.vocabulary_)

    def unigram_features_template(self, scope='*'):
        """
        Return Wapiti template with unigram features for each of
        known features.

            >>> we = WapitiFeatureEncoder(['token', 'tag'])
            >>> seq_features = [{'token': 'the', 'tag': 'DT'}, {'token': 'dog', 'tag': 'NN'}]
            >>> we.fit([seq_features])
            WapitiFeatureEncoder(move_to_front=('token', 'tag'))
            >>> print(we.unigram_features_template())
            <BLANKLINE>
            # Unigrams for all custom features
            *feat:token=%x[0,0]
            *feat:tag=%x[0,1]
            <BLANKLINE>
            >>> print(we.unigram_features_template('u'))
            <BLANKLINE>
            # Unigrams for all custom features
            ufeat:token=%x[0,0]
            ufeat:tag=%x[0,1]
            <BLANKLINE>
        """
        lines = ['\n# Unigrams for all custom features']
        for col, name in enumerate(self.feature_names_):
            line = '{scope}feat:{name}=%x[0,{col}]'.format(scope=scope, name=name, col=col)
            lines.append(line)
        return "\n".join(lines) + '\n'

    def reset(self):
        self.feature_names_ = None



class WapitiCRF(BaseEstimator, TransformerMixin):
    """
    Class for training and applying Wapiti CRF models.

    For training it relies on calling original Wapiti binary (via
    subprocess), so "wapiti" binary must be available if you need "fit"
    method.

    For prediction it relies on python-wapiti library.
    """

    WAPITI_CMD = 'wapiti'

    def __init__(self, model_filename, wapiti_args,
                 feature_template, unigrams_scope="u",
                 tempdir=None, unlink_temp=True, verbose=True):

        self.model_filname_ = model_filename
        if isinstance(wapiti_args, list):
            self.wapiti_args_ = wapiti_args
        else:
            self.wapiti_args_ = shlex.split(wapiti_args)
        self.feature_template_ = feature_template
        self.unigrams_scope_ = unigrams_scope
        self.tempdir_ = tempdir
        self.unlink_temp_ = unlink_temp
        self.verbose_ = verbose

        self.feature_encoder_ = WapitiFeatureEncoder()
        super(WapitiCRF, self).__init__()

    def fit(self, X, y, X_dev=None, y_dev=None, out_dev=None):
        """
        Train a model.

        Parameters
        ----------
        X: a sequence of lists of feature dicts

        y: a sequence of lists of labels

        X_dev: (optional) a sequence of lists of feature dicts
            Data used for testing and as a stopping criteria.

        y_dev: (optional) a sequence of lists of labels
            Labels corresponding to X_dev.

        out_dev: (optional) string
            Path to a file where tagged development data will be written.

        """
        if any([X_dev, y_dev, out_dev]):
            if X_dev is None or y_dev is None:
                raise ValueError("Pass both X_dev and y_dev to use the development data")

        self.feature_encoder_.reset()
        self.feature_encoder_.fit(X, y)

        dev_fn = None
        to_unlink = []
        try:
            train_fn = self._create_wapiti_data_file(X, y)
            to_unlink.append(train_fn)

            if X_dev is not None:
                dev_fn = self._create_wapiti_data_file(X_dev, y_dev)
                if out_dev is None:
                    _, out_dev = tempfile.mkstemp(dir=self.tempdir_, suffix=".txt", prefix="wapiti-dev-data")
                    to_unlink.append(out_dev)

            template_fn = self._create_wapiti_feature_template_file()
            to_unlink.append(template_fn)

            # run wapiti training
            args = ['train', '--pattern', template_fn] + self.wapiti_args_
            if dev_fn:
                args += ['--devel', dev_fn]
            args += [train_fn, self.model_filname_]
            self.run_wapiti(args)

            # do a final check on development data
            if dev_fn:
                args = ['label', '-m', self.model_filname_, '--check', dev_fn, out_dev]
                self.run_wapiti(args)

        finally:
            if self.unlink_temp_:
                for filename in to_unlink:
                    os.unlink(filename)

    def run_wapiti(self, args):
        return run_command([self.WAPITI_CMD] + args, self.verbose_)

    def _create_wapiti_data_file(self, X, y):
        """
        Create a file with input data for wapiti. Return a resulting file name;
        caller should unlink the file.
        """
        X = self.feature_encoder_.transform(X)
        wapiti_sequences = [
            self._to_train_sequence(lines, tags) for lines, tags in zip(X, y)
        ]
        with tempfile.NamedTemporaryFile('wb', prefix="wapiti-data-", suffix=".txt", dir=self.tempdir_, delete=False) as fp:
            for seq in wapiti_sequences:
                fp.write(seq.encode('utf8'))
                fp.write(b"\n\n")
        return fp.name

    def _create_wapiti_feature_template_file(self):
        # create feature template
        with tempfile.NamedTemporaryFile('wb', prefix="feature-template-", suffix=".txt", dir=self.tempdir_, delete=False) as fp:
            template = self.feature_encoder_.prepare_template(self.feature_template_)
            fp.write(template.encode('utf8'))

            if self.unigrams_scope_ is not None:
                unigram_template = self.feature_encoder_.unigram_features_template(self.unigrams_scope_)
                fp.write(b"\n")
                fp.write(unigram_template.encode('utf8'))
        return fp.name

    def _to_train_sequence(self, wapiti_lines, tags):
        return "\n".join(["%s %s" %(line, tag) for line, tag in zip(wapiti_lines, tags)])



WAPITI_MACRO_PATTERN = re.compile(r'''
    (?P<macro>%[xXtTmM])
    \[
    \s*(?P<offset>[-]?\d+)\s*
    ,
    \s*(?P<column>[^\],\s]+)\s*  # identifier: anything but closing bracket or comma
    (?P<rest>[\],])              # closing bracket or comma
    ''', re.VERBOSE | re.UNICODE
)

def prepare_wapiti_template(template, vocabulary):
    r"""
    Prepare Wapiti template by replacing feature names with feature
    column indices inside ``%x[row,col]`` macros::

        >>> vocab = {'token': 0, 'tag': 1}
        >>> prepare_wapiti_template('*:Pos-1 L=%x[-1, tag]\n*:Suf-2 X=%m[ 0,token,".?.?$"]', vocab)
        '*:Pos-1 L=%x[-1,1]\n*:Suf-2 X=%m[0,0,".?.?$"]'

    It understands which lines are comments::

        >>> prepare_wapiti_template('*:Pos-1 L=%x[-1, tag]\n# *:Suf-2 X=%m[ 0,token,".?.?$"]', vocab)
        '*:Pos-1 L=%x[-1,1]\n# *:Suf-2 X=%m[ 0,token,".?.?$"]'

    Check these links for more info about template format:

    * http://wapiti.limsi.fr/manual.html
    * http://crfpp.googlecode.com/svn/trunk/doc/index.html#templ

    """
    def repl(m):
        column = m.group('column')
        if not column.isdigit():
            column = vocabulary[column]
        return "{0[macro]}[{0[offset]},{1}{0[rest]}".format(m.groupdict(), column)

    lines = [
        (WAPITI_MACRO_PATTERN.sub(repl, line) if not _wapiti_line_is_comment(line) else line)
        for line in template.splitlines()
    ]

    return "\n".join(lines)


def _wapiti_line_is_comment(line):
    return line.strip().startswith('#')
