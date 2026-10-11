"""Rebuild olxbot/data/wroclaw_streets.txt from an OpenStreetMap Overpass export (one name per line).

  curl -sS -A "olx-tiktok-bot-streets/1.0" -o osm.csv --data-urlencode \
    'data=[out:csv(name;false)][timeout:90];area["name"="Wrocław"]["admin_level"="8"]["boundary"="administrative"]->.a;(way(area.a)["highway"]["name"];way(area.a)["place"="square"]["name"];);out tags;' \
    https://overpass-api.de/api/interpreter
  python3 -I tools/build_streets.py osm.csv olxbot/data/wroclaw_streets.txt

Keeps only the proper-name part ("aleja Karkonoska" -> "Karkonoska", "bulwar Dedala" -> "Dedala") and drops
objects that are not addresses (bridges, roundabouts, flyovers...)."""
import sys

PREFIXES = {"aleja", "aleje", "plac", "bulwar", "skwer", "osiedle", "ulica", "rynek", "wybrzeże", "park"}
NOT_ADDRESS = {"most", "mosty", "kładka", "rondo", "wiadukt", "estakada", "tunel", "obwodnica", "autostradowa",
               "węzeł", "przejście", "zjazd", "łącznik"}


def main(src: str, dst: str) -> None:
    names = set()
    for raw in open(src, encoding="utf-8"):
        words = raw.strip().split()
        if not words or words[0].lower() in NOT_ADDRESS:
            continue
        while words and words[0].islower() and words[0] in PREFIXES:
            words = words[1:]
        if not words or not words[0][:1].isupper() or any(c.isdigit() for c in words[0]):
            continue
        names.add(" ".join(words))
    with open(dst, "w", encoding="utf-8") as f:
        f.write("# Wrocław street names (proper part), source: OpenStreetMap contributors, ODbL. "
                "Rebuild: tools/build_streets.py\n")
        f.write("\n".join(sorted(names)) + "\n")
    print(f"{len(names)} names -> {dst}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
