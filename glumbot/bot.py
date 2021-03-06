import re
import sys
import json
import uuid
import logging
import inspect
import argparse
import traceback
import importlib.util
from enum import Enum
from pathlib import Path
from pydoc import locate as locate_type
from jsonschema import validate as validate_json

from glumbot.config import Config
from glumbot.logger import init as init_logger
from glumbot.integrations import setup as setup_integrations
from glumbot.nlp_qa.model import Model as NLPModel, ModelType as NLPModelType
import glumbot.commands.builtin
import twitchio.dataclasses
import twitchio.ext.commands

class BotArgumentParser(argparse.ArgumentParser):
    def _get_action_from_name(self, name):
        '''
        Given a name, get the Action instance registered with this parser.
        If only it were made available in the ArgumentError object. It is 
        passed as it's first arg...
        '''

        container = self._actions
        if name is None:
            return None
        for action in container:
            if '/'.join(action.option_strings) == name:
                return action
            elif action.metavar == name:
                return action
            elif action.dest == name:
                return action

    def error(self, message):
        exception = sys.exc_info()[1]
        if exception:
            exception.argument = self._get_action_from_name(exception.argument_name)
            raise exception
        else:
            raise argparse.ArgumentError(None, message)

class CommandPermissionLevel(Enum):
    '''
    Permission levels for command usage.
    '''

    Everyone = 1,
    Follower = 2,
    Subscriber = 3,
    Moderator = 4,
    Editor = 5,
    Caster = 6

    @classmethod
    def get_names_for_schema(cls):
        def to_snake_case(name):
            s1 = re.sub(r'(.)([A-Z][a-z]+)', r'\1_\2', name)
            return re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', s1).lower()
        
        return [to_snake_case(value.name) for value in CommandPermissionLevel]

    @classmethod
    def from_snake_case_name(cls, name):
        name = ''.join(x.capitalize() or '_' for x in name.split('_'))
        return cls[name]

