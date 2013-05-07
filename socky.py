#!/usr/bin/env python3
# -*- coding: UTF-8 -*-

from whoosh.fields import Schema, TEXT, STORED, ID 
from whoosh.index import create_in, open_dir
from whoosh.query import Term, FuzzyTerm, Or
from whoosh.analysis import StemmingAnalyzer

from irclib.client import client
from irclib.common.line import Line

from functools import partial
from collections import OrderedDict
import shelve
import random
import os, time
import re

# Globals deaugh
ix = None

parser = re.compile("""
    \[(.+)\]        # Match first portion
    (?:\W+)?        # Eat whitespace
    ([~!=@\-\#\$])  # Type of command (addition predicates ~!=), @ (search), -
                    # (delete), # (chan event), or $ (command)
    (?:\W+)?        # Eat whitespace
    \[(.+)\]        # Second portion
""", re.VERBOSE|re.UNICODE)

# Bootstrap admins (always allowed)
admins = ['Elizacat', 'SilentPenguin']
cfgadmins = []

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
        self.db = kwargs.get('db', 'socky')

        self.load_admins()

        self.add_dispatch_in('PRIVMSG', 1000, self.handle_privmsg)
        self.add_dispatch_in('JOIN', 1000, self.handle_join)
        self.add_dispatch_in('QUIT', 1000, self.handle_exit)
        self.add_dispatch_in('PART', 1000, self.handle_exit)
        self.add_dispatch_in('KICK', 1000, self.handle_kick)

    def handle_join(self, discard, line):
        if not line.hostmask: return

        # 1 in 5 chance
        if random.randint(1, 5) != 5: return

        target = line.params[0]
        nick = line.hostmask.nick

        # Don't trigger on ourselves
        if nick == self.current_nick: return

        searcher = ix.searcher()
        results = searcher.search(Term('querytype', 'JOIN'))
        if len(results) == 0: return

        response = random.choice(results)['response']
        response = build_response(response, who=nick, where=target,
                                  mynick=self.current_nick)
        sayfunc = partial(self.cmdwrite, 'PRIVMSG', (target, response))
        self.timer_oneshot('socky_joinspew', random.randint(10, 30) / 10, sayfunc)

    def handle_exit(self, discard, line):
        if not line.hostmask: return

        # 1 in 5 chance
        if random.randint(1, 5) != 5: return

        target = line.params[0]
        nick = line.hostmask.nick

        # Don't trigger on ourselves
        if nick == self.current_nick: return

        searcher = ix.searcher()
        results = searcher.search(Term('querytype', 'EXIT'))
        if len(results) == 0: return

        response = random.choice(results)['response']
        response = build_response(response, who=nick, where=target,
                                  mynick=self.current_nick)
        sayfunc = partial(self.cmdwrite, 'PRIVMSG', (target, response))
        self.timer_oneshot('socky_exitspew', random.randint(10, 30) / 10, sayfunc)

    def handle_kick(self, discard, line):
        if not line.hostmask: return
        if not line.hostmask.nick: return

        target = line.params[0]
        nick = line.params[1]

        # Don't trigger on ourselves
        if nick == self.current_nick: return

        searcher = ix.searcher()
        results = searcher.search(Term('querytype', 'EXIT'))
        if len(results) == 0: return

        response = random.choice(results)['response']
        response = build_response(response, who=nick, where=target,
                                  mynick=self.current_nick)
        sayfunc = partial(self.cmdwrite, 'PRIVMSG', (target, response))
        self.timer_oneshot('socky_exitspew', random.randint(10, 30) / 10,
                           sayfunc)

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
        if time.time() - self.lastsaid < 60:
            return

        query = make_query(message)
        searcher = ix.searcher()
        results = searcher.search(query)
        if len(results) == 0: return

        response = select_query(message, results)
        if not response: return

        response = build_response(response, who=line.hostmask.nick,
                                  where=target, mynick=self.current_nick)

        sayfunc = partial(self.cmdwrite, 'PRIVMSG', (target, response))
        self.timer_oneshot('socky_spew', random.randint(10, 50) / 10, sayfunc)

        self.lastsaid = time.time()

    def handle_command(self, line, target, message, useaction):
        nick = self.nickchan_lower(line.hostmask.nick)

        if nick not in self.users: return

        account = self.users[nick].account

        # No account?
        if not account or account == '*': return

        account = account.lower()

        # Not an admin?
        if account not in self.admins:
            print('Permission denied for account', account)
            return

        # Parse
        parsed = parser.match(message)
        if not parsed: return

        # Split
        firstparam, type_, secondparam = parsed.groups()
        firstparam = firstparam.lower()

        types = {
            '=':'MATCHALL',
            '!':'LITERAL',
            '~':'FUZZY',
            '#':'CHANEVENT'
        }

        if type_ in types:
            # Add
            type_ = types[type_]
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
                self.cmdwrite('PRIVMSG', (target, 'Adios'))
                self.quitme(secondparam)
            elif firstparam == 'reload':
                secondparam = secondparam.lower()
                if secondparam.startswith('admin'):
                    self.load_admins()
                    self.cmdwrite('PRIVMSG', (target, 'As you wish.'))
            elif firstparam == 'addadmin':
                secondparam = secondparam.lower()
                self.add_admin(secondparam)
                self.cmdwrite('PRIVMSG', (target, 'New boss added to the obedience file'))
            elif firstparam == 'deladmin':
                secondparam = secondparam.lower()
                self.del_admin(secondparam)
                self.cmdwrite('PRIVMSG', (target, 'Boss has been removed from the obedience file'))
            else:
                return
        else:
            return
    
    def quitme(self, message=''):
        self.quitme = True
        self.cmdwrite('QUIT', (message,))

    def handle_quoteadd(self, line, target, trigger, type_, response, useaction):
        if type_ == 'CHANEVENT':
            if trigger.startswith('join'):
                type_ = 'JOIN'
            elif (trigger.startswith('part') or trigger.startswith('quit') or
                  trigger.startswith('exit')):
                type_ = 'EXIT'
            else:
                return

        writer = ix.writer()
        try:
            writer.add_document(trigger=trigger, querytype=type_,
                                response=response, useaction=useaction)
        except Exception as e:
            self.cmdwrite('PRIVMSG', (target, 'Error: ' + str(e)))
            return

        writer.commit()
        if useaction:
            self.ctcpwrite(target, 'ACTION', 'your humour has been added to the hive')
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

    def load_admins(self):
        # Bootstrap admins
        self.admins = set([x.lower() for x in admins])

        s = shelve.open(self.db)
        if 'admins' not in s:
            s['admins'] = set()

        self.admins = self.admins.union(s['admins'])

        s.close()

    def add_admin(self, admin):
        s = shelve.open(self.db)
        if 'admins' not in s:
            newadmins = set()
        else:
            newadmins = s['admins']

        admin = admin.lower()

        newadmins.add(admin)
        s['admins'] = newadmins
        s.close()

        self.admins.add(admin) 

    def del_admin(self, admin):
        s = shelve.open(self.db)
        if 'admins' not in s: return

        admin = admin.lower()

        if admin not in admins:
            # Don't discard the bootstrapped admins!
            self.admins.discard(admin)

        newadmins = s['admins']
        newadmins.discard(admin)
        s['admins'] = newadmins
        s.close()

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
    'db' : 'interlinked',
}

# Initalise the DB or create it
if not os.path.exists("index"):
    trigtype = TEXT(stored=True, chars=True, vector=True)
    schema = Schema(trigger=trigtype, querytype=ID(stored=True), useaction=STORED, response=STORED)
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

