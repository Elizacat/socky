#!/usr/bin/env python3

from datetime import datetime

from whoosh.fields import Schema, TEXT, STORED, ID, DATETIME, BOOLEAN
from whoosh.index import create_in, open_dir
from whoosh.analysis import RegexTokenizer, LowercaseFilter

# Define the NEW schema here
defaultvalues = {
    # new schema values
    'who' : 'Elizacat',
    'time' : datetime.now(),
}

analyzer = RegexTokenizer('[\w:;=]+') | LowercaseFilter()
trigtype = TEXT(stored=True, chars=True, analyzer=analyzer)
schema = Schema(trigger=trigtype, querytype=ID(stored=True),
                useaction=BOOLEAN(stored=True),
                response=TEXT(stored=True, chars=True), who=ID(stored=True),
                time=DATETIME(stored=True))

ix = open_dir("index")

docs = [x for x in ix.searcher().documents()]
ix.close()

ix = create_in("index", schema)
writer = ix.writer()
for x in docs:
    x.update(defaultvalues)
    writer.add_document(**x)

writer.commit()

