# -*- coding: utf-8 -*-
# C pyright © 2014—2016 Dontnod Entertainment

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
''' Easier buildbot configuration. '''

import abc
import re
import contextlib
import shlex

import buildbot.buildslave
import buildbot.changes
import buildbot.changes.p4poller
import buildbot.config
import buildbot.interfaces
import buildbot.process.factory
import buildbot.schedulers.basic
import buildbot.schedulers.forcesched
import buildbot.schedulers.timed
import buildbot.status.html
import buildbot.status.mail
import buildbot.status.web.auth
import buildbot.status.web.authz
import buildbot.steps.shell
import buildbot.steps.source.p4
import zope.interface

class Scope(object):
    ''' Config node : inherit parent config values '''

    # Config is created using context managers describing a tree. The top node
    # is the active context
    _top = None

    def __init__(self):
        self._parent = Scope._top
        self.children = []
        if Scope._top is not None:
            Scope._top.children.append(self)
        self.properties = {}

    def __enter__(self):
        assert Scope._top != self
        Scope._top = self
        return self

    def __exit__(self, ex_type, value, traceback):
        Scope._top = self._parent

    @staticmethod
    def set(name, value):
        ''' Sets a property on the active node '''
        assert Scope._top is not None
        Scope._top.properties[name] = value

    @staticmethod
    def set_checked(name, value, expected_type):
        ''' Sets a property on the active node if it's not None, and checks it's
            type '''
        if value is None:
            return
        if expected_type is not None:
            assert isinstance(value, expected_type)
        Scope.set(name, value)

    @staticmethod
    def append(name, *values):
        ''' Appends values to a list property with key name on this node '''
        assert isinstance(name, basestring)
        assert Scope._top is not None
        properties = Scope._top.properties
        if name not in properties:
            properties[name] = []
        assert isinstance(properties[name], list)
        properties[name].extend(values)

    @staticmethod
    def update(name, key, value):
        ''' Adds a {key : value} entry on the dictionary named name on the top
            node '''
        assert isinstance(name, basestring)
        assert Scope._top is not None
        properties = Scope._top.properties
        if name not in properties:
            properties[name] = {}
        assert isinstance(properties[name], dict)
        properties[name][key] = value

    @staticmethod
    @contextlib.contextmanager
    def push(func, *args, **kwargs):
        ''' Pushes a scope and calls a function before yielding '''
        with Scope() as scope:
            func(*args, **kwargs)
            yield scope

    def get(self, name, default=None):
        ''' Search for a property up the tree and returns the first occurence
        '''
        assert isinstance(name, basestring)
        if name not in self.properties:
            if self._parent is not None:
                return self._parent.get(name, default)
            return default

        value = self.properties[name]

        if isinstance(value, list):
            if self._parent is not None:
                parent_value = self._parent.get(name, default)
                if parent_value is not None:
                    assert isinstance(parent_value, list)
                    return value + parent_value

        elif isinstance(value, dict):
            if self._parent is not None:
                parent_value = self._parent.get(name, default)
                if parent_value is not None:
                    assert isinstance(parent_value, dict)
                    parent_value.update(value)
                    return parent_value
            return value.copy()

        return value

    def get_interpolated(self, name, default=None):
        ''' Search for a property up the tree, and returns it's value
            interpolated with this node and all of it's parents property values
        '''
        assert isinstance(name, basestring)
        result = self.get(name, default)
        return self.interpolate(result)

    def interpolate(self, value):
        ''' Interpolates value with all properties from current node and it's
            parents
        '''
        if isinstance(value, basestring):
            format_args = self.get_interpolation_values()
            try:
                return value.format(**format_args)
            except KeyError as error:
                raise KeyError('Key %s not found while interpolating %s on %s' % (error, value, self))
        elif isinstance(value, list):
            return [self.interpolate(it) for it in value]
        elif isinstance(value, dict):
            result = {}
            for key, it in value.iteritems():
                result[key] = self.interpolate(it)
            return result

        return value

    def get_rendered(self, name, default=None):
        ''' Search for a property up the tree, and returns it's value
            interpolated with this node and all of it's parents property values
        '''
        assert isinstance(name, basestring)
        result = self.get(name, default)
        return self.render(result)

    def render(self, value):
        ''' Interpolates value with all properties from current node and it's
            parents
        '''
        if isinstance(value, basestring):
            return _Renderer(value, self)
        elif isinstance(value, list):
            return [self.render(it) for it in value]
        elif isinstance(value, dict):
            result = {}
            for key, it in value.iteritems():
                result[key] = self.render(it)
            return result

        return value

    def get_interpolation_values(self):
        ''' Parses the tree bottom-up, getting string property values '''
        if self._parent is None:
            result = {}
        else:
            result = self._parent.get_interpolation_values()

        for key, value in self.properties.iteritems():
            if isinstance(value, basestring):
                result[key] = value

        return result

    def get_parent_of_type(self, parent_type):
        ''' Find closest parent of specified node type '''
        if isinstance(self._parent, parent_type):
            return self._parent

        result = self._parent.get_parent_of_type(parent_type)
        assert result is not None
        return result

    def build(self, config):
        ''' Builds this node '''
        self._build(config)
        for child in self.children:
            child.build(config)

    @abc.abstractmethod
    def _build(self, config):
        pass

    def get_properties_names(self, prefix):
        ''' Returs all properties defined on this node and parent starting
            with given prefix '''
        if self._parent is not None:
            result = self._parent.get_properties_names(prefix)
        else:
            result = set()

        for key, _ in self.properties.iteritems():
            if key.startswith(prefix + '_'):
                result.add(key)

        return result

    def _get_prefixed_properties(self, prefixes, rendered=None, raw=None):
        result = {}
        rendered = set([] if rendered is None else rendered)
        raw = set(() if raw is None else raw)

        if not isinstance(prefixes, tuple):
            assert isinstance(prefixes, str)
            prefixes = (prefixes,)

        props = {}
        for prefix in prefixes:
            interpolated_it = self.get_properties_names(prefix)
            raw_it = interpolated_it & raw
            rendered_it = interpolated_it & rendered
            interpolated_it = interpolated_it - raw_it - rendered_it
            props[prefix] = (interpolated_it, raw_it, rendered_it)

        for prefix, (interpolated_it, raw_it, rendered_it) in props.iteritems():
            for key in interpolated_it:
                key_without_prefix = key[len(prefix) + 1:]
                result[key_without_prefix] = self.get_interpolated(key)

            for key in raw_it:
                key_without_prefix = key[len(prefix) + 1:]
                result[key_without_prefix] = self.get(key)

            for key in rendered_it:
                key_without_prefix = key[len(prefix) + 1:]
                result[key_without_prefix] = self.get_rendered(key)
            # result[name] = self.properties[key]

        return result

    def _build_class(self,
                     buildbot_class,
                     prefixes,
                     positional=None,
                     raw=None,
                     rendered=None,
                     additional=None):

        kwargs = self._get_prefixed_properties(prefixes,
                                               rendered=rendered,
                                               raw=raw)
        if additional is not None:
            kwargs.update(additional)
        args = []
        if positional is not None:
            for name in positional:
                assert name in kwargs, '%s argument missing' % name
                args.append(kwargs[name])
                del kwargs[name]

        return buildbot_class(*args, **kwargs)

