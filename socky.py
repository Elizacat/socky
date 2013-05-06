#!/usr/bin/env python3
# -*- coding: UTF-8 -*-

from whoosh.fields import Schema, TEXT, STORED
from whoosh.index import create_in, open_dir
from whoosh.query import FuzzyTerm, Or

from irclib.client import client
from irclib.common.line import Line

from collections import OrderedDict 
import random
import os, time
import re

# Globals deaugh
ix = None

parser = re.compile("""
    \[(.+)\]        # Match first portion
    (?:\W+)?        # Eat whitespace
    ([~!=@\$\-])    # Type of command (addition predicates ~!=), @ (search), -
                    # (delete), or $ (command)
    (?:\W+)?        # Eat whitespace
    \[(.+)\]        # Second portion
""", re.VERBOSE|re.UNICODE)

# XXX hardcoded
admins = ['Elizacat', 'SilentPenguin']

def make_query(text):
    return Or([FuzzyTerm('trigger', t) for t in text.split()]) 

def select_query(message, results):
    newresults = []
    for result in results:
        querytype = result['querytype']
        trigger = result['trigger']
        if querytype == 'MATCHALL':
            if trigger not in message: continue
        elif querytype == 'LITERAL':
            if trigger != message: continue
        elif querytype == 'FUZZY':
            # Fall through
            pass
        else:
            continue

        # XXX FIXME a hack for now!
        response = result['response']
        if result['useaction'] == True:
            response = '\x01ACTION ' + response + '\x01'

        newresults.append(response)

    if not newresults: return None
    return random.choice(newresults)

def build_response(response, **kwargs):
    # safe dictionary building
    try:
        return response.format(**kwargs)
    except (KeyError, ValueError) as e:
        return response

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

        useaction = False
        if message.startswith('\x01'):
            # CTCP stripping
            message = message.strip('\x01')
            type_, sep, message = message.partition(' ')

            if type_.lower() == 'action':
                useaction = True

        if message.startswith(self.current_nick):
            # Cut off the nick
            newmessage = message[len(self.current_nick):]

            # Cut off non-alphanum
            while (newmessage and (not newmessage[0].isalnum()) and
                   (newmessage[0] not in ('[', ']'))):
                newmessage = newmessage[1:]

            if newmessage:
                self.handle_command(line, target, newmessage, useaction)
                return

        # Check last said time
        if time.time() - self.lastsaid < 15:
            return

        # Lowercase for trigger search
        message = message.lower()

        query = make_query(message)
        searcher = ix.searcher()
        results = searcher.search(query)
        if len(results) == 0: return

        response = select_query(message, results)
        if not response: return

        response = build_response(response, who=line.hostmask.nick,
                                  where=target, mynick=self.current_nick)
        self.cmdwrite('PRIVMSG', (target, response))

        self.lastsaid = time.time()

    def handle_command(self, line, target, message, useaction):
        nick = self.nickchan_lower(line.hostmask.nick)

        if nick not in self.users: return

        account = self.users[nick].account

        # No account?
        if not account or account == '*': return

        # Not an admin?
        if account not in admins: return

        # Parse
        parsed = parser.match(message)
        print(message, parsed)
        if not parsed: return

        # Split
        firstparam, type_, secondparam = parsed.groups()
        firstparam = firstparam.lower()

        if type_ in ('=', '!', '~'):
            # Add
            if type_ == '=':
                type_ = 'MATCHALL'
            elif type_ == '!':
                type_ = 'LITERAL'
            elif type_ == '~':
                type_ = 'FUZZY'
            else:
                return

            self.handle_quoteadd(line, target, firstparam, type_, secondparam,
                                 useaction)
        elif type_ == '@':
            if firstparam == 'text':
                self.handle_quotesearch(line, target, secondparam)
            else:
                # TODO other types, e.g. ID
                return
        elif type_ == '-':
            # Delete
            if firstparam == 'all':
                self.handle_quotedel_all(line, target, secondparam)
            elif firstparam.startswith('num'):
                self.handle_quotedel_single(line, target, secondparam)
            else:
                return
        elif type_ == '$':
            if firstparam == 'quit':
                self.quitme(secondparam)
            else:
                return
        else:
            return
    
    def quitme(self, message=''):
        self.quitme = True
        self.cmdwrite('QUIT', (message,))

    def handle_quoteadd(self, line, target, trigger, type_, response, useaction):
        writer = ix.writer()
        try:
            writer.add_document(trigger=trigger, querytype=type_,
                                response=response, useaction=useaction)
        except Exception as e:
            self.cmdwrite('PRIVMSG', (target, 'Error: ' + str(e)))
            return

        writer.commit()
        if useaction:
            self.ctcpwrite(target, 'ACTION', 'Your humour has been added to the hive')
        else:
            self.cmdwrite('PRIVMSG', (target, 'Your humour has been added to the hive'))

    def handle_quotesearch(self, line, target, searchterm):
        limit = 425 # rather arbitrary

        query = make_query(searchterm.lower())
        searcher = ix.searcher()
        results = searcher.search(query)
        if len(results) == 0:
            self.cmdwrite('PRIVMSG', (target, 'Drawing a blank here :/'))
            return

        responses = OrderedDict()
        for index, result in enumerate(results):
            trigger = result['trigger']
            response = result['response']
            querytype = result['querytype']
            useaction = '* ' if result['useaction'] else ''

            if querytype == 'MATCHALL': querytype = '='
            elif querytype == 'LITERAL': querytype = '!'
            elif querytype == 'FUZZY': querytype = '~'
            else: querytype = '?'

            docnum = results.docnum(index)

            if trigger not in responses:
                responses[trigger] = list()

            responses[trigger].append((docnum, response, querytype, useaction))

        # Iterate through responses
        for k, v in responses.items():
            start = '[' + k + ' # '
            curstr = start
            for x in v:
                docnum, response, querytype, useaction = x
                new = '{d}: {q} {u}{r} & '.format(d=docnum, q=querytype,
                                                  u=useaction, r=response)
                if len(curstr) + len(new) > limit:
                    curstr = curstr[:-3]
                    curstr += ']'
                    self.cmdwrite('PRIVMSG', (target, curstr))
                    curstr = start

                curstr += new
            
            curstr = curstr[:-3]
            curstr += ']'
            self.cmdwrite('PRIVMSG', (target, curstr))

    def handle_quotedel_single(self, line, target, num):
        if not isinstance(num, int):
            try:
                num = int(num)
            except ValueError:
                self.cmdwrite('PRIVMSG', (target, 'Dumbass.'))
                return

        writer = ix.writer()
        try:
            writer.delete_document(num)
        except Exception as e:
            self.cmdwrite('PRIVMSG', (target, 'Error: ' + str(e)))
            return

        writer.commit()
        self.cmdwrite('PRIVMSG', (target, 'Humour has been removed from the hive'))

    def handle_quotedel_all(self, line, target, trigger):
        writer = ix.writer()
        try:
            writer.delete_by_term('trigger', trigger)
        except Exception as e:
            self.cmdwrite('PRIVMSG', (target, 'Error: ' + str(e)))
            return

        writer.commit()
        self.cmdwrite('PRIVMSG', (target, 'Humour has been purged from the hive'))

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
                    querytype=STORED, useaction=STORED, response=STORED)
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

