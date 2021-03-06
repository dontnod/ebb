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
import logging
import os
import re
import sys

import dateutil.tz
import pytz

import elasticsearch
import elasticsearch.connection
import elasticsearch.helpers

import buildbot.status.builder

_LOGGER = logging.getLogger('elastic-bot')

def main():
    ''' Entry Point '''
    args = _load_arguments()
    _init_logging(args.verbose)

    tz_name = args.buildbot_timezone
    timezone = dateutil.tz.tzlocal() if not tz_name else pytz.timezone(tz_name)

    database = elasticsearch.Elasticsearch(args.nodes)
    actions = _get_bulk_actions(database,
                                args.index,
                                args.builders_dir,
                                args.overwrite,
                                timezone)

    bulk = elasticsearch.helpers.parallel_bulk
    error = False
    for success, result in bulk(database, actions, thread_count=args.threads):
        doc_id = result['index']['_id']
        if not success:
            _LOGGER.error('Error indexing object %s : %s', doc_id, result)
            error = True
        else:
            _LOGGER.info('Indexed item %s', doc_id)

    return 1 if error else 0

def _init_logging(verbose):
    _LOGGER.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(fmt=('[%(asctime)s] [%(levelname)s] '
                                       '%(message)s'),
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

    parser.add_argument('--buildbot-timezone',
                        type=str,
                        default=None,
                        help=('Timezone of timestamps stored in buildbot pickles'))

    return parser.parse_args()

def _get_bulk_actions(database, index, builders_dir, overwrite, timezone):
    try:
        last_builds = {} if overwrite else _get_last_builds(database, index)

        for builder_name, builder_dir in _discover_builders(builders_dir):
            if builder_name in last_builds:
                last_build = last_builds[builder_name]
            else:
                last_build = -1

            for build in _load_builds(builder_name, builder_dir, last_build):
                build_id = '_'.join([builder_name, str(build.number)])
                build_properties = _get_build_properties(build, timezone)
                _LOGGER.debug('Created build %s:%s document',
                              builder_name,
                              build.number)
                for key, value in build_properties.iteritems():
                    _LOGGER.debug('%s : %s', key, value)

                for step in build.steps:
                    if step.started is None:
                        continue

                    step_id = '_'.join([build_id, str(step.step_number)])
                    step_properties = _get_step_properties(build, step, timezone)
                    _LOGGER.debug('Created step %s:%s:%s document',
                                  builder_name,
                                  build.number,
                                  step.step_number)
                    for key, value in step_properties.iteritems():
                        _LOGGER.debug('%s : %s', key, value)
                    yield _get_action(index, 'step', step_id, step_properties)

                yield _get_action(index, 'build', build_id, build_properties)

                _LOGGER.debug('Loaded build %s:%s', builder_name, build.number)
    #pylint: disable=broad-except
    except Exception as ex:
        _LOGGER.error('An error occured during build informations gathering %s',
                      ex)

def _get_last_builds(database, index):
    body = {
        "query": {
            'match_all' : {}
        },
        "aggs" : {
            "builders": {
                "terms": {"field": "buildername", 'size':0},
                "aggs": {
                    "last_build": {
                        "max": {"field": "buildnumber"}
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

def _get_build_properties(build, timezone):
    document = _get_properties('build', build, build, timezone)

    trigger_date = 0
    has_changes = False
    if hasattr(build, 'sources'):
        for source_stamp in build.sources:
            if hasattr(source_stamp, 'changes'):
                for change in source_stamp.changes:
                    has_changes = True
                    if trigger_date < change.when:
                        trigger_date = change.when

    if has_changes:
        document['waiting_duration'] = build.started - trigger_date
        document['total_duration'] = build.finished  - trigger_date

    return document

def _get_step_properties(build, step, timezone):
    document = _get_properties('step', build, step, timezone)
    document['step_name'] = step.name
    document['step_number'] = step.step_number
    return document

def _get_properties(doc_type, build, build_or_step, timezone):
    start = datetime.datetime.fromtimestamp(build_or_step.started,
                                            tz=timezone)
    end = datetime.datetime.fromtimestamp(build_or_step.finished,
                                          tz=timezone)

    document = {
        'type' : doc_type,
        'blamelist' : '-'.join(build.blamelist),
        'start' : start,
        'end' : end,
        'duration' : build_or_step.finished - build_or_step.started,
        'result': buildbot.status.builder.Results[build_or_step.results],
    }

    for key, value in build.properties.asDict().iteritems():
        if key in ['workdir', 'scheduler', 'builddir']:
            continue
        value = value[0]
        if value is not None and value != '':
            document[key] = value

    return document

def _get_action(index, doc_type, doc_id, body):
    return {
        "_index": index,
        "_type": doc_type,
        "_id": doc_id,
        "_source": body
    }

if __name__ == '__main__':
    sys.exit(main())