class Config(Scope):
    ''' Root config node '''
    def __init__(self):
        super(Config, self).__init__()
        self.buildbot_config = {}
        self._parsers = {}
        self._schedulers = {}
        self._slaves = []
        self._triggerables = {}
        self._locks = {}
        self.buildbot_config['builders'] = []
        self.buildbot_config['schedulers'] = []
        self.buildbot_config['slaves'] = []
        self.buildbot_config['status'] = []
        self.buildbot_config['change_source'] = []

    @staticmethod
    def db(url, poll_interval=None):
        ''' Configures db parameters '''
        Scope.set_checked('db_db_url', url, basestring)
        Scope.set_checked('db_poll_interval', poll_interval, int)

    @staticmethod
    def site(title, title_url, buildbot_url):
        ''' Configures site parameters '''
        Scope.set_checked('base_title', title, basestring)
        Scope.set_checked('base_titleURL', title_url, basestring)
        Scope.set_checked('base_buildbotURL', buildbot_url, basestring)

    @staticmethod
    def logging(compression_limit=None,
                compression_method=None,
                max_size=None,
                max_tail_size=None):
        ''' Configures logging parameters '''
        Scope.set_checked('base_logCompressionLimit', compression_limit, int)
        Scope.set_checked('base_logCompressionMethod',
                          compression_method,
                          basestring)
        Scope.set_checked('base_logMaxSize', max_size, int)
        Scope.set_checked('base_logMaxTailSize', max_tail_size, int)

    @staticmethod
    def horizons(change_horizon=None,
                 build_horizon=None,
                 event_horizon=None,
                 log_horizon=None):
        ''' Configures horizon parameters '''
        Scope.set_checked('base_changeHorizon', change_horizon, int)
        Scope.set_checked('base_buildHorizon', build_horizon, int)
        Scope.set_checked('base_eventHorizon', event_horizon, int)
        Scope.set_checked('base_logHorizon', log_horizon, int)

    @staticmethod
    def cache(changes=None,
              builds=None,
              chdicts=None,
              build_requests=None,
              source_stamps=None,
              ssdicts=None,
              objectids=None,
              usdicts=None):
        ''' Configures horizon parameters '''
        Scope.set_checked('cache_Changes', changes, int)
        Scope.set_checked('cache_Builds', builds, int)
        Scope.set_checked('cache_chdicts', chdicts, int)
        Scope.set_checked('cache_BuildRequests', build_requests, int)
        Scope.set_checked('cache_SourceStamps', source_stamps, int)
        Scope.set_checked('cache_ssdicts', ssdicts, int)
        Scope.set_checked('cache_objectids', objectids, int)
        Scope.set_checked('cache_usdicts', usdicts, int)

    @staticmethod
    def set_protocol(protocol, port):
        ''' Sets given protocol to given port '''
        Scope.update('protocols_%s' % protocol, 'port', port)

    @staticmethod
    def web_status(port, user, password):
        ''' Sets web status configuration '''
        Scope.set_checked('web_status_port', port, int)
        Scope.set_checked('web_status_user', user, str)
        Scope.set_checked('web_status_password', password, str)

    @staticmethod
    def add_renderer_handlers(*handlers):
        ''' Add rendering handlers that can udpate rendering arguments at build
            time '''
        Scope.append('config_renderer_handlers', *handlers)

    def build_config(self):
        ''' Builds the buildbot config '''
        self.build(self)
        return self.buildbot_config

    def add_slave(self, slave):
        ''' Adds a slave for later tag filtering '''
        self._slaves.append(slave)

    def get_slave_list(self, *tags):
        """ Returns declared slaves matching *all* given tags """
        result = []
        tags = set(*tags)
        for slave in self._slaves:
            slave_tags = set(slave.get_interpolated('_slave_tags', []))
            if len(tags - slave_tags) == 0:
                slave_name = slave.get_interpolated('slave_name')
                result.append(slave_name)
        return result

    def _build(self, config):
        assert config == self
        conf_dict = config.buildbot_config
        # Db config
        conf_dict['db'] = self._get_prefixed_properties('db')
        conf_dict['caches'] = self._get_prefixed_properties('cache')
        conf_dict['protocols'] = self._get_prefixed_properties('protocols')

        conf_dict.update(self._get_prefixed_properties('base'))
        self._add_web_status()

    def _add_web_status(self):
        http_port = self.get('web_status_port')
        if http_port is None:
            return

        users = [(self.get_interpolated('web_status_user'),
                  self.get_interpolated('web_status_password'))]
        auth = buildbot.status.web.auth.BasicAuth(users)
        authz = buildbot.status.web.authz.Authz(auth=auth,
                                                view=True,
                                                gracefulShutdown='auth',
                                                forceBuild='auth',
                                                forceAllBuilds='auth',
                                                pingBuilder='auth',
                                                stopBuild='auth',
                                                stopAllBuilds='auth',
                                                cancelPendingBuild='auth',
                                                showUsersPage='auth',
                                                cleanShutdown='auth')
        web_status = buildbot.status.html.WebStatus(http_port=http_port,
                                                    authz=authz)
        self.buildbot_config['status'].append(web_status)

