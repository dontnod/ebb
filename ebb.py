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
''' Easier buildbot configuration. '''

import abc
import cgi
import contextlib
import re
import shlex
import time

import buildbot.buildslave
import buildbot.changes
import buildbot.changes.p4poller
import buildbot.config
import buildbot.interfaces
import buildbot.process.factory
import buildbot.schedulers.basic
import buildbot.schedulers.forcesched
import buildbot.schedulers.timed
import buildbot.schedulers.triggerable
import buildbot.status.html
import buildbot.status.mail
import buildbot.status.web.auth
import buildbot.status.web.authz
import buildbot.status.words
import buildbot.steps.shell
import buildbot.steps.source.p4
import buildbot.steps.trigger
import buildbot.util

import jinja2

import twisted.internet.utils
import twisted.internet.defer

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

    def get(self, name, default=None, public_only=False):
        ''' Search for a property up the tree and returns the first occurence
        '''
        assert isinstance(name, basestring)
        if name not in self.properties:
            value = None
        else:
            value = self.properties[name]

        if value is not None:
            if not isinstance(value, list) and not isinstance(value, dict):
                return value

        for scope in self._get_related_scopes(public_only):
            scope_value = scope.get(name, public_only=True)
            if isinstance(scope_value, list):
                if value is not None:
                    assert isinstance(value, list)
                    scope_value.extend(value)

            elif isinstance(scope_value, dict):
                if value is not None:
                    assert isinstance(value, dict)
                    scope_value.update(value)

            if scope_value is not None:
                value = scope_value

        if isinstance(value, list):
            return value[:]

        elif isinstance(value, dict):
            return value.copy()

        return value if value is not None else default

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
        if hasattr(value, '__call__'):
            return value(self)
        elif isinstance(value, basestring):
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

    def get_interpolation_values(self, public_only=False):
        ''' Parses the tree bottom-up, getting string property values '''
        result = {}
        for scope in self._get_related_scopes(public_only):
            scope_values = scope.get_interpolation_values(True)
            result.update(scope_values)

        for key, value in self.properties.iteritems():
            if isinstance(value, basestring):
                result[key] = value

        return result

    def get_parent_of_type(self, parent_type):
        ''' Find closest parent of specified node type '''
        if self._parent is None:
            return None

        if isinstance(self._parent, parent_type):
            return self._parent

        return self._parent.get_parent_of_type(parent_type)

    def build(self, config):
        ''' Builds this node '''
        for child in self.children:
            child.build(config)
        self._build(config)

    def _get_related_scopes(self, public_only):
        if self._parent is not None:
            yield self._parent

        if not public_only:
            for it in self.children:
                if isinstance(it, Private):
                    yield it

    def get_properties_names(self, prefix, public_only=False):
        ''' Returs all properties defined on this node and parent starting
            with given prefix '''
        result = set()
        for scope in self._get_related_scopes(public_only):
            result = result | scope.get_properties_names(prefix, True)

        for key, _ in self.properties.iteritems():
            if key.startswith(prefix + '_'):
                result.add(key)

        return result

    @abc.abstractmethod
    def _build(self, config):
        pass

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

