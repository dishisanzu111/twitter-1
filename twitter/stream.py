try:
    import urllib.request as urllib_request
    import urllib.error as urllib_error
    import io
except ImportError:
    import urllib2 as urllib_request
    import urllib2 as urllib_error
import json
from ssl import SSLError
import socket
import sys, select, time

from .api import TwitterCall, wrap_response, TwitterHTTPError

python27_3 = sys.version_info >= (2, 7)
def recv_chunk(sock): # -> bytearray:

    header = sock.recv(8)  # Scan for an up to 16MiB chunk size (0xffffff).
    crlf = header.find(b'\r\n')  # Find the HTTP chunk size.

    if crlf > 0:  # If there is a length, then process it

        size = int(header[:crlf], 16)  # Decode the chunk size. Rarely exceeds 8KiB.
        chunk = bytearray(size)
        start = crlf + 2  # Add in the length of the header's CRLF pair.

        if size <= 3:  # E.g. an HTTP chunk with just a keep-alive delimiter or end of stream (0).
            chunk[:size] = header[start:start + size]
        # There are several edge cases (size == [4-6]) as the chunk size exceeds the length
        # of the initial read of 8 bytes. With Twitter, these do not, in practice, occur. The
        # shortest JSON message starts with '{"limit":{'. Hence, it exceeds in size the edge cases
        # and eliminates the need to address them.
        else:  # There is more to read in the chunk.
            end = len(header) - start
            chunk[:end] = header[start:]
            if python27_3:  # When possible, use less memory by reading directly into the buffer.
                buffer = memoryview(chunk)[end:]  # Create a view into the bytearray to hold the rest of the chunk.
                sock.recv_into(buffer)
            else:  # less efficient for python2.6 compatibility
                chunk[end:] = sock.recv(max(0, size - end))
            sock.recv(2)  # Read the trailing CRLF pair. Throw it away.

        return chunk

    return bytearray()


class TwitterJSONIter(object):

    def __init__(self, handle, uri, arg_data, block=True, timeout=None):
        self.handle = handle
        self.uri = uri
        self.arg_data = arg_data
        self.block = block
        self.timeout = timeout


    def __iter__(self):
        sock = self.handle.fp.raw._sock if sys.version_info >= (3, 0) else self.handle.fp._sock.fp._sock
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setblocking(self.block and not self.timeout)
        buf = ''
        json_decoder = json.JSONDecoder()
        timer = time.time()
        while True:
            try:
                buf = buf.lstrip()
                res, ptr = json_decoder.raw_decode(buf)
                buf = buf[ptr:]
                yield wrap_response(res, self.handle.headers)
                continue
            except ValueError as e:
                if self.block: pass
                else: yield None
            try:
                buf = buf.lstrip()  # Remove any keep-alive delimiters to detect hangups.
                if self.timeout and not buf:  # This is a non-blocking read.
                    ready_to_read = select.select([sock], [], [], self.timeout)
                    if not ready_to_read[0] and time.time() - timer > self.timeout:
                        yield {'timeout': True}
                        continue
                timer = time.time()
                buf += recv_chunk(sock).decode('utf-8')
                if not buf:
                    yield {'hangup': True}
                    break
            except SSLError as e:
                # Error from a non-blocking read of an empty buffer.
                if (not self.block or self.timeout) and (e.errno == 2): pass
                else: raise

def handle_stream_response(req, uri, arg_data, block, timeout=None):
    try:
        handle = urllib_request.urlopen(req,)
    except urllib_error.HTTPError as e:
        raise TwitterHTTPError(e, uri, 'json', arg_data)
    return iter(TwitterJSONIter(handle, uri, arg_data, block, timeout=timeout))

class TwitterStreamCallWithTimeout(TwitterCall):
    def _handle_response(self, req, uri, arg_data, _timeout=None):
        return handle_stream_response(req, uri, arg_data, block=True, timeout=self.timeout)

class TwitterStreamCall(TwitterCall):
    def _handle_response(self, req, uri, arg_data, _timeout=None):
        return handle_stream_response(req, uri, arg_data, block=True)

class TwitterStreamCallNonBlocking(TwitterCall):
    def _handle_response(self, req, uri, arg_data, _timeout=None):
        return handle_stream_response(req, uri, arg_data, block=False)

class TwitterStream(TwitterStreamCall):
    """
    The TwitterStream object is an interface to the Twitter Stream API
    (stream.twitter.com). This can be used pretty much the same as the
    Twitter class except the result of calling a method will be an
    iterator that yields objects decoded from the stream. For
    example::

        twitter_stream = TwitterStream(auth=OAuth(...))
        iterator = twitter_stream.statuses.sample()

        for tweet in iterator:
            ...do something with this tweet...

    The iterator will yield tweets forever and ever (until the stream
    breaks at which point it raises a TwitterHTTPError.)

    The `block` parameter controls if the stream is blocking. Default
    is blocking (True). When set to False, the iterator will
    occasionally yield None when there is no available message.
    """
    def __init__(
        self, domain="stream.twitter.com", secure=True, auth=None,
        api_version='1.1', block=True, timeout=None):
        uriparts = ()
        uriparts += (str(api_version),)

        if block:
            if timeout:
                call_cls = TwitterStreamCallWithTimeout
            else:
                call_cls = TwitterStreamCall
        else:
            call_cls = TwitterStreamCallNonBlocking

        TwitterStreamCall.__init__(
            self, auth=auth, format="json", domain=domain,
            callable_cls=call_cls,
            secure=secure, uriparts=uriparts, timeout=timeout, gzip=False)