class Slave(Scope):
    ''' Creates a new buildbot slave '''
    def __init__(self, name):
        super(Slave, self).__init__()
        self.properties['slave_name'] = name
        config = self.get_parent_of_type(Config)
        assert config is not None
        config.add_slave(self)

    @staticmethod
    def config(password=None,
               max_builds=None,
               keepalive_interval=None,
               missing_timeout=None):
        ''' Sets some buildbot slaves settings for current scope '''
        Scope.set_checked('slave_password', password, basestring)
        Scope.set_checked('slave_max_builds', max_builds, int)
        Scope.set_checked('slave_keepalive_interval', keepalive_interval, int)
        Scope.set_checked('slave_missing_timeout', missing_timeout, int)

    @staticmethod
    def add_property(key, value):
        ''' Adds a build property on this slave '''
        Scope.update('slave_properties', key, value)

    @staticmethod
    def add_notified_on_missing(*emails):
        ''' Adds emails to notify when slaves in scope are missing '''
        Scope.append('slave_notify_on_missing', *emails)

    @staticmethod
    def add_tags(*tags):
        ''' Adds specified tags to slaves in scope '''
        Scope.append('_slave_tags', *tags)

    def _build(self, config):
        slave = self._build_class(buildbot.buildslave.BuildSlave, 'slave',
                                  ['name', 'password'])
        config.buildbot_config['slaves'].append(slave)

