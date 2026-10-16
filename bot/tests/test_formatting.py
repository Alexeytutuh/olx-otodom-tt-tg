import datetime as dt
import re
import unittest

from tests.helpers import Listing, listing, live_listings  # noqa: F401  (sets sys.path)

from olxbot import formatting as F
from tools.fake_telegram import check_html, html_plain

TODAY = dt.date(2026, 10, 9)


class FriendFormat(unittest.TestCase):
    def friend_example(self, **kw):
        d = dict(rooms=1, area_m2=35.0, price_pln=2400.0, czynsz_pln=400.0, media_pln=100.0, parking_pln=100.0,
                 prad_pln=150.0, kaucja_pln=3000.0, street="Bulwar Dedala 22", district="Krzyki",
                 description_text="Mieszkanie z dużym tarasem. Miejsce w garażu podziemnym 100 zł.",
                 available_from="2026-11-01")
        d.update(kw)
        return listing("1", **d)

    def test_exact_friend_layout(self):
        post = F.build_post(self.friend_example(), TODAY)
        expected = (
            "Кавалерка | 35 м² | тераса\n\nPL | UA | RU | EN\n\nОРЕНДА\n\n"
            "Здається комфортна квартира\nWrocław | ul. Bulwar Dedala 22\n\n"
            "Ціна: 2400 zł\nЧинш + комунальні: 500 zł\nГараж: 100 zł\nЕлектрика: бл. 150 zł\n\n"
            "Разом: 3150 zł/міс.\n\nЗастава: 3000 zł\nВільна від: 01.11.2026")
        self.assertEqual(post.plain, expected)
        self.assertIn("<b>Кавалерка | 35 м² | тераса</b>", post.html)
        self.assertIn("<b>ОРЕНДА</b>", post.html)
        self.assertIn("<b>Разом: 3150 zł/міс.</b>", post.html)
        self.assertIsNone(check_html(post.html))
        self.assertTrue(post.fits_caption)

    def test_razem_is_sum_of_known_numbers(self):
        _, r = F.cost_lines(self.friend_example())
        self.assertEqual(r, 3150)
        _, r = F.cost_lines(self.friend_example(media_pln=None, parking_pln=None))
        self.assertEqual(r, 2400 + 400 + 150)
        _, r = F.cost_lines(self.friend_example(czynsz_pln=0, media_pln=None, parking_pln=None, prad_pln=None))
        self.assertEqual(r, 2400)                       # "w cenie" is known, total = price
        _, r = F.cost_lines(self.friend_example(czynsz_pln=None, media_pln=None, parking_pln=None, prad_pln=None))
        self.assertIsNone(r)                            # only the price known -> no Razem line
        _, r = F.cost_lines(self.friend_example(price_pln=None))
        self.assertIsNone(r)

    def test_unknown_lines_are_omitted(self):
        l = listing("2", rooms=2, area_m2=None, price_pln=3100.0, street=None, district=None,
                    description_text="Ładne mieszkanie, brak balkonu.")
        p = F.build_post(l, TODAY).plain
        for word in ("Чинш", "Комунальні", "Гараж", "Електрика", "Разом", "Застава", "Вільна", "м²", "None", "балкон"):
            self.assertNotIn(word, p.split("PL | UA")[1] if word == "м²" else p, word)
        self.assertTrue(p.startswith("2 кімнати\n"))
        self.assertIn("\nWrocław\n", p)
        self.assertIn("Ціна: 3100 zł", p)
        self.assertNotIn("\n\n\n", p)

    def test_included_values(self):
        l = listing("3", czynsz_pln=0.0, prad_pln=0.0, parking_pln=0.0, kaucja_pln=0.0,
                    description_text="Parking przy bloku bezpłatny.")
        p = F.build_post(l, TODAY).plain
        self.assertIn("Чинш: у вартості", p)
        self.assertIn("Електрика: у вартості", p)
        self.assertIn("Паркомісце: у вартості", p)
        self.assertIn("Застава: немає", p)

    def test_available(self):
        self.assertEqual(F.available_text("immediately", TODAY), "od zaraz")
        self.assertEqual(F.available_text("2026-10-01", TODAY), "od zaraz")
        self.assertEqual(F.available_text("2026-10-15", TODAY), "15.10.2026")
        self.assertIsNone(F.available_text(None, TODAY))

    def test_numbers(self):
        self.assertEqual(F.fmt_num(2400.0), "2400")
        self.assertEqual(F.fmt_num(35.5), "35,5")
        self.assertEqual(F.fmt_num(47.25), "47,25")

    def test_room_labels_and_sentences(self):
        self.assertEqual(F.room_label(1), "Kawalerka")
        self.assertEqual(F.room_label(2), "2 pokoje")
        self.assertEqual(F.room_label(3), "3 pokoje")
        self.assertEqual(F.room_label(None), "Mieszkanie")
        self.assertEqual([F.room_label_ua(r) for r in (1, 2, 4, 5, None)],
                         ["Кавалерка", "2 кімнати", "4 кімнати", "5 кімнат", "Квартира"])
        s = {F.template_sentence(r, a) for r, a in ((1, 30), (2, 40), (2, 55), (3, 50), (3, 75), (None, None))}
        self.assertEqual(len(s), 6)


