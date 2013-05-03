#!/usr/bin/env python3
# -*- coding: UTF-8 -*-

from whoosh.fields import Schema, TEXT, STORED
from whoosh.index import create_in, open_dir
from whoosh.query import Term, Or

from irclib.client import client
from irclib.common.line import Line

import os, time
import re

# Globals deaugh
ix = None

# XXX hardcoded
admins = ['Elizacat', 'SilentPenguin']

def make_query(text):
    return Or([Term('trigger', t) for t in text.split()]) 

class SockyIRCClient(client.IRCClient):
    def __init__(self, *args, **kwargs):
        client.IRCClient.__init__(self, *args, **kwargs)

        self.quitmme = False
        self.lastsaid = 0

        self.add_dispatch_in('PRIVMSG', 1000, self.handle_privmsg)

    def handle_privmsg(self, discard, line):
        if len(line.params) <= 1: return
        if not line.hostmask: return

        target = self.nickchan_lower(line.params[0])
        message = line.params[-1]

        # Select the correct target
        if target[0] not in self.isupport['CHANTYPES']:
            target = line.hostmask.nick

        if message.startswith('\x01'):
            # CTCP stripping
            message = message.strip('\x01')
            junk, sep, message = message.partition(' ')

        if message.startswith(self.current_nick):
            # Cut off the nick
            newmessage = message[len(self.current_nick):]

            # Cut off non-alphanum
            while newmessage and (not newmessage[0].isalnum()):
                newmessage = newmessage[1:]

            if newmessage:
                self.handle_command(line, target, newmessage)
                return

        # Check last said time
        if time.time() - self.lastsaid < 15:
            return

        # Lowercase for trigger search
        message = message.lower()

        query = make_query(message)
        searcher = ix.searcher()
        results = searcher.search(query)
        print(results)
        if len(results) == 0: return

        # Best match wins
        # XXX this logic will need to change at some point to scan for literal
        # matches...
        response = results[0]['response']
        self.cmdwrite('PRIVMSG', (target, response))

        self.lastsaid = time.time()

    def handle_command(self, line, target, message):
        nick = self.nickchan_lower(line.hostmask.nick)

        if nick not in self.users: return

        account = self.users[nick].account

        # No account?
        if not account or account == '*': return

        if account not in admins: return

        command, sep, params = message.partition(' ')
        command = command.lower()

        if command == 'add': self.handle_quoteadd(line, target, params)
        elif command == 'del': self.handle_quotedel(line, target, params)
        elif command == 'quit': self.quitme()
    
    def quitme(self):
        self.quitme = True
        self.cmdwrite('QUIT')

    def handle_quoteadd(self, line, target, params):
        # XXX Different types
        parsed = re.match('\[(.+)\]\W?=\W?\[(.+)\]', params)

        if not parsed: return

        trigger, response = parsed.groups()
        trigger = trigger.lower()

        writer = ix.writer()
        try:
            writer.add_document(trigger=trigger, querytype='MATCHALL',
                                response=response)
        except Exception as e:
            self.cmdwrite('PRIVMSG', (target, 'Error: ' + str(e)))
            return

        writer.commit()
        self.cmdwrite('PRIVMSG', (target, 'Done'))

    def handle_quotedel(self, line, target, params):
        # XXX delete by ID
        parsed = re.match('\[(.+)\]', params)

        if not parsed: return

        trigger = parsed.group(1)
        trigger = trigger.lower()

        writer = ix.writer()
        try:
            writer.delete_by_term('trigger', trigger)
        except Exception as e:
            self.cmdwrite('PRIVMSG', (target, 'Error: ' + str(e)))
            return

        writer.commit()
        self.cmdwrite('PRIVMSG', (target, 'Done'))


def run(instance):
    try:
        generator = instance.get_lines()
        for line in generator: pass
    except (OSError, IOError) as e:
        if instance.quitme: quit()
        print("Disconnected", str(e))
        time.sleep(5)

kwargs = {
    'nick' : 'Socky',
    'host' : 'okami.interlinked.me',
    'port' : 6667,
    'channels' : ['#irclib', '#sporks'],
    'use_sasl' : True,
    'sasl_username' : 'Socky',
    'sasl_pw' : 'changeme',
}

# Initalise the DB or create it
if not os.path.exists("index"):
    schema = Schema(trigger=TEXT(stored=True, chars=True, vector=True),
                    querytype=STORED, response=STORED)
    os.mkdir("index")
    ix = create_in("index", schema)
else:
    ix = open_dir("index")

instance = SockyIRCClient(**kwargs)

while True:
    try:
        run(instance)
    except BaseException as e:
        print('Exception caught:', e)
        instance.terminate()
        raise

