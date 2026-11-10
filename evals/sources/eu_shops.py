"""
Legitimate EU web shops, fetched live: the counterweight to a corpus whose EU pages are mostly small 2023 shops.

The Mendeley set gives the corpus few legitimate shops per EU country and none of the large ones, so legitimate EU
shops were scored with too little to compare against. This list adds the home pages of established retailers,
brand shops and smaller direct-to-consumer brands across the EU (curated by hand: each is a long-established,
registered business selling to EU consumers), labelled ``legit`` as a dataset label (``origin_dataset`` =
:data:`DATASET`, ``source`` = ``live``, so they form their own split stratum and leave the existing test set
untouched).

Each page is fetched the way a URL-only check is (:func:`unfret.page_fetch.fetch_page`: public addresses only, no bot
walls, no empty shells), only where the shop's ``robots.txt`` allows Unfret's user agent to fetch it, one shop at a
time. Pages that don't look like a shop (:func:`evals.shopcheck.looks_like_shop`) are skipped. Misses are recorded in
``raw/eu_shops_misses.tsv`` with their reason and skipped on re-runs; ``--retry`` tries them again.

Usage: ``uv run python -m evals.sources.eu_shops [--limit N] [--retry]``.
"""

import argparse
import asyncio
import urllib.robotparser
from datetime import UTC, datetime
from urllib.parse import urlsplit

import structlog

from evals.corpus import Corpus, LabelRecord, page_id
from evals.shopcheck import looks_like_shop
from unfret.net import fetch_public
from unfret.page_fetch import PageUnreadableError, fetch_page

logger = structlog.get_logger("unfret.evals")

DATASET = "eu-legit-shops-2026"
ROBOTS_AGENT = "Unfret"  # the product token of unfret.net's User-Agent
_ROBOTS_MAX_BYTES = 512 * 1024