class StreetsAndFeatures(unittest.TestCase):
    def st(self, street, from_text=False):
        return F.normalize_street(listing("9", street=street, raw_extra={"street_from_text": from_text}))

    def test_streets(self):
        self.assertEqual(self.st("Braterskiej", True), "ul. Braterska")
        self.assertEqual(self.st("Ciepłej", True), "ul. Ciepła")
        self.assertEqual(self.st("Saperów", True), "ul. Saperów")
        self.assertEqual(self.st("Białowieska 52"), "ul. Białowieska 52")
        self.assertEqual(self.st("Grabiszyńska Wrocław", True), "ul. Grabiszyńska")
        self.assertEqual(self.st("ul.Graniczna 2CC"), "ul. Graniczna 2CC")
        self.assertEqual(self.st("obornicka 101"), "ul. Obornicka 101")
        self.assertEqual(self.st("Al. Generała Józefa Hallera"), "al. Generała Józefa Hallera")
        self.assertEqual(self.st("Jana Pawła II", True), "ul. Jana Pawła II")
        self.assertIsNone(self.st(None))

    def test_streets_completed_from_list(self):
        def st(street, text):
            return F.normalize_street(listing("9", street=street, title="Mieszkanie", description_text=text,
                                              raw_extra={"street_from_text": True}))
        self.assertEqual(st("Róży", "Mieszkanie przy ul. Róży wiatrów, blisko tramwaju."), "ul. Róży Wiatrów")
        self.assertEqual(st("Róży 5", "ul. Róży  Wiatrów 5, 3 piętro"), "ul. Róży Wiatrów 5")
        self.assertEqual(st("Antonia", "Osiedle Cztery Pory Roku- ul. AntoniaVivaldiego)*."), "ul. Antonia Vivaldiego")
        self.assertEqual(st("Braterskiej", "przy ulicy Braterskiej"), "ul. Braterska")
        self.assertEqual(st("Kościuszki", "ul. Kościuszki 10"), "ul. Kościuszki")   # kept as written
        self.assertEqual(st("Zmyślonej", "przy ulicy Zmyślonej"), "ul. Zmyślona")    # not in list: old rule

    def test_amount_not_glued_across_sentences(self):
        from parsers.extract import extract_costs
        t = ("Najem: 2400 zł/mies. + 250 zł czynszu administracyjnego + woda, prąd, internet i kablówka. Miejsce w "
             "garażu podziemnym dodatkowo 200 zł/mies. Kaucja zwrotna: 2500 zł. Umowa na czas nieokreślony.")
        costs, _ = extract_costs(t, 2400.0)
        self.assertEqual(costs["kaucja_pln"], 2500.0)
        self.assertEqual(costs["czynsz_pln"], 250.0)
        self.assertEqual(costs["parking_pln"], 200.0)
        self.assertEqual(extract_costs("Cena 3000 zł, 500 zł kaucji.", 3000.0)[0]["kaucja_pln"], 500.0)

    def test_czynsz_range(self):
        from parsers.extract import extract_costs
        t = ("- czynsz najmu: 3000 zł miesięcznie,\n- kaucja zwrotna: 3000 zł,\n- czynsz administracyjny i pozostałe "
             "opłaty eksploatacyjne: orientacyjnie 800–1000 zł miesięcznie, zależnie od liczby mieszkańców,")
        costs, _ = extract_costs(t, 3000.0)
        self.assertEqual((costs["czynsz_pln"], costs["czynsz_max_pln"]), (800.0, 1000.0))
        self.assertEqual(costs["kaucja_pln"], 3000.0)
        for s, want in (("Czynsz: 800-1000 zł", (800.0, 1000.0)), ("czynsz od 600 do 700 zł", (600.0, 700.0)),
                        ("Czynsz administracyjny 1.000 - 1.200 zł", (1000.0, 1200.0)),
                        ("Czynsz 500 zł. Kaucja 3000-4000 zł", (500.0, None))):
            c = extract_costs(s, 3000.0)[0]
            self.assertEqual((c["czynsz_pln"], c.get("czynsz_max_pln")), want, s)
        post = F.build_post(listing("10", rooms=3, area_m2=60.0, price_pln=3000.0, czynsz_pln=800.0,
                                    czynsz_max_pln=1000.0, media_pln=None, parking_pln=None, prad_pln=None,
                                    kaucja_pln=3000.0), TODAY)
        self.assertIn("Ціна: 3000 zł\nЧинш: 800–1000 zł\n\nРазом: 3800–4000 zł/міс.", post.plain)

    def test_parking_without_price(self):
        def lines(desc, parking=None, price=5000.0):
            return F.cost_lines(listing("9", description_text=desc, parking=parking, parking_pln=None,
                                        czynsz_pln=None, media_pln=None, prad_pln=None, price_pln=price))[0]
        self.assertIn("Паркомісце: у вартості", lines(
            "Udogodnienia: własny balkon oraz prywatne miejsce postojowe. Cena: 5000 PLN — cena stała za wszystko, "
            "bez żadnych dodatkowych opłat za media czy czynsz!", parking="parking strzeżony"))
        self.assertIn("Гараж: доступний", lines("Mieszkanie z miejscem w garażu podziemnym."))
        self.assertFalse([x for x in lines("Na terenie osiedla są ogólnodostępne miejsca parkingowe.")
                          if "паркомісце" in x.lower() or "гараж" in x.lower()])
        self.assertFalse([x for x in lines("Brak miejsca parkingowego.", parking="brak") if "паркомісце" in x.lower() or "гараж" in x.lower()])
        self.assertFalse([x for x in lines("Miejsca parkingowego nie ma przypisanego do mieszkania.",
                                           parking="przynależne na ulicy, brak") if "паркомісце" in x.lower() or "гараж" in x.lower()])
        self.assertFalse([x for x in lines("Ładne mieszkanie.", parking="przynależne na ulicy")
                          if "паркомісце" in x.lower() or "гараж" in x.lower()])
        _, razem = F.cost_lines(listing("9", description_text="Garaż w cenie najmu.", parking_pln=None,
                                        czynsz_pln=300.0, price_pln=2000.0))
        self.assertEqual(razem, 2300.0)                     # no amount for parking -> not in Razem

    def test_footer_and_info_card(self):
        l = listing("9", floor=3, seller_name="Jakub", price_pln=3200.0, area_m2=80.0)
        foot = "Szczegóły na zapytanie\nZapraszam do kontaktu\nJan Kowalski\n☎️ +48 000"
        post = F.build_post(l, footer=foot)
        self.assertTrue(post.plain.endswith("\n\nSzczegóły na zapytanie\nZapraszam do kontaktu\nJan Kowalski\n☎️ +48 000"))
        cap = F.tiktok_caption(l, footer=foot)
        self.assertIn("ідеальн", cap)                                  # TikTok keeps the "ideal for ..." line
        self.assertNotIn("ідеальн", post.plain)                        # Telegram does not
        body, tags = cap.rsplit("\n\n", 1)
        self.assertTrue(body.endswith("Szczegóły na zapytanie\nZapraszam do kontaktu\nJan Kowalski\n☎️ +48 000"))
        self.assertTrue(tags.startswith("#wroclaw "))
        self.assertNotIn("#", post.plain)                                                 # Telegram: no hashtags
        self.assertTrue(F.tiktok_caption(l).startswith("PL | UA | RU | EN"))
        self.assertNotIn("Szczegóły", F.build_post(l).plain)
        info = F.build_info(l)
        self.assertNotIn("Jakub", info)
        self.assertNotIn("поверх", info)
        self.assertNotIn("zł/m²", info)
        self.assertEqual(F.tiktok_title(l), F.build_post(l).plain.split("\n")[0])

    def test_location_fallback_district(self):
        l = listing("9", street=None, district="Krzyki / Tarnogaj")
        self.assertEqual(F.location_line(l), "Wrocław | Tarnogaj (Krzyki)")
        self.assertEqual(F.district_short(l), "Tarnogaj")

    def test_features(self):
        f = lambda txt, **kw: F.key_feature(listing("9", description_text=txt, **kw))  # noqa: E731
        self.assertEqual(f("Mieszkanie z balkonem i tarasem"), "taras")
        self.assertEqual(f("Duży balkon od południa"), "balkon")
        self.assertIsNone(f("Niestety brak balkonu. Blisko Ogrodu Botanicznego."))
        self.assertEqual(f("Mieszkanie z własnym ogródkiem"), "ogródek")
        self.assertEqual(f("Parter z ogrodem 50 m2"), "ogródek")
        self.assertIsNone(f('Blisko: Centrum Handlowe "Tarasy Grabiszyńskie", ogródek plażowy przy pętli'))
        self.assertEqual(f("- balkon\n- ławki, ogród w patio, plac zabaw"), "balkon")   # live OLX 1103764243
        self.assertEqual(f("Miejsce w hali garażowej w cenie"), "garaż")
        self.assertEqual(f("Nic", raw_extra={"extras": ["balcony", "lift"]}), "balkon")
        self.assertIsNone(f("Mieszkanie bez balkonu"))


