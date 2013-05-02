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
lastsaid = 0
quitme = False

# XXX hardcoded
admins = ['Elizacat', 'SilentPenguin']

def make_query(text):
    orlist = []
    for token in text.split():
        orlist.append(Term('trigger', token))

    return Or(orlist)

def handle_quoteadd(instance, line, target, params):
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
        instance.cmdwrite('PRIVMSG', (target, 'Error: ' + str(e)))
        return

    writer.commit()
    instance.cmdwrite('PRIVMSG', (target, 'Done'))

def handle_quotedel(instance, line, target, params):
    # XXX delete by ID
    parsed = re.match('\[(.+)\]', params)

    if not parsed: return

    trigger = parsed.group(1)
    trigger = trigger.lower()

    writer = ix.writer()
    try:
        writer.delete_by_term('trigger', trigger)
    except Exception as e:
        instance.cmdwrite('PRIVMSG', (target, 'Error: ' + str(e)))
        return

    writer.commit()
    instance.cmdwrite('PRIVMSG', (target, 'Done'))

def handle_command(instance, line, target, message):
    nick = instance.nickchan_lower(line.hostmask.nick)

    if nick not in instance.users: return

    account = instance.users[nick].account

    # No account?
    if not account or account == '*': return

    if account not in admins: return

    command, sep, params = message.partition(' ')
    command = command.lower()

    global quitme

    if command == 'add': handle_quoteadd(instance, line, target, params)
    elif command == 'del': handle_quotedel(instance, line, target, params)
    elif command == 'quit': quitme = True

def handle_privmsg(instance, line):
    if len(line.params) <= 1: return
    if not line.hostmask: return

    target = instance.nickchan_lower(line.params[0])
    message = line.params[-1]

    # Select the correct target
    if target[0] not in instance.isupport['CHANTYPES']:
        target = line.hostmask.nick

    if message.startswith('\x01'):
        # CTCP stripping
        message = message.strip('\x01')
        junk, sep, message = message.partition(' ')

    if message.startswith(instance.current_nick):
        print("startswith", message)
        # Cut off the nick
        newmessage = message[len(instance.current_nick):]

        # Cut off non-alphanum
        while newmessage and (not newmessage[0].isalnum()):
            newmessage = newmessage[1:]

        if newmessage:
            handle_command(instance, line, target, newmessage)
            return

    global lastsaid

    # Check last said time
    if time.time() - lastsaid < 15:
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
    print(response)
    instance.cmdwrite('PRIVMSG', (target, response))

    lastsaid = time.time()

def run(instance):
    try:
        generator = instance.get_lines()
        for line in generator:
            if line.command == "PRIVMSG":
                handle_privmsg(instance, line)

        if quitme: instance.cmdwrite('QUIT')
    except (OSError, IOError) as e:
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

instance = client.IRCClient(**kwargs)

# Initalise the DB or create it
if not os.path.exists("index"):
    schema = Schema(trigger=TEXT(stored=True, chars=True, vector=True),
                    querytype=STORED, response=STORED)
    os.mkdir("index")
    ix = create_in("index", schema)
else:
    ix = open_dir("index")

while True:
    try:
        run(instance)
    except BaseException as e:
        if quitme: quit()
        print('Exception caught:', e)
        instance.terminate()
        raise
