import unittest

from worldterm.severity import Severity, classify, display_keyword, match_details, matched_keywords


class ClassifyTests(unittest.TestCase):
    def test_critical_keywords(self):
        self.assertEqual(classify("Earthquake kills dozens in coastal city"), Severity.CRITICAL)
        self.assertEqual(classify("Missile strikes on capital overnight"), Severity.CRITICAL)
        self.assertEqual(classify("Plane crash leaves 40 dead"), Severity.CRITICAL)

    def test_major_keywords(self):
        self.assertEqual(classify("Court ruling on tariffs expected Monday"), Severity.MAJOR)
        self.assertEqual(classify("Protests spread as inflation climbs"), Severity.MAJOR)

    def test_minor_keywords(self):
        self.assertEqual(classify("Film festival opens with royal guest"), Severity.MINOR)

    def test_default_warning(self):
        self.assertEqual(classify("Town council meets to discuss library hours"), Severity.WARNING)

    def test_indeterminate_when_empty(self):
        self.assertEqual(classify("", ""), Severity.INDETERMINATE)

    def test_first_matching_rule_wins_most_severe_first(self):
        # Contains both a MINOR and a CRITICAL keyword -> CRITICAL
        self.assertEqual(classify("Football fans killed in stadium collapse"), Severity.CRITICAL)

    def test_summary_contributes(self):
        self.assertEqual(classify("Latest from the region", "Explosion reported near port"), Severity.CRITICAL)

    def test_live_breaking_bump(self):
        self.assertEqual(classify("Live: Court ruling on tariffs"), Severity.CRITICAL)
        self.assertEqual(classify("Breaking: Town council meets"), Severity.MINOR)

    def test_analysis_opinion_downgrade(self):
        self.assertEqual(classify("Analysis: Why the war matters"), Severity.MAJOR)
        self.assertEqual(classify("Opinion: tariffs are a mistake"), Severity.MINOR)
        self.assertEqual(classify("Why is inflation so sticky?"), Severity.MINOR)

    def test_downgrade_floor_is_warning(self):
        self.assertEqual(classify("Explainer: how the library works"), Severity.WARNING)

    def test_categories_used_when_text_neutral(self):
        self.assertEqual(classify("Town council meets", "", ["Sport"]), Severity.MINOR)

    def test_whole_word_keywords_no_longer_fire_on_prefixes(self):
        # Each pair was a measured false positive on live feeds before tuning.
        self.assertEqual(classify("Flood warning issued for the valley"), Severity.MAJOR)       # warn, not war
        self.assertEqual(classify("Thieves raid wine warehouse in Italy"), Severity.MAJOR)      # thieves, not war
        self.assertEqual(classify("Couple celebrate 70th anniversary"), Severity.WARNING)        # not coup
        self.assertEqual(classify("Electric car sales keep rising"), Severity.WARNING)          # not elect
        self.assertEqual(classify("Striker signs new contract"), Severity.WARNING)              # not strike
        self.assertEqual(classify("Suez canal traffic resumes"), Severity.WARNING)              # not sue
        self.assertEqual(classify("Budget talks end in deadlock"), Severity.WARNING)            # not dead
        self.assertEqual(classify("Warsaw hosts regional summit"), Severity.MAJOR)              # summit, not war

    def test_whole_word_keywords_still_fire_on_real_forms(self):
        self.assertEqual(classify("War in the east enters third year"), Severity.CRITICAL)
        self.assertEqual(classify("Wartime letters found in attic"), Severity.CRITICAL)
        self.assertEqual(classify("Military coup in the capital"), Severity.CRITICAL)
        self.assertEqual(classify("Deadly floods sweep the region"), Severity.CRITICAL)
        self.assertEqual(classify("Election results delayed"), Severity.MAJOR)
        self.assertEqual(classify("Rail workers strike for third day"), Severity.MAJOR)
        self.assertEqual(classify("Company sued over data breach"), Severity.MAJOR)

    def test_longest_phrase_wins_the_alternation(self):
        d = match_details("Death toll rises after quake")
        self.assertEqual({h.keyword for h in d.hits}, {"death toll"})

    def test_match_details_text(self):
        d = match_details("Football fans killed in stadium collapse")
        self.assertEqual(d.severity, Severity.CRITICAL)
        self.assertEqual(d.base, Severity.CRITICAL)
        self.assertEqual(d.origin, "text")
        self.assertEqual([(h.keyword, h.level) for h in d.hits],
                         [("kill", Severity.CRITICAL), ("collapse", Severity.MAJOR), ("football", Severity.MINOR)])
        self.assertEqual(d.deciding, ("kill",))
        self.assertIsNone(d.prefix)
        self.assertEqual(d.modifier, 0)

    def test_match_details_canonical_keyword(self):
        d = match_details("Death toll rises; deaths mount after strikes on the port")
        keywords = {h.keyword for h in d.hits}
        self.assertIn("death toll", keywords)
        self.assertIn("deaths?", keywords)
        self.assertIn("strikes? on", keywords)
        self.assertEqual(display_keyword("deaths?"), "death")
        self.assertEqual(display_keyword("strikes? on"), "strike on")
        self.assertEqual(display_keyword("kill"), "kill")

    def test_match_details_modifiers_and_origin(self):
        up = match_details("Live: Court ruling on tariffs")
        self.assertEqual((up.base, up.severity, up.prefix, up.modifier), (Severity.MAJOR, Severity.CRITICAL, "up", 1))
        clamped = match_details("Breaking: Earthquake kills dozens")
        self.assertEqual((clamped.prefix, clamped.modifier), ("up", 0))
        down = match_details("Analysis: Why the war matters")
        self.assertEqual((down.base, down.severity, down.modifier), (Severity.CRITICAL, Severity.MAJOR, -1))
        cat = match_details("Town council meets", "", ["Sport"])
        self.assertEqual((cat.origin, cat.base, cat.deciding), ("category", Severity.MINOR, ("sport",)))
        default = match_details("Town council meets to discuss library hours")
        self.assertEqual((default.origin, default.hits, default.deciding), ("default", (), ()))
        empty = match_details("")
        self.assertEqual((empty.origin, empty.severity), ("empty", Severity.INDETERMINATE))

    def test_classify_matches_details(self):
        for title in ("Earthquake kills dozens", "Opinion: tariffs are a mistake", "Town council meets", ""):
            self.assertEqual(classify(title), match_details(title).severity)

    def test_matched_keywords(self):
        self.assertIn("earthquake", matched_keywords("Earthquake hits; tsunami warning"))


if __name__ == "__main__":
    unittest.main()