class HighlightsHashtagsSlides(unittest.TestCase):
    def hl(self, txt, **kw):
        return [h.key for h in F.highlights(listing("9", description_text=txt, **kw))]

    def test_highlights_from_description(self):
        self.assertEqual(self.hl("Blisko parku Szczytnickiego, cicha i spokojna okolica."), ["park", "cicha"])
        self.assertEqual(self.hl("Zielona okolica, przystanek tramwajowy 100 m."), ["zielona", "tramwaj"])
        self.assertEqual(self.hl("Nowy budynek, klimatyzacja i jacuzzi."), ["jacuzzi", "nowy", "klima"])
        self.assertEqual(self.hl("Mieszkanie po remoncie, z tarasem."), ["taras", "remont"])
        self.assertEqual(self.hl("Blisko centrum, ale nie centrum handlowe."), ["centrum"])
        self.assertEqual(self.hl("Blisko Centrum Handlowego Magnolia."), [])

    def test_highlights_false_friends(self):
        self.assertEqual(self.hl("Blisko Magnolia Park, parking dla gości."), [])           # estate name, not a park
        self.assertEqual(self.hl("Po remoncie ul. Buforowej wygodny dojazd."), [])          # the street was repaired
        self.assertEqual(self.hl("Do Rynku 10 minut tramwajem."), [])                       # a ride, not a stop nearby
        self.assertEqual(self.hl("Bez klimatyzacji, brak tarasu."), [])
        self.assertEqual(self.hl("Do remontu, do wyremontowania."), [])

    def test_slide_texts(self):
        l = listing("9", rooms=3, area_m2=60.0, price_pln=3000.0, district="Krzyki / Tarnogaj", street="ul. Balonowa",
                    description_text="Blisko parku, taras.")
        t = F.slide_texts(l)
        self.assertEqual((t.city, t.bottom, t.price), ("Вроцлав (Tarnogaj)", "Тераса", "3000 zł/міс."))
        self.assertEqual(t.short, ["Тераса", "Парк"])
        self.assertEqual(t.where, "Вроцлав, ul. Balonowa")
        self.assertIn("3 кімнати | 60\u00a0м² | Вроцлав", t.top)
        # nothing special -> the district, as written (no translation); no district -> the price
        t = F.slide_texts(listing("9", description_text="Ładne mieszkanie.", district="Krzyki / Huby"))
        self.assertEqual((t.bottom, t.short), ("Huby", []))
        t = F.slide_texts(listing("9", description_text="Ładne mieszkanie.", district=None, street=None))
        self.assertEqual(t.bottom, "2400 zł/міс.")

    def test_hashtags(self):
        l = listing("9", rooms=3, district="Krzyki / Tarnogaj", description_text="Z tarasem, blisko parku.")
        tags = F.hashtags(l)
        self.assertEqual(tags[:3], ["#wroclaw", "#wynajem", "#mieszkaniewroclaw"])
        self.assertTrue({"#3pokoje", "#krzyki", "#tarnogaj", "#taras", "#park"} <= set(tags))
        self.assertEqual(len(tags), len(set(tags)))
        self.assertEqual(tags, F.hashtags(l))                      # stable: slides/caption are generated twice
        self.assertEqual(F._tag("Śródmieście"), "#srodmiescie")
        self.assertEqual(F._tag("Psie Pole"), "#psiepole")
        self.assertIn("#kawalerka", F.hashtags(listing("9", rooms=1)))
        pools = {tuple(t for t in F.hashtags(listing(str(i), rooms=2)) if t in F.POOL_TAGS) for i in range(40)}
        self.assertGreater(len(pools), 3)                          # different flats, different pool picks

    def test_cover_styles(self):
        import tempfile
        from pathlib import Path
        from PIL import Image
        from olxbot import slides
        from tests.helpers import fake_download
        self.assertEqual({slides.pick_style(listing(str(i))) for i in range(200)}, set(slides.STYLES))
        l = listing("9", rooms=3, area_m2=60.0, district="Krzyki", description_text="Taras, klimatyzacja, tramwaj.")
        self.assertEqual(slides.pick_style(l), slides.pick_style(l))                 # same flat, same cover
        with tempfile.TemporaryDirectory() as tmp:
            for st in slides.STYLES:
                paths = slides.generate(l, Path(tmp) / st, 2, fetch=lambda u: fake_download(u), style=st)
                self.assertEqual(len(paths), 2, st)
                im = Image.open(paths[0])
                self.assertEqual(im.size, (1080, 1920), st)
                im.close()
            a = Image.open(Path(tmp) / "A" / "slide_01.jpg").tobytes()
            for st in "BCD":
                self.assertNotEqual(a, Image.open(Path(tmp) / st / "slide_01.jpg").tobytes(), st)
        # the longest labels still render in every style
        long_l = listing("9", rooms=3, area_m2=60.0, district="Przedmieście Oławskie",
                         street="al. Generała Józefa Hallera 12", description_text="Taras, klimatyzacja, tramwaj.")
        t = F.slide_texts(long_l)
        for st in slides.STYLES:
            slides.render_cover(Image.new("RGB", (1200, 1600), "gray"), t, st).close()


