from . import format

from asyncio import async, coroutine
from asyncio.futures import Future
from grazyna.models import Message

from .request import RequestBot
from importlib import reload
from collections import defaultdict
from inspect import getcallargs
from datetime import datetime, timedelta
from sqlalchemy.sql.functions import func

import re
import traceback
import sys

re_split = re.compile(r' *(?:(\w+)= *)?(?:"([^"]+)"|(\S+))')  # parse args


class Plugin(list):

    __slots__ = ('temp', 'name', 'module_path', 'future', 'config')

    def __init__(self, name, module_path, funcs):
        super().__init__(funcs)
        self.temp = {}
        self.name = name
        self.module_path = module_path
        self.config = {}
        self.future = Future()

    def __repr__(self):
        return "Plugin('%s')" % self.name


class ExecutedCounter(object):

    __slots__ = ('last_time', 'counter')

    def __init__(self):
        self.last_time = datetime.now()
        self.counter = 0

    def inc(self):
        self.counter += 1


class ModuleManager(object):

    protocol = None
    plugins = None
    executed_counters = None

    def __init__(self, protocol):
        self.protocol = protocol
        self.plugins = {}
        self.executed_counters = defaultdict(ExecutedCounter)

    @property
    def config(self):
        return self.protocol.config

    def load_all(self):
        for name, module_path in self.config.items('plugins'):
            self.load(name, module_path)

    def cancel_tasks(self):
        for plugin in self.plugins.values():
            plugin.future.cancel()

    def load(self, name, module_path):
        path, module_name = module_path.rsplit(".", 1)
        module = __import__(module_path, globals(), locals(), [module_name])
        reload(module)
        self.plugins[name] = plugin = Plugin(
            name, module_path,
            [
                obj for obj in module.__dict__.values()
                if getattr(obj, 'is_bot_event', False) is True
            ]
        )

        cfg = self.config
        plugin_name = 'plugin:%s' % name
        if cfg.has_section(plugin_name):
            plugin.config.update(cfg.items(plugin_name))

        initials_funcs = (
            func for func in module.__dict__.values()
            if getattr(func, 'is_initial', False) is True
        )

        for initial_func in initials_funcs:
            initial_func(plugin, self.protocol)

        future = plugin.future
        loops = [
            func(self.protocol, plugin, future)
            for func in module.__dict__.values()
            if getattr(func, 'is_loop', False) is True
        ]

        for loop in loops:
            async(loop)

    def reload(self, name):
        plugin = self.plugins[name]
        plugin.future.cancel()
        self.load(name, plugin.module_path)

    def remove(self, name):
        plugin = self.plugins.pop(name)
        plugin.future.cancel()

    @coroutine
    def execute(self, channel, user, msg):
        cmd, text = self.get_cmd_and_text(msg)
        private = channel[0] != "#"

        if cmd is not None:
            yield from self.execute_command(cmd, text, private, channel, user)
        else:
            yield from self.execute_regs(msg, private, channel, user)

    @coroutine
    def execute_command(self, cmd, text, private, channel, user):
        plugin, func = self.find_command(cmd, private, channel)
        if func is None:
            if private:
                return
            yield from self.find_message_in_db(cmd, channel)
            return

        args, kwargs = get_args_from_text(text, func.max_args)
        yield from self.execute_func(
            func, plugin, private, channel, user, args, kwargs)


    @coroutine
    def find_message_in_db(self, cmd, channel):
        if not self.protocol.db:
            return
        print(cmd, channel)
        with self.protocol.get_session() as session:
            row = (
                session
                    .query(Message.message)
                    .filter_by(key=cmd, channel=channel)
                    .order_by(func.random())
                    .first()
            )
        if row is None:
            return
        return self.protocol.say(channel, row.message)


    @coroutine
    def execute_regs(self, msg, private, channel, user):
        for plugin, func, matches in self.find_regs(msg, private):
            for match, _ in zip(matches, range(3)):
                args = match.groups()
                kwargs = match.groupdict()
                yield from self.execute_func(
                    func, plugin, private, channel, user, args, kwargs)

    def find_function_in_plugin_with_name(self, private=None):
        return (
            (plugin, func)
            for name, plugin in self.plugins.items()
            for func in plugin
            if private is None
            or (not private and func.on_channel)
            or (private and func.on_private)
        )

    def find_command(self, cmd, private=None, channel=None):
        return next(
            (
                (plugin, func) for plugin, func
                in self.get_commands(private)
                if self.cmd_is_good(plugin, func, cmd, private, channel)
            ),
            (None, None)
        )

    def cmd_is_good(self, plugin, func, cmd=None, private=False, channel=None):
        cfg = self.get_plugin_cfg(plugin.name)

        if cmd is not None and func.cmd.format(**cfg) != cmd:
            return False

        if private or channel is None:
            return True

        def get_list(key):
            return [
                x for x in (x.strip() for x in cfg.get(key, '').split(','))
                if x
            ]

        whitelist = get_list('whitelist')
        if whitelist:
            return channel in whitelist

        blacklist = get_list('blacklist')
        return channel not in blacklist


    def get_commands(self, private=None):
        return (
            (plugin, func) for plugin, func
            in self.find_function_in_plugin_with_name(private)
            if func.is_reg is False
        )

    def find_regs(self, msg, private):
        return (
            (plugin, func, func.compiled_reg.finditer(msg))
            for plugin, func in self.find_function_in_plugin_with_name(private)
            if func.is_reg is True
        )

    def get_plugin_cfg(self, name):
        cfg = self.config
        conf = self.plugins[name].config.copy()
        conf.update(
            __nick__=cfg.get('main', 'nick')
        )

        return conf

    def get_cmd_and_text(self, msg):
        cfg = self.config['main']
        startswith = lambda key: msg.startswith(cfg[key])
        if startswith('command-prefix'):
            data = msg.split(maxsplit=1)
            cmd = data[0][1:]
        elif startswith('nick'):
            data = msg.split(maxsplit=2)[1:]
            cmd = data[0]
        else:
            return None, msg
        text = data[1] if len(data) > 1 else ""
        return cmd, text

    @coroutine
    def execute_func(self, func, plugin, private, channel, user, args, kwargs):
        if func.block and self.is_blocked(user):
            return  # filtr antyspamowy, nienawidze was

        bot = RequestBot(
            protocol=self.protocol,
            private=private,
            chan=channel if not private else None,
            config=self.get_plugin_cfg(plugin.name),
            plugin=plugin,
            user=user,
            temp=plugin.temp
        )

        try:
            dict_arg = check_type(args, kwargs, func)
        except Exception as e:
            print(type(e), e)
            return

        yield from self._execute_func(func, bot, dict_arg, user, channel)

    @coroutine
    def _execute_func(self, func, bot, args, user, channel):
        if func.admin_required:
            is_admin = yield from bot.is_admin()
            if not is_admin:
                return
        try:
            yield from func(bot, **args)
        except Exception:
            traceback.print_exc(file=sys.stdout)
            tb = traceback.format_exc().split('\n')[-4:-1]
            self.protocol.reply(
                user.nick,
                '{} {} {}'.format(
                    format.bold('ERR:'),
                    tb[2],
                    format.color(tb[0], format.color.red),
                ),
                channel
            )

    def is_blocked(self, user):
        now = datetime.now()
        delta = timedelta(
            seconds=self.config.getint('main', 'time_to_block')
        )

        self.executed_counters = defaultdict(ExecutedCounter, { # cleanups
            key: counter for key, counter in self.executed_counters.items()
            if now - counter.last_time < delta
        })

        nick = user.prefix
        counter = self.executed_counters[nick]

        executed_commands_per_time = self.config.getint(
            'main', 'executed_commands_per_time'
        )

        if counter.counter > executed_commands_per_time:
            return True
        else:
            counter.inc()
            return False


def get_args_from_text(text, max_args):
    if not text:
        return [], {}
    args = []
    kwargs = {}
    for arg_group in re_split.finditer(text):
        arg_name, arg_quotes, arg = arg_group.groups()
        arg = arg_quotes or arg

        if arg_name:
            kwargs[arg_name] = arg
        else:
            if max_args > -1:
                args.append(arg)

    if len(args) > max_args != -1:
        last_arg = ' '.join(args[max_args:])
        args = args[0:max_args]
        try:
            args[-1] += ' ' + last_arg
        except IndexError:
            args = [last_arg]

    return args, kwargs


def check_type(args, kwargs, func):
    real_func = getattr(func, '__wrapped__', func)
    dict_arg = getcallargs(real_func, None, *args, **kwargs)
    for key, item in dict_arg.items():
        arg_type = real_func.__annotations__.get(key, str)
        if item is not None:
            dict_arg[key] = arg_type(item)
    del dict_arg['bot']
    return dict_arg
