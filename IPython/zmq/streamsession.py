#!/usr/bin/env python
"""edited session.py to work with streams, and move msg_type to the header
"""


import os
import sys
import traceback
import pprint
import uuid

import zmq
from zmq.eventloop.zmqstream import ZMQStream

from IPython.zmq.pickleutil import can, uncan, canSequence, uncanSequence
from IPython.zmq.newserialized import serialize, unserialize

try:
    import cjson
    json = cjson
except ImportError:
    cjson = None
    try:
        import json
    except:
        try:
            import simplejson as json
        except ImportError:
            json = None

try:
    import cPickle
    pickle = cPickle
except:
    cPickle = None
    import pickle


if cjson is not None:
    to_json = lambda o: json.encode(o)
    from_json = json.decode
else:
    to_json = lambda o: json.dumps(o, separators=(',',':'))
    from_json = json.loads

to_pickle = lambda o: pickle.dumps(o, 2)

# packer priority: cjson, cPickle, json/simplejson, pickle

if cjson is not None or (cPickle is None and json is not None): # 1,3
    default_packer = to_json
    default_unpacker = from_json
else: # 2,4
    default_packer = to_pickle
    default_unpacker = pickle.loads
# else:
#     default_packer = to_json
#     default_unpacker = json.loads


DELIM="<IDS|MSG>"

def wrap_exception():
    etype, evalue, tb = sys.exc_info()
    tb = traceback.format_exception(etype, evalue, tb)
    exc_content = {
        u'status' : u'error',
        u'traceback' : tb,
        u'etype' : unicode(etype),
        u'evalue' : unicode(evalue)
    }
    return exc_content

class KernelError(Exception):
    pass

def unwrap_exception(content):
    err = KernelError(content['etype'], content['evalue'])
    err.evalue = content['evalue']
    err.etype = content['etype']
    err.traceback = ''.join(content['traceback'])
    return err
    

class Message(object):
    """A simple message object that maps dict keys to attributes.

    A Message can be created from a dict and a dict from a Message instance
    simply by calling dict(msg_obj)."""
    
    def __init__(self, msg_dict):
        dct = self.__dict__
        for k, v in dict(msg_dict).iteritems():
            if isinstance(v, dict):
                v = Message(v)
            dct[k] = v

    # Having this iterator lets dict(msg_obj) work out of the box.
    def __iter__(self):
        return iter(self.__dict__.iteritems())
    
    def __repr__(self):
        return repr(self.__dict__)

    def __str__(self):
        return pprint.pformat(self.__dict__)

    def __contains__(self, k):
        return k in self.__dict__

    def __getitem__(self, k):
        return self.__dict__[k]


def msg_header(msg_id, msg_type, username, session):
    return locals()
    # return {
    #     'msg_id' : msg_id,
    #     'msg_type': msg_type,
    #     'username' : username,
    #     'session' : session
    # }


def extract_header(msg_or_header):
    """Given a message or header, return the header."""
    if not msg_or_header:
        return {}
    try:
        # See if msg_or_header is the entire message.
        h = msg_or_header['header']
    except KeyError:
        try:
            # See if msg_or_header is just the header
            h = msg_or_header['msg_id']
        except KeyError:
            raise
        else:
            h = msg_or_header
    if not isinstance(h, dict):
        h = dict(h)
    return h

def rekey(dikt):
    """rekey a dict that has been forced to use str keys where there should be
    ints by json.  This belongs in the jsonutil added by fperez."""
    for k in dikt.iterkeys():
        if isinstance(k, str):
            ik=fk=None
            try:
                ik = int(k)
            except ValueError:
                try:
                    fk = float(k)
                except ValueError:
                    continue
            if ik is not None:
                nk = ik
            else:
                nk = fk
            if nk in dikt:
                raise KeyError("already have key %r"%nk)
            dikt[nk] = dikt.pop(k)
    return dikt

def serialize_object(obj, threshold=64e-6):
    """serialize an object into a list of sendable buffers.
    
    Returns: (pmd, bufs)
        where pmd is the pickled metadata wrapper, and bufs
        is a list of data buffers"""
    # threshold is 100 B
    databuffers = []
    if isinstance(obj, (list, tuple)):
        clist = canSequence(obj)
        slist = map(serialize, clist)
        for s in slist:
            if s.getDataSize() > threshold:
                databuffers.append(s.getData())
                s.data = None
        return pickle.dumps(slist,-1), databuffers
    elif isinstance(obj, dict):
        sobj = {}
        for k in sorted(obj.iterkeys()):
            s = serialize(can(obj[k]))
            if s.getDataSize() > threshold:
                databuffers.append(s.getData())
                s.data = None
            sobj[k] = s
        return pickle.dumps(sobj,-1),databuffers
    else:
        s = serialize(can(obj))
        if s.getDataSize() > threshold:
            databuffers.append(s.getData())
            s.data = None
        return pickle.dumps(s,-1),databuffers
            
        
