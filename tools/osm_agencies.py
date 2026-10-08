"""Find estate agencies in one city from OpenStreetMap - on your own computer.

Why this exists: the public OpenStreetMap query servers refuse connections
from cloud hosting. They blocked whole AWS and Azure ranges in October 2025
after being abused from them, and the app runs on Render, which is cloud
hosting. Your own computer is not cloud hosting, so the same query works
from here.

It prints lines in exactly the shape the Prospects screen takes:

    https://agency.fr, Agency Name

So the whole job is: run it, copy the lines, paste them into
"Add prospects by hand", press Add them.

    python tools\\osm_agencies.py "Paris, France"
    python tools\\osm_agencies.py "Nice, France" --limit 60 --out nice.txt

Nothing is installed and nothing is sent anywhere except the two
OpenStreetMap services. It asks for one city at a time, waits a second
between requests and says who it is, which is what their usage policy asks
for. Please do not point it at a list of a hundred cities in a loop.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

NOMINATIM = 'https://nominatim.openstreetmap.org/search'
OVERPASS = 'https://overpass-api.de/api/interpreter'
AGENT = ('LuxuryLeadsAI-local/1.0 (+https://luxury-leads-ai.onrender.com; '
         'one city at a time, run by hand)')
TAGS = (('office', 'estate_agent'), ('shop', 'estate_agent'))
SHARED_HOSTS = ('facebook.com', 'instagram.com', 'linkedin.com', 'twitter.com',
                'x.com', 'youtube.com', 'wix.com', 'wixsite.com', 'google.com',
                'business.site', 'seloger.com', 'leboncoin.fr', 'rightmove.co.uk',
                'zillow.com', 'realtor.com', 'idealista.com', 'immoweb.be',
                'bayut.com', 'propertyfinder.ae', 'pap.fr', 'logic-immo.com')


def fetch(url, data=None, timeout=90):
    request = urllib.request.Request(
        url, data=data.encode('utf-8') if data else None,
        headers={'User-Agent': AGENT,
                 'Accept': 'application/json',
                 'Content-Type': 'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(request, timeout=timeout) as answer:
        return json.loads(answer.read().decode('utf-8', errors='replace'))


def box_for(city, country, server):
    query = urllib.parse.urlencode({'city': city, 'country': country,
                                    'format': 'json', 'limit': 1})
    rows = fetch(f"{server}?{query}", timeout=30)
    if not rows:
        raise SystemExit(f"OpenStreetMap does not know a city called {city}, {country}")
    south, north, west, east = (float(value) for value in rows[0]['boundingbox'])
    return south, north, west, east


def agencies_in(box, limit, server, seconds=60):
    south, north, west, east = box
    inside = f"{south},{west},{north},{east}"
    parts = ''.join(f'nwr["{key}"="{value}"]({inside});' for key, value in TAGS)
    query = f"[out:json][timeout:{seconds}];({parts});out center tags {limit};"
    answer = fetch(server, data=urllib.parse.urlencode({'data': query}),
                   timeout=seconds + 20)
    return answer.get('elements') or []


def domain_of(website):
    try:
        parsed = urllib.parse.urlparse(website if '//' in website
                                       else 'https://' + website)
    except ValueError:
        return ''
    host = (parsed.hostname or '').lower()
    return host[4:] if host.startswith('www.') else host


def lines_from(elements):
    """(paste-ready lines, how many were skipped and why)."""
    lines, seen = [], set()
    no_site = shared = duplicate = 0
    for element in elements:
        tags = element.get('tags') or {}
        website = (tags.get('website') or tags.get('contact:website')
                   or tags.get('url') or '').strip()
        if not website:
            no_site += 1
            continue
        host = domain_of(website)
        if not host or '.' not in host:
            no_site += 1
            continue
        if any(host == bad or host.endswith('.' + bad) for bad in SHARED_HOSTS):
            shared += 1
            continue
        if host in seen:
            duplicate += 1
            continue
        seen.add(host)
        name = (tags.get('name') or '').strip().replace(',', ' ')
        lines.append(f"https://{host}, {name}" if name else f"https://{host}")
    return lines, {'no website recorded': no_site,
                   'a page on someone else\'s site': shared,
                   'already in this list': duplicate}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Estate agencies in one city, from OpenStreetMap, ready to paste.')
    parser.add_argument('city', help='"Paris, France" - city then country')
    parser.add_argument('--limit', type=int, default=60,
                        help='most agencies to ask for (default 60)')
    parser.add_argument('--out', help='write the lines to this file as well')
    parser.add_argument('--nominatim', default=NOMINATIM)
    parser.add_argument('--overpass', default=OVERPASS)
    args = parser.parse_args(argv)

    if ',' not in args.city:
        raise SystemExit('Write the city as "Paris, France" - city, then country.')
    city, country = (part.strip() for part in args.city.split(',', 1))

    print(f"Looking up {city}, {country}...", file=sys.stderr)
    try:
        box = box_for(city, country, args.nominatim)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"OpenStreetMap answered {e.code} looking up the city. "
                         f"Wait a minute and try again.")
    time.sleep(1)                      # their usage policy: one request a second

    print(f"Asking OpenStreetMap for estate agents in that area...", file=sys.stderr)
    try:
        elements = agencies_in(box, args.limit, args.overpass)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise SystemExit("The OpenStreetMap query server is busy (429). "
                             "Wait a few minutes and run it again.")
        raise SystemExit(f"The OpenStreetMap query server answered {e.code}.")
    except urllib.error.URLError as e:
        raise SystemExit(f"Could not reach {args.overpass}: {e.reason}")

    lines, skipped = lines_from(elements)
    for line in lines:
        print(line)

    print(f"\n{len(lines)} agencies with their own website, out of "
          f"{len(elements)} mapped.", file=sys.stderr)
    for why, count in skipped.items():
        if count:
            print(f"  {count} skipped: {why}", file=sys.stderr)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as handle:
            handle.write('\n'.join(lines) + ('\n' if lines else ''))
        print(f"Also written to {args.out}", file=sys.stderr)
    print("Copy the lines above into Prospects -> Add prospects by hand.",
          file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