class Private(Scope):
    ''' Defines not inherited values on the parent scope '''
    def __init__(self):
        super(Private, self).__init__()

    def _get_related_scopes(self, public_only):
        return []

    def _build(self, config):
        pass

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
        self._builders_scopes = {}
        self.buildbot_config['builders'] = []
        self.buildbot_config['schedulers'] = []
        self.buildbot_config['slaves'] = []
        self.buildbot_config['status'] = []
        self.buildbot_config['change_source'] = []
        self.buildbot_config['prioritizeBuilders'] = self._prioritize_builders

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
    def irc_status(server, nick, password=None):
        ''' Sets web status configuration '''
        Scope.set_checked('irc_server', server, str)
        Scope.set_checked('irc_nick', nick, str)
        Scope.set_checked('irc_password', password, str)

    @staticmethod
    def add_irc_channel(channel, password=None):
        ''' Adds a channel to join for buildbot '''
        args = {'channel' : channel}
        if password is not None:
            args['password'] = password

        Scope.append('irc_channels', args)

    @staticmethod
    def set_irc_notify_events(*events):
        ''' Adds events to notify via irc '''
        for event in events:
            Scope.update('irc_notify_events', event, 1)

    @staticmethod
    def add_renderer_handlers(*handlers):
        ''' Add rendering handlers that can udpate rendering arguments at build
            time '''
        Scope.append('config_renderer_handlers', *handlers)

    def get_builder(self, name):
        ''' Returns a declared Builder '''
        return self._builders_scopes[name]

    def get_slave(self, name):
        ''' Returns a declared slave '''
        for slave in self._slaves:
            if slave.get_interpolated('slave_name') == name:
                return slave

    def build_config(self):
        ''' Builds the buildbot config '''
        self.build(self)
        return self.buildbot_config

    def add_slave(self, slave):
        ''' Adds a slave for later tag filtering '''
        self._slaves.append(slave)

    def add_builder(self, builder, scope):
        ''' Adds a builder to this config '''
        self._builders_scopes[builder.name] = scope
        self.buildbot_config['builders'].append(builder)

    def get_slave_list(self, *tags):
        """ Returns declared slaves matching *all* given tags """
        result = []
        tagset = set()
        for tag in tags:
            tagset.add(tag)
        for slave in self._slaves:
            slave_tagset = set()
            for tag in slave.get_interpolated('_slave_tags', []):
                slave_tagset.add(tag)
            if len(tagset - slave_tagset) == 0:
                slave_name = slave.get_interpolated('slave_name')
                result.append(slave_name)
        if len(result) == 0:
            print 'Error : no slave found builder with tags %s' % tagset
            for slave in self._slaves:
                slave_tagset = set()
                for tag in slave.get_interpolated('_slave_tags', []):
                    slave_tagset.add(tag)
                args = (slave.get_interpolated('slave_name'),
                        tagset - slave_tagset)
                print 'Slave %s is missing tags %s' % args

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
        self._add_irc_status()

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

    def _add_irc_status(self):
        status = self._build_class(buildbot.status.words.IRC,
                                   'irc',
                                   positional=('server', 'nick', 'channels'))
        self.buildbot_config['status'].append(status)


    def _prioritize_builders(self, _, builders):
        def _get_priority(builder):
            return self._builders_scopes[builder.name].get('_builder_priority', 0)
        builders.sort(key=_get_priority, reverse=True)
        return builders

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
        self._nightly = None

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

    def trigger_nightly(self,
                        minute=None,
                        hour=None,
                        day_of_month=None,
                        month=None,
                        day_of_week=None):
        ''' Triggers this build nightly '''
        self._nightly = {'minute' : minute,
                         'hour' : hour,
                         'dayOfMonth': day_of_month,
                         'month' : month,
                         'dayOfWeek' : day_of_week}

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
               project=None,
               priority=None):
        ''' Sets builder config values '''
        Scope.set_checked('builder_name', name, basestring)
        Scope.set_checked('builder_category', category, basestring)
        Scope.set_checked('builder_builddir', build_dir, None)
        Scope.set_checked('builder_slavebuilddir', slave_build_dir, basestring)
        Scope.set_checked('builder_nextSlave', next_slave, None)
        Scope.set_checked('builder_nextBuild', next_build, None)
        Scope.set_checked('builder_canStartBuild', can_start_build, None)
        Scope.set_checked('builder_mergeRequests', merge_requests, None)
        Scope.set_checked('_force_scheduler_enabled', forcable, bool)
        Scope.set_checked('scheduler_onlyImportant', only_important, bool)
        Scope.set_checked('branch_scheduler_treeStableTimer', tree_stable_timer, int)
        Scope.set_checked('scheduler_fileIsImportant', file_is_important, None)
        Scope.set_checked('change_filter_project', project, basestring)
        Scope.set_checked('_builder_priority', priority, int)

    @staticmethod
    def mail_config(from_address=None,
                    send_to_interested_users=None,
                    subject=None,
                    mode=None,
                    add_logs=None,
                    relay_host=None,
                    smpt_port=None,
                    use_tls=None,
                    smtp_user=None,
                    smtp_password=None,
                    lookup=None,
                    message_formatter=None,
                    template_directory=None,
                    template=None,
                    body_type=None):
        ''' Sets mail related settings '''
        Scope.set_checked('mail_fromaddr', from_address, str)
        Scope.set_checked('mail_sendToInterestedUsers',
                          send_to_interested_users, bool)
        Scope.set_checked('mail_subject', subject, str)
        Scope.set_checked('mail_mode', mode, None)
        Scope.set_checked('mail_addLogs', add_logs, bool)
        Scope.set_checked('mail_relayhost', relay_host, str)
        Scope.set_checked('mail_smtpPort', smpt_port, int)
        Scope.set_checked('mail_useTls', use_tls, bool)
        Scope.set_checked('mail_smtpUser', smtp_user, str)
        Scope.set_checked('mail_smtpPassword', smtp_password, str)
        Scope.set_checked('mail_lookup', lookup, None)

        Scope.set_checked('_mail_message_formatter', message_formatter, None)
        Scope.set_checked('_mail_template_directory', template_directory, None)
        Scope.set_checked('_mail_template', template, None)
        Scope.set_checked('_mail_body_type', body_type, None)

    @staticmethod
    def add_extra_recipients(*emails):
        ''' Adds extra recipients to users '''
        Scope.append('mail_extraRecipients', *emails)

    @staticmethod
    def add_slave_tags(*tags):
        ''' Adds builder tags to current scope '''
        Scope.append('_builder_slave_tags', *tags)

    @staticmethod
    def add_env_variable(name, value):
        ''' Adds an envrionment variable to builders in scope '''
        Scope.update('builder_env', name, value)

    @staticmethod
    def add_tags(*tags):
        ''' Adds a tag to a builder '''
        Scope.append('builder_tags', *tags)

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
                                    raw=['builder_nextSlave'],
                                    additional=args)

        config.add_builder(builder, self)

        self._add_single_branch_scheduler(config)
        self._add_nightly_scheduler(config)
        self._add_force_scheduler(config)
        self._add_mail_status(config)

        parent_trigger = self.get_parent_of_type(Trigger)
        if parent_trigger is not None:
            parent_trigger.add_builder(self.get_interpolated('builder_name'))

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
                                      ('scheduler', 'branch_scheduler'),
                                      additional=args)
        config.buildbot_config['schedulers'].append(scheduler)

    def _add_nightly_scheduler(self, config):
        if self._nightly is None:
            return
        builder_name = self.get_interpolated('builder_name')
        args = {'name' : '%s nightly scheduler' % builder_name,
                'builderNames' : [builder_name],
                'branch' : None} #We don't use branches the way buildbot expects it
        for key, value in self._nightly.iteritems():
            if value is not None:
                args[key] = value

        scheduler_class = buildbot.schedulers.timed.Nightly
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

    def _add_mail_status(self, config):
        extra_recipients = self.get_interpolated('mail_extraRecipients')
        send_mail = self.get_interpolated('mail_sendToInterestedUsers')
        if extra_recipients or send_mail:

            formatter = self.get('_mail_message_formatter')
            if formatter is None:
                formatter = _HtmlMailFormatter(self)

            args = {'messageFormatter': formatter,
                    'builders': [self.get_interpolated('builder_name')]}

            mail_status = self._build_class(buildbot.status.mail.MailNotifier,
                                            'mail',
                                            additional=args)
            config.buildbot_config['status'].append(mail_status)

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
    def _build_change_sources(self, config, args):
        ''' Creates a ChangeSource for this repository '''

    def _build(self, config):
        args = self._get_prefixed_properties('change_source')
        for change_source in self._build_change_sources(config, args):

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

    def _build_change_sources(self, config, args):
        paths_to_poll = []
        for (depot_path, _) in self.get('p4_sync_p4viewspec', []):
            if depot_path.startswith('//'):
                # Get depot from //depot/
                base = depot_path[2:-1]
                paths_to_poll.append(base)

        for base in paths_to_poll:
            split_file = lambda branchfile: (None, branchfile)
            args['split_file'] = split_file
            args['p4base'] = '//' + base
            project_name = self.get_interpolated('project_name')
            if project_name is not None:
                args['project'] = project_name

            p4 = self._build_class(buildbot.changes.p4poller.P4Source,
                                   ('p4_common', 'p4_poll'),
                                   additional=args)
            yield p4

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
        assert mode in ['incremental', 'full', None]
        Scope.set_checked('sync_alwaysUseLatest', always_use_latest, bool)
        Scope.set_checked('sync_retry', retry, tuple)
        assert len(retry) == 2
        Scope.set_checked('sync_logEnviron', log_environ, bool)

    def _get_step(self, config, step_args):
        repos = self.get_interpolated('source_control_repositories')
        if repos is None or self._repo_name not in repos:
            msg = 'Unable to find repository %s in scope.' % self._repo_name
            raise Exception(msg)
        step_args.update(self._get_prefixed_properties('sync'))
        return repos[self._repo_name].get_sync_step(config, step_args)

