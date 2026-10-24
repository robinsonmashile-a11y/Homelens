"""
Johannesburg suburb dictionary for scripts/fix_suburbs.py.

Seeded from the suburb slugs the scrapers already produce for Johannesburg
listings (taken from each site's own URLs, so they are the spelling the engine
groups on). A suburb's name is its slug with hyphens as spaces. Add to ALIASES
for spellings that differ from the slug.

Never shared with Cape Town: fix_suburbs.py picks the dictionary from the
listing's `city`, and Cape Town keeps its own (CAPE_TOWN_SUBURBS).
"""
from __future__ import annotations

SLUGS = (
    'abbotsford', 'aeroton', 'alan-manor', 'albertskroon', 'albertville', 'alveda',
    'aspen-hills', 'aspen-lakes', 'atholl', 'atholl-gardens', 'auckland-park', 'austin-view',
    'bagleyston', 'barbeque-downs', 'bassonia', 'bassonia-rock', 'beaulieu', 'bellairspark',
    'bellevue', 'bellevue-east', 'benmore-gardens', 'benrose', 'berea', 'bergbron', 'bertrams',
    'beverley', 'beverley-gardens', 'bezuidenhout-valley', 'birdhaven', 'birnam', 'blackheath',
    'blair-atholl', 'blairgowrie', 'bloubosrand', 'blue-hills', 'booysens', 'bordeaux',
    'boskruin', 'bosmont', 'braamfontein', 'braamfontein-werf', 'bram-fischerville', 'bramley',
    'bramley-manor', 'bramley-view', 'brixton', 'broadacres', 'bromhof', 'bruma', 'bryanston',
    'bryanston-east', 'bryanston-west', 'buccleuch', 'carlswald', 'carlswald-estate',
    'carlswald-north-estate', 'cedar-creek', 'cedar-lakes', 'chartwell', 'cheltondale',
    'chiawelo', 'chrisville', 'city-&-suburban', 'city-deep', 'city-suburban', 'commercia',
    'constantia-kloof', 'corlett-gardens', 'coronationville', 'cosmo-city', 'country-view',
    'craigavon', 'craighall', 'craighall-park', 'crosby', 'crown', 'crown-gardens', 'cyrildene',
    'dainfern', 'dainfern-area', 'dainfern-golf-estate', 'dainfern-ridge',
    'dainfern-valley-estate', 'de-wetshof', 'denver', 'devland', 'diepkloof',
    'diepkloof-zone-4', 'diepsloot-wes', 'discovery', 'doornfontein', 'douglasdale', 'dunkeld',
    'dunkeld-west', 'eagle-canyon-golf-estate', 'eastcliff', 'ebony-park', 'edenburg',
    'eikenhof', 'elandsdrift', 'elandspark', 'elladoone', 'eltonhill', 'emmarentia',
    'ennerdale', 'ennerdale-south', 'erand-gardens', 'fairland', 'fairmount', 'fellside',
    'fernbrook-estate', 'ferndale', 'ferreirasdorp', 'fleurhof', 'florida', 'florida-hills',
    'florida-lake', 'florida-park', 'fontainebleau', 'fordsburg', 'forest-hill', 'forest-town',
    'fountainbrook-estate', 'fourways', 'fourways-area', 'fourways-gardens',
    'franklin-roosevelt-park', 'gallo-manor', 'gillview', 'glen-austin', 'glen-austin-ah',
    'glenadrienne', 'glenanda', 'gleneagles', 'glenferness', 'glenhazel', 'glensan',
    'glenvista', 'grand-central', 'greenside', 'gresswold', 'greymont', 'groblerpark',
    'groblerpark-ext', 'haddon', 'halfway-gardens', 'hamberg', 'helderfontein-estate',
    'helderkruin', 'helderkruin-view', 'highlands', 'highlands-north', 'hillbrow', 'honeydew',
    'honeydew-grove', 'honeydew-ridge', 'honeypark', 'horison', 'horison-park', 'horizon-view',
    'houghton-estate', 'hurlingham', 'hurlingham-manor', 'hurst-hill', 'hyde-park', 'illovo',
    'inanda', 'ivory-park', 'jackal-creek-golf-estate', 'jan-hofmeyer', 'jeppestown',
    'johannesburg-central', 'johannesburg-north', 'judiths-paarl', 'jukskei-park',
    'jukskei-view', 'kelvin', 'kengies', 'kenilworth', 'kensington', 'kensington-b', 'kentview',
    'kew', 'khyber-rock', 'kibler-heights', 'kibler-park', 'killarney', 'kleve-hill-park',
    'kloofendal', 'knopjeslaagte-ah', 'kya-sands', 'kyalami', 'kyalami-ah', 'kyalami-estates',
    'kyalami-hills', 'la-rochelle', 'lanseria', 'lehae', 'lenasia', 'lenasia-ext-1',
    'lenasia-ext-3', 'lenasia-south', 'liefde-en-vrede', 'linbro-park', 'linbro-park-ah',
    'lindbergh-park', 'linden', 'linden-ext', 'lindhaven', 'linksfield', 'linksfield-north',
    'linksfield-ridge', 'linmeyer', 'lion-park', 'lion-pride', 'lombardy', 'lombardy-east',
    'lombardy-west', 'lonehill', 'longlake', 'longmeadow', 'lorentzville', 'lyme-park',
    'lyndhurst', 'maboneng', 'magaliessig', 'malvern', 'marlboro-gardens', 'maroeladal',
    'marshalltown', 'maryvale', 'mayfair', 'mayfair-west', 'mayfield-park', 'melrose',
    'melrose-arch', 'melrose-estate', 'melrose-north', 'melville', 'meredale', 'mid-ennerdale',
    'midrand', 'midridge-park', 'mill-hill', 'millgate-farm-ah', 'milpark', 'moffat-view',
    'monaghan-farm', 'mondeor', 'montgomery-park', 'montroux', 'morningside',
    'morningside-manor', 'mountain-view', 'mulbarton', 'naturena', 'new-doornfontein',
    'newclare', 'newlands', 'newtown', 'noordhang', 'noordwyk', 'north-riding',
    'north-riding-ah', 'northcliff', 'northern-acres', 'northgate', 'northwold', 'norwood',
    'oakdene', 'oaklands', 'observatory', 'olifantsvlei', 'olivedale', 'ontdekkerspark',
    'orange-grove', 'orchards', 'ormonde', 'ormonde-view', 'parkhurst', 'parkmore', 'parktown',
    'parktown-north', 'parkview', 'parkwood', 'patlynn-ah', 'paulshof', 'petervale',
    'phomolong', 'pimville-zone-6', 'pineslopes', 'president-park', 'protea-glen',
    'protea-north', 'rabie-ridge', 'radiokop', 'randburg', 'randburg-central', 'randpark-ridge',
    'regents-park', 'rembrandt-park', 'rembrandt-ridge', 'rewlatch', 'richmond', 'ridgeway',
    'risana', 'river-club', 'riverlea', 'riverside-view', 'riviera', 'rivonia', 'robertsham',
    'roodekrans', 'roodepoort', 'roodepoort-north', 'roodepoort-west', 'roseacre', 'rosebank',
    'rosettenville', 'rouxville', 'ruimsig', 'saddlebrook-estate', 'sagewood', 'sandhurst',
    'sandown', 'sandown-estate', 'sandringham', 'sandton', 'sandton-central', 'saranton',
    'savannah-hills-estate', 'savoy-estate', 'saxonwold', 'senaoane', 'sharonlea', 'sonneglans',
    'sophiatown', 'south-hills', 'south-kensington', 'southdale', 'southfork', 'steyn-city',
    'stratford-gardens', 'strathavon', 'strijdompark', 'strubensvallei', 'suideroord',
    'summerset', 'summerset-estate', 'sun-valley', 'sundowner', 'sunninghill',
    'sunninghill-gardens', 'sydenham', 'the-gardens', 'the-hill', 'the-polofields',
    'the-william-estate', 'towerby', 'townsview', 'treesbank-ah', 'troyeville', 'tulisa-park',
    'turf-club', 'turffontein', 'unigray', 'vorna-valley', 'waterfall',
    'waterfall-country-estate', 'waterfall-country-village', 'waterfall-equestrian-estate',
    'waterfall-view', 'waterford-estate', 'waterval-estate', 'waverley', 'weltevreden-park',
    'wendywood', 'west-turffontein', 'westbury', 'westcliff', 'westdene', 'whitney-gardens',
    'wilgeheuwel', 'wilro-park', 'winchester-hills', 'windsor', 'windsor-east', 'windsor-west',
    'winston-ridge', 'witkoppen', 'witpoortjie', 'woodmead', 'yeoville', 'zakariyya-park',
    'zandspruit',
)

# Spelling in a title / raw suburb  ->  slug
ALIASES = {
    'johannesburg cbd': 'johannesburg-central',
    'joburg cbd': 'johannesburg-central',
    'jhb cbd': 'johannesburg-central',
    'johannesburg city': 'johannesburg-central',
    'sandton central': 'sandton',
    'sandton city': 'sandton',
    'bryanston ext': 'bryanston',
}


def suburb_names() -> dict[str, str]:
    """{lowercase name: slug} for every Johannesburg suburb."""
    names = {s.replace('-', ' '): s for s in SLUGS}
    names.update(ALIASES)
    return names
