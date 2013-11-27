#!/usr/bin/env python3
import asyncio
import signal
import logging

from asyncio.futures import Future
from asyncio.queues import Queue
from asyncio.tasks import gather
from asyncio.log import logger

from collections import deque
from functools import wraps
from inspect import getfullargspec, formatargspec, getcallargs

loop = asyncio.get_event_loop()
loop.add_signal_handler(signal.SIGINT, loop.stop)


__all__ = (
    'RedisProtocol',
    'Connection',
    'Transaction',
    'RedisException',
    'StatusReply',
    'MultiBulkReply',
)
__author__ = 'Jonathan Slenders'
__doc__ = \
"""
Redis protocol implementation for asyncio (PEP 3156)
"""

NoneType = type(None)
SetType = set

class RedisException(Exception):
    pass

class StatusReply:
    """
    Wrapper for Redis status replies.
    (for messages like OK, QUEUED, etc...)
    """
    def __init__(self, status):
        self.status = status

    def __repr__(self):
        return 'StatusReply(status=%r)' % self.status

    def __eq__(self, other):
        return self.status == other.status


class MultiBulkReply:
    """
    Container for a multi bulk reply.
    There are two ways of retrieving the content:

    1. you call ``yield from get_as_list()`` which returns when all the
       items arrived;

    2. or you iterate over it for every item, using this pattern:

    ::

        for f in multi_bulk_reply:
            item = yield from f
            print(item)
    """
    def __init__(self, count):
        self.queue = Queue()
        self.count = count

    def __iter__(self):
        for i in range(self.count):
            yield self.queue.get()

    def get_as_list(self):
        """
        Wait for all of the items of the multibulk reply to come in.
        and return it as a list.
        """
        return gather(*list(self))

class ZRangeResult:
    """
    Container for a zrange query result.
    """
    def __init__(self, multibulk_reply):
        self._result = multibulk_reply

    def __iter__(self):
        """ Yield a list of futures that yield {key: score_as_float} tuples. """
        i = iter(self._result)

        @asyncio.coroutine
        def getter(key_f, score_f):
            """ Coroutine which processes one item. """
            key, score = yield from gather(key_f, score_f)
            return { key: float(score) }

        while True:
            yield asyncio.Task(getter(next(i), next(i)))

    @asyncio.coroutine
    def get_as_dict(self):
        result = { }
        for f in self:
            result.update((yield from f))
        return result

    @asyncio.coroutine
    def get_as_list(self):
        result = []
        for f in self:
            result += (yield from f).keys()
        return result


class ZScoreBoundary:
    """
    Score boundary for a sorted set.
    for queries like zrangebyscore and similar

    :param value: Value for the boundary.
    :param type: float
    """
    def __init__(self, value, exclude_boundary=False):
        assert isinstance(value, float) or value in ('+inf', '-inf')
        self.value = value
        self.exclude_boundary = exclude_boundary

    def __repr__(self):
        return 'ZScoreBoundary(value=%r, exclude_boundary=%r)' % (
                    self.value, self.exclude_boundary)

ZScoreBoundary.MIN_VALUE = ZScoreBoundary('-inf')
ZScoreBoundary.MAX_VALUE = ZScoreBoundary('+inf')


class PipelinedCall:
    """ Track record for call that is being executed in a protocol. """
    def __init__(self, cmd, is_blocking):
        self.cmd = cmd
        self.is_blocking = is_blocking


class _PostProcessor:
    @asyncio.coroutine
    def multibulk_as_list(multibulk):
        assert isinstance(multibulk, MultiBulkReply)
        return (yield from multibulk.get_as_list())

    @asyncio.coroutine
    def multibulk_as_set(multibulk):
        assert isinstance(multibulk, MultiBulkReply)
        lst = yield from multibulk.get_as_list()
        return set(lst)

    @asyncio.coroutine
    def int_to_bool(result):
        assert isinstance(result, int)
        return bool(result) # Convert int to bool

    @asyncio.coroutine
    def multibulk_as_zrangeresult(result):
        assert isinstance(result, MultiBulkReply)
        return ZRangeResult(result)

    @asyncio.coroutine
    def str_to_float(result):
        assert isinstance(result, str)
        return float(result)


# List of all command methods.
_all_commands = []