def unserialize_object(bufs):
    """reconstruct an object serialized by serialize_object from data buffers"""
    bufs = list(bufs)
    sobj = pickle.loads(bufs.pop(0))
    if isinstance(sobj, (list, tuple)):
        for s in sobj:
            if s.data is None:
                s.data = bufs.pop(0)
        return uncanSequence(map(unserialize, sobj))
    elif isinstance(sobj, dict):
        newobj = {}
        for k in sorted(sobj.iterkeys()):
            s = sobj[k]
            if s.data is None:
                s.data = bufs.pop(0)
            newobj[k] = uncan(unserialize(s))
        return newobj
    else:
        if sobj.data is None:
            sobj.data = bufs.pop(0)
        return uncan(unserialize(sobj))

def pack_apply_message(f, args, kwargs, threshold=64e-6):
    """pack up a function, args, and kwargs to be sent over the wire
    as a series of buffers. Any object whose data is larger than `threshold`
    will not have their data copied (currently only numpy arrays support zero-copy)"""
    msg = [pickle.dumps(can(f),-1)]
    databuffers = [] # for large objects
    sargs, bufs = serialize_object(args,threshold)
    msg.append(sargs)
    databuffers.extend(bufs)
    skwargs, bufs = serialize_object(kwargs,threshold)
    msg.append(skwargs)
    databuffers.extend(bufs)
    msg.extend(databuffers)
    return msg

def unpack_apply_message(bufs, g=None, copy=True):
    """unpack f,args,kwargs from buffers packed by pack_apply_message()
    Returns: original f,args,kwargs"""
    bufs = list(bufs) # allow us to pop
    assert len(bufs) >= 3, "not enough buffers!"
    if not copy:
        for i in range(3):
            bufs[i] = bufs[i].bytes
    cf = pickle.loads(bufs.pop(0))
    sargs = list(pickle.loads(bufs.pop(0)))
    skwargs = dict(pickle.loads(bufs.pop(0)))
    # print sargs, skwargs
    f = cf.getFunction(g)
    for sa in sargs:
        if sa.data is None:
            m = bufs.pop(0)
            if sa.getTypeDescriptor() in ('buffer', 'ndarray'):
                if copy:
                    sa.data = buffer(m)
                else:
                    sa.data = m.buffer
            else:
                if copy:
                    sa.data = m
                else:
                    sa.data = m.bytes
    
    args = uncanSequence(map(unserialize, sargs), g)
    kwargs = {}
    for k in sorted(skwargs.iterkeys()):
        sa = skwargs[k]
        if sa.data is None:
            sa.data = bufs.pop(0)
        kwargs[k] = uncan(unserialize(sa), g)
    
    return f,args,kwargs

