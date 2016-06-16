#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright © 2014—2016 Dontnod Entertainment

# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:

# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
''' Gathers buildbot build statistics to an ElasticSerach database '''

import argparse
import cPickle
import datetime
import hashlib
import logging
import os
import re
import sys

import elasticsearch
import elasticsearch.connection
import elasticsearch.helpers

import buildbot.status.builder

_LOGGER = logging.getLogger('elastic-bot')

def main():
    ''' Entry Point '''
    arguments = _load_arguments()
    _init_logging(arguments.verbose)
    database = elasticsearch.Elasticsearch(arguments.nodes)
    actions = _get_bulk_actions(database,
                                arguments.index,
                                arguments.builders_dir,
                                arguments.overwrite,
                                arguments.tag_pattern,
                                arguments.build_properties)

    bulk = elasticsearch.helpers.parallel_bulk
    for _ in bulk(database, actions, thread_count=arguments.threads):
        pass

    return 0

def _init_logging(verbose):
    _LOGGER.setLevel(logging.DEBUG if verbose else logging.info)
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(fmt='[%(asctime)s] [%(levelname)s] %(message)s',
                                  datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    _LOGGER.addHandler(handler)

def _load_arguments():
    parser = argparse.ArgumentParser(description=('Gathers buildbot build '
                                                  'statistics and stores them '
                                                  'in an Elasticsearch database'
                                                  '.'))
    parser.add_argument('--verbose',
                        '-v',
                        action='store_true',
                        help='Enable verbose output')

    parser.add_argument('--builders-dir',
                        metavar='<dir>',
                        default='.',
                        help='Directory where are stored the buildbot logs')

    parser.add_argument('--nodes',
                        metavar='<node>',
                        default=['localhost:9200'],
                        nargs='*',
                        help='Elasticsearch database hosts')

    parser.add_argument('--index',
                        metavar='<index>',
                        default='buildbot',
                        help='Elasticsearch index where to store statistics')

    parser.add_argument('--overwrite',
                        action='store_true',
                        help=('Overwrites builds already indexed'))

    parser.add_argument('--threads',
                        '-t',
                        type=int,
                        default=1,
                        help=('Number of database threads to create.'))

    parser.add_argument('--build-properties',
                        type=str,
                        nargs='*',
                        default=[],
                        help=('Build properties add to each build document'))

    parser.add_argument('--tag-pattern',
                        type=str,
                        metavar=('<pattern>', '<tag>'),
                        nargs=2,
                        action='append',
                        default=[],
                        help=('Adds a tag to a build or step if one of his '
                              'the specified properties matches the pattern'))

    return parser.parse_args()

def _get_bulk_actions(database,
                      index,
                      builders_dir,
                      overwrite,
                      tag_patterns,
                      build_properties):

    last_builds = {} if overwrite else _get_last_builds(database, index)

    for builder_name, builder_dir in _discover_builders(builders_dir):
        if builder_name in last_builds:
            last_build = last_builds[builder_name]
        else:
            last_build = -1

        for build in _load_builds(builder_name, builder_dir, last_build):
            build_id = _get_id(builder_name, build.number)
            build_document = _get_build_document(tag_patterns,
                                                 build_properties,
                                                 builder_name,
                                                 build)
            _LOGGER.debug('Created build %s:%s document',
                          builder_name,
                          build.number)
            for key, value in build_document.iteritems():
                _LOGGER.debug('%s : %s', key, value)

            yield _get_action(index, 'build', build_id, build_document)

            for step in build.steps:
                if step.started is None:
                    continue

                step_id = _get_id(build_id, step.step_number)
                step_document = _get_step_document(tag_patterns,
                                                   builder_name,
                                                   build,
                                                   step)
                _LOGGER.debug('Created step %s:%s:%s document',
                              builder_name,
                              build.number,
                              step.step_number)
                for key, value in step_document.iteritems():
                    _LOGGER.debug('%s : %s', key, value)
                yield _get_action(index, 'step', step_id, step_document)

            _LOGGER.info('Loaded build %s:%s', builder_name, build.number)

def _get_last_builds(database, index):
    body = {
        "query": {
            'match_all' : {}
        },
        "aggs" : {
            "builders": {
                "terms": {"field": "name", 'size':0},
                "aggs": {
                    "last_build": {
                        "max": {"field": "number"}
                    }
                }
            }
        }
    }

    page = database.search(index=index, doc_type='build', body=body)
    last_builds = {}
    for bucket in page['aggregations']['builders']['buckets']:
        builder_name = bucket['key']
        last_build = bucket['last_build']['value']
        if last_build is None:
            last_build = -1
        last_builds[builder_name] = int(last_build)

    for builder_name, last_build in last_builds.iteritems():
        logging.debug('Builder %s last indexed build is %s',
                      builder_name,
                      last_build)

    return last_builds

def _discover_builders(root_directory):
    for root, _, files in os.walk(root_directory):
        for file_name in files:
            if file_name == 'builder':
                builder_pickle_path = os.path.join(root, file_name)
                try:
                    with open(builder_pickle_path, 'r') as builder_pickle:
                        builder = cPickle.load(builder_pickle)
                #pylint: disable=broad-except
                except cPickle.UnpicklingError as ex:
                    _LOGGER.warn('Error unpickling builder file %s : %s.'
                                 ' this builder will be ignored.',
                                 builder_pickle_path, ex)
                    continue

                builder_dir = os.path.dirname(builder_pickle_path)
                builder_dir = os.path.abspath(builder_dir)
                builder_dir = os.path.normpath(builder_dir)
                _LOGGER.debug('Found new builder directory %s.', builder_dir)

                yield builder.name, builder_dir

def _load_builds(builder_name, builder_dir, last_indexed_build):
    for root, _, files in os.walk(builder_dir):
        for file_name in files:
            if re.match(r'^\d+$', file_name):
                build_number = int(file_name)
                build_pickle_path = os.path.join(root, file_name)

                if build_number <= last_indexed_build:
                    _LOGGER.debug('Ignoring already indexed build  %s',
                                  build_pickle_path)
                    continue

                try:
                    with open(build_pickle_path, 'r') as file_content:
                        build = cPickle.load(file_content)
                #pylint: disable=broad-except
                except cPickle.UnpicklingError as ex:
                    _LOGGER.warn(('Error while loading build pickle %s : '
                                  '%s. This build will be discarded.'),
                                 build_pickle_path, ex)
                    continue

                _LOGGER.debug('Loaded %s build pickle', build_pickle_path)

                if build.results is None:
                    _LOGGER.info(('Build %s:%s is not finished yet, '
                                  'ignoring it for now'),
                                 builder_name, build.number)
                    continue

                yield build

def _get_id(*args):
    md5_hash = hashlib.md5()
    for arg in args:
        md5_hash.update(str(arg).encode())
    return md5_hash.hexdigest()

def _get_build_document(tag_patterns, build_properties, builder_name, build):
    document = _get_document(build, build)
    document['name'] = builder_name
    document['number'] = build.number

    properties_dict = build.properties.asDict()
    for property_name in build_properties:
        if property_name in properties_dict:
            value = properties_dict[property_name][0]
            document[property_name] = value

    _add_tags(tag_patterns, document)

    trigger_date = 0
    has_changes = False
    if hasattr(build, 'sources'):
        for source_stamp in build.sources:
            if hasattr(source_stamp, 'changes'):
                for change in source_stamp.changes:
                    has_changes = True
                    if trigger_date < change.when:
                        trigger_date = change.when

    trigger_date = trigger_date - 7400

    if has_changes:
        document['waiting_duration'] = build.started - trigger_date
        document['total_duration'] = build.finished  - trigger_date

    return document

def _get_step_document(tag_patterns, builder_name, build, step):
    document = _get_document(build, step)
    document['builder'] = builder_name
    document['number'] = step.step_number
    _add_tags(tag_patterns, document)
    return document

def _get_document(build, build_or_step):
    return {
        'slave' : build.slavename,
        'blamelist' : '-'.join(build.blamelist),
        'start' : datetime.datetime.fromtimestamp(build_or_step.started),
        'end' : datetime.datetime.fromtimestamp(build_or_step.finished),
        'duration' : build_or_step.finished - build_or_step.started,
        'result': buildbot.status.builder.Results[build_or_step.results],
    }

def _add_tags(tag_patterns, document):
    tags = []
    for _, value in document.iteritems():
        for pattern, tag in tag_patterns:
            if isinstance(value, basestring) and re.match(pattern, value):
                tags.append(tag)

    if tags:
        document['tags'] = '-'.join(tags)

def _get_action(index, doc_type, doc_id, body):
    return {
        "_index": index,
        "_type": doc_type,
        "_id": doc_id,
        "_source": body
    }

if __name__ == '__main__':
    sys.exit(main())