def _command(method):
    """
    Wrapper for all redis commands.
    This will also keep track of all the available commands and do type
    checking.
    """
    # Register command.
    _all_commands.append(method.__name__)

    # Read specs
    specs = getfullargspec(method)
    return_type = specs.annotations.get('return', None)
    params = { k:v for k, v in specs.annotations.items() if k != 'return' }

    def typecheck_input(*a, **kw):
        if params:
            for name, value in getcallargs(method, None, *a, **kw).items():
                if name in params and not isinstance(value, params[name]):
                    raise TypeError('%s received %r, expected %r' %
                                    (method.__name__, type(value), params[name]))

    def typecheck_return(result):
        if return_type:
            if not isinstance(result, return_type):
                raise TypeError('Got unexpected return type %r in %s, expected %r' %
                                (type(result), method.__name__, return_type))

    # Wrap it into a check which allows this command to be run either directly
    # on the protocol, outside of transactions or from the transaction object.
    @wraps(method)
    def wrapper(self, *a, **kw):
        # When calling from a transaction, the first arg is the transaction object.
        if self.in_transaction and (len(a) == 0 or a[0] != self._transaction):
            raise RedisException('Cannot run command inside transaction')
        elif self.in_transaction:
            # In case of a transaction, we receive a Future
            typecheck_input(*a[1:], **kw)
            future = yield from method(self, *a[1:], **kw)

            # Typecheck the future when the result is available.
            @future.add_done_callback
            def callback(result):
                typecheck_return(result.result())

            return future
        else:
            typecheck_input(*a, **kw)
            result = yield from method(self, *a, **kw)
            typecheck_return(result)
            return result

    # Append the real signature as the first line in the docstring.
    # (This will make the sphinx docs show the real signature instead of
    # (*a, **kw) of the wrapper.)
    # (But don't put the anotations inside the copied signature, that's rather
    # ugly in the docs.)
    signature = formatargspec(* specs[:6])

    # Use function annotations to generate param documentation.

    def get_name(type):
        if type == MultiBulkReply:
            return ":class:`asyncio_redis.MultiBulkReply`"
        elif type == StatusReply:
            return ":class:`asyncio_redis.StatusReply`"
        elif type == ZRangeResult:
            return ":class:`asyncio_redis.ZRangeResult`"
        elif type == ZScoreBoundary:
            return ":class:`asyncio_redis.ZScoreBoundary`"
        if isinstance(type, tuple):
            return ' or '.join("``%s``" % t.__name__ for t in type)
        else:
            return "``%s``" % type.__name__

    def get_param(k, v):
        return ':param %s: %s\n' % (k, get_name(v))

    params_str = [ get_param(k, v) for k, v in params.items() ]
    returns = ':returns: (Future of) %s\n' % get_name(return_type) if return_type else ''

    wrapper.__doc__ = '%s%s\n%s\n\n%s%s' % (
            method.__name__, signature,
            method.__doc__,
            ''.join(params_str),
            returns
            )

    return wrapper


class RedisProtocol(asyncio.Protocol):
    """
    The Redis Protocol implementation.

    ::

        self.loop = asyncio.get_event_loop()
        transport, protocol = yield from loop.create_connection(RedisProtocol, 'localhost', 6379)
    """
    encoding = 'utf-8'
    """
    Redis keeps all values in binary. Set the encoding to be used to
    decode/encode Python string values from and to binary.
    """

    password = None
    """
    Password to be send using the "AUTH" command when a connection has been
    established.
    """

    db = 0
    """
    Database to connect using "SELECT" when a connection has been established.
    """

    def connection_made(self, transport):
        self.transport = transport
        self._queue = deque() # Input parser queues
        self._messages_queue = None # Pubsub queue

        # Input parser state
        self._buffer = b''
        self._in_bulk_reply = False
        self._bulk_reply_len = 0
        self._bulk_reply_buffer = b''

        # State
        self._in_pubsub = False
        self._pipelined_calls = set() # Set of all the pipelined calls.

        # Transaction related stuff.
        self._in_transaction = False
        self._transaction = None
        self._transaction_response_queue = None # Transaction answer queue

        # If a password or database was been given, first connect to that one.
        if self.password:
            asyncio.Task(self._auth(self.password))

        if self.db:
            asyncio.Task(self._select(self.db))

        logger.log(logging.INFO, 'Redis connection made')

    def data_received(self, data):
        """ Process data received from Redis server.  """
        self._buffer += data

        while b'\r\n' in self._buffer:
            line, self._buffer = self._buffer.split(b'\r\n', 1)

            if not self._in_bulk_reply:
                self._line_received(line)
            else:
                self._handle_bulk_reply_part(line)

    def _encode(self, data:str) -> bytes:
        """ Encodes unicode data to bytes. """
        return data.encode(self.encoding)

    def _encode_int(self, value:int) -> bytes:
        """ Encodes an integer to bytes. """
        return str(value).encode('ascii')

    def _encode_float(self, value:float) -> bytes:
        """ Encodes a float to bytes. """
        return str(value).encode('ascii')

    def _encode_zscore_boundary(self, value:ZScoreBoundary) -> str:
        if isinstance(value.value, str):
            return self._encode(value.value) # +inf and -inf
        elif value.exclude_boundary:
            return self._encode("(%f" % value.value)
        else:
            return self._encode("%f" % value.value)

    def _decode(self, data:bytes) -> str:
        """ Decodes bytes to unicode. """
        return data.decode(self.encoding)

    def _line_received(self, line):