class Builder(Scope):
    ''' Builder wrapper '''
    def __init__(self, name, category=None, description=None):
        super(Builder, self).__init__()
        self._accept_regex = None
        self._reject_regex = None
        self._factory = buildbot.process.factory.BuildFactory()

        self.properties['builder_name'] = name
        if category is not None:
            self.properties['builder_category'] = category
        if description is not None:
            self.properties['builder_description'] = description

    def add_step(self, step):
        ''' Adds a step to this builder '''
        self._factory.addStep(step)

    def trigger_on_change(self, accept_regex, reject_regex=None):
        ''' Triggers this build on change from source control '''
        self._accept_regex = accept_regex
        self._reject_regex = reject_regex

    @staticmethod
    def config(name=None,
               category=None,
               build_dir=None,
               slave_build_dir=None,
               next_slave=None,
               next_build=None,
               can_start_build=None,
               merge_requests=None,
               forcable=None,
               only_important=None,
               tree_stable_timer=None,
               file_is_important=None,
               project=None):
        ''' Sets builder config values '''
        Scope.set_checked('builder_name', name, basestring)
        Scope.set_checked('builder_category', category, basestring)
        Scope.set_checked('builder_builddir', build_dir, basestring)
        Scope.set_checked('builder_slavebuilddir', slave_build_dir, basestring)
        Scope.set_checked('builder_nextSlave', next_slave, None)
        Scope.set_checked('builder_nextBuild', next_build, None)
        Scope.set_checked('builder_canStartBuild', can_start_build, None)
        Scope.set_checked('builder_mergeRequests', merge_requests, None)
        Scope.set_checked('_force_scheduler_enabled', forcable, bool)
        Scope.set_checked('scheduler_onlyImportant', only_important, bool)
        Scope.set_checked('scheduler_treeStableTimer', tree_stable_timer, int)
        Scope.set_checked('scheduler_fileIsImportant', file_is_important, None)
        Scope.set_checked('change_filter_project', project, basestring)

    @staticmethod
    def add_slave_tags(*tags):
        ''' Adds builder tags to current scope '''
        Scope.append('_builder_slave_tags', tags)

    @staticmethod
    def add_env_variable(name, value):
        ''' Adds an envrionment variable to builders in scope '''
        Scope.update('builder_env', name, value)

    @staticmethod
    def add_tags(*tags):
        ''' Adds a tag to a builder '''
        Scope.append('builder_tags', tags)

    @staticmethod
    def add_property(name, value):
        ''' Adds a property to builders in scope '''
        Scope.update('builder_properties', name, value)

    def _build(self, config):
        slave_tags = self.get_interpolated('_builder_slave_tags', [])
        slavenames = config.get_slave_list(*slave_tags)

        # TODO locks = get_locks('job', config, scope)
        args = {'slavenames' : slavenames,
                'factory' : self._factory}

        builder = self._build_class(buildbot.config.BuilderConfig,
                                    'builder',
                                    additional=args)

        config.buildbot_config['builders'].append(builder)
        self._add_single_branch_scheduler(config)
        self._add_force_scheduler(config)

    def _add_single_branch_scheduler(self, config):
        if self._accept_regex is None:
            return
        args = {'filter_fn' : _ChangeFilter(self._accept_regex,
                                            self._reject_regex)}

        change_filter = self._build_class(buildbot.changes.filter.ChangeFilter,
                                          'change_filter',
                                          additional=args)

        builder_name = self.get_interpolated('builder_name')
        args = {'name' : '%s single branch scheduler' % builder_name,
                'builderNames' : [builder_name],
                'change_filter' : change_filter,
                'reason' : 'A CL Triggered this build'}

        scheduler_class = buildbot.schedulers.basic.SingleBranchScheduler
        scheduler = self._build_class(scheduler_class,
                                      'scheduler',
                                      additional=args)
        config.buildbot_config['schedulers'].append(scheduler)

    def _add_force_scheduler(self, config):
        if not self.get('_force_scheduler_enabled'):
            return

        builder_name = self.get_interpolated('builder_name')
        args = {'name' : '%s force scheduler' % builder_name,
                'builderNames' : [builder_name]}
        scheduler_class = buildbot.schedulers.forcesched.ForceScheduler
        scheduler = scheduler_class(**args)
        config.buildbot_config['schedulers'].append(scheduler)