class StreamSession(object):
    """tweaked version of IPython.zmq.session.Session, for development in Parallel"""

    def __init__(self, username=None, session=None, packer=None, unpacker=None):
        if username is None:
            username = os.environ.get('USER','username')
        self.username = username
        if session is None:
            self.session = str(uuid.uuid4())
        else:
            self.session = session
        self.msg_id = str(uuid.uuid4())
        if packer is None:
            self.pack = default_packer
        else:
            if not callable(packer):
                raise TypeError("packer must be callable, not %s"%type(packer))
            self.pack = packer
        
        if unpacker is None:
            self.unpack = default_unpacker
        else:
            if not callable(unpacker):
                raise TypeError("unpacker must be callable, not %s"%type(unpacker))
            self.unpack = unpacker
        
        self.none = self.pack({})
            
    def msg_header(self, msg_type):
        h = msg_header(self.msg_id, msg_type, self.username, self.session)
        self.msg_id = str(uuid.uuid4())
        return h

    def msg(self, msg_type, content=None, parent=None, subheader=None):
        msg = {}
        msg['header'] = self.msg_header(msg_type)
        msg['msg_id'] = msg['header']['msg_id']
        msg['parent_header'] = {} if parent is None else extract_header(parent)
        msg['msg_type'] = msg_type
        msg['content'] = {} if content is None else content
        sub = {} if subheader is None else subheader
        msg['header'].update(sub)
        return msg

    def send(self, stream, msg_type, content=None, buffers=None, parent=None, subheader=None, ident=None):
        """send a message via stream"""
        msg = self.msg(msg_type, content, parent, subheader)
        buffers = [] if buffers is None else buffers
        to_send = []
        if isinstance(ident, list):
            # accept list of idents
            to_send.extend(ident)
        elif ident is not None:
            to_send.append(ident)
        to_send.append(DELIM)
        to_send.append(self.pack(msg['header']))
        to_send.append(self.pack(msg['parent_header']))
        # if parent is None:
        #     to_send.append(self.none)
        # else:
        #     to_send.append(self.pack(dict(parent)))
        if content is None:
            content = self.none
        elif isinstance(content, dict):
            content = self.pack(content)
        elif isinstance(content, str):
            # content is already packed, as in a relayed message
            pass
        else:
            raise TypeError("Content incorrect type: %s"%type(content))
        to_send.append(content)
        flag = 0
        if buffers:
            flag = zmq.SNDMORE
        stream.send_multipart(to_send, flag, copy=False)
        for b in buffers[:-1]:
            stream.send(b, flag, copy=False)
        if buffers:
            stream.send(buffers[-1], copy=False)
        omsg = Message(msg)
        return omsg

    def recv(self, socket, mode=zmq.NOBLOCK, content=True, copy=True):
        """receives and unpacks a message
        returns [idents], msg"""
        if isinstance(socket, ZMQStream):
            socket = socket.socket
        try:
            msg = socket.recv_multipart(mode)
        except zmq.ZMQError, e:
            if e.errno == zmq.EAGAIN:
                # We can convert EAGAIN to None as we know in this case
                # recv_json won't return None.
                return None
            else:
                raise
        # return an actual Message object
        # determine the number of idents by trying to unpack them.
        # this is terrible:
        idents, msg = self.feed_identities(msg, copy)
        try:
            return idents, self.unpack_message(msg, content=content, copy=copy)
        except Exception, e:
            print idents, msg
            # TODO: handle it
            raise e
    
    def feed_identities(self, msg, copy=True):
        """This is a completely horrible thing, but it strips the zmq
        ident prefixes off of a message. It will break if any identities
        are unpackable by self.unpack."""
        msg = list(msg)
        idents = []
        while len(msg) > 3:
            if copy:
                s = msg[0]
            else:
                s = msg[0].bytes
            if s == DELIM:
                msg.pop(0)
                break
            else:
                idents.append(s)
                msg.pop(0)
                
        return idents, msg
    
    def unpack_message(self, msg, content=True, copy=True):
        """return a message object from the format
        sent by self.send.
        
        parameters:
        
        content : bool (True)
            whether to unpack the content dict (True), 
            or leave it serialized (False)
        
        copy : bool (True)
            whether to return the bytes (True), 
            or the non-copying Message object in each place (False)
        
        """
        if not len(msg) >= 3:
            raise TypeError("malformed message, must have at least 3 elements")
        message = {}
        if not copy:
            for i in range(3):
                msg[i] = msg[i].bytes
        message['header'] = self.unpack(msg[0])
        message['msg_type'] = message['header']['msg_type']
        message['parent_header'] = self.unpack(msg[1])
        if content:
            message['content'] = self.unpack(msg[2])
        else:
            message['content'] = msg[2]
    
        # message['buffers'] = msg[3:]
        # else:
        #     message['header'] = self.unpack(msg[0].bytes)
        #     message['msg_type'] = message['header']['msg_type']
        #     message['parent_header'] = self.unpack(msg[1].bytes)
        #     if content:
        #         message['content'] = self.unpack(msg[2].bytes)
        #     else:
        #         message['content'] = msg[2].bytes
        
        message['buffers'] = msg[3:]# [ m.buffer for m in msg[3:] ]
        return message
            
        

def test_msg2obj():
    am = dict(x=1)
    ao = Message(am)
    assert ao.x == am['x']

    am['y'] = dict(z=1)
    ao = Message(am)
    assert ao.y.z == am['y']['z']
    
    k1, k2 = 'y', 'z'
    assert ao[k1][k2] == am[k1][k2]
    
    am2 = dict(ao)
    assert am['x'] == am2['x']
    assert am['y']['z'] == am2['y']['z']