#        print ('line received', line)
        first_byte, line = line[:1], line[1:]

        if first_byte == b'+':
            self._handle_status_reply(line)

        elif first_byte == b'-':
            self._handle_error_reply(line)

        elif first_byte == b'$':
            self._handle_bulk_reply(line)

        elif first_byte == b'*':
            self._handle_multi_bulk_reply(line)

        elif first_byte == b':':
            self._handle_int_reply(line)

        else:
            print ('err...', line)

    def eof_received(self):
        print ('***** EOF received ******')

    def connection_lost(self, exc):
        print('connection lost:', exc)

        # Raise exception on all waiting futures.
        while self._queue:
            f = self._queue.popleft()
            f.set_exception(RedisException('Connection lost: %s' % exc))

        logger.log(logging.INFO, 'Redis connection lost')

    # Request state

    @property
    def in_blocking_call(self): # TODO: unittest
        """ True when waiting for answer to blocking command. """
        return any(c.is_blocking for c in self._pipelined_calls)

    @property
    def in_pubsub(self):
        """ True when the protocol is in pubsub mode. """
        return self._in_pubsub

    @property
    def in_transaction(self):
        """ True when we're inside a transaction. """
        return self._in_transaction

    @property
    def in_use(self):
        """ True when this protocol is in use. """
        return self.in_blocking_call or self.in_pubsub or self.in_transaction

    # Handle replies

    def _handle_status_reply(self, line):
        self._push_answer(StatusReply(line.decode('ascii')))

    def _handle_int_reply(self, line):
        self._push_answer(int(line))

    def _handle_error_reply(self, line):
        self._push_answer(RedisException(line))

    def _handle_bulk_reply(self, line):
        if int(line) == -1:
            # Null bulk reply
            self._push_answer(None)
        else:
            self._in_bulk_reply = True
            self._bulk_reply_len = int(line)
            self._bulk_reply_buffer = b''

    def _handle_bulk_reply_part(self, line):
        self._bulk_reply_buffer += line
        self._bulk_reply_buffer += b'\r\n'

        if len(self._bulk_reply_buffer) > self._bulk_reply_len:
            value = self._decode(self._bulk_reply_buffer[:self._bulk_reply_len])

            # Bulk reply came in.
            self._push_answer(value)

            self._in_bulk_reply = False
            self._bulk_reply_len = 0
            self._bulk_reply_buffer = b''

    def _handle_multi_bulk_reply(self, line):
        count = int(line)

        # Create a queue for receiving the multi-bulk reply
        reply = MultiBulkReply(count)

        # Return the empty queue immediately as an answer.
        if self._in_pubsub:
            asyncio.Task(self._handle_pubsub_multibulk_reply(reply))
        else:
            self._push_answer(reply)

        # Create futures to be inserted before other replies.
        futures = [ Future() for f in range(count) ]
        for f in futures[::-1]:
            self._queue.appendleft(f)

        @asyncio.coroutine
        def handle_results():
            # Wait for the next N items to come in, copy
            for f in futures:
                # Wait for the next item to be received
                item = yield from f

                # Put it on the queue (Don't wait here -- we don't even know
                # whether the receiving end still reads the output.)
                reply.queue.put_nowait(item)

        asyncio.Task(handle_results())

    @asyncio.coroutine
    def _handle_pubsub_multibulk_reply(self, multibulk_reply):
        result = yield from multibulk_reply.get_as_list()
        assert result[0] == u'message'
        yield from self._messages_queue.put(result)

    # Redis operations.

    def _send_command(self, *args):
        """
        Send Redis request command.
        """