# (country, home page). Large retailers, brand shops, and smaller direct-to-consumer brands, per country.
SHOPS: tuple[tuple[str, str], ...] = (
    # Netherlands
    ("NL", "https://www.coolblue.nl/"),
    ("NL", "https://www.wehkamp.nl/"),
    ("NL", "https://www.hema.nl/"),
    ("NL", "https://www.blokker.nl/"),
    ("NL", "https://www.gamma.nl/"),
    ("NL", "https://www.praxis.nl/"),
    ("NL", "https://www.debijenkorf.nl/"),
    ("NL", "https://www.kruidvat.nl/"),
    ("NL", "https://www.etos.nl/"),
    ("NL", "https://www.intertoys.nl/"),
    ("NL", "https://www.bever.nl/"),
    ("NL", "https://www.dille-kamille.nl/"),
    ("NL", "https://www.xenos.nl/"),
    ("NL", "https://www.hunkemoller.nl/"),
    ("NL", "https://www.fonq.nl/"),
    ("NL", "https://www.bax-shop.nl/"),
    ("NL", "https://www.azerty.nl/"),
    ("NL", "https://www.simonlevelt.nl/"),
    ("NL", "https://www.athenaeum.nl/"),
    ("NL", "https://www.lucardi.nl/"),
    ("NL", "https://www.dopper.com/"),
    ("NL", "https://www.tonyschocolonely.com/"),
    ("NL", "https://www.secrid.com/"),
    ("NL", "https://www.aceandtate.com/"),
    ("NL", "https://www.rituals.com/"),
    ("NL", "https://www.suitsupply.com/"),
    ("NL", "https://www.g-star.com/"),
    ("NL", "https://www.mudjeans.eu/"),
    # Germany
    ("DE", "https://www.otto.de/"),
    ("DE", "https://www.thomann.de/"),
    ("DE", "https://www.mediamarkt.de/"),
    ("DE", "https://www.dm.de/"),
    ("DE", "https://www.rossmann.de/"),
    ("DE", "https://www.douglas.de/"),
    ("DE", "https://www.tchibo.de/"),
    ("DE", "https://www.bonprix.de/"),
    ("DE", "https://www.cyberport.de/"),
    ("DE", "https://www.notebooksbilliger.de/"),
    ("DE", "https://www.alternate.de/"),
    ("DE", "https://www.conrad.de/"),
    ("DE", "https://www.hornbach.de/"),
    ("DE", "https://www.obi.de/"),
    ("DE", "https://www.manufactum.de/"),
    ("DE", "https://www.hessnatur.com/"),
    ("DE", "https://www.armedangels.com/"),
    ("DE", "https://www.globetrotter.de/"),
    ("DE", "https://www.bergfreunde.de/"),
    ("DE", "https://www.bike-components.de/"),
    ("DE", "https://www.rosebikes.de/"),
    ("DE", "https://www.breuninger.com/"),
    ("DE", "https://www.thalia.de/"),
    ("DE", "https://www.hugendubel.de/"),
    ("DE", "https://www.koro.com/"),
    ("DE", "https://www.mymuesli.com/"),
    ("DE", "https://www.waschbaer.de/"),
    ("DE", "https://www.memo.de/"),
    ("DE", "https://www.zweitausendeins.de/"),
    ("DE", "https://www.asphaltgold.com/"),
    ("DE", "https://www.snocks.com/"),
    # France
    ("FR", "https://www.fnac.com/"),
    ("FR", "https://www.darty.com/"),
    ("FR", "https://www.boulanger.com/"),
    ("FR", "https://www.laredoute.fr/"),
    ("FR", "https://www.decathlon.fr/"),
    ("FR", "https://www.leroymerlin.fr/"),
    ("FR", "https://www.castorama.fr/"),
    ("FR", "https://www.sephora.fr/"),
    ("FR", "https://www.maisonsdumonde.com/"),
    ("FR", "https://www.but.fr/"),
    ("FR", "https://www.ldlc.com/"),
    ("FR", "https://www.cultura.com/"),
    ("FR", "https://www.natureetdecouvertes.com/"),
    ("FR", "https://www.petit-bateau.fr/"),
    ("FR", "https://www.aigle.com/"),
    ("FR", "https://www.saint-james.com/"),
    ("FR", "https://www.armorlux.com/"),
    ("FR", "https://www.sezane.com/"),
    ("FR", "https://www.1083.fr/"),
    ("FR", "https://www.veja-store.com/"),
    ("FR", "https://www.cabaia.com/"),
    # Belgium
    ("BE", "https://www.vandenborre.be/"),
    ("BE", "https://www.krefel.be/"),
    ("BE", "https://www.standaardboekhandel.be/"),
    ("BE", "https://www.iciparisxl.be/"),
    ("BE", "https://www.aveve.be/"),
    ("BE", "https://www.brico.be/"),
    ("BE", "https://www.hubo.be/"),
    ("BE", "https://www.torfs.be/"),
    ("BE", "https://www.jbc.be/"),
    ("BE", "https://www.zeb.be/"),
    ("BE", "https://www.dreamland.be/"),
    ("BE", "https://www.neuhauschocolates.com/"),
    # Spain
    ("ES", "https://www.elcorteingles.es/"),
    ("ES", "https://www.pccomponentes.com/"),
    ("ES", "https://www.mediamarkt.es/"),
    ("ES", "https://www.fnac.es/"),
    ("ES", "https://www.mango.com/"),
    ("ES", "https://www.decathlon.es/"),
    ("ES", "https://www.leroymerlin.es/"),
    ("ES", "https://www.casadellibro.com/"),
    ("ES", "https://www.druni.es/"),
    ("ES", "https://www.primor.eu/"),
    ("ES", "https://www.camper.com/"),
    ("ES", "https://www.desigual.com/"),
    ("ES", "https://www.hawkersco.com/"),
    ("ES", "https://www.ecoalf.com/"),
    # Italy
    ("IT", "https://www.unieuro.it/"),
    ("IT", "https://www.mediaworld.it/"),
    ("IT", "https://www.euronics.it/"),
    ("IT", "https://www.lafeltrinelli.it/"),
    ("IT", "https://www.ibs.it/"),
    ("IT", "https://www.yoox.com/"),
    ("IT", "https://www.eataly.net/"),
    ("IT", "https://www.coin.it/"),
    ("IT", "https://www.ovs.it/"),
    ("IT", "https://www.calzedonia.com/"),
    ("IT", "https://www.decathlon.it/"),
    ("IT", "https://www.leroymerlin.it/"),
    ("IT", "https://www.mondadoristore.it/"),
    ("IT", "https://www.giglio.com/"),
    # Elsewhere in the EU
    ("AT", "https://www.thalia.at/"),
    ("AT", "https://www.xxxlutz.at/"),
    ("AT", "https://www.mediamarkt.at/"),
    ("AT", "https://www.grueneerde.com/"),
    ("IE", "https://www.harveynorman.ie/"),
    ("IE", "https://www.currys.ie/"),
    ("IE", "https://www.brownthomas.com/"),
    ("IE", "https://www.easons.com/"),
    ("PL", "https://www.empik.com/"),
    ("PL", "https://www.x-kom.pl/"),
    ("PL", "https://www.mediaexpert.pl/"),
    ("PL", "https://www.rtveuroagd.pl/"),
    ("PT", "https://www.worten.pt/"),
    ("PT", "https://www.fnac.pt/"),
    ("SE", "https://www.clasohlson.com/se/"),
    ("SE", "https://www.apotea.se/"),
    ("SE", "https://www.happysocks.com/"),
    ("DK", "https://www.magasin.dk/"),
    ("DK", "https://www.saxo.com/dk"),
    ("FI", "https://www.verkkokauppa.com/"),
    ("FI", "https://www.stockmann.com/"),
    # More of each country, weighted towards brand shops and smaller retailers
    ("NL", "https://www.dailypaperclothing.com/"),
    ("NL", "https://www.lovestoriesintimates.com/"),
    ("NL", "https://www.vanharen.nl/"),
    ("NL", "https://www.omoda.nl/"),
    ("NL", "https://www.ziengs.nl/"),
    ("NL", "https://www.sissy-boy.com/"),
    ("NL", "https://www.hollandandbarrett.nl/"),
    ("NL", "https://www.bruna.nl/"),
    ("NL", "https://www.libris.nl/"),
    ("NL", "https://www.leenbakker.nl/"),
    ("NL", "https://www.kwantum.nl/"),
    ("NL", "https://www.karwei.nl/"),
    ("NL", "https://www.nikkie.com/"),
    ("NL", "https://www.bloomon.nl/"),
    ("NL", "https://www.123inkt.nl/"),
    ("NL", "https://www.wefashion.nl/"),
    ("NL", "https://www.prenatal.nl/"),
    ("DE", "https://www.galeria.de/"),
    ("DE", "https://www.engelhorn.de/"),
    ("DE", "https://www.sportscheck.com/"),
    ("DE", "https://www.jako-o.de/"),
    ("DE", "https://www.baur.de/"),
    ("DE", "https://www.teekampagne.de/"),
    ("DE", "https://www.ankerkraut.de/"),
    ("DE", "https://www.einhorn.my/"),
    ("DE", "https://www.ooia.de/"),
    ("DE", "https://www.ehrenkind.de/"),
    ("DE", "https://www.wildling.shoes/"),
    ("DE", "https://www.lillydoo.com/"),
    ("DE", "https://www.rapunzel.de/"),
    ("DE", "https://www.vaude.com/"),
    ("DE", "https://www.jack-wolfskin.de/"),
    ("DE", "https://www.tonies.com/"),
    ("FR", "https://www.monoprix.fr/"),
    ("FR", "https://www.galerieslafayette.com/"),
    ("FR", "https://www.printemps.com/"),
    ("FR", "https://www.rueducommerce.fr/"),
    ("FR", "https://www.intersport.fr/"),
    ("FR", "https://www.jules.com/"),
    ("FR", "https://www.celio.com/"),
    ("FR", "https://www.kiabi.com/"),
    ("FR", "https://www.etam.com/"),
    ("FR", "https://www.leslipfrancais.fr/"),
    ("FR", "https://www.balzac-paris.fr/"),
    ("FR", "https://www.rouje.com/"),
    ("FR", "https://www.jimmyfairly.com/"),
    ("FR", "https://www.mariagefreres.com/"),
    ("FR", "https://www.kusmitea.com/"),
    ("FR", "https://www.palaisdesthes.com/"),
    ("FR", "https://www.lagrandeepicerie.com/"),
    ("FR", "https://www.diptyqueparis.com/"),
    ("FR", "https://www.fragonard.com/"),
    ("BE", "https://www.ava.be/"),
    ("BE", "https://www.club.be/"),
    ("BE", "https://www.casashops.com/"),
    ("BE", "https://www.decathlon.be/"),
    ("BE", "https://www.coolblue.be/"),
    ("BE", "https://www.galler.com/"),
    ("BE", "https://www.pierremarcolini.com/"),
    ("BE", "https://www.delvaux.com/"),
    ("BE", "https://www.essentiel-antwerp.com/"),
    ("ES", "https://www.bershka.com/"),
    ("ES", "https://www.pullandbear.com/"),
    ("ES", "https://www.massimodutti.com/"),
    ("ES", "https://www.worten.es/"),
    ("ES", "https://www.tiendanimal.es/"),
    ("ES", "https://www.kiwoko.com/"),
    ("ES", "https://www.mifarma.es/"),
    ("ES", "https://www.dosfarma.com/"),
    ("ES", "https://www.naturitas.es/"),
    ("ES", "https://www.pompeiibrand.com/"),
    ("ES", "https://www.mrwonderfulshop.es/"),
    ("ES", "https://www.singularu.com/"),
    ("ES", "https://www.tous.com/"),
    ("ES", "https://www.unode50.com/"),
    ("ES", "https://www.bimbaylola.com/"),
    ("IT", "https://www.expert.it/"),
    ("IT", "https://www.comet.it/"),
    ("IT", "https://www.peck.it/"),
    ("IT", "https://www.illy.com/"),
    ("IT", "https://www.kikocosmetics.com/"),
    ("IT", "https://www.goldenpoint.com/"),
    ("IT", "https://www.tezenis.com/"),
    ("IT", "https://www.intimissimi.com/"),
    ("IT", "https://www.falconeri.com/"),
    ("IT", "https://www.carpisa.it/"),
    ("IT", "https://www.yamamay.com/"),
    ("IT", "https://www.boggi.com/"),
    ("IT", "https://www.alcott.eu/"),
    ("IT", "https://www.dmail.it/"),
    ("IT", "https://www.giuntialpunto.it/"),
    ("AT", "https://www.libro.at/"),
    ("AT", "https://www.hervis.at/"),
    ("AT", "https://www.bipa.at/"),
    ("AT", "https://www.sonnentor.com/"),
    ("AT", "https://www.zotter.at/"),
    ("AT", "https://www.manner.com/"),
    ("AT", "https://www.riedel.com/"),
    ("IE", "https://www.dunnesstores.com/"),
    ("IE", "https://www.arnotts.ie/"),
    ("IE", "https://www.woodies.ie/"),
    ("IE", "https://www.boots.ie/"),
    ("IE", "https://www.avoca.com/"),
    ("IE", "https://www.carrolls-irish-gifts.com/"),
    ("IE", "https://www.butlerschocolates.com/"),
    ("IE", "https://www.kilkennyshop.com/"),
    ("IE", "https://www.weirandsons.ie/"),
    ("PL", "https://www.morele.net/"),
    ("PL", "https://www.euro.com.pl/"),
    ("PL", "https://www.komputronik.pl/"),
    ("PL", "https://www.reserved.com/"),
    ("PL", "https://www.ccc.eu/"),
    ("PL", "https://www.rossmann.pl/"),
    ("PL", "https://www.hebe.pl/"),
    ("PL", "https://www.smyk.com/"),
    ("PL", "https://www.wittchen.com/"),
    ("PL", "https://www.answear.com/"),
    ("PT", "https://www.continente.pt/"),
    ("PT", "https://www.radiopopular.pt/"),
    ("PT", "https://www.bertrand.pt/"),
    ("PT", "https://www.wook.pt/"),
    ("PT", "https://www.vistaalegre.com/"),
    ("PT", "https://www.bordallopinheiro.com/"),
    ("PT", "https://www.castelbel.com/"),
    ("SE", "https://www.elgiganten.se/"),
    ("SE", "https://www.webhallen.com/"),
    ("SE", "https://www.adlibris.com/"),
    ("SE", "https://www.lyko.com/"),
    ("SE", "https://www.netonnet.se/"),
    ("SE", "https://www.inet.se/"),
    ("DK", "https://www.elgiganten.dk/"),
    ("DK", "https://www.proshop.dk/"),
    ("DK", "https://www.illum.dk/"),
    ("FI", "https://www.gigantti.fi/"),
    ("FI", "https://www.tokmanni.fi/"),
    ("FI", "https://www.marimekko.com/"),
    ("FI", "https://www.iittala.com/"),
    ("CZ", "https://www.alza.cz/"),
    ("CZ", "https://www.czc.cz/"),
    ("CZ", "https://www.datart.cz/"),
    ("GR", "https://www.public.gr/"),
    ("GR", "https://www.plaisio.gr/"),
    ("GR", "https://www.kotsovolos.gr/"),
    ("RO", "https://altex.ro/"),
    ("SI", "https://www.bigbang.si/"),
    ("LT", "https://www.varle.lt/"),
)