class Command(Step):
    ''' Executes a shell command '''
    def __init__(self, name, command):
        super(Command, self).__init__(name)
        self._command = shlex.split(command.strip())

    @staticmethod
    def config(want_stdout=None, want_stderr=None, lazy_log_files=None,
               max_time=None, interrupt_signal=None, sigterm_time=None,
               initial_stdin=None):
        Scope.set_checked('shell_command_want_stdout', want_stdout, bool)
        Scope.set_checked('shell_command_want_stderr', want_stderr, bool)
        Scope.set_checked('shell_command_lazylogfiles', lazy_log_files, bool)
        Scope.set_checked('shell_command_maxTime', max_time, int)
        assert interrupt_signal in [None, 'KILL', 'TERM']
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
                                 rendered=['shell_command_logfiles'],
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

class Trigger(Step):
    ''' Triggers builders declared in child scope '''
    def __init__(self, name, *builder_names):
        super(Trigger, self).__init__(name)
        self._builder_names = []
        self._builder_names.extend(builder_names)
        self._nightly = None

    @staticmethod
    @contextlib.contextmanager
    def builder(name, category=None, description=None):
        ''' Helper to trigger a single builder '''
        with Trigger('%s-trigger' % name):
            with Builder(name, category, description) as builder:
                yield builder

    @staticmethod
    def config(wait_for_finish=None,
               always_use_latest=None):
        Scope.set_checked('trigger_waitForFinish', wait_for_finish, bool)
        Scope.set_checked('trigger_alwaysUseLatest', always_use_latest, bool)

    def nightly(self,
                minute=None,
                hour=None,
                day_of_month=None,
                month=None,
                day_of_week=None):
        ''' Uses a nightly triggerable instead of a triggerable '''
        self._nightly = {'minute' : minute,
                         'hour' : hour,
                         'dayOfMonth': day_of_month,
                         'month' : month,
                         'dayOfWeek' : day_of_week}

    def add_builder(self, builder_name):
        ''' Adds a builder to this trigger '''
        self._builder_names.append(builder_name)

    def _get_step(self, config, step_args):
        args = {'name' : '%s-scheduler' % self.get_interpolated('step_name'),
                'builderNames' : self._builder_names}

        if self._nightly is not None:
            for key, value in self._nightly.iteritems():
                if value is not None:
                    args[key] = value
            scheduler_class = buildbot.schedulers.timed.NightlyTriggerable
        else:
            scheduler_class = buildbot.schedulers.triggerable.Triggerable

        scheduler = scheduler_class(**args)

        config.buildbot_config['schedulers'].append(scheduler)

        step_args['schedulerNames'] = [scheduler.name]

        if 'workdir' in step_args:
            del step_args['workdir']
        return self._build_class(buildbot.steps.trigger.Trigger, 'trigger',
                                 additional=step_args)