#        print('send: ', *args)
        self.transport.write((u'*%i\r\n' % len(args)).encode('ascii'))

        for a in args:
            if isinstance(a, bytes):
                self.transport.write((u'$%i\r\n' % len(a)).encode('ascii'))
                self.transport.write(a)
                self.transport.write(b'\r\n')
            else:
                raise RedisException('Cannot encode %r' % type(a))

    @asyncio.coroutine
    def _get_answer(self, _bypass=False, post_process_func=None, call=None):
        """
        Return an answer to the pipelined query.
        (Or when we are in a transaction, return a future for the answer.)
        """
        # Add a new future to our queue.
        f = Future()
        self._queue.append(f)
        result = yield from f

        if self._in_transaction and not _bypass:
            # When the connection is inside a transaction, the query will be queued.
            if result != StatusReply('QUEUED'):
                raise RedisException('Expected to receive QUEUED for query in transaction, received %r.' % result)

            # Return a future which will contain the result when it arrives.
            f = Future()
            self._transaction_response_queue.append( (f, post_process_func, call) )
            return f
        else:
            if post_process_func:
                result = yield from post_process_func(result)
            if call:
                self._pipelined_calls.remove(call)
            return result

    def _push_answer(self, answer):
        """
        Answer future at the queue.
        """
        f = self._queue.popleft()

        if isinstance(answer, Exception):
            f.set_exception(answer)
        else:
            f.set_result(answer)

    @asyncio.coroutine
    def _query(self, *args, _bypass=False, post_process_func=None, set_blocking=False):
        """ Wrapper around both _send_command and _get_answer. """
        call = PipelinedCall(args[0], set_blocking)
        self._pipelined_calls.add(call)

        # Send command
        self._send_command(*args)

        # Receive answer.
        result = yield from self._get_answer(_bypass=_bypass, post_process_func=post_process_func, call=call)
        return result

    # Internal

    def _auth(self, password):
        return self._query(b'auth', self._encode(password))

    def _select(self, db:int):
        return self._query(b'select', self._encode_int(db))

    # Strings

    @_command
    def set(self, key:str, value:str) -> StatusReply:
        """ Set the string value of a key """
        return self._query(b'set', self._encode(key), self._encode(value))

    @_command
    def get(self, key:str) -> (str, NoneType):
        """ Get the value of a key """
        return self._query(b'get', self._encode(key))

    @_command
    def mget(self, *keys) -> list:
        """ Returns the values of all specified keys. """
        return self._query(b'mget', *map(self._encode, keys),
                post_process_func=_PostProcessor.multibulk_as_list)

    @_command
    def strlen(self, key:str) -> int:
        """ Returns the length of the string value stored at key. An error is
        returned when key holds a non-string value.  """
        return self._query(b'strlen', self._encode(key))

    @_command
    def append(self, key:str, value:str) -> int:
        """ Append a value to a key """
        return self._query(b'append', self._encode(key), self._encode(value))

    @_command
    def getset(self, key:str, value:str):
        """ Set the string value of a key and return its old value """
        return self._query(b'getset', self._encode(key), self._encode(value))

    @_command
    def incr(self, key:str) -> int:
        """ Increment the integer value of a key by one """
        return self._query(b'incr', self._encode(key))

    @_command
    def incrby(self, key:str, increment:int) -> int:
        """ Increment the integer value of a key by the given amount """
        return self._query(b'incrby', self._encode(key), self._encode_int(increment))

    @_command
    def decr(self, key:str) -> int:
        """ Decrement the integer value of a key by one """
        return self._query(b'decr', self._encode(key))

    @_command
    def decrby(self, key:str, increment:int) -> int:
        """ Decrement the integer value of a key by the given number """
        return self._query(b'decrby', self._encode(key), self._encode_int(increment))

    @_command
    def randomkey(self) -> str:
        """ Return a random key from the keyspace """
        return self._query(b'randomkey')

    @_command
    def exists(self, key:str) -> bool:
        """ Determine if a key exists """
        return self._query(b'exists', self._encode(key),
                post_process_func=_PostProcessor.int_to_bool)

    @_command
    def delete(self, *keys) -> int:
        """ Delete a key """
        return self._query(b'del', *map(self._encode, keys))

    @_command
    def move(self, key:str, database:int) -> int:
        """ Move a key to another database """
        return self._query(b'move', self._encode(key), self._encode(destination)) # TODO: test

    @_command
    def rename(self, key:str, newkey:str) -> StatusReply:
        """ Rename a key """
        return self._query(b'rename', self._encode(key), self._encode(newkey))

    @_command
    def renamenx(self, key:str, newkey:str) -> int:
        """ Rename a key, only if the new key does not exist
        (Returns 1 if the key was successfully renamed.) """
        return self._query(b'renamenx', self._encode(key), self._encode(newkey))

    @_command
    def getbit(self, key:str, offset):
        """ Returns the bit value at offset in the string value stored at key """
        raise NotImplementedError

    @_command
    def bitop_and(self, destkey:str, *srckeys) -> int:
        """ Perform a bitwise AND operation between multiple keys. """
        return self._bitop(b'and', destkey, srckeys)

    @_command
    def bitop_or(self, destkey:str, *srckeys) -> int:
        """ Perform a bitwise OR operation between multiple keys. """
        return self._bitop(b'or', destkey, srckeys)

    @_command
    def bitop_xor(self, destkey:str, *srckeys) -> int:
        """ Perform a bitwise XOR operation between multiple keys. """
        return self._bitop(b'xor', destkey, srckeys)

    def _bitop(self, op, destkey, srckeys):
        return self._query(b'bitop', op, self._encode(destkey), *map(self._encode, srckeys))

    @_command
    def bitop_not(self, destkey:str, key:str) -> int:
        """ Perform a bitwise NOT operation between multiple keys. """
        return self._query(b'bitop', b'not', self._encode(destkey), self._encode(key))

    @_command
    def bitcount(self, key:str, start:int=0, end:int=-1):
        """ Count the number of set bits (population counting) in a string. """
        return self._query(b'bitcount', self._encode(key), self._encode_int(start), self._encode_int(end))

    @_command
    def getbit(self, key:str, offset:int) -> bool:
        """ Returns the bit value at offset in the string value stored at key """
        return self._query(b'getbit', self._encode(key), self._encode_int(offset),
                     post_process_func=_PostProcessor.int_to_bool)

    @_command
    def setbit(self, key:str, offset:int, value:bool) -> bool:
        """ Sets or clears the bit at offset in the string value stored at key """
        return self._query(b'setbit', self._encode(key), self._encode_int(offset),
                    self._encode_int(int(value)), post_process_func=_PostProcessor.int_to_bool)

    # Keys

    @_command
    def keys(self, pattern:str) -> MultiBulkReply:
        """
        Find all keys matching the given pattern.
        """
        return self._query(b'keys', self._encode(pattern))

    @_command
    def dump(self, key:str):
        """ Return a serialized version of the value stored at the specified key. """
        # Dump does not work yet. It shouldn't be decoded using utf-8'
        raise NotImplementedError('Not supported.')

    @_command
    def expire(self, key:str, seconds:int) -> int:
        """ Set a key's time to live in seconds """
        return self._query(b'expire', self._encode(key), self._encode_int(seconds))

    @_command
    def pexpire(self, key:str, milliseconds:int) -> int:
        """ Set a key's time to live in milliseconds """
        return self._query(b'pexpire', self._encode(key), self._encode_int(milliseconds))

    @_command
    def expireat(self, key:str, timestamp:int) -> int:
        """ Set the expiration for a key as a UNIX timestamp """
        return self._query(b'expireat', self._encode(key), self._encode_int(timestamp))

    @_command
    def pexpireat(self, key:str, milliseconds_timestamp:int) -> int:
        """ Set the expiration for a key as a UNIX timestamp specified in milliseconds """
        return self._query(b'pexpireat', self._encode(key), self._encode_int(milliseconds_timestamp))

    @_command
    def persist(self, key:str) -> int:
        """ Remove the expiration from a key """
        return self._query(b'persist', self._encode(key))

    @_command
    def ttl(self, key:str) -> int:
        """ Get the time to live for a key """
        return self._query(b'ttl', self._encode(key))

    @_command
    def pttl(self, key:str) -> int:
        """ Get the time to live for a key in milliseconds """
        return self._query(b'pttl', self._encode(key))

    # Set operations

    @_command
    def sadd(self, key:str, *members) -> int:
        """ Add one or more members to a set """
        return self._query(b'sadd', self._encode(key), *map(self._encode, members))

    @_command
    def srem(self, key:str, *members) -> int:
        """ Remove one or more members from a set """
        return self._query(b'srem', self._encode(key), *map(self._encode, members))

    @_command
    def spop(self, key:str) -> str:
        """ Removes and returns a random element from the set value stored at key. """
        return self._query(b'spop', self._encode(key))

    @_command
    def srandmember(self, key:str, count:int=1) -> list:
        """ Get one or multiple random members from a set
        (Returns a list of members, even when count==1)
        """
        return self._query(b'srandmember', self._encode(key), self._encode_int(count),
                post_process_func=_PostProcessor.multibulk_as_list)

    @_command
    def sismember(self, key:str, value:str) -> bool:
        """ Determine if a given value is a member of a set """
        return self._query(b'sismember', self._encode(key), self._encode(value),
                post_process_func=_PostProcessor.int_to_bool)

    @_command
    def scard(self, key:str) -> int:
        """ Get the number of members in a set """
        return self._query(b'scard', self._encode(key))

    @_command
    def smembers(self, key:str) -> SetType:
        """ Get all the members in a set """
        return self._query(b'smembers', self._encode(key),
                post_process_func=_PostProcessor.multibulk_as_set)

    @_command
    def sinter(self, *keys) -> SetType:
        """ Intersect multiple sets """
        return self._query(b'sinter', *map(self._encode, keys),
                post_process_func=_PostProcessor.multibulk_as_set)

    @_command
    def sinterstore(self, destination:str, *keys) -> int:
        """ Intersect multiple sets and store the resulting set in a key """
        return self._query(b'sinterstore', self._encode(destination), *map(self._encode, keys))

    @_command
    def sdiff(self, key:str, *keys) -> SetType:
        """ Subtract multiple sets """
        return self._query(b'sdiff', self._encode(key), *map(self._encode, keys),
                post_process_func=_PostProcessor.multibulk_as_set)

    @_command
    def sdiffstore(self, destination:str, *keys) -> int:
        """ Subtract multiple sets and store the resulting set in a key """
        return self._query(b'sdiffstore', self._encode(destination), *map(self._encode, keys))

    @_command
    def sunion(self, *keys) -> SetType:
        """ Add multiple sets """
        return self._query(b'sunion', *map(self._encode, keys),
                post_process_func=_PostProcessor.multibulk_as_set)

    @_command
    def sunionstore(self, destination:str, *keys) -> int:
        """ Add multiple sets and store the resulting set in a key """
        return self._query(b'sunionstore', self._encode(destination), *map(self._encode, keys))

    @_command
    def smove(self, source:str, destination:str, value:str) -> int:
        """ Move a member from one set to another """
        return self._query(b'smove', self._encode(source), self._encode(destination), self._encode(value))

    # List operations

    @_command
    def lpush(self, key:str, *values) -> int:
        """ Prepend one or multiple values to a list """
        return self._query(b'lpush', self._encode(key), *map(self._encode, values))

    @_command
    def lpushx(self, key:str, value:str) -> int:
        """ Prepend a value to a list, only if the list exists """
        return self._query(b'lpushx', self._encode(key), self._encode(value))

    @_command
    def rpush(self, key:str, *values) -> int:
        """ Append one or multiple values to a list """
        return self._query(b'rpush', self._encode(key), *map(self._encode, values))

    @_command
    def rpushx(self, key:str, value:str) -> int:
        """ Append a value to a list, only if the list exists """
        return self._query(b'rpushx', self._encode(key), self._encode(value))

    @_command
    def llen(self, key:str) -> int:
        """ Returns the length of the list stored at key. """
        return self._query(b'llen', self._encode(key))

    @_command
    def lrem(self, key:str, count:int=0, value='') -> int:
        """ Remove elements from a list """
        return self._query(b'lrem', self._encode(key), self._encode_int(count), self._encode(value))

    @_command
    def lrange(self, key, start:int=0, stop:int=-1) -> list:
        """ Get a range of elements from a list. """
        return self._query(b'lrange', self._encode(key), self._encode_int(start), self._encode_int(stop),
                post_process_func=_PostProcessor.multibulk_as_list)

    @_command
    def ltrim(self, key:str, start:int=0, stop:int=-1) -> StatusReply:
        """ Trim a list to the specified range """
        return self._query(b'ltrim', self._encode(key), self._encode_int(start), self._encode_int(stop))

    @_command
    def lpop(self, key:str) -> (str, NoneType):
        """ Remove and get the first element in a list """
        return self._query(b'lpop', self._encode(key))

    @_command
    def rpop(self, key:str) -> (str, NoneType):
        """ Remove and get the last element in a list """
        return self._query(b'rpop', self._encode(key))

    @_command
    def rpoplpush(self, source:str, destination:str) -> str:
        """ Remove the last element in a list, append it to another list and return it """
        return self._query(b'rpoplpush', self._encode(source), self._encode(destination))

    @_command
    def lindex(self, key:str, index:int) -> (str, NoneType):
        """ Get an element from a list by its index """
        return self._query(b'lindex', self._encode(key), self._encode_int(index))

    @_command
    def blpop(self, *keys, timeout:int=0) -> list: # TODO: Returns (list_name, value) -> is that the best way?
        """ Remove and get the first element in a list, or block until one is available. """
        return self._blocking_pop(*keys, timeout=timeout, right=False)

    @_command
    def brpop(self, *keys, timeout:int=0) -> list: # TODO: Returns (list_name, value) -> is that the best way?
        """ Remove and get the last element in a list, or block until one is available. """
        return self._blocking_pop(*keys, timeout=timeout, right=True)

    @_command
    def brpoplpush(self, source:str, destination:str, timeout:int=0) -> str:
        """ Pop a value from a list, push it to another list and return it; or block until one is available """
        return self._query(b'brpoplpush', self._encode(source), self._encode(destination),
                    self._encode_int(timeout), set_blocking=True)

    def _blocking_pop(self, *keys, timeout=0, right=False):
        command = b'brpop' if right else b'blpop'
        return self._query(command, *map(self._encode, list(keys) + [str(timeout)]),
                post_process_func=_PostProcessor.multibulk_as_list, set_blocking=True)

    @_command
    def lset(self, key:str, index:int, value:str) -> StatusReply:
        """ Set the value of an element in a list by its index. """
        return self._query(b'lset', self._encode(key), self._encode_int(index), self._encode(value))

    @_command
    def linsert(self, key:str, pivot:str, value:str, before=False) -> int:
        """ Insert an element before or after another element in a list """
        return self._query(b'linsert', self._encode(key), (b'BEFORE' if before else b'AFTER'),
                self._encode(pivot), self._encode(value))

    # Sorted Sets

    @_command
    def zadd(self, key:str, values:dict) -> int:
        """
        Add one or more members to a sorted set, or update its score if it already exists

        ::

            yield protocol.zadd('myzset', { 'key': 4, 'key2': 5 })
        """
        data = [ ]
        for k,score in values.items():
            assert isinstance(k, str)
            assert isinstance(score, (int, float))

            data.append(self._encode_float(score))
            data.append(self._encode(k))

        return self._query(b'zadd', self._encode(key), *data)

    @_command
    def zrange(self, key:str, start:int=0, stop:int=-1) -> ZRangeResult:
        """
        Return a range of members in a sorted set, by index.

        You can do the following to receive the slice of the sorted set as a
        python dict (mapping the keys to their scores):

        ::

            result = yield protocol.zrange('myzset', start=10, stop=20)
            my_dict = yield result.get_as_dict()

        or the following to retrieve it as a list of keys:

        ::

            result = yield protocol.zrange('myzset', start=10, stop=20)
            my_dict = yield result.get_as_list()
        """
        return self._query(b'zrange', self._encode(key),
                    self._encode_int(start), self._encode_int(stop), b'withscores',
                    post_process_func=_PostProcessor.multibulk_as_zrangeresult)

    @_command
    def zrangebyscore(self, key:str,
                min:ZScoreBoundary=ZScoreBoundary.MIN_VALUE,
                max:ZScoreBoundary=ZScoreBoundary.MAX_VALUE) -> ZRangeResult:
        """ Return a range of members in a sorted set, by score """
        return self._query(b'zrangebyscore', self._encode(key),
                    self._encode_zscore_boundary(min), self._encode_zscore_boundary(max),
                    b'withscores',
                    post_process_func=_PostProcessor.multibulk_as_zrangeresult)

    @_command
    def zrevrangebyscore(self, key:str,
                max:ZScoreBoundary=ZScoreBoundary.MAX_VALUE,
                min:ZScoreBoundary=ZScoreBoundary.MIN_VALUE) -> ZRangeResult:
        """ Return a range of members in a sorted set, by score, with scores ordered from high to low """
        return self._query(b'zrevrangebyscore', self._encode(key),
                    self._encode_zscore_boundary(max), self._encode_zscore_boundary(min),
                    b'withscores',
                    post_process_func=_PostProcessor.multibulk_as_zrangeresult)

    @_command
    def zremrangebyscore(self, key:str,
                min:ZScoreBoundary=ZScoreBoundary.MIN_VALUE,
                max:ZScoreBoundary=ZScoreBoundary.MAX_VALUE) -> int:
        """ Remove all members in a sorted set within the given scores """
        return self._query(b'zremrangebyscore', self._encode(key),
                    self._encode_zscore_boundary(min), self._encode_zscore_boundary(max))

    @_command
    def zremrangebyrank(self, key:str, min:int=0, max:int=-1) -> int:
        """ Remove all members in a sorted set within the given indexes """
        return self._query(b'zremrangebyrank', self._encode(key),
                    self._encode_int(min), self._encode_int(max))

    @_command
    def zcount(self, key:str, min:ZScoreBoundary, max:ZScoreBoundary) -> int:
        """ Count the members in a sorted set with scores within the given values """
        return self._query(b'zcount', self._encode(key),
                    self._encode_zscore_boundary(min), self._encode_zscore_boundary(max))

    @_command
    def zscore(self, key:str, member:str) -> float:
        """ Get the score associated with the given member in a sorted set """
        return self._query(b'zscore', self._encode(key), self._encode(member))

    #def zunionstore(self, destination:str, min:ZScoreBoundary, max:ZScoreBoundary) -> int: # XXX: TODO: test it
    #def zinterstore(self, destination:str, min:ZScoreBoundary, max:ZScoreBoundary) -> int: # XXX: TODO: test it

    @_command
    def zcard(self, key:str) -> int:
        """ Get the number of members in a sorted set """
        return self._query(b'zcard', self._encode(key))

    @_command
    def zrank(self, key:str, member:str) -> (int, NoneType):
        """ Determine the index of a member in a sorted set """
        return self._query(b'zrank', self._encode(key), self._encode(member))

    @_command
    def zrevrank(self, key:str, member:str) -> (int, NoneType):
        """ Determine the index of a member in a sorted set, with scores ordered from high to low """
        return self._query(b'zrevrank', self._encode(key), self._encode(member))

    @_command
    def zincrby(self, key:str, increment:float, member:str) -> float:
        """ Increment the score of a member in a sorted set """
        return self._query(b'zincrby', self._encode(key),
                    self._encode_float(increment), self._encode(member),
                    post_process_func=_PostProcessor.str_to_float)

    @_command
    def zrem(self, key:str, *members) -> int:
        """ Remove one or more members from a sorted set """
        return self._query(b'zrem', self._encode(key), *map(self._encode, members))

    # Hashes

    @_command
    def hset(self, key:str, field:str, value:str) -> int:
        """ Set the string value of a hash field """
        return self._query(b'hset', self._encode(key), self._encode(field), self._encode(value))

    @_command
    def hmset(self, key:str, **values) -> StatusReply:
        """ Set multiple hash fields to multiple values """
        data = [ ]
        for k,v in values.items():
            assert isinstance(k, str)
            assert isinstance(v, str)

            data.append(self._encode(k))
            data.append(self._encode(v))

        return self._query(b'hmset', self._encode(key), *data)

    @_command
    def hsetnx(self, key:str, field:str, value:str) -> int:
        """ Set the value of a hash field, only if the field does not exist """
        return self._query(b'hsetnx', self._encode(key), self._encode(field), self._encode(value))

    @_command
    def hdel(self, key:str, *fields) -> int:
        """ Delete one or more hash fields """
        return self._query(b'hdel', self._encode(key), *map(self._encode, fields))

    @_command
    def hget(self, key:str, field:str) -> (str, NoneType):
        """ Get the value of a hash field """
        return self._query(b'hget', self._encode(key), self._encode(field))

    @_command
    def hexists(self, key:str, field:str) -> bool:
        """ Returns if field is an existing field in the hash stored at key. """
        return self._query(b'hexists', self._encode(key), self._encode(field),
                post_process_func=_PostProcessor.int_to_bool)

    @_command
    def hkeys(self, key:str) -> SetType:
        """ Get all the keys in a hash. (Returns a set) """
        return self._query(b'hkeys', self._encode(key),
                post_process_func=_PostProcessor.multibulk_as_set)

    @_command
    def hvals(self, key:str) -> list:
        """ Get all the values in a hash. (Returns a list) """
        return self._query(b'hvals', self._encode(key),
                post_process_func=_PostProcessor.multibulk_as_list)

    @_command
    def hlen(self, key:str) -> int:
        """ Returns the number of fields contained in the hash stored at key. """
        return self._query(b'hlen', self._encode(key))

    @_command
    def hgetall(self, key:str) -> dict:
        """ Get the value of a hash field """
        @asyncio.coroutine
        def post(multibulk):
            result = yield from multibulk.get_as_list()
            return { result[i]: result[i+1] for i in range(0, len(result), 2) }

        return self._query(b'hgetall', self._encode(key), post_process_func=post)

    @_command
    def hmget(self, key:str, *fields) -> list:
        """ Get the values of all the given hash fields """
        return self._query(b'hmget', self._encode(key), *map(self._encode, fields),
                post_process_func=_PostProcessor.multibulk_as_list)

    @_command
    def hincrby(self, key:str, field:str, increment) -> int:
        """ Increment the integer value of a hash field by the given number
        Returns: the value at field after the increment operation. """
        assert isinstance(increment, int)
        return self._query(b'hincrby', self._encode(key), self._encode(field), self._encode_int(increment))

    @_command
    def hincrbyfloat(self, key:str, field:str, increment:(int,float)) -> float:
        """ Increment the float value of a hash field by the given amount
        Returns: the value at field after the increment operation. """
        @asyncio.coroutine
        def post(result):
            assert isinstance(result, str)
            return float(result) # Convert str to float

        return self._query(b'hincrbyfloat', self._encode(key), self._encode(field), self._encode_float(increment),
                post_process_func=post)

    # Pubsub

    @_command
    @asyncio.coroutine
    def subscribe(self, *channels) -> list: # TODO: wrap result in SubscribeReply
        """ Listen for messages published to the given channels """
        if self.in_transaction:
            raise RedisException('Cannot call subscribe inside a transaction.')

        multibulk = yield from self._query(b'subscribe', *map(self._encode, channels))

        # Something like [ 'subscribe', 'our_channel', 1]
        result = yield from multibulk.get_as_list()
        assert result[0] == u'subscribe'

        if not self._in_pubsub:
            self._in_pubsub = True # Put this on True, only after the result has been received.
            self._messages_queue = Queue() # Create pubsub queue

        return result

    def get_next_published(self):
        """ Wait for next pubsub message to be received and return it. """
        return self._messages_queue.get()

    @_command
    def publish(self, channel:str, message:str) -> int:
        """ Post a message to a channel
        (Returns the number of clients that received this message.) """
        return self._query(b'publish', self._encode(channel), self._encode(message))

    # Server

    @_command
    def ping(self) -> StatusReply:
        """ Ping the server (Returns PONG) """
        return self._query(b'ping')

    @_command
    def echo(self, string:str) -> str:
        """ Echo the given string """
        return self._query(b'echo', self._encode(string))

    @_command
    def save(self) -> StatusReply:
        """ Synchronously save the dataset to disk """
        return self._query(b'save')

    @_command
    def bgsave(self) -> StatusReply:
        """ Asynchronously save the dataset to disk """
        return self._query(b'bgsave')

    @_command
    def lastsave(self) -> int:
        """ Get the UNIX time stamp of the last successful save to disk """
        return self._query(b'lastsave')

    @_command
    def dbsize(self) -> int:
        """ Return the number of keys in the currently-selected database. """
        return self._query(b'dbsize')

    @_command
    def flushall(self) -> StatusReply:
        """ Remove all keys from all databases """
        return self._query(b'flushall')

    @_command
    def flushdb(self) -> StatusReply:
        """ Delete all the keys of the currently selected DB. This command never fails. """
        return self._query(b'flushdb')

    @_command
    def object(self, subcommand, *args):
        """ Inspect the internals of Redis objects """
        raise NotImplementedError

    @_command
    def type(self, key:str) -> StatusReply:
        """ Determine the type stored at key """
        return self._query(b'type', self._encode(key))

    # Transaction

    @_command
    @asyncio.coroutine
    def multi(self, keys:(list,NoneType)=None):
        """
        Start of transaction.

        ::

            transaction = yield from protocol.multi()

            # Run commands in transaction
            f1 = yield from transaction.set('key', 'value')
            f1 = yield from transaction.set('another_key', 'another_value')

            # Commit transaction
            yield from transaction.exec()

            # Retrieve results (you can also use asyncio.tasks.gather)
            result1 = yield from f1
            result2 = yield from f2

        :returns: A :class:`asyncio_redis.Transaction` instance.
        """