async def robots_allows(url: str) -> bool:
    """
    Whether the site's ``robots.txt`` lets Unfret fetch ``url`` (no file, or an unreadable one, allows it).

    :param url: The page.
    :return: ``True`` when the fetch is allowed.
    """
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        body = await fetch_public(robots_url, max_bytes=_ROBOTS_MAX_BYTES)
    except Exception:  # noqa: BLE001 - no robots.txt (404) or none we can read: the default is allowed
        return True
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(body.decode("utf-8", errors="replace").splitlines())
    return parser.can_fetch(ROBOTS_AGENT, url)


async def collect(url: str) -> tuple[str | None, str]:
    """
    Fetch one shop's page, if robots.txt allows it and it is a readable shop page.

    :param url: The page.
    :return: ``(html, "")``, or ``(None, reason)`` when it was skipped.
    """
    if not await robots_allows(url):
        return None, "robots"
    try:
        html = await fetch_page(url)
    except PageUnreadableError as exc:
        return None, exc.reason
    if not looks_like_shop(html):
        return None, "not_a_shop"
    return html, ""


async def build(corpus: Corpus, limit: int | None, retry: bool, concurrency: int = 4) -> None:
    """
    Fetch the listed shops and add the readable ones to the corpus.

    :param corpus: The corpus to add to.
    :param limit: Stop after this many listed shops (for trial runs).
    :param retry: Try earlier misses again.
    :param concurrency: Shops fetched at the same time (each one a different site).
    """
    misses_path = corpus.root / "raw" / "eu_shops_misses.tsv"
    misses_path.parent.mkdir(parents=True, exist_ok=True)
    misses: dict[str, str] = {}
    if misses_path.exists() and not retry:
        misses = dict(line.split("\t", 1) for line in misses_path.read_text().splitlines() if "\t" in line)
    existing = set(corpus.labels())
    todo = [
        (country, url) for country, url in SHOPS[:limit] if page_id("eus", url) not in existing and url not in misses
    ]
    semaphore = asyncio.Semaphore(concurrency)

    async def one(country: str, url: str) -> tuple[str, str, str | None, str]:
        async with semaphore:
            html, reason = await collect(url)
        return country, url, html, reason

    stamp = datetime.now(UTC).strftime("%Y%m%d")
    added: list[tuple[LabelRecord, str]] = []
    for result in asyncio.as_completed([one(c, u) for c, u in todo]):
        country, url, html, reason = await result
        if html is None:
            misses[url] = reason
            logger.info("eu_shop_skipped", url=url, reason=reason)
            continue
        record = LabelRecord(
            id=page_id("eus", url),
            label="legit",
            label_source="dataset",
            prior_label="legit",
            confidence=0.9,
            page_type="storefront",
            source="live",
            origin_dataset=DATASET,
            origin_url=url,
            collected_at=stamp,
            notes=f"Curated legitimate EU shop ({country}), home page fetched live",
        )
        added.append((record, html))
    if added:
        corpus.add_many(added)
    misses_path.write_text("".join(f"{u}\t{r}\n" for u, r in sorted(misses.items())))
    logger.info("eu_shops_done", added=len(added), skipped=len(misses))


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--retry", action="store_true", help="try the shops that were skipped before again")
    args = parser.parse_args()
    asyncio.run(build(Corpus(), args.limit, args.retry))


if __name__ == "__main__":
    main()