class Repository(Scope):
    ''' Change source base scope '''
    def __init__(self, name):
        super(Repository, self).__init__()
        self.name = name
        Scope.update('source_control_repositories', name, self)

    @staticmethod
    def config(poll_interval=None,
               poll_at_launch=None,
               hitsmax=None):
        ''' Common change source parameters '''
        Scope.set_checked('change_source_pollInterval', poll_interval, int)
        Scope.set_checked('change_source_pollAtLaunch', poll_at_launch, bool)
        Scope.set_checked('change_source_hitsmax', hitsmax, int)


    @abc.abstractmethod
    def get_sync_step(self, config, step_args):
        ''' Returns a step to sync this repository '''

    @abc.abstractmethod
    def _build_change_source(self, config, args):
        ''' Creates a ChangeSource for this repository '''

    def _build(self, config):
        args = self._get_prefixed_properties('change_source')
        change_source = self._build_change_source(config, args)

        # XXX: this attribute works around a bug in buildbot that would
        # cause it to only trigger rebuilds for the first project using
        # that source
        change_source.compare_attrs.append('project')

        config.buildbot_config['change_source'].append(change_source)

class P4(Repository):
    ''' P4 handling '''
    def __init__(self, name):
        super(P4, self).__init__(name)

    @staticmethod
    def config(port=None, user=None, password=None, client=None,
               binary=None, encoding=None, timezone=None):
        ''' Common global p4 parameters '''
        # TODO : Add ticket management
        Scope.set_checked('p4_common_p4port', port, str)
        Scope.set_checked('p4_common_p4user', user, str)
        Scope.set_checked('p4_common_p4passwd', password, str)
        Scope.set_checked('p4_sync_p4client', client, str)
        Scope.set_checked('p4_poll_p4bin', binary, str)
        Scope.set_checked('p4_poll_encoding', encoding, str)
        Scope.set_checked('p4_poll_server_tz', timezone, str)

    @staticmethod
    def add_views(*views):
        ''' Adds p4 mappings for current scope '''
        Scope.append('p4_sync_p4viewspec', *views)

    def get_sync_step(self, _, step_args):
        ''' Returns sync step for this repository '''
        return self._build_class(buildbot.steps.source.p4.P4,
                                 ('p4_common', 'p4_sync'),
                                 rendered=['p4_sync_p4client'],
                                 additional=step_args)

    def _build_change_source(self, config, args):
        paths_to_poll = []
        for (depot_path, _) in self.get('p4_sync_p4viewspec', []):
            if depot_path.startswith('//'):
                # Get depot from //depot/
                base = depot_path[2:-1]
                paths_to_poll.append(base)

        for base in paths_to_poll:
            split_file = lambda branchfile: (None, branchfile)
            args['split_file'] = split_file
            project_name = self.get_interpolated('project_name')
            if project_name is not None:
                args['project'] = project_name

            p4 = self._build_class(buildbot.changes.p4poller.P4Source,
                                   ('p4_common', 'p4_poll'),
                                   additional=args)
            return p4