#        assert isinstance(keys, list) or keys is None # TODO: implement watch

        if (self._in_transaction):
            raise RedisException('Multi calls can not be nested.')

        # Call watch
        if keys is not None:
            for k in keys:
                result = yield from self._query(b'watch', self._encode(k)) # XXX: unittest
                assert result == StatusReply('OK')

        # Call multi
        result = yield from self._query(b'multi')
        assert result == StatusReply('OK')

        self._in_transaction = True
        self._transaction_response_queue = deque()

        # Create transaction object.
        t = Transaction(self)
        self._transaction = t
        return t

    @asyncio.coroutine
    def _exec(self):
        """
        Execute all commands issued after MULTI
        """
        if not self._in_transaction:
            raise RedisException('Not in transaction')

        futures_and_postprocessors = self._transaction_response_queue
        self._transaction_response_queue = None

        # Get transaction answers.
        multi_bulk_reply = yield from self._query(b'exec', _bypass=True)

        if multi_bulk_reply is None:
            # We get None when a transaction failed.
            raise RedisException('Transaction failed.') # XXX test this

        for f in multi_bulk_reply:
            answer = yield from f
            f2, post_process_func, call = futures_and_postprocessors.popleft()

            if isinstance(answer, Exception):
                f2.set_exception(answer)
            else:
                if post_process_func:
                    answer = yield from post_process_func(answer)
                if call:
                    self._pipelined_calls.remove(call)

                f2.set_result(answer)

        self._transaction_response_queue = deque()
        self._in_transaction = False
        self._transaction = None

    @asyncio.coroutine
    def _discard(self):
        """
        Discard all commands issued after MULTI
        """
        if not self._in_transaction:
            raise RedisException('Not in transaction')

        self._transaction_response_queue = deque()
        self._in_transaction = False
        self._transaction = None
        result = yield from self._query(b'discard')
        assert result == StatusReply('OK')

    @asyncio.coroutine
    def _unwatch(self):
        """
        Forget about all watched keys
        """
        if not self._in_transaction:
            raise RedisException('Not in transaction')

        result = yield from self._query(b'unwatch')
        assert result == StatusReply('OK')


