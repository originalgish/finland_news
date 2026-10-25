-- Seed the five outlets, the starter tag set, and feed category mappings.
--
-- Feed URLs were checked on 2026-10-25: each returned HTTP 200 and parsed with
-- feedparser into current items. Check them again before changing a URL.
--   Yle:         feeds.yle.fi redirects to the canonical URL used here.
--   Iltalehti:   tuoreimmat.xml returns zero items, so uutiset.xml is used.
--   Kauppalehti: www.kauppalehti.fi/rss is an HTML index; it links this feed.
--
-- Category strings match what each feed sends, including case (Iltalehti is lowercase).
-- Only clear matches are mapped. Other categories still reach articles.feed_categories.

INSERT INTO sources (name, feed_url, language) VALUES
    ('Yle',               'https://yle.fi/rss/uutiset/paauutiset',    'fi'),
    ('Helsingin Sanomat', 'https://www.hs.fi/rss/tuoreimmat.xml',     'fi'),
    ('Ilta-Sanomat',      'https://www.is.fi/rss/tuoreimmat.xml',     'fi'),
    ('Iltalehti',         'https://www.iltalehti.fi/rss/uutiset.xml', 'fi'),
    ('Kauppalehti',       'https://feeds.kauppalehti.fi/rss/main',    'fi');

INSERT INTO tags (name) VALUES
    ('politics'), ('economy'), ('business'), ('sports'), ('culture'), ('crime'),
    ('health'), ('tech'), ('world'), ('local'), ('weather');

INSERT INTO feed_category_map (source_id, category, tag_id)
SELECT s.id, m.category, t.id
FROM (VALUES
    -- Yle
    ('Yle', 'Ulkomaat', 'world'),
    ('Yle', 'Talous', 'economy'),
    ('Yle', 'Politiikka', 'politics'),
    ('Yle', 'Vaalit', 'politics'),
    ('Yle', 'Sää', 'weather'),
    ('Yle', 'Sääennusteet', 'weather'),

    -- Helsingin Sanomat
    ('Helsingin Sanomat', 'Politiikka', 'politics'),
    ('Helsingin Sanomat', 'Maailma', 'world'),
    ('Helsingin Sanomat', 'Urheilu', 'sports'),
    ('Helsingin Sanomat', 'Kulttuuri', 'culture'),
    ('Helsingin Sanomat', 'Popkulttuuri', 'culture'),
    ('Helsingin Sanomat', 'Helsinki', 'local'),
    ('Helsingin Sanomat', 'Tiede', 'tech'),
    ('Helsingin Sanomat', 'Pelit', 'tech'),

    -- Ilta-Sanomat
    ('Ilta-Sanomat', 'Ulkomaat', 'world'),
    ('Ilta-Sanomat', 'Politiikka', 'politics'),
    ('Ilta-Sanomat', 'Taloussanomat', 'economy'),
    ('Ilta-Sanomat', 'Urheilu', 'sports'),
    ('Ilta-Sanomat', 'NHL', 'sports'),
    ('Ilta-Sanomat', 'SM-liiga', 'sports'),
    ('Ilta-Sanomat', 'NBA', 'sports'),
    ('Ilta-Sanomat', 'Formula 1', 'sports'),
    ('Ilta-Sanomat', 'Jääkiekko', 'sports'),
    ('Ilta-Sanomat', 'Jalkapallo', 'sports'),
    ('Ilta-Sanomat', 'Valioliiga', 'sports'),
    ('Ilta-Sanomat', 'Veikkausliiga', 'sports'),
    ('Ilta-Sanomat', 'Koripallo', 'sports'),
    ('Ilta-Sanomat', 'Yleisurheilu', 'sports'),
    ('Ilta-Sanomat', 'Alppihiihto', 'sports'),
    ('Ilta-Sanomat', 'Kamppailulajit', 'sports'),
    ('Ilta-Sanomat', 'Viihde', 'culture'),
    ('Ilta-Sanomat', 'TV & elokuvat', 'culture'),
    ('Ilta-Sanomat', 'Musiikki', 'culture'),
    ('Ilta-Sanomat', 'Terveys', 'health'),
    ('Ilta-Sanomat', 'Digitoday', 'tech'),
    ('Ilta-Sanomat', 'Tietoturva', 'tech'),
    ('Ilta-Sanomat', 'Turun seutu', 'local'),

    -- Iltalehti
    ('Iltalehti', 'politiikka', 'politics'),
    ('Iltalehti', 'ulkomaat', 'world'),
    ('Iltalehti', 'talous', 'economy'),

    -- Kauppalehti
    ('Kauppalehti', 'Politiikka', 'politics'),
    ('Kauppalehti', 'Talousaamu', 'economy'),
    ('Kauppalehti', 'Markkinaraati', 'economy'),
    ('Kauppalehti', 'Sijoittaminen', 'economy'),
    ('Kauppalehti', 'Kauppa', 'business'),
    ('Kauppalehti', 'Yrittäminen', 'business')
) AS m(source_name, category, tag_name)
JOIN sources s ON s.name = m.source_name
JOIN tags t ON t.name = m.tag_name;