class Step(Scope):
    ''' Build step '''
    def __init__(self, name):
        super(Step, self).__init__()
        self.properties['step_name'] = name

    @staticmethod
    def config(halt_on_failure=None,
               flunk_on_warnings=None,
               flunk_on_failure=None,
               warn_on_warnings=None,
               warn_on_failure=None,
               always_run=None,
               description=None,
               description_done=None,
               do_step_if=None,
               hide_step_if=None,
               workdir=None,
               timeout=None):
        ''' Fail behavior settings '''
        Scope.set_checked('step_haltOnFailure', halt_on_failure, bool)
        Scope.set_checked('step_flunkOnWarnings', flunk_on_warnings, bool)
        Scope.set_checked('step_flunkOnFailure', flunk_on_failure, bool)
        Scope.set_checked('step_warnOnWarnings', warn_on_warnings, bool)
        Scope.set_checked('step_warnOnFailure', warn_on_failure, bool)
        Scope.set_checked('step_alwaysRun', always_run, bool)

        Scope.set_checked('step_description', description, str)
        Scope.set_checked('step_descriptionDone', description_done, str)

        Scope.set_checked('step_doStepIf', do_step_if, None)
        Scope.set_checked('step_hideStepIf', hide_step_if, None)

        Scope.set_checked('step_workdir', workdir, str)
        Scope.set_checked('step_timeout', timeout, int)

    @abc.abstractmethod
    def _get_step(self, config, step_args):
        ''' Builds the buildbot step '''
        pass

    def _build(self, config):
        builder = self.get_parent_of_type(Builder)
        if builder is None:
            step_name = self.get_interpolated('step_name')
            msg = 'Step %s not declared in Builder scope' % step_name
            raise Exception(msg)

        step_args = self._get_prefixed_properties('step')
        builder.add_step(self._get_step(config, step_args))