class Connection:
    """
    Wrapper around the Redis protocol.
    Takes care of setting up the connection and connection pooling.

    When poolsize > 1 and some connections are in use because of transactions
    or blocking requests, the other are preferred.

    ::

        connection = yield from Connection(poolsize=10)
        connection.set('key', 'value')
    """
    @classmethod
    @asyncio.coroutine
    def create(cls, host='localhost', port=6379, loop=None, poolsize=1, password=None, db=0):
        """
        Create a new connection instance.
        """
        if loop is None:
            loop = asyncio.get_event_loop()

        # Inherit protocol
        redis_protocol = type('RedisProtocol', (RedisProtocol,), { 'password': password, 'db': db })

        self = cls()
        self._poolsize = poolsize

        # Create connections
        self._transport_protocol_pairs  = []

        for i in range(poolsize):
            transport, protocol = yield from asyncio.Task(loop.create_connection(redis_protocol, host, port))
            self._transport_protocol_pairs.append( (transport, protocol) )

        return self

    @property
    def poolsize(self):
        """ Number of parallel connections in the pool."""
        return self._poolsize

    @property
    def connections_in_use(self):
        """
        Return how many protocols are in use.
        """
        return sum([ 1 for transport, protocol in self._transport_protocol_pairs if protocol.in_use ])

    def _get_free_protocol(self):
        """
        Return the next protocol instance that's not in use.
        (A protocol in pubsub mode or doing a blocking request is considered busy,
        and can't be used for anything else.)
        """
        self._shuffle_protocols()

        for transport, protocol in self._transport_protocol_pairs:
            if not protocol.in_use:
                return protocol

    def _shuffle_protocols(self):
        """
        'shuffle' protocols. Make sure that we devide the load equally among the protocols.
        """
        self._transport_protocol_pairs = self._transport_protocol_pairs[1:] + self._transport_protocol_pairs[:1]

    def __getattr__(self, name): # Don't proxy everything, (no private vars, and use decorator to mark exceptions)
        """
        Proxy to a protocol. (This will choose a protocol instance that's not
        busy in a blocking request or transaction.)
        """
        # Only proxy commands.
        if name not in _all_commands:
            raise AttributeError

        protocol = self._get_free_protocol()

        if protocol:
            return getattr(protocol, name)
        else:
            raise RedisException('All connection in the pool are in use. Please increase the poolsize.')


class Transaction:
    """
    Transaction context. This is a proxy to a :class:`RedisProtocol` instance.
    Every redis command called on this object will run inside the transaction.
    The transaction can be finished by calling either ``discard`` or ``exec``.

    More info: http://redis.io/topics/transactions
    """
    def __init__(self, protocol):
        self._protocol = protocol

    def __getattr__(self, name):
        """
        Proxy to a protocol.
        """
        # Only proxy commands.
        if name not in _all_commands:
            raise AttributeError

        if self._protocol._transaction != self:
            raise RedisException('Transaction already finished or invalid.')

        method = getattr(self._protocol, name)

        # Wrap the method into something that passes the transaction object as
        # first argument.
        @wraps(method)
        def wrapper(*a, **kw):
            return method(self, *a, **kw)
        return wrapper

    def discard(self):
        """
        Discard all commands issued after MULTI
        """
        return self._protocol._discard()

    def exec(self):
        """
        Execute transaction. Returns a list of futures.
        """
        return self._protocol._exec()

    def unwatch(self): # XXX: test
        """
        Forget about all watched keys
        """
        return self._protocol._unwatch()