class Bot(twitchio.ext.commands.Bot):
    _CONFIG_DEFAULTS = {
        'INSTANCE_PATH': str(Path('./instance').resolve().absolute()),
        'APP_NAME': __name__,
        'PREFIX': '!',
        'DISPLAY_MESSAGES': True,
        'DISPLAY_OWN_MESSAGES': False,
        'USE_NLP_QA': False,
        'NLP_QA_PREFIX': '>',
        'NLP_QA_DATA_FORMAT': 'csv',
        'NLP_MODEL_TYPE': NLPModelType.FastText,
        'SPOTIFY_AUTH_CACHE_FILENAME': 'spotify_auth.cache'
    }

    _COMMANDS_SCHEMA = {
        'type': 'array',
        'items': {'type': 'object'}
    }

    _COMMAND_SCHEMA = {
        'type': 'object',
        'properties': {
                'name': {'type': 'string'},
                'description': {'type': 'string'},
                'args': {'type': 'object'},
                'response': {'type': 'string'},
                'script': {'type': 'string'},
                'aliases': {
                    'type': 'array',
                    'items': {
                        'type': 'string'
                    }
                },
                'parameters': {'type': 'object'},
                'execute_function': {
                    'type': 'string',
                    'minLength': 1
                },
                'permission': {
                    'type': 'string',
                    'enum': CommandPermissionLevel.get_names_for_schema()
                }
            },
        'required': ['name'],
        'anyOf': [
            {'required': ['response']},
            {'required': ['script']}
        ]
    }

    _COMMAND_ARG_SCHEMA = {
        'type': 'object',
        'properties': {
            'type': {'type': 'string'},
            'help': {'type': 'string'},
            'nargs': {
                'anyOf': [
                    { 'type': 'integer' },
                    { 
                        'type': 'string',
                        'enum': ['?', '*', '+', 'REMAINDER']
                    }
                ]
            },
            'default': {}
        }
    }

    def __init__(self):
        '''
        Initialize the bot.
        '''
        
        self.config = Config(Bot._CONFIG_DEFAULTS)
        self.cogs = {}
        self._caster_users_cache = {}

    def init_logger(self):
        '''
        Initialize the logger.
        '''

        self.logger = init_logger(self.config['APP_NAME'])

    def run(self):
        '''
        Setup and run the bot.
        '''

        self._load_commands()
        self._load_nlp_qa()
        
        setup_integrations(self)

        super().__init__(
            irc_token=self.config['IRC_TOKEN'], 
            client_id=self.config['CLIENT_ID'], 
            nick=self.config['NICK'], 
            prefix=self.config['PREFIX'], 
            initial_channels=self.config['INITIAL_CHANNELS']
        )

        super().run()
    
    def _load_commands(self):
        '''
        Load and initialize commands from JSON.
        '''

        builtin_path = str(Path(glumbot.commands.builtin.__file__).parent.resolve().absolute())

        # The command_json_path is relative to the working directory, NOT the instance folder.
        command_json_path = Path(self.config['COMMANDS_FILE'])
        count = 0
        with open(command_json_path, 'r') as file:
            commands = json.load(file)
            command_filepath_str = str(command_json_path.resolve())

            try:
                validate_json(instance=commands, schema=Bot._COMMANDS_SCHEMA)
            except:
                error_message = 'Could not load commands from file \'{}\'.'
                self.logger.exception(error_message.format(command_filepath_str))

            for command in commands:
                try:
                    validate_json(instance=command, schema=Bot._COMMAND_SCHEMA)
                except:
                    error_message = 'Could not load command with name \'{}\' from file \'{{}}\''.format(command['name']) if 'name' in command \
                        else 'Could not load command from file \'{}\'.'
                        
                    self.logger.exception(error_message.format(command_filepath_str))
                    continue

                command_id = str(uuid.uuid4()).replace('-', '_')
                method_name = 'commandhandler_{}_{}'.format(command['name'], command_id)

                script_module = None
                execute_fname = command.get('execute_function', 'execute')

                if 'script' in command:
                    # The script path is relative to the command JSON file...
                    script_path = (command_json_path.parent / Path(command['script'].format(BUILTIN_PATH=builtin_path))).resolve().absolute()
                    if not script_path.exists():
                        self.logger.warn('Encountered error in processing custom script for command with name \'{}\'. \
                            Could not find the file at path \'{}\'.'.format(command['name'], str(script_path)))
                        continue
                        
                    spec = importlib.util.spec_from_file_location(script_path.stem + ''.join(script_path.suffixes), str(script_path))
                    script_module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(script_module)

                    # Verify that the execute method has a valid signature...
                    if hasattr(script_module, execute_fname):
                        valid = True
                        execute_func = getattr(script_module, execute_fname)
                        if len(inspect.signature(execute_func).parameters) < 3:
                            self.logger.warn('Encountered error in processing custom script for command with name \'{}\'. ' +
                                'The \'{}\' method has an invalid signature. At least two parameters are required for the class ' +
                                'instance and context parameters; \'self\', \'ctx\', and \'parameters\' respectively.' \
                                    .format(command['name'], execute_fname))

                            valid = False
                        elif not inspect.iscoroutinefunction(execute_func):
                            self.logger.warn('Encountered error in processing custom script for command with name \'{}\'. ' +
                                'The \'{}\' method must be a coroutine function (a function defined with an async def syntax).' \
                                    .format(command['name'], execute_fname))
                                    
                            valid = False

                        if not valid:
                            delattr(script_module, execute_fname)
                    else:
                        self.logger.warn('Encountered error in processing custom script for command with name \'{}\'. '
                            'The \'{}\' method could not be found.'.format(command['name'], execute_fname))

                command_args = command.get('args', dict())
                command_argparser = BotArgumentParser(description=command.get('description', None))
                for arg_name in command_args:
                    arg = command_args[arg_name]
                    try:
                        validate_json(instance=arg, schema=Bot._COMMAND_ARG_SCHEMA)
                    except:
                        self.logger.exception('Could not setup command argument with name \'{}\', from command with name \'{}\''.format(arg_name, command['name']))
                        continue

                    # TODO: Add more extensive implementation of argparse (i.e. support more features)...
                    arg_type = locate_type(arg['type']) if 'type' in arg else None
                    nargs = None
                    if 'nargs' in arg:
                        nargs = argparse.REMAINDER if arg['nargs'] == 'REMAINDER' else arg['nargs']
                    
                    command_argparser.add_argument(arg_name, type=arg_type, nargs=nargs, help=arg.get('help', None))
                    if 'default' in arg:
                        command_argparser.set_defaults(**{arg_name: arg['default']})

                def create_command_func(command, script_module, execute_fname, argparser):
                    async def _command_handler(self, ctx, *args):
                        try:
                            known_args, _ = argparser.parse_known_args(args)
                        except argparse.ArgumentError as exception:
                            await ctx.send(exception.message)
                            return

                        if 'response' in command:
                            await ctx.send(command['response'])

                        if script_module and hasattr(script_module, execute_fname):
                            parameters = command['parameters'] if 'parameters' in command else None
                            await getattr(script_module, execute_fname)(self, ctx, parameters, known_args)

                    return _command_handler

                def create_permission_check(permission_level, bot):
                    async def _permission_check(ctx):
                        if permission_level == CommandPermissionLevel.Everyone: return True
                        if permission_level == CommandPermissionLevel.Follower:
                            caster_user = await bot.get_caster_user(ctx.message.channel)
                            follow_relationship = await bot.get_follow(ctx.author.id, caster_user.id)
                            return follow_relationship is not None

                        if permission_level == CommandPermissionLevel.Subscriber: return ctx.author.is_subscriber()
                        if permission_level == CommandPermissionLevel.Moderator: return ctx.author.is_mod
                        if permission_level == CommandPermissionLevel.Editor:
                            raise NotImplementedError('The command permission level \'Editor\' is not implemented.')

                        if permission_level == CommandPermissionLevel.Caster:
                            caster_user = await bot.get_caster_user(ctx.message.channel)
                            return caster_user.id == ctx.author.id

                    return _permission_check 

                command_object = twitchio.ext.commands.Command(
                    name=command['name'], 
                    func=create_command_func(command, script_module, execute_fname, command_argparser), 
                    aliases=command['aliases'] if 'aliases' in command else None, 
                    no_global_checks=False
                )
                
                command_permission = CommandPermissionLevel.from_snake_case_name(command.get('permission', 'everyone'))
                command_object._checks.append(create_permission_check(command_permission, self))

                setattr(self.__class__, method_name, command_object)
                count += 1

        self.logger.info('Finished loading commands ({}).'.format(count))

    def _load_nlp_qa(self):
        if not self.config['USE_NLP_QA']: return

        data_path = str(Path(self.config['NLP_QA_DATA_PATH']).resolve().absolute())
        self.nlp_qa_model = NLPModel(self.config['NLP_MODEL_TYPE'], data_path, self.config['NLP_QA_DATA_FORMAT'])
        self.logger.info('Finished building NLP QA model.')

    async def event_ready(self):
        '''
        Raised when the bot is ready.
        '''
    
        self.logger.info('Twitch bot is ready!')
    
    async def event_message(self, message):
        '''
        Raised when a new message is sent in one of the connected channels.
        '''

        if self.config['DISPLAY_MESSAGES']:
            is_own_message = message.author.name.lower() == self.config['NICK'].lower()
            display_own = self.config['DISPLAY_OWN_MESSAGES']
            if display_own or not display_own and not is_own_message:
                self.logger.info('{}: {} (in #{})'.format(message.author.name, message.content, message.channel))

        if self.config['USE_NLP_QA'] and message.content.startswith(self.config['NLP_QA_PREFIX']):
            clean_message = message.content[1:].strip()
            if not clean_message: return

            ctx = await self.get_context(message)

            prediction, confidence = self.nlp_qa_model.predict([clean_message])
            self.logger.info('Matched NLP query (message = "{}", prediction = \"{}\", confidence = {:.3f})' \
                .format(clean_message, prediction, confidence))

            await self.handle_commands(twitchio.dataclasses.Message(
                author=message.author,
                channel=message.channel,
                content=prediction,
                clean_content=prediction
            ))
        else:
            await self.handle_commands(message)

    async def handle_commands(self, message):
        try:
            await super().handle_commands(message)
        except twitchio.ext.commands.errors.CommandError:
            pass

    async def event_command_error(self, ctx, error):
        '''
        Raised on an error in command execution.
        '''
        
        is_checkfailure = isinstance(error, twitchio.ext.commands.errors.CheckFailure)
        self.logger.log(logging.DEBUG if is_checkfailure else logging.ERROR, 'Excpetion in command: {0}:'.format(error))
        if not is_checkfailure:
            traceback.print_exception(type(error), error, error.__traceback__)

    async def get_caster_user(self, channel: [str, twitchio.dataclasses.Channel]):
        '''
        Gets the user object associated with the specified channel name.
        '''

        channel = str(channel)
        if channel in self._caster_users_cache:
            return self._caster_users_cache[channel]

        user = await self.get_users(channel)
        if user is None or len(user) == 0:
            raise Exception('Could not locate user information given the channel name \'{}\''.format(channel))

        user = user[0]
        self._caster_users_cache[channel] = user
        return user