class Sync(Step):
    ''' Syncs a previoulsy declared repository '''
    def __init__(self, repo_name):
        super(Sync, self).__init__('sync %s' % repo_name)
        self._repo_name = repo_name

    @staticmethod
    def config(mode=None,
               always_use_latest=None,
               retry=None,
               log_environ=None):
        ''' Common sync steps config '''
        Scope.set_checked('sync_mode', mode, str)
        assert mode in ['incremental', 'full']
        Scope.set_checked('sync_alwaysUseLatest', always_use_latest, bool)
        Scope.set_checked('sync_retry', retry, tuple)
        assert len(retry) == 2
        Scope.set_checked('sync_logEnviron', log_environ, bool)

    def _get_step(self, config, step_args):
        repos = self.get('source_control_repositories')
        if repos is None or self._repo_name not in repos:
            msg = 'Unable to find repository %s in scope.' % self._repo_name
            raise Exception(msg)
        step_args.update(self._get_prefixed_properties('sync'))
        return repos[self._repo_name].get_sync_step(config, step_args)

class Command(Step):
    ''' Executes a shell command '''
    def __init__(self, name, command):
        super(Command, self).__init__(name)
        self._command = shlex.split(command)

    @staticmethod
    def config(want_stdout=None, want_stderr=None, lazylogfiles=None,
               max_time=None, interrupt_signal=None, sigterm_time=None,
               initial_stdin=None):
        Scope.set_checked('shell_command_want_stdout', want_stdout, bool)
        Scope.set_checked('shell_command_want_stderr', want_stderr, bool)
        Scope.set_checked('shell_command_lazylogfiles', lazylogfiles, bool)
        Scope.set_checked('shell_command_maxTime', max_time, int)
        assert interrupt_signal in ['KILL', 'TERM']
        Scope.set_checked('shell_command_interruptSignal', interrupt_signal, str)
        Scope.set_checked('shell_command_sigterm_time', sigterm_time, int)
        Scope.set_checked('shell_command_initialStdin', initial_stdin, int)

    @staticmethod
    def set_decode_rc(return_value, meaning):
        ''' Adds a return code meaning to this command '''
        assert isinstance(return_value, int)
        buildbot_enum = {'success' : buildbot.status.results.SUCCESS,
                         'warnings' : buildbot.status.results.WARNINGS,
                         'error' : buildbot.status.results.FAILURE}

        assert meaning in buildbot_enum

        Scope.update('shell_command_decodeRC',
                     return_value,
                     buildbot_enum[meaning])

    @staticmethod
    def set_log_file(name, path):
        ''' Adds a log file to this command '''
        Scope.update('shell_command_logfiles', name, path)

    def _get_step(self, config, step_args):
        step_args['command'] = self.render(self._command)
        return self._build_class(buildbot.steps.shell.ShellCommand,
                                 'shell_command',
                                 additional=step_args)

class _ChangeFilter(object):
    ''' Callable filtering change matching a regular expression against modified
        files
    '''
    def __init__(self, accept, reject=None):
        self._accept = accept
        self._reject = reject

    def __call__(self, change):
        if 'buildbot' in change.who.lower():
            return False
        for file_it in change.files:
            if re.match(self._accept, file_it) is not None:
                if self._reject is not None:
                    if re.match(self._reject, file_it) is not None:
                        continue
                return True
        return False

class _Renderer(object):
    zope.interface.implements(buildbot.interfaces.IRenderable)

    def __init__(self, fmt, scope):
        self._fmt = fmt
        self._scope = scope

    #pylint: disable=invalid-name,missing-docstring
    def getRenderingFor(self, props):
        format_vars = self._scope.get_interpolation_values()

        for handler in self._scope.get('config_renderer_handlers', []):
            handler(self._scope, props, format_vars)

        # for key in self._build_vars:
        #    if props.hasProperty(key):
        #        format_vars[key] = props.getProperty(key)

        # Commonly used : revision
        revision = props.getProperty('got_revision')

        if revision is None:
            revision = "no_revision"

        format_vars['got_revision'] = revision
        format_vars['revisions'] = ' '.join([change.revision for change in props.getBuild().allChanges()])
        return self._fmt.format(**format_vars)