def p4_email_lookup(scope):
    ''' Returns a callable to use in the 'lookup' argument of Builder
        mail_config that will get email from Perforce users '''
    class _Lookup(buildbot.util.ComparableMixin):
        zope.interface.implements(buildbot.interfaces.IEmailLookup)
        def __init__(self, port, user, password, p4bin):
            self._port = str(port)
            self._user = user
            self._password = password
            self._p4bin = p4bin if p4bin is not None else '/usr/local/bin/p4'

            assert isinstance(self._port, str)
            assert isinstance(self._user, str)
            assert isinstance(self._password, str)
            assert isinstance(self._p4bin, str)

            self._email_re = re.compile(r"Email:\s+(?P<email>\S+@\S+)\s*$")

        @twisted.internet.defer.deferredGenerator
        #pylint: disable=invalid-name,missing-docstring
        def getAddress(self, name):
            if '@' in name:
                yield name
                return

            args = []
            if self._port:
                args.extend(['-p', self._port])
            if self._user:
                args.extend(['-u', self._user])
            if self._password:
                args.extend(['-P', self._password])
            args.extend(['user', '-o', name])
            output = twisted.internet.utils.getProcessOutput(self._p4bin, args)
            deferred = twisted.internet.defer.waitForDeferred(output)
            yield deferred
            result = deferred.getResult()

            for line in result.split('\n'):
                line = line.strip()
                if not line:
                    continue
                match = self._email_re.match(line)
                if match:
                    yield match.group('email')
                    return

            yield name
    return _Lookup(scope.get_interpolated('p4_common_p4port'),
                   scope.get_interpolated('p4_common_p4user'),
                   scope.get_interpolated('p4_common_p4passwd'),
                   scope.get_interpolated('p4_poll_p4bin'))