class LiveListings(unittest.TestCase):
    """Real normalized listings from both parsers (captured 2026-10-09)."""

    def test_every_live_listing_formats(self):
        ls = live_listings()
        self.assertGreaterEqual(len(ls), 20)
        self.assertTrue({l.source for l in ls} == {"olx", "otodom"})
        for l in ls:
            post = F.build_post(l, TODAY)
            self.assertNotIn("None", post.plain, l.url)
            self.assertNotIn("nan", post.plain.lower().replace("nancy", ""), l.url)
            self.assertIsNone(check_html(post.html), l.url)
            self.assertEqual(html_plain(post.html), post.plain)
            self.assertTrue(post.plain.startswith(F.room_label_ua(l.rooms)), l.url)
            self.assertIn("\nPL | UA | RU | EN\n", post.plain)
            self.assertIn(f"Ціна: {F.fmt_num(l.price_pln)} zł", post.plain)
            if l.kaucja_pln is None:
                self.assertNotIn("Застава", post.plain)
            if l.czynsz_pln is None and l.media_pln is None:
                self.assertNotIn("Чинш", post.plain)
            self.assertLessEqual(F.utf16_len(post.plain), 1024)
            info = F.build_info(l)
            self.assertIsNone(check_html(info), l.url)
            self.assertIn(l.url.replace("&", "&amp;"), info)
            cap = F.tiktok_caption(l, TODAY)
            self.assertLessEqual(F.utf16_len(cap), 4000)
            self.assertNotRegex(cap, r"</?b>|\*\*")
            body, tags = cap.rsplit("\n\n", 1)
            want = F.post_lines(l, TODAY, intro_sentence=True)[1:]       # no title line; with the "ideal for" line
            while want and not want[0].text:
                want = want[1:]
            self.assertEqual(body, F.render_plain(want))
            self.assertNotIn("ідеальн", post.plain, l.url)                 # Telegram post has no such line
            self.assertIn("ідеальн", cap, l.url)
            self.assertTrue(tags.startswith("#wroclaw #wynajem") and "#" not in post.plain, l.url)
            self.assertEqual(cap, F.tiktok_caption(l, TODAY))      # same text when generated again
            self.assertNotIn("Продавець", info)
            self.assertNotIn("zł/m²", info)
            self.assertLessEqual(F.utf16_len(F.tiktok_title(l)), 90)
            top, bottom = F.slide_labels(l)
            self.assertIn("Вроцлав", top)
            self.assertTrue(bottom)

    def test_razem_matches_components_on_live(self):
        for l in live_listings():
            lines, razem = F.cost_lines(l)
            if razem is None:
                continue
            nums = []
            for ln in lines:
                m = re.search(r"(\d+(?:,\d+)?) zł", ln)
                if m:
                    nums.append(float(m.group(1).replace(",", ".")))
            self.assertAlmostEqual(sum(nums), razem, places=2, msg=l.url)

    def test_info_card_flags(self):
        l = live_listings("otodom")[0]
        info = F.build_info(l, dup={"source": "olx", "url": "https://olx.pl/x", "sent_at": 1760000000, "why": "ціна"},
                            blacklisted=True)
        self.assertIn("Можливий дубль", info)
        self.assertIn("Позначено як посередника", info)
        self.assertIn("Otodom", info)


if __name__ == "__main__":
    unittest.main()
