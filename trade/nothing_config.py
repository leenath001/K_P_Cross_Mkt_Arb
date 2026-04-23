"""
nothing_config.py — Curated series list for the "Nothing Ever Happens" bot.

Only non-sport Kalshi series belong here. Sports tickers are blocklisted
in nothing.py via config.SPORTS_CONFIG.

Shape:
    NOTHING_SERIES = [
        ('KXSERIES',        'label',           category),
        ...
    ]

`category` is free-form (e.g. 'politics', 'econ', 'crypto', 'pop').
"""

NOTHING_SERIES = [
    # Core Trump-mention markets — "will Trump say X". Most specific words
    # don't get said in a given speech/window, so NO tends to resolve.
    ('KXTRUMPMENTION',        'What will Trump say?',      'mentions'),

    # Earnings-call mention markets — will company CEO say word X on call.
    # Same "nothing" thesis: most specific words aren't mentioned.
    ('KXEARNINGSMENTIONUBER', 'Uber earnings mentions',       'mentions'),
    ('KXEARNINGSMENTIONLYFT', 'Lyft earnings mentions',       'mentions'),
    ('KXEARNINGSMENTIONABNB', 'ABNB earnings mentions',       'mentions'),
    ('KXEARNINGSMENTIONAMZN', 'Amazon earnings mentions',     'mentions'),
    ('KXEARNINGSMENTIONAAPL', 'Apple earnings mentions',      'mentions'),
    ('KXEARNINGSMENTIONSBUX', 'Starbucks earnings mentions',  'mentions'),
    ('KXEARNINGSMENTIONMETA', 'Meta earnings mentions',       'mentions'),
    ('KXEARNINGSMENTIONV',    'Visa earnings mentions',       'mentions'),
    ('KXEARNINGSMENTIONINTC', 'Intel earnings mentions',      'mentions'),
    ('KXEARNINGSMENTIONSPOT', 'Spotify earnings mentions',    'mentions'),
    ('KXEARNINGSMENTIONHOOD', 'Robinhood earnings mentions',  'mentions'),
    ('KXEARNINGSMENTIONRDDT', 'Reddit earnings mentions',     'mentions'),
    ('KXEARNINGSMENTIONMSFT', 'Microsoft earnings mentions',  'mentions'),
    ('KXEARNINGSMENTIONKO',   'Coca-Cola earnings mentions',  'mentions'),
    ('KXFEDMENTION',          'Fed meeting mentions',         'mentions'),

    # Candidates worth evaluating but not added by default:
    # ('KXLABORANNOUNCE',     'Trump Labor Sec announce',  'politics'),  # time-bucket
    # ('KXLABORSECCONF',      'Labor Sec confirmation',    'politics'),  # time-bucket
]


def tickers() -> list:
    """Return just the ticker strings for use as default --series."""
    return [t for t, _label, _cat in NOTHING_SERIES]