class _Renderer(object):
    zope.interface.implements(buildbot.interfaces.IRenderable)

    def __init__(self, fmt, scope):
        self._fmt = fmt
        self._scope = scope

    def __repr__(self):
        return self._fmt

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

        for key, value in props.asDict().iteritems():
            format_vars[key] = value[0]

        format_vars['revisions'] = ' '.join([change.revision for change in props.getBuild().allChanges()])
        return self._fmt.format(**format_vars)

class _HtmlMailFormatter(object):
    def __init__(self, scope):
        self._scope = scope

    def __call__(self, _, name, build, results, master_status):
        body = ''
        template_directory = self._scope.get_interpolated('template_directory',
                                                          'templates')
        template = self._scope.get_interpolated('_mail_template',
                                                'mail_template.html')
        mail_type = self._scope.get_interpolated('_mail_type', 'html')
        (start, end) = build.getTimes()

        args = {'results_string' : buildbot.status.builder.Results[results],
                'build_slave' : build.getSlavename(),
                'name' : name,
                'build' : build,
                'cgi' : cgi,
                'master_status' : master_status,
                'start' : time.ctime(start),
                'end' : time.ctime(end),
                'elapsed' : buildbot.util.formatInterval(end - start)}
        try:
            loader = jinja2.FileSystemLoader(template_directory,
                                             encoding='utf-8')
            env = jinja2.Environment(loader=loader)
            template = env.get_template('mail_template.html')

            #pylint: disable=no-member
            body = template.render(**args)
        except Exception as ex:
            body = 'An exception occured during message rendering : %s' % ex.message
            raise ex
        return {'body' : body,
                'type' : mail_type}

