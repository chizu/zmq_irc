#!/usr/bin/env python
import sys
import json
import time
from collections import defaultdict

from zmq.core import constants
from txzmq import ZmqEndpoint
from twisted.internet import protocol, reactor, defer
from twisted.internet.endpoints import TCP4ClientEndpoint, SSL4ClientEndpoint
from twisted.internet.ssl import ClientContextFactory
from twisted.words.protocols import irc

from connections import *

e = ZmqEndpoint("connect", "tcp://127.0.0.1:9913")
event_publisher = ZmqPushConnection(zmqfactory, "client", e)

class RemoteEventPublisher(object):
    def __init__(self, network, identity, email, initial_event=0):
        self.network = network
        self.identity = identity
        self.email = email
        self.current_id = initial_event

    def event(self, kind, *args):
        self.current_id += 1
        send = [self.email, str(self.current_id), self.network,
                self.identity, kind, str(time.time())]
        send.extend(args)
        print(send)
        for i, value in enumerate(send):
            if isinstance(value, unicode):
                send[i] = value.encode("utf-8")
        event_publisher.send(send)


class NamesIRCClient(irc.IRCClient):
    def __init__(self, *args, **kwargs):
        self._namescallback = {}

    def names(self, channel):
        channel = channel.lower()
        d = defer.Deferred()
        if channel not in self._namescallback:
            self._namescallback[channel] = ([], [])

        self._namescallback[channel][0].append(d)
        self.sendLine("NAMES %s" % channel)
        return d

    def irc_RPL_NAMREPLY(self, prefix, params):
        channel = params[2].lower()
        nicklist = params[3].split(' ')

        if channel not in self._namescallback:
            return

        n = self._namescallback[channel][1]
        n += nicklist

    def irc_RPL_ENDOFNAMES(self, prefix, params):
        channel = params[1].lower()
        if channel not in self._namescallback:
            return

        callbacks, namelist = self._namescallback[channel]

        for cb in callbacks:
            cb.callback(namelist)

        del(self._namescallback[channel])


class Client(NamesIRCClient):
    @property
    def email(self):
        return self.factory.email

    @property
    def nickname(self):
        return self.factory.nickname

    @property
    def network(self):
        return self.factory.network

    def signedOn(self):
        self.channels = list()
        self.publish = RemoteEventPublisher(self.network, self.nickname, self.email)
        self.publish.event("signedOn", self.nickname)

    def got_names(self, nicklist, channel):
        print("Got {0} nicklist {1}".format(channel, nicklist))
        self.publish.event("names", channel, *nicklist)

    def topicUpdated(self, user, channel, newTopic):
        self.publish.event("topic", channel, newTopic)

    def joined(self, channel):
        self.channels.append(channel)
        self.names(channel).addCallback(self.got_names, channel)
        self.topic(channel)
        self.publish.event("joined", channel)

    def userJoined(self, user, channel):
        self.publish.event("userJoined", user, channel)

    def left(self, channel):
        self.channels.pop(channel)
        self.publish.event("left", channel)

    def userLeft(self, user, channel):
        self.publish.event("userLeft", user, channel)

    def userQuit(self, user, msg):
        self.publish.event("userQuit", user, msg)

    def userKicked(self, user, channel, kicker, msg):
        self.publish.event("userKicked", user, channel, kicker, msg)

    def userRenamed(self, oldname, newname):
        self.publish.event("userRenamed", oldname, newname)

    def privmsg(self, user, channel, msg):
        self.publish.event("privmsg", user, channel, msg)

    def noticed(self, user, channel, msg):
        self.publish.event("notice", user, channel, msg)

    def action(self, user, channel, msg):
        self.publish.event("action", user, channel, msg)


class ClientFactory(protocol.ClientFactory):
    protocol = Client

    def __init__(self, email, network, nickname='zmq_irc_bridge'):
        self.email = email
        self.network = network
        self.nickname = nickname

    def clientConnectionLost(self, connector, reason):
        print("Lost connection ({0}), reconnecting.".format(reason))
        connector.connect()

    def clientConnectionFailed(self, connector, reason):
        print("Could not connect: {0}" % (reason))


class IRCController(ZmqPullConnection):
    def __init__(self, zf, e, irc):
        self.irc = irc
        super(IRCController, self).__init__(zf, e)

    def connectCallback(self, rows, user, nick):
        hostname, port, ssl = rows[0]

    def joinCallback(self, rows, server):
        server.join(*rows[0])

    def messageReceived(self, message):
        """Protocol is this:

        Servers control: {user} global {command} {arguments...}
        Connection control: {user} {server} {command} {arguments...}
        """
        print("Controller message: {0}".format(message))
        user, server = message[:2]
        command = message[2]
        if len(message) >= 3:
            command_args = message[3:]
        else:
            command_args = None
        subject = self.irc.clients[user]
        # If there's a server specified, pick that server here
        if server != "global" and command != "connect":
            subject = subject[server]
        # Otherwise, the iterable subject means for all servers
        if command == 'connect':
            # Arguments are 'nick port [ssl]'
            hostname, nick, port, ssl = command_args
            self.irc.server_connect(user, hostname, port, ssl, nick)
        elif command == 'join':
            # Arguments are 'channel [key]'
            subject.join(*command_args[:2])
        elif command == 'msg':
            # Arguments are 'target message ...'
            target = command_args[0]
            msg = command_args[1]
            subject.msg(target, msg, 500 - len(target))
            subject.publish.event("privmsg", subject.nickname, target, msg)
        elif command == 'action':
            target = command_args[0]
            msg = command_args[1]
            # IRCClient.me prepends # erroneously
            subject.ctcpMakeQuery(target, [('ACTION', command_args[1])])
            subject.publish.event("action", subject.nickname, target, msg)
        elif command == 'names':
            channel = command_args[0]
            subject.names(channel).addCallback(subject.got_names, channel)


class IRC(object):
    def __init__(self):
        # Dict of all clients indexed by email
        self.clients = defaultdict(dict)
        e = ZmqEndpoint("bind", "tcp://127.0.0.1:9912")
        self.socket = IRCController(zmqfactory, e, self)
        self.ssl_context = ClientContextFactory()

    def start(self):
        """Initialize the IRC client.

        Load existing configuration and join all clients."""
        self.server_init([('default@default', 'irc.freenode.org',
                           6697, True, 'zmq_irc_bridge')])

    def server_init(self, servers_config):
        """Do connections for all configured clients."""
        for email, hostname, port, ssl, nick in servers_config:
            self.server_connect(email, hostname, port, ssl, nick)

    def server_connect(self, email, hostname, port, ssl, nick):
        """Connect to an IRC server."""
        # We're reusing hostnames as network names for now
        client_f = ClientFactory(email, hostname, nick)
        if ssl:
            point = SSL4ClientEndpoint(reactor, hostname, port,
                                       self.ssl_context)
        else:
            point = TCP4ClientEndpoint(reactor, hostname, port)
        d = point.connect(client_f)
        d.addCallback(self.register_client, email)

    def register_client(self, client, email):
        """Register each client so they can be managed later."""
        self.clients[email][client.network] = client
        print("Registered '{0}' for user '{1}'".format(client.network, email))
        print(self.clients)


if __name__ == "__main__":
    irc = IRC()
    irc.start()

    reactor.run